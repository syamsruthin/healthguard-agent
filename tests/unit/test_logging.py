"""PII masking in structured logs."""

from __future__ import annotations

from backend.core.logging import _pii_processor, mask_pii


def test_mask_pii_covers_member_ids_emails_and_phones() -> None:
    text = mask_pii("M100004 m100001 maria@example.com 1-800-446-3623 1990-04-12")
    assert text == "<member_id> <member_id> <email> <phone> <date>"


def test_nested_values_are_masked_but_exception_info_is_untouched() -> None:
    exc_info = (ValueError, ValueError("x"), None)
    event = {
        "event": "answer_ungrounded M100001",
        "unsupported": ["Claim for M100004 was denied", {"email": "a@b.co"}],
        "trace_id": "M100001-not-masked-key",
        "exc_info": exc_info,
        "count": 3,
    }
    out = _pii_processor(None, "info", event)
    assert out["event"] == "answer_ungrounded <member_id>"
    assert out["unsupported"] == ["Claim for <member_id> was denied", {"email": "<email>"}]
    assert out["trace_id"] == "M100001-not-masked-key" and out["exc_info"] is exc_info and out["count"] == 3
