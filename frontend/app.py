"""
HealthGuard Streamlit UI — `streamlit run frontend/app.py`.

Pages are chosen per role with `st.navigation`: signed-out users only see the
login page; admin pages are only registered for admins.
"""

from __future__ import annotations

import streamlit as st

from frontend.lib.api import ApiError, SessionExpired, clear_session, client
from frontend.lib.nav import pages_for
from frontend.views import login

st.set_page_config(page_title="HealthGuard", page_icon=":material/health_and_safety:", layout="wide")


def _guard(render):  # type: ignore[no-untyped-def]
    """Run a page; send the user back to login if their session has expired."""

    def page() -> None:
        try:
            render()
        except SessionExpired:
            clear_session()
            st.rerun()
        except ApiError as exc:
            st.error(f"{exc.message} (HTTP {exc.status})")

    page.__name__ = render.__module__.rsplit(".", 1)[-1]
    return page


user = st.session_state.get("user")
if not user:
    nav = st.navigation([st.Page(login.render, title="Sign in", icon=":material/login:")], position="hidden")
else:
    pages = {
        section: [
            st.Page(_guard(p.render), title=p.title, icon=p.icon, url_path=p.url_path, default=p.default)
            for p in specs
        ]
        for section, specs in pages_for(user).items()
    }
    nav = st.navigation(pages)
    with st.sidebar:
        st.caption(
            f"Signed in as **{user['full_name']}**"
            + (f" · {user['member_id']}" if user.get("member_id") else "")
        )
        if st.button("Sign out", icon=":material/logout:", use_container_width=True):
            client().logout()
            st.rerun()
nav.run()
