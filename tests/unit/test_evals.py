from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd
import pytest
from instructor.core import IncompleteOutputException

from backend.evals.build_golden import in_network, load, money_re, name_res
from backend.evals.checks import aggregate, case_checks
from backend.evals.ragas_jury import Jury, failure_reason, judge_model_args
from backend.evals.run import (
    conversation_contexts,
    estimate_cost,
    flag_disagreement,
    judge_weight,
    judged_question,
    select_cases,
    turn_cost,
)

JURY = ["openai/gpt-5.4-mini", "google/gemini-3.8-flash", "anthropic/claude-haiku-5.5"]
CORE = ("faithfulness", "answer_relevancy", "context_recall")

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


def test_in_network_filters_lake_rows_and_names_become_regexes() -> None:
    lake = pd.DataFrame(
        [
            ("1", "Ann Lee, MD", "Lee", "Cardiology", "Choice PPO|Core HMO", "Miami", "FL", True),
            ("2", "Bo O'Neil, MD", "O'Neil", "Cardiology", "Choice PPO", "Miami", "FL", True),
            ("3", "Cy Diaz, MD", "Diaz", "Cardiology", "Core HMO", "Miami", "FL", True),
            ("4", "Di Fox, MD", "Fox", "Cardiology", "Choice PPO", "Miami", "FL", False),
        ],
        columns=["npi", "provider_name", "last_name", "specialty", "networks", "city", "state", "accepting"],
    )
    rows = in_network(
        lake,
        network="Choice PPO",
        city="Miami",
        state="FL",
        exclude_npi="1",
        specialty="Cardiology",
        accepting=True,
    )
    assert list(rows["npi"]) == ["2"]
    assert [bool(re.search(p, "See Dr. O'Neil")) for p in name_res(rows)] == [True]


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


def test_cost_estimate_scales_with_turns_judges_and_answer_parts() -> None:
    cases = [ANSWER_CASE, {**BLOCK_CASE, "prior_turns": ["hi"]}]
    no_judges = estimate_cost(cases, (), [], per_turn=0.01)
    assert no_judges == {"agent": 0.03, "judges": 0.0, "total": 0.03}  # 3 turns; blocked cases aren't judged
    many = [ANSWER_CASE] * 20
    single = [{**ANSWER_CASE, "expected_tools": ["sql"]}] * 20
    follow_ups = [{**ANSWER_CASE, "expected_tools": ["sql"], "prior_turns": ["q"]}] * 20
    assert (
        estimate_cost(many, ("faithfulness",), JURY)["judges"]
        > estimate_cost(many, ("faithfulness",), JURY[:1])["judges"]
        > estimate_cost(many, (), [])["judges"]
    )
    assert estimate_cost(many, CORE, JURY)["judges"] > estimate_cost(single, CORE, JURY)["judges"]
    assert estimate_cost(follow_ups, CORE, JURY)["judges"] > estimate_cost(single, CORE, JURY)["judges"]


def test_judge_and_turn_costs_follow_measured_prices() -> None:
    gemini, gpt, haiku = (judge_weight(j) for j in ("google/x", "openai/x", "anthropic/x"))
    assert gemini > gpt > haiku
    assert judge_weight("mistralai/new-judge") == gemini  # unmeasured judges are assumed expensive
    assert turn_cost(["typesafe/jev-router"] * 3) < turn_cost(
        ["typesafe/jev-router", "anthropic/claude-sonnet-5.5"]
    )


