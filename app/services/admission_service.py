"""Atomic project-wide admission and conservative per-attempt reconciliation."""

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import Project, UsageReservation
from app.providers.base import ProviderResponse
from app.services.budget_service import BudgetExceededError, BudgetService
from app.services.pricing_service import PricingService


class AdmissionService:
    def reserve(
        self,
        database: Session,
        *,
        project_id: int,
        request_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        max_tokens: int,
        request_maximum_cost_usd: float | None = None,
        request_maximum_total_tokens: int | None = None,
    ) -> UsageReservation:
        # PostgreSQL serializes contenders for the same project until commit.
        project = database.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is None or not project.is_active:
            raise BudgetExceededError("Project is inactive.", ["Project is inactive."])
        prior = database.execute(
            select(
                func.coalesce(func.sum(UsageReservation.total_tokens), 0),
                func.coalesce(func.sum(UsageReservation.estimated_cost_usd), 0.0),
            ).where(
                UsageReservation.project_id == project_id,
                UsageReservation.request_id == request_id,
            )
        ).one()
        remaining_tokens = (
            max(0, request_maximum_total_tokens - int(prior[0] or 0))
            if request_maximum_total_tokens is not None
            else None
        )
        remaining_cost = (
            max(0.0, request_maximum_cost_usd - float(prior[1] or 0.0))
            if request_maximum_cost_usd is not None
            else None
        )
        decision = BudgetService().evaluate(
            database,
            project=project,
            request_id=request_id,
            provider=provider,
            model=model,
            estimated_input_tokens=input_tokens,
            requested_output_tokens=max_tokens,
            request_maximum_cost_usd=remaining_cost,
            request_maximum_total_tokens=remaining_tokens,
            allow_downgrade=False,
            persist=False,
        )
        if not decision.allowed:
            database.rollback()
            raise BudgetExceededError("Project budget is exhausted.", decision.reasons)
        reservation = UsageReservation(
            id=str(uuid4()),
            request_id=request_id,
            project_id=project_id,
            provider=provider,
            model=model,
            total_tokens=decision.estimated_total_tokens,
            estimated_cost_usd=decision.estimated_cost_usd,
            status="reserved",
        )
        database.add(reservation)
        database.commit()
        return reservation

    @staticmethod
    def settle(
        database: Session,
        reservation: UsageReservation,
        response: ProviderResponse | None,
        input_estimate: int,
    ) -> None:
        if (
            response is None
            or response.input_tokens is None
            or response.output_tokens is None
        ):
            # Do not silently release spend on timeout/cancellation or missing usage.
            reservation.status = "uncertain"
        else:
            reservation.total_tokens = max(0, response.input_tokens) + max(
                0, response.output_tokens
            )
            cost = PricingService().calculate_cost(
                provider=reservation.provider,
                model=reservation.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                database=database,
                project_id=reservation.project_id,
            )
            reservation.estimated_cost_usd = cost.total_cost_usd
            reservation.status = "completed"
        database.commit()
