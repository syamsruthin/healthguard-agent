"""
`hg-models-check` — verify every configured OpenRouter model slug exists.

Model catalogs change quickly; run this in CI (eval workflow) and after
changing MODEL_* / EVAL_JUDGES env vars. Exit code 1 if anything is missing.
"""

from __future__ import annotations

import sys

import httpx

from backend.core.config import settings
from backend.llm.registry import all_configured_models


def _ids(url: str) -> set[str]:
    key = settings.openrouter_api_key
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = httpx.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return {m["id"] for m in resp.json().get("data", [])}


def main() -> None:
    base = settings.llm_base_url.rstrip("/")
    try:
        chat = _ids(f"{base}/models")
    except Exception as exc:
        print(f"❌ Could not reach {base}/models: {exc}")
        sys.exit(2)
    try:
        embed = _ids(f"{base}/embeddings/models")
    except Exception:
        embed = set()

    missing = []
    print(f"Checking {len(all_configured_models())} chat models against {len(chat)} available:")
    for model in all_configured_models():
        ok = model in chat
        print(f"  {'✅' if ok else '❌'} {model}")
        if not ok:
            missing.append(model)
    if settings.embedding_provider == "openrouter":
        ok = settings.embedding_model in embed or settings.embedding_model in chat
        print(f"  {'✅' if ok else '⚠️ '} embedding: {settings.embedding_model}")
        if embed and not ok:
            missing.append(settings.embedding_model)
    if missing:
        print(f"\n{len(missing)} model(s) not found — update MODEL_* / EVAL_JUDGES / EMBEDDING_MODEL.")
        sys.exit(1)
    print("\nAll configured models are available.")


if __name__ == "__main__":
    main()
