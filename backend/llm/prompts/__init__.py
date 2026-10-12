"""
Versioned prompt templates.

Each builder returns OpenAI-style messages. The large, stable prefix of
every prompt (role, rules, data catalog, few-shot examples) is a separate
system message flagged `cache=True` so providers can serve it from the
prompt cache; per-request context comes after it.

Bump PROMPT_VERSION whenever a prompt changes — it is part of the exact
LLM cache key and the semantic answer cache filter.
"""

PROMPT_VERSION = "2026.10.4"
