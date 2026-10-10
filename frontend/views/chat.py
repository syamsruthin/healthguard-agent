"""Chat page: conversation list, streamed answers with live progress, citations, feedback."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend.lib import ui
from frontend.lib.api import ApiError, client

QUICK_PROMPTS: dict[str, list[str]] = {
    "maria.garcia@demo.healthguard.dev": [
        "Why was my knee MRI in April denied, and how do I appeal?",
        "What's my deductible status?",
        "Find a Spanish-speaking cardiologist near me",
    ],
    "james.chen@demo.healthguard.dev": [
        "Is Ozempic covered, and does it need prior authorization?",
        "Why was my dermatology visit denied?",
        "Do I need a referral to see a specialist?",
    ],
    "priya.patel@demo.healthguard.dev": [
        "How much will I pay for my delivery?",
        "What is my HSA balance?",
        "Is prenatal care covered at no cost?",
    ],
    "robert.johnson@demo.healthguard.dev": [
        "Was my out-of-network ER visit covered?",
        "Have I reached my out-of-pocket maximum?",
        "Why was one of my claims marked as a duplicate?",
    ],
    "aisha.okafor@demo.healthguard.dev": [
        "How many free primary care visits do I get?",
        "Which of my claims are still pending?",
        "Is therapy by telehealth covered?",
    ],
    "david.kim@demo.healthguard.dev": [
        "Why was the urgent care visit for my family denied?",
        "Find an in-network pediatrician near me with telehealth",
        "How close is my family to the deductible?",
    ],
    "admin@demo.healthguard.dev": [
        "What are the top 5 denial reasons this year, with counts?",
        "Which plans have the highest average member out-of-pocket cost?",
        "How many prior authorizations are pending by service?",
    ],
}
DEFAULT_PROMPTS = [
    "What's the difference between an HMO and a PPO?",
    "How do I appeal a denied claim?",
    "Is telehealth covered?",
]
TOOL_ICON = {"ok": ":material/check_circle:", "error": ":material/error:"}


def _send_feedback(message_id: str) -> None:
    value = st.session_state.get(f"fb_{message_id}")
    if value is None:
        return
    try:
        client().feedback(message_id, 1 if value == 1 else -1)
        st.toast("Thanks for the feedback")
    except ApiError as exc:
        st.toast(f"Feedback not saved: {exc.message}")


def _select(conversation_id: str | None) -> None:
    st.session_state["conversation_id"] = conversation_id
    st.session_state["messages"] = client().messages(conversation_id) if conversation_id else []


def _sidebar(api: Any) -> bool:
    with st.sidebar:
        if st.button("New chat", icon=":material/add:", use_container_width=True):
            _select(None)
            st.rerun()
        show_reasoning = st.toggle("Show how answers are produced", value=False)
        st.caption("Recent conversations")
        current = st.session_state.get("conversation_id")
        for conv in api.conversations()[:25]:
            if st.button(
                conv["title"],
                key=f"conv_{conv['id']}",
                type="primary" if conv["id"] == current else "tertiary",
                use_container_width=True,
            ):
                _select(conv["id"])
                st.rerun()
        if current and st.button(
            "Delete this conversation", icon=":material/delete:", use_container_width=True
        ):
            api.delete_conversation(current)
            _select(None)
            st.rerun()
    return show_reasoning


def _render_message(m: dict[str, Any], show_reasoning: bool) -> None:
    with st.chat_message(m["role"]):
        st.markdown(ui.md(m["content"]))
        if m["role"] != "assistant":
            return
        meta = {
            **(m.get("meta") or {}),
            "tools": m.get("tools", []),
            "cache_hit": m.get("cache_hit"),
            "latency_ms": m.get("latency_ms"),
        }
        ui.answer_badges(meta)
        ui.citations(m.get("citations", []))
        if show_reasoning:
            ui.reasoning(meta, m.get("trace"))
        st.feedback("thumbs", key=f"fb_{m['id']}", on_change=_send_feedback, args=(m["id"],))


def _stream_turn(api: Any, prompt: str) -> None:
    cid = st.session_state.get("conversation_id")
    if not cid:
        cid = api.create_conversation()["id"]
        st.session_state["conversation_id"] = cid
    st.session_state.setdefault("messages", []).append({"id": "pending", "role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(ui.md(prompt))
    with st.chat_message("assistant"):
        status = st.status("Understanding your question…", expanded=False)
        placeholder = st.empty()
        text, final = "", None
        try:
            for ev in api.stream(cid, prompt):
                kind = ev["type"]
                if kind == "status":
                    status.update(label=f"{ev['message']}…")
                elif kind == "plan":
                    steps = "; ".join(
                        f"{ui.TOOL_LABEL.get(s['tool'], s['tool'])}: {s['task']}" for s in ev["steps"]
                    )
                    status.write(ui.md(f"**Plan** — {steps}"))
                elif kind == "tool" and ev["status"] != "running":
                    icon = TOOL_ICON.get(ev["status"], "")
                    status.write(
                        f"{icon} {ui.TOOL_LABEL.get(ev['tool'], ev['tool'])} · {ev.get('row_count', 0)} results · "
                        f"{ev.get('duration_ms', 0) / 1000:.1f}s"
                    )
                elif kind == "token":
                    text += ev["text"]
                    placeholder.markdown(ui.md(text) + "▌")
                elif kind == "answer_reset":
                    text = ""
                    placeholder.markdown("_Refining the answer…_")
                elif kind == "answer_replaced":
                    text = ev["answer"]
                    placeholder.markdown(ui.md(text))
                elif kind == "error":
                    status.update(label="Something went wrong", state="error")
                elif kind == "final":
                    final = ev
        except ApiError as exc:
            status.update(label="Request failed", state="error")
            st.error(exc.message)
            st.session_state["messages"].pop()
            return
        status.update(label="Done", state="complete")
        if final is not None:
            placeholder.markdown(ui.md(final["answer"]))
    if final is not None:
        st.session_state["messages"][-1]["id"] = final.get("message_id", "pending") + ":user"
        st.session_state["messages"].append(
            {
                "id": final["message_id"],
                "role": "assistant",
                "content": final["answer"],
                "citations": final.get("citations", []),
                "tools": final.get("tools", []),
                "cache_hit": final.get("cache_hit"),
                "latency_ms": final.get("latency_ms"),
                "trace": final.get("trace"),
                "meta": {
                    "outcome": final.get("outcome"),
                    "verification": final.get("verification"),
                    "usage": final.get("usage"),
                    "plan": next(
                        (
                            s["output"].get("plan", [])
                            for s in reversed(final.get("trace", []))
                            if s["node"] == "understand" and isinstance(s.get("output"), dict)
                        ),
                        [],
                    ),
                },
            }
        )
    st.rerun()


def render() -> None:
    api = client()
    show_reasoning = _sidebar(api)
    messages: list[dict[str, Any]] = st.session_state.setdefault("messages", [])
    pending = None
    if not messages:
        user = st.session_state.get("user", {})
        st.subheader(f"Hi {user.get('full_name', '').split(' ')[0] or 'there'}, how can I help?")
        prompts = QUICK_PROMPTS.get(user.get("email", ""), DEFAULT_PROMPTS)
        pending = st.pills(
            "Try asking", prompts, selection_mode="single", key="quick_prompt", label_visibility="collapsed"
        )
    for m in messages:
        _render_message(m, show_reasoning)
    prompt = st.chat_input("Ask about your plan, claims, prescriptions or providers")
    if prompt or pending:
        _stream_turn(api, prompt or pending)
