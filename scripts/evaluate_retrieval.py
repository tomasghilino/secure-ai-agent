"""Compare dense, keyword and hybrid retrieval on the labelled evaluation set.

Needs `make ingest` with the configured embedding model. Free with Ollama. Usage:
    uv run python -m scripts.evaluate_retrieval
Prints Markdown tables and writes every per-question score to reports/ (not committed).
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.evaluation.dataset import load_dataset
from app.evaluation.retrieval import by_mode_and_category, evaluate_retrieval, summarize
from app.rag.embeddings import build_embedding_provider

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
MODES = ("dense", "keyword", "hybrid")


async def main() -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    questions = load_dataset()
    engine = build_engine(settings.database_url)
    try:
        scores = await evaluate_retrieval(build_session_factory(engine), embeddings, questions)
    finally:
        await engine.dispose()

    groups = by_mode_and_category(scores)
    print(f"Embedding model: {embeddings.model_id}")
    print(f"Questions: {sum(1 for q in questions if q.answerable)} answerable\n")
    print("| Mode | hit@1 | hit@3 | hit@5 | recall@5 | MRR@10 | forbidden |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for mode in MODES:
        s = summarize(groups[mode, "all"])
        print(
            f"| {mode} | {s.hit_at_1:.2f} | {s.hit_at_3:.2f} | {s.hit_at_5:.2f} "
            f"| {s.recall_at_5:.2f} | {s.mrr_at_10:.2f} | {s.forbidden} |"
        )

    categories = sorted({category for _, category in groups if category != "all"})
    print("\nhit@5 by category\n")
    print("| Category | n | " + " | ".join(MODES) + " |")
    print("| --- | --- | " + " | ".join("---" for _ in MODES) + " |")
    for category in categories:
        n = len(groups[MODES[0], category])
        row = " | ".join(f"{summarize(groups[mode, category]).hit_at_5:.2f}" for mode in MODES)
        print(f"| {category} | {n} | {row} |")

    missed = sorted({s.question_id for s in scores if s.mode == "hybrid" and not s.first_relevant})
    print(f"\nNot in the hybrid top 10: {', '.join(missed) or 'none'}")

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"retrieval-{stamp}.json"
    payload = {"embedding_model": embeddings.model_id, "scores": [asdict(s) for s in scores]}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Details: {path.relative_to(REPORTS_DIR.parent)}")


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
