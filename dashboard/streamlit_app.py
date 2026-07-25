from __future__ import annotations

import sys
from pathlib import Path

# Add the TokenGuardAI project root to Python's import path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.core.config import get_settings
from dashboard.api_client import (
    DashboardAPIError,
    TokenGuardAPIClient,
)

st.set_page_config(
    page_title="TokenGuard AI Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)


settings = get_settings()

api_client = TokenGuardAPIClient(base_url=(settings.token_guard_api_base_url))


def initialize_session_state() -> None:
    defaults = {
        "admin_api_key": "",
        "project_api_key": "",
        "selected_project_id": None,
        "authenticated": False,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def format_integer(value: int | float) -> str:
    return f"{int(value):,}"


def format_duration(milliseconds: float) -> str:
    if milliseconds >= 60_000:
        return f"{milliseconds / 60_000:,.2f} min"

    if milliseconds >= 1_000:
        return f"{milliseconds / 1_000:,.2f} sec"

    return f"{milliseconds:,.2f} ms"


def render_sidebar() -> dict[str, object] | None:
    with st.sidebar:
        st.title("🛡️ TokenGuard AI")

        st.caption("Project analytics and governance")

        st.divider()

        admin_api_key = st.text_input(
            "Administrator API key",
            type="password",
            value=(st.session_state.admin_api_key),
            placeholder="tg_admin_...",
        )

        project_api_key = st.text_input(
            "Selected project API key",
            type="password",
            value=(st.session_state.project_api_key),
            placeholder="tg_live_...",
        )

        if st.button(
            "Connect",
            use_container_width=True,
        ):
            st.session_state.admin_api_key = admin_api_key.strip()

            st.session_state.project_api_key = project_api_key.strip()

            st.cache_data.clear()
            st.rerun()

        if not st.session_state.admin_api_key:
            st.warning("Enter an administrator API key to load projects.")
            return None

        try:
            projects = api_client.list_projects(
                admin_api_key=(st.session_state.admin_api_key)
            )
        except DashboardAPIError as error:
            st.error(str(error))
            return None

        active_projects = [project for project in projects if project.get("is_active")]

        if not active_projects:
            st.info("No active projects are available.")
            return None

        project_labels = {
            int(project["id"]): (f"{project['name']} (ID {project['id']})")
            for project in active_projects
        }

        project_ids = list(project_labels.keys())

        default_index = 0

        if st.session_state.selected_project_id in project_ids:
            default_index = project_ids.index(st.session_state.selected_project_id)

        selected_project_id = st.selectbox(
            "Project",
            options=project_ids,
            index=default_index,
            format_func=lambda project_id: project_labels[project_id],
        )

        st.session_state.selected_project_id = selected_project_id

        selected_project = next(
            project
            for project in active_projects
            if int(project["id"]) == selected_project_id
        )

        st.divider()

        if st.button(
            "Refresh dashboard",
            use_container_width=True,
        ):
            st.cache_data.clear()
            st.rerun()

        return selected_project


@st.cache_data(ttl=10)
def load_project_analytics(
    project_api_key: str,
    days: int,
) -> dict[str, object]:
    return api_client.get_project_analytics(
        project_api_key=project_api_key,
        days=days,
    )


def render_api_metrics(
    summary: dict[str, object],
) -> None:
    requests = summary["requests"]
    tokens = summary["tokens"]
    latency = summary["latency"]

    first_row = st.columns(4)

    total_requests = requests["total_requests"]
    cache_hit_rate_percent = requests["cache_hit_rate_percent"]
    actual_tokens_consumed = tokens["actual_tokens_consumed"]
    tokens_avoided = tokens["tokens_avoided"]

    first_row[0].metric(
        "Total requests",
        f"{total_requests:,}",
    )

    first_row[1].metric(
        "Cache-hit rate",
        f"{cache_hit_rate_percent:.2f}%",
    )

    first_row[2].metric(
        "Tokens consumed",
        f"{actual_tokens_consumed:,}",
    )

    first_row[3].metric(
        "Tokens avoided",
        f"{tokens_avoided:,}",
    )

    second_row = st.columns(4)

    token_saving_percent = tokens["token_saving_percent"]
    generated_requests = requests["generated_requests"]
    cache_hits = requests["cache_hits"]
    estimated_time_saved_ms = latency["estimated_time_saved_ms"]

    second_row[0].metric(
        "Token savings",
        f"{token_saving_percent:.2f}%",
    )

    second_row[1].metric(
        "Generated requests",
        f"{generated_requests:,}",
    )

    second_row[2].metric(
        "Cache hits",
        f"{cache_hits:,}",
    )

    second_row[3].metric(
        "Time saved",
        format_duration(estimated_time_saved_ms),
    )


def render_provider_usage(
    provider_usage: list[dict[str, object]],
) -> None:
    st.subheader("Provider usage")

    if not provider_usage:
        st.info("No provider usage is available.")
        return

    dataframe = pd.DataFrame(provider_usage)

    st.bar_chart(
        dataframe.set_index("provider")[
            [
                "generated_requests",
                "exact_cache_hits",
                "semantic_cache_hits",
                "failed_requests",
            ]
        ]
    )

    st.dataframe(
        dataframe,
        use_container_width=True,
        hide_index=True,
    )


def render_model_usage(
    model_usage: list[dict[str, object]],
) -> None:
    st.subheader("Model usage")

    if not model_usage:
        st.info("No model usage is available.")
        return

    dataframe = pd.DataFrame(model_usage)

    dataframe["provider_model"] = dataframe["provider"] + " / " + dataframe["model"]

    st.bar_chart(dataframe.set_index("provider_model")[["request_count"]])

    st.dataframe(
        dataframe[
            [
                "provider",
                "model",
                "request_count",
                "total_tokens",
                "total_cost_usd",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_daily_api_usage(
    daily_usage: list[dict[str, object]],
) -> None:
    st.subheader("Daily usage")

    if not daily_usage:
        st.info("No daily usage data is available.")
        return

    dataframe = pd.DataFrame(daily_usage)

    dataframe["date"] = pd.to_datetime(
        dataframe["date"],
        errors="coerce",
    )

    dataframe = dataframe.set_index("date")

    st.bar_chart(
        dataframe[
            [
                "generated_requests",
                "exact_cache_hits",
                "semantic_cache_hits",
                "failed_requests",
            ]
        ]
    )

    st.line_chart(
        dataframe[
            [
                "actual_tokens",
                "cost_usd",
            ]
        ]
    )


def render_api_request_history(
    recent_requests: list[dict[str, object]],
) -> None:
    st.subheader("Recent requests")

    if not recent_requests:
        st.info("No request history is available.")
        return

    dataframe = pd.DataFrame(recent_requests)

    dataframe["created_at"] = pd.to_datetime(
        dataframe["created_at"],
        errors="coerce",
    )

    dataframe["request_id"] = dataframe["request_id"].astype(str).str[:8]

    st.dataframe(
        dataframe,
        use_container_width=True,
        hide_index=True,
    )


def main() -> None:
    initialize_session_state()

    selected_project = render_sidebar()

    if selected_project is None:
        st.title("TokenGuard AI")
        st.info("Connect with administrator and project credentials to continue.")
        return

    if not st.session_state.project_api_key:
        st.warning("Enter the API key for the selected project.")
        return

    st.title("TokenGuard AI Analytics")

    st.caption(f"Project: {selected_project['name']}")

    days = st.selectbox(
        "Analytics period",
        options=[7, 14, 30, 60, 90],
        index=2,
    )

    try:
        data = load_project_analytics(
            project_api_key=(st.session_state.project_api_key),
            days=days,
        )
    except DashboardAPIError as error:
        st.error(str(error))
        return

    returned_project = data["project"]

    if int(returned_project["id"]) != int(selected_project["id"]):
        st.error("The supplied project API key belongs to a different project.")
        return

    summary = data["summary"]
    provider_usage = data["provider_usage"]
    model_usage = data["model_usage"]
    daily_usage = data["daily_usage"]
    recent_requests = data["recent_requests"]

    render_api_metrics(summary)

    st.divider()

    render_provider_usage(provider_usage)

    st.divider()

    render_model_usage(model_usage)

    st.divider()

    render_daily_api_usage(daily_usage)

    st.divider()

    render_api_request_history(recent_requests)


if __name__ == "__main__":
    main()
