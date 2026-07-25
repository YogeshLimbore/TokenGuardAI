from typing import Literal

from pydantic import BaseModel

BudgetAction = Literal[
    "allow",
    "downgrade",
    "reject",
]


class BudgetDecision(BaseModel):
    enforcement_enabled: bool
    allowed: bool
    action: BudgetAction

    original_provider: str
    original_model: str

    final_provider: str
    final_model: str

    estimated_input_tokens: int
    requested_output_tokens: int
    estimated_total_tokens: int
    estimated_cost_usd: float

    daily_tokens_used: int
    daily_token_budget: int

    daily_cost_used_usd: float
    daily_cost_budget_usd: float

    reasons: list[str]
