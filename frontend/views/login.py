from __future__ import annotations

import os

import streamlit as st

from frontend.lib.api import ApiError, client

DEMO_ACCOUNTS = {
    "Maria Garcia — PPO, denied MRI & appeal": "maria.garcia@demo.healthguard.dev",
    "James Chen — HMO, Ozempic & referral denial": "james.chen@demo.healthguard.dev",
    "Priya Patel — HDHP, pregnancy & HSA": "priya.patel@demo.healthguard.dev",
    "Robert Johnson — out-of-network ER, OOP max": "robert.johnson@demo.healthguard.dev",
    "Aisha Okafor — catastrophic plan": "aisha.okafor@demo.healthguard.dev",
    "David Kim — family EPO plan": "david.kim@demo.healthguard.dev",
    "Admin — portfolio analytics": "admin@demo.healthguard.dev",
}


def render() -> None:
    st.title("HealthGuard")
    st.caption("Your AI assistant for plans, claims, prescriptions and in-network care.")
    api = client()
    sign_in, demo, create = st.tabs(["Sign in", "Demo accounts", "Create account"])

    with sign_in, st.form("login"):
        email = st.text_input("Email", autocomplete="username")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        if st.form_submit_button("Sign in", type="primary", use_container_width=True):
            _attempt(lambda: api.login(email, password))

    with demo:
        st.write(
            "Synthetic member personas with realistic claims, prior authorizations and coverage stories."
        )
        choice = st.selectbox("Persona", list(DEMO_ACCOUNTS))
        demo_password = os.environ.get("DEMO_PASSWORD", "")
        if demo_password:
            if st.button("Sign in as this persona", type="primary", use_container_width=True):
                _attempt(lambda: api.login(DEMO_ACCOUNTS[choice], demo_password))
        else:
            st.info(f"Sign in as `{DEMO_ACCOUNTS[choice]}` with the demo password (`DEMO_PASSWORD`).")

    with create, st.form("register"):
        st.caption(
            "New accounts can ask general plan and policy questions; member records are linked by HealthGuard."
        )
        name = st.text_input("Full name")
        new_email = st.text_input("Email ", autocomplete="email")
        new_password = st.text_input(
            "Password (10+ characters)", type="password", autocomplete="new-password"
        )
        if st.form_submit_button("Create account", use_container_width=True):
            _attempt(lambda: api.register(new_email, new_password, name))


def _attempt(action) -> None:  # type: ignore[no-untyped-def]
    try:
        action()
    except ApiError as exc:
        st.error(exc.message)
        return
    st.rerun()
