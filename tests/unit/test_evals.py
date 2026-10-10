from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from backend.evals.build_golden import money_re
from backend.evals.checks import aggregate, case_checks
from backend.evals.ragas_jury import Jury
from backend.evals.run import estimate_cost, flag_disagreement

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
    assert no_judges == pytest.approx(3 * 0.025, abs=0.01)
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
