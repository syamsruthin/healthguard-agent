from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from backend.evals.build_golden import money_re
from backend.evals.checks import aggregate, case_checks
from backend.evals.ragas_jury import Jury
from backend.evals.run import estimate_cost, flag_disagreement, judged_question, select_cases

ANSWER_CASE = {
    "expected_outcome": "answered",
    "expected_tools": ["sql", "policy_search"],
    "must_include": [money_re(2700), r"180"],
    "forbidden_ids": ["M100004"],
}
BLOCK_CASE = {
    "expected_outcome": "blocked",
    "expected_fallback": "other_member_data",
    "expected_tools": [],
    "must_include": [],
    "forbidden_ids": ["M100004"],
}


def _result(answer: str, outcome: str = "answered", tools: tuple[str, ...] = ("sql", "policy_search"), **kw):
    return {"answer": answer, "outcome": outcome, "tools": [{"tool": t} for t in tools], **kw}


@pytest.mark.parametrize(
    ("text", "ok"), [("$2,700.00", True), ("2700", True), ("$2,700", True), ("$27,000", False)]
)
def test_money_regex(text: str, ok: bool) -> None:
    assert bool(re.search(money_re(2700), text)) is ok


def test_answered_case_checks() -> None:
    good = case_checks(ANSWER_CASE, _result("Met $2,700.00; appeal within 180 days."))
    assert good == {"outcome": True, "route": True, "isolation": True, "content": True}
    bad = case_checks(ANSWER_CASE, _result("Member M100004 met $2,700.", tools=("sql",)))
    assert bad == {"outcome": True, "route": False, "isolation": False, "content": False}


def test_blocked_case_checks() -> None:
    assert (
        case_checks(BLOCK_CASE, _result("no", "blocked", (), fallback="other_member_data"))["outcome"] is True
    )
    assert case_checks(BLOCK_CASE, _result("no", "blocked", (), fallback="out_of_scope"))["outcome"] is False
    assert (
        case_checks(BLOCK_CASE, _result("Here are M100004's claims", "answered", ("sql",)))["isolation"]
        is False
    )


def test_aggregate_rates() -> None:
    cases = [ANSWER_CASE, BLOCK_CASE]
    checks = [
        {"outcome": True, "route": False, "isolation": True, "content": True},
        {"outcome": True, "route": None, "isolation": True, "content": None},
    ]
    assert aggregate(cases, checks) == {
        "answer_rate": 1.0,
        "refusal_accuracy": 1.0,
        "route_accuracy": 0.0,
        "isolation": 1.0,
        "content_accuracy": 1.0,
    }


def test_cost_estimate_scales_with_cases_and_judges() -> None:
    cases = [ANSWER_CASE, {**BLOCK_CASE, "prior_turns": ["hi"]}]
    no_judges = estimate_cost(cases, (), 0)
    assert no_judges == pytest.approx(3 * 0.015, abs=0.01)
    many = [ANSWER_CASE] * 20
    assert (
        estimate_cost(many, ("faithfulness",), 3)
        > estimate_cost(many, ("faithfulness",), 1)
        > estimate_cost(many, (), 0)
    )


def test_disagreement_flag() -> None:
    assert flag_disagreement({"faithfulness": {"spread": 0.5, "judges": {"a": 1, "b": 0.5}}}, 2)
    assert flag_disagreement({"faithfulness": {"spread": 0.0, "judges": {"a": 1}}}, 2)  # a judge failed
    assert not flag_disagreement({"faithfulness": {"spread": 0.1, "judges": {"a": 1, "b": 0.9}}}, 2)
    assert not flag_disagreement({"faithfulness": {"spread": 0.0, "judges": {"a": 1}}}, 1)


