from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.core.config import get_settings
from dashboard.api_client import DashboardAPIError, TokenGuardAPIClient

st.set_page_config(
    page_title="TokenGuard AI Dashboard",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

settings = get_settings()
api_client = TokenGuardAPIClient(base_url=settings.token_guard_api_base_url)


def _object_id(value: object) -> int:
    if isinstance(value, (int, str)):
        return int(value)
    raise DashboardAPIError("The API returned an invalid identifier.")


def initialize_session_state() -> None:
    defaults: dict[str, object] = {
        "auth_mode": "Account",
        "access_token": "",
        "refresh_token": "",
        "access_token_expires_at": "",
        "admin_api_key": "",
        "project_api_key": "",
        "selected_organization_id": None,
        "selected_project_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _save_token_pair(token_pair: dict[str, object]) -> None:
    st.session_state.access_token = str(token_pair["access_token"])
    st.session_state.refresh_token = str(token_pair["refresh_token"])
    st.session_state.access_token_expires_at = str(
        token_pair["access_token_expires_at"]
    )


def _clear_account_session() -> None:
    st.session_state.access_token = ""
    st.session_state.refresh_token = ""
    st.session_state.access_token_expires_at = ""
    st.session_state.selected_organization_id = None
    st.session_state.selected_project_id = None
    st.cache_data.clear()


def _account_access_token() -> str:
    access_token = str(st.session_state.access_token)
    refresh_token = str(st.session_state.refresh_token)
    expires_at = str(st.session_state.access_token_expires_at)
    if not access_token or not refresh_token:
        return ""
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        should_refresh = expiry <= datetime.now(UTC)
    except ValueError:
        should_refresh = True
    if should_refresh:
        try:
            _save_token_pair(api_client.refresh(refresh_token=refresh_token))
        except DashboardAPIError:
            _clear_account_session()
            return ""
    return str(st.session_state.access_token)


def _active_project(
    projects: list[dict[str, object]], *, widget_key: str
) -> dict[str, object] | None:
    active = [project for project in projects if project.get("is_active")]
    if not active:
        st.info("No active projects are available.")
        return None
    labels = {
        _object_id(project["id"]): f"{project['name']} (ID {project['id']})"
        for project in active
    }
    project_ids = list(labels)
    current = st.session_state.selected_project_id
    index = project_ids.index(current) if current in project_ids else 0
    project_id = st.selectbox(
        "Project",
        project_ids,
        index=index,
        format_func=lambda value: labels[_object_id(value)],
        key=widget_key,
    )
    st.session_state.selected_project_id = project_id
    return next(
        project for project in active if _object_id(project["id"]) == project_id
    )


def _render_account_login() -> dict[str, object] | None:
    access_token = _account_access_token()
    if not access_token:
        with st.form("account-login"):
            email = st.text_input("Email", autocomplete="email")
            password = st.text_input(
                "Password", type="password", autocomplete="current-password"
            )
            submitted = st.form_submit_button("Sign in", width="stretch")
        if submitted:
            try:
                _save_token_pair(
                    api_client.login(email=email.strip(), password=password)
                )
                st.rerun()
            except DashboardAPIError as error:
                st.error(str(error))
        return None

    if st.button("Sign out", icon=":material/logout:", width="stretch"):
        try:
            api_client.logout(refresh_token=str(st.session_state.refresh_token))
        except DashboardAPIError:
            pass
        _clear_account_session()
        st.rerun()

    try:
        organizations = api_client.list_organizations(access_token=access_token)
    except DashboardAPIError as error:
        st.error(str(error))
        return None
    if not organizations:
        st.info("Your account does not belong to an organization yet.")
        return None

    labels = {
        _object_id(org["id"]): f"{org['name']} (ID {org['id']})"
        for org in organizations
    }
    organization_ids = list(labels)
    current = st.session_state.selected_organization_id
    index = organization_ids.index(current) if current in organization_ids else 0
    organization_id = st.selectbox(
        "Organization",
        organization_ids,
        index=index,
        format_func=lambda value: labels[_object_id(value)],
        key="account_organization",
    )
    st.session_state.selected_organization_id = organization_id
    try:
        projects = api_client.list_organization_projects(
            access_token=access_token, organization_id=organization_id
        )
    except DashboardAPIError as error:
        st.error(str(error))
        return None
    return _active_project(projects, widget_key="account_project")


def _render_api_key_login() -> dict[str, object] | None:
    with st.form("api-key-login"):
        admin_api_key = st.text_input(
            "Administrator API key",
            type="password",
            value=str(st.session_state.admin_api_key),
            placeholder="tg_admin_...",
        )
        project_api_key = st.text_input(
            "Project API key",
            type="password",
            value=str(st.session_state.project_api_key),
            placeholder="tg_live_...",
        )
        submitted = st.form_submit_button("Connect", width="stretch")
    if submitted:
        st.session_state.admin_api_key = admin_api_key.strip()
        st.session_state.project_api_key = project_api_key.strip()
        st.cache_data.clear()
        st.rerun()
    if not st.session_state.admin_api_key:
        st.warning("Enter an administrator API key to load projects.")
        return None
    try:
        projects = api_client.list_projects(
            admin_api_key=str(st.session_state.admin_api_key)
        )
    except DashboardAPIError as error:
        st.error(str(error))
        return None
    return _active_project(projects, widget_key="api_key_project")


def render_sidebar() -> dict[str, object] | None:
    with st.sidebar:
        st.title("🛡️ TokenGuard AI")
        st.caption("Project analytics and provider governance")
        st.segmented_control(
            "Sign-in method",
            ["Account", "API keys"],
            key="auth_mode",
            width="stretch",
        )
        st.divider()
        project = (
            _render_account_login()
            if st.session_state.auth_mode == "Account"
            else _render_api_key_login()
        )
        if project and st.button(
            "Refresh dashboard", icon=":material/refresh:", width="stretch"
        ):
            st.cache_data.clear()
            st.rerun()
        return project


def _project_auth(project_id: int) -> dict[str, Any]:
    if st.session_state.auth_mode == "Account":
        return {
            "access_token": _account_access_token(),
            "project_id": project_id,
            "project_api_key": None,
        }
    return {
        "access_token": None,
        "project_id": project_id,
        "project_api_key": str(st.session_state.project_api_key),
    }


@st.cache_data(ttl=10)
def load_project_analytics(
    access_token: str | None,
    project_id: int,
    project_api_key: str | None,
    days: int,
) -> dict[str, object]:
    return api_client.get_project_analytics(
        access_token=access_token,
        project_id=project_id,
        project_api_key=project_api_key,
        days=days,
    )


def format_duration(milliseconds: float) -> str:
    if milliseconds >= 60_000:
        return f"{milliseconds / 60_000:,.2f} min"
    if milliseconds >= 1_000:
        return f"{milliseconds / 1_000:,.2f} sec"
    return f"{milliseconds:,.2f} ms"


def render_analytics(project: dict[str, object]) -> None:
    days = st.selectbox("Analytics period", [7, 14, 30, 60, 90], index=2)
    auth = _project_auth(_object_id(project["id"]))
    try:
        data = load_project_analytics(days=days, **auth)
    except DashboardAPIError as error:
        st.error(str(error))
        return
    returned_project = data["project"]
    if not isinstance(returned_project, dict) or _object_id(
        returned_project["id"]
    ) != _object_id(project["id"]):
        st.error("The supplied credentials belong to a different project.")
        return

    summary = data["summary"]
    if not isinstance(summary, dict):
        st.error("The API returned an invalid analytics summary.")
        return
    requests = summary["requests"]
    tokens = summary["tokens"]
    latency = summary["latency"]
    columns = st.columns(4)
    columns[0].metric("Total requests", f"{requests['total_requests']:,}")
    columns[1].metric("Cache-hit rate", f"{requests['cache_hit_rate_percent']:.2f}%")
    columns[2].metric("Tokens consumed", f"{tokens['actual_tokens_consumed']:,}")
    columns[3].metric("Tokens avoided", f"{tokens['tokens_avoided']:,}")
    columns = st.columns(4)
    columns[0].metric("Token savings", f"{tokens['token_saving_percent']:.2f}%")
    columns[1].metric("Generated requests", f"{requests['generated_requests']:,}")
    columns[2].metric("Cache hits", f"{requests['cache_hits']:,}")
    columns[3].metric("Time saved", format_duration(latency["estimated_time_saved_ms"]))

    sections = (
        ("Provider usage", data["provider_usage"], "provider"),
        ("Model usage", data["model_usage"], "model"),
        ("Daily usage", data["daily_usage"], "date"),
        ("Recent requests", data["recent_requests"], None),
    )
    for title, rows, index_column in sections:
        st.divider()
        st.subheader(title)
        if not rows:
            st.info(f"No {title.lower()} is available.")
            continue
        dataframe = pd.DataFrame(rows)
        if index_column and index_column in dataframe:
            numeric = dataframe.select_dtypes(include="number").columns.tolist()
            if numeric:
                st.bar_chart(dataframe.set_index(index_column)[numeric])
        st.dataframe(dataframe, width="stretch", hide_index=True)


def render_providers(project: dict[str, object]) -> None:
    project_id = _object_id(project["id"])
    auth = _project_auth(project_id)
    try:
        providers = api_client.list_providers(**auth)
        credentials = api_client.list_credentials(**auth)
        prices = api_client.list_model_prices(**auth)
    except DashboardAPIError as error:
        st.error(str(error))
        return

    st.subheader("Provider catalog")
    st.caption(
        "Use any registered OpenAI-compatible provider, Anthropic, Gemini, or Ollama. "
        "Pricing must be configured before cost-based routing is reliable."
    )
    st.dataframe(pd.DataFrame(providers), width="stretch", hide_index=True)

    st.subheader("Project model pricing")
    st.caption(
        "Add current prices for every paid model used by this project. Use * as "
        "the model to set a provider fallback. These values drive admission, "
        "budget, and savings calculations."
    )
    provider_ids = [str(item["id"]) for item in providers]
    with st.form("save-model-price", clear_on_submit=True):
        price_provider = st.selectbox("Pricing provider", provider_ids)
        price_model = st.text_input("Model", placeholder="model-name or *")
        price_columns = st.columns(2)
        input_price = price_columns[0].number_input(
            "Input USD per 1M tokens", min_value=0.0, format="%.8f"
        )
        output_price = price_columns[1].number_input(
            "Output USD per 1M tokens", min_value=0.0, format="%.8f"
        )
        price_submitted = st.form_submit_button(
            "Save model price", icon=":material/attach_money:", width="stretch"
        )
    if price_submitted:
        try:
            api_client.save_model_price(
                provider=price_provider,
                model=price_model.strip(),
                input_price=input_price,
                output_price=output_price,
                **auth,
            )
            st.success("Model price saved.")
            st.rerun()
        except DashboardAPIError as error:
            st.error(str(error))

    if prices:
        st.dataframe(pd.DataFrame(prices), width="stretch", hide_index=True)
        price_labels = {
            _object_id(item["id"]): f"{item['provider']} / {item['model']}"
            for item in prices
        }
        selected_price_id = st.selectbox(
            "Saved model price",
            list(price_labels),
            format_func=lambda value: price_labels[_object_id(value)],
        )
        selected_price = next(
            item for item in prices if _object_id(item["id"]) == selected_price_id
        )
        if st.button("Remove model price", icon=":material/delete:", width="stretch"):
            try:
                api_client.delete_model_price(
                    provider=str(selected_price["provider"]),
                    model=str(selected_price["model"]),
                    **auth,
                )
                st.success("Model price removed.")
                st.rerun()
            except DashboardAPIError as error:
                st.error(str(error))
    else:
        st.info("No project-specific model prices are configured.")

    keyed_providers = [
        str(item["id"]) for item in providers if item.get("requires_api_key")
    ]
    st.subheader("Add credential")
    with st.form("add-provider-credential", clear_on_submit=True):
        provider = st.selectbox("Provider", keyed_providers)
        name = st.text_input("Credential name", placeholder="Production key")
        secret = st.text_input("API key", type="password")
        is_default = st.checkbox("Use as the default for this provider", value=True)
        submitted = st.form_submit_button(
            "Save encrypted credential", icon=":material/key:", width="stretch"
        )
    if submitted:
        try:
            api_client.create_credential(
                provider=provider,
                name=name.strip(),
                api_key=secret.strip(),
                is_default=is_default,
                **auth,
            )
            st.success("Credential saved.")
            st.rerun()
        except DashboardAPIError as error:
            st.error(str(error))

    st.subheader("Stored credentials")
    if not credentials:
        st.info("No provider credentials are stored for this project.")
        return
    st.dataframe(pd.DataFrame(credentials), width="stretch", hide_index=True)
    credential_labels = {
        _object_id(item["id"]): (
            f"{item['provider']} / {item['name']} ({item['masked_key']})"
        )
        for item in credentials
    }
    credential_id = st.selectbox(
        "Credential",
        list(credential_labels),
        format_func=lambda value: credential_labels[_object_id(value)],
    )
    selected = next(
        item for item in credentials if _object_id(item["id"]) == credential_id
    )
    actions = st.columns(3)
    if actions[0].button("Test", icon=":material/check_circle:", width="stretch"):
        try:
            result = api_client.test_credential(credential_id=credential_id, **auth)
            (st.success if result.get("valid") else st.error)(
                str(result.get("message"))
            )
        except DashboardAPIError as error:
            st.error(str(error))
    if actions[1].button("Discover models", icon=":material/search:", width="stretch"):
        try:
            result = api_client.list_models(
                provider=str(selected["provider"]),
                credential_id=credential_id,
                **auth,
            )
            models = result.get("models") or []
            if models:
                st.dataframe(pd.DataFrame(models), width="stretch", hide_index=True)
            else:
                st.info("The provider returned no models.")
        except DashboardAPIError as error:
            st.error(str(error))
    if actions[2].button("Delete", icon=":material/delete:", width="stretch"):
        try:
            api_client.delete_credential(credential_id=credential_id, **auth)
            st.success("Credential deleted.")
            st.rerun()
        except DashboardAPIError as error:
            st.error(str(error))


def render_account_security() -> None:
    if st.session_state.auth_mode != "Account":
        st.info("Account security is available when signed in with a user account.")
        return
    access_token = _account_access_token()
    if not access_token:
        st.warning("Sign in again to manage account security.")
        return

    st.subheader("Change password")
    with st.form("change-password", clear_on_submit=True):
        current_password = st.text_input("Current password", type="password")
        new_password = st.text_input("New password", type="password")
        confirm_password = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button(
            "Change password", icon=":material/password:", width="stretch"
        )
    if submitted:
        if new_password != confirm_password:
            st.error("The new passwords do not match.")
        else:
            try:
                api_client.change_password(
                    access_token=access_token,
                    current_password=current_password,
                    new_password=new_password,
                )
                _clear_account_session()
                st.success("Password changed. Sign in again on this device.")
                st.rerun()
            except DashboardAPIError as error:
                st.error(str(error))

    st.subheader("Sessions")
    try:
        sessions = api_client.list_sessions(access_token=access_token)
    except DashboardAPIError as error:
        st.error(str(error))
        return
    if not sessions:
        st.info("No sessions are recorded.")
        return
    st.dataframe(pd.DataFrame(sessions), width="stretch", hide_index=True)
    token_ids = [str(record["token_id"]) for record in sessions]
    selected_token = st.selectbox("Session to revoke", token_ids)
    actions = st.columns(2)
    if actions[0].button("Revoke selected", width="stretch"):
        try:
            api_client.revoke_session(
                access_token=access_token, token_id=selected_token
            )
            st.success("Session revoked.")
            st.rerun()
        except DashboardAPIError as error:
            st.error(str(error))
    if actions[1].button("Sign out everywhere", width="stretch"):
        try:
            api_client.revoke_all_sessions(access_token=access_token)
        except DashboardAPIError as error:
            st.error(str(error))
            return
        _clear_account_session()
        st.rerun()


def main() -> None:
    initialize_session_state()
    project = render_sidebar()
    if project is None:
        st.title("TokenGuard AI")
        st.info("Sign in or connect with API keys to continue.")
        return
    if (
        st.session_state.auth_mode == "API keys"
        and not st.session_state.project_api_key
    ):
        st.warning("Enter the API key for the selected project.")
        return
    st.title("TokenGuard AI")
    st.caption(f"Project: {project['name']}")
    analytics_tab, providers_tab, account_tab = st.tabs(
        ["Analytics", "Providers", "Account"]
    )
    with analytics_tab:
        render_analytics(project)
    with providers_tab:
        render_providers(project)
    with account_tab:
        render_account_security()


if __name__ == "__main__":
    main()
