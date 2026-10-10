from __future__ import annotations

from frontend.lib.sse import parse_sse


def test_parses_events_pings_and_multiline_data() -> None:
    lines = [
        "event: meta\r\n",
        'data: {"turn_id": "t1"}\r\n',
        "\r\n",
        ": ping - 2026-10-10\r\n",
        "\r\n",
        "event: token\n",
        'data: {"text":\n',
        'data: "Hello"}\n',
        "\n",
        "event: final\n",
        'data: {"outcome": "answered"}\n',  # no trailing blank line
    ]
    events = list(parse_sse(lines))
    assert events == [
        {"type": "meta", "turn_id": "t1"},
        {"type": "token", "text": "Hello"},
        {"type": "final", "outcome": "answered"},
    ]


def test_non_json_and_non_object_payloads() -> None:
    assert list(parse_sse(["event: x\n", "data: not json\n", "\n"])) == [{"type": "x", "raw": "not json"}]
    assert list(parse_sse(["data: [1, 2]\n", "\n"])) == [{"type": "message", "value": [1, 2]}]
