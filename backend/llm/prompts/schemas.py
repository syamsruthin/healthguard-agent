"""Pydantic schemas for structured LLM outputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ToolName = Literal["sql", "providers", "policy_search"]


class PlanStep(BaseModel):
    id: str = Field(description="Short step id, e.g. 's1'")
    tool: ToolName
    task: str = Field(description="Self-contained instruction for the tool, with every specific it needs")
    depends_on: list[str] = Field(
        default_factory=list, description="Ids of steps whose output this step needs"
    )


class Understanding(BaseModel):
    in_scope: bool
    safety: Literal["ok", "prompt_injection", "other_member_data", "medical_advice", "harmful"] = "ok"
    standalone_query: str = Field(description="The user's request rewritten as a self-contained question")
    needs_member_data: bool = Field(description="True if answering requires the user's own member records")
    plan: list[PlanStep] = Field(default_factory=list, description="1-4 tool steps; empty for small talk")
    reason: str = Field(description="One sentence explaining the scope/safety decision and plan")


class Verification(BaseModel):
    grounded: bool = Field(description="Every factual claim is supported by the sources or conversation")
    complete: bool = Field(
        description="All parts of the question are answered (or honestly marked unavailable)"
    )
    unsupported_claims: list[str] = Field(default_factory=list)
    missing: str = Field(default="", description="What is missing, phrased as a follow-up retrieval need")
    reason: str = ""


class ExtractedMemory(BaseModel):
    content: str
    category: Literal[
        "location", "language", "provider_preference", "plan_interest", "communication_style", "other"
    ]


class MemoryExtraction(BaseModel):
    memories: list[ExtractedMemory] = Field(default_factory=list)


class Title(BaseModel):
    title: str