async def test_jury_aggregates_and_tolerates_failing_judges() -> None:
    jury = Jury(["judge-a", "judge-b"], ("faithfulness", "answer_relevancy", "context_recall"))

    def scorer(value: float | Exception):
        async def ascore(**_: object) -> SimpleNamespace:
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(value=value)

        return SimpleNamespace(ascore=ascore)

    jury._scorers = {
        "judge-a": {
            "faithfulness": scorer(0.9),
            "answer_relevancy": scorer(0.8),
            "context_recall": scorer(1.0),
        },
        "judge-b": {
            "faithfulness": scorer(0.5),
            "answer_relevancy": scorer(RuntimeError("down")),
            "context_recall": scorer(1.0),
        },
    }
    scores = await jury.score(question="q", answer="a", contexts=["[D1] ctx"], reference="")
    assert scores["faithfulness"] == {"judges": {"judge-a": 0.9, "judge-b": 0.5}, "score": 0.7, "spread": 0.4}
    assert scores["answer_relevancy"]["score"] == 0.8 and scores["answer_relevancy"]["judges"] == {
        "judge-a": 0.8
    }
    assert "context_recall" not in scores  # needs a reference


def test_select_cases_stratified_sample() -> None:
    cases = [{"id": f"{cat}-{i}", "category": cat} for cat in ("a", "b", "c") for i in range(3)]
    assert [c["id"] for c in select_cases(cases, per_category=1)] == ["a-0", "b-0", "c-0"]
    assert [c["id"] for c in select_cases(cases, categories=["b"], limit=2)] == ["b-0", "b-1"]
    assert len(select_cases(cases, per_category=2, limit=5)) == 5


def test_grounding_reaches_faithfulness_only() -> None:
    faith = Jury._inputs("faithfulness", "q", "a", ["[S1] data"], "ref", ["Member profile: x"])
    recall = Jury._inputs("context_recall", "q", "a", ["[S1] data"], "ref", ["Member profile: x"])
    assert faith is not None and faith["retrieved_contexts"] == ["[S1] data", "Member profile: x"]
    assert recall is not None and recall["retrieved_contexts"] == ["[S1] data"]
    assert (
        Jury._inputs("faithfulness", "q", "a", [], "ref", ["g"]) is None
    )  # nothing retrieved, nothing to judge


def test_follow_ups_are_judged_against_the_resolved_question() -> None:
    follow_up = {
        "question": "How much more before I hit it?",
        "prior_turns": ["What is my deductible status?"],
    }
    first_turn = {"question": "What is my deductible?", "prior_turns": []}
    result = {"standalone_query": "How much more do I need to pay to meet my individual deductible?"}
    assert judged_question(follow_up, result) == result["standalone_query"]
    assert judged_question(first_turn, result) == "What is my deductible?"
    assert judged_question(follow_up, {}) == follow_up["question"]


async def test_spend_meter_sums_reported_judge_cost() -> None:
    import importlib
    import json as _json

    from openai import DefaultAsyncHttpxClient

    # Whichever httpx distribution the SDK is built on (httpx or httpx2).
    sdk_httpx = importlib.import_module(DefaultAsyncHttpxClient.__mro__[1].__module__.split(".")[0])

    from backend.evals.ragas_jury import SpendMeter
    from backend.evals.run import judge_cost

    replies = iter([{"usage": {"cost": 0.0021}}, {"usage": {"cost": 0.0004}}, {"error": "x"}])

    def handler(request):  # type: ignore[no-untyped-def]
        body = next(replies)
        return sdk_httpx.Response(200 if "usage" in body else 500, json=body)

    meter = SpendMeter()
    client = DefaultAsyncHttpxClient(
        transport=sdk_httpx.MockTransport(handler), event_hooks={"response": [meter.on_response]}
    )
    for _ in range(3):
        response = await client.post("https://openrouter.test/v1/chat/completions", json={})
        assert _json.loads(response.content)  # the hook read the body; the caller can still parse it
    await client.aclose()
    assert meter.calls == 2 and round(meter.cost_usd, 6) == 0.0025
    assert judge_cost(SimpleNamespace(spend=meter)) == 0.0025 and judge_cost(None) == 0.0