@pytest.mark.parametrize(
    ("case_ids", "agent_usd", "judge_usd"),
    [
        # run 60d20114: multi-intent, dependent-hop and follow-up cases (5 with a prior turn)
        (
            [
                "mi-01", "mi-02", "mi-03", "mi-04", "mi-05", "dep-01", "dep-02", "dep-03", "dep-04",
                "memory-01", "memory-02", "memory-03", "memory-04", "memory-05",
            ],
            0.080,
            0.596,
        ),
        # run 82775a45: one case per category of the original set (3 of them blocked)
        (
            [
                "plan-01", "prov-01", "policy-01", "member-maria-ded", "multi-01",
                "memory-01", "oos-01", "inject-01", "xmember-01", "admin-01",
            ],
            0.0364,
            0.2036,
        ),
    ],
)  # fmt: skip
def test_cost_estimate_covers_recorded_runs(case_ids: list[str], agent_usd: float, judge_usd: float) -> None:
    golden = {c["id"]: c for c in load()}
    est = estimate_cost(
        [golden[i] for i in case_ids], CORE, JURY, per_turn=turn_cost(["typesafe/jev-router"])
    )
    assert agent_usd <= est["agent"] <= 2 * agent_usd
    assert judge_usd <= est["judges"] <= 1.4 * judge_usd  # over-estimates, but not wildly


def test_follow_up_contexts_add_earlier_turns_sources() -> None:
    assert conversation_contexts([]) == []
    assert conversation_contexts([("What is my deductible?", ["[S1] ded"])]) == ["[S1] ded"]
    contexts = conversation_contexts(
        [
            ("What is my HSA balance?", ["[S1] hsa_balance 2150"]),
            ("And my claims?", []),
            ("Is it enough to cover the rest of my deductible?", ["[S1] deductible left 4078.16"]),
        ]
    )
    assert contexts[0] == "[S1] deductible left 4078.16"  # the judged turn's own sources stay first
    assert len(contexts) == 2 and contexts[1].endswith("\n[S1] hsa_balance 2150")
    assert "What is my HSA balance?" in contexts[1] and "earlier turn" in contexts[1]


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
    assert scores["answer_relevancy"]["failed"] == {"judge-b": "RuntimeError"}
    assert "failed" not in scores["faithfulness"]


async def test_jury_records_truncated_judges() -> None:
    jury = Jury(["judge-a", "judge-b"], ("faithfulness",))

    def scorer(value: float | Exception):
        async def ascore(**_: object) -> SimpleNamespace:
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(value=value)

        return SimpleNamespace(ascore=ascore)

    jury._scorers = {
        "judge-a": {"faithfulness": scorer(0.9)},
        "judge-b": {"faithfulness": scorer(IncompleteOutputException())},
    }
    scores = await jury.score(question="q", answer="a", contexts=["[S1] ctx"], reference="")
    assert scores["faithfulness"] == {
        "judges": {"judge-a": 0.9},
        "failed": {"judge-b": "max_tokens"},
        "score": 0.9,
        "spread": 0.0,
    }
    assert failure_reason(RuntimeError("finish_reason=length: max_tokens reached")) == "max_tokens"


async def test_gemini_judges_cap_their_reasoning_on_the_wire() -> None:
    import importlib
    import json as _json

    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    from pydantic import BaseModel
    from ragas.llms import llm_factory

    sdk_httpx = importlib.import_module(DefaultAsyncHttpxClient.__mro__[1].__module__.split(".")[0])
    bodies: list[dict] = []

    def handler(request):  # type: ignore[no-untyped-def]
        body = _json.loads(request.content)
        bodies.append(body)
        message = {"role": "assistant", "content": _json.dumps({"answer": "ok"})}
        return sdk_httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    class Out(BaseModel):
        answer: str

    client = AsyncOpenAI(
        api_key="k",
        base_url="https://openrouter.test/v1",
        http_client=DefaultAsyncHttpxClient(transport=sdk_httpx.MockTransport(handler)),
    )
    for judge in JURY:
        llm = llm_factory(judge, client=client, adapter="instructor", **judge_model_args(judge))
        assert (await llm.agenerate("hi", Out)).answer == "ok"
    await client.close()
    by_model = {b["model"]: b for b in bodies}
    assert by_model["google/gemini-3.8-flash"]["reasoning"] == {"effort": "low"}
    assert "reasoning" not in by_model["anthropic/claude-haiku-5.5"]  # would switch Haiku's thinking on
    assert "reasoning" not in by_model["openai/gpt-5.4-mini"]
    assert {b["max_tokens"] for b in bodies} == {8192}


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
