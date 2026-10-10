"""Memory: what the assistant remembers about the user's preferences, with full control."""

from __future__ import annotations

import streamlit as st

from frontend.lib.api import client


def render() -> None:
    api = client()
    st.title("Memory")
    st.write(
        "HealthGuard remembers lasting preferences — like your preferred language, location or how you like answers "
        "formatted — to personalise future conversations. It never stores diagnoses, medications, claim details, "
        "amounts or IDs."
    )
    user = st.session_state.get("user", {})
    enabled = st.toggle("Remember my preferences", value=bool(user.get("memory_enabled", True)))
    if enabled != bool(user.get("memory_enabled", True)):
        api.set_memory_enabled(enabled)
        st.rerun()

    memories = api.memories()
    if not memories:
        st.info("Nothing remembered yet." if enabled else "Memory is off.")
        return
    for m in memories:
        left, right = st.columns([6, 1], vertical_alignment="center")
        left.markdown(f"{m['content']}  \n:gray-badge[{m['category'].replace('_', ' ')}]")
        if right.button("Forget", key=f"forget_{m['id']}", icon=":material/delete:"):
            api.delete_memory(m["id"])
            st.rerun()
    if st.button("Forget everything", type="secondary"):
        api.clear_memories()
        st.rerun()
