"""The labelled evaluation set (data/evaluation/retrieval.yaml).

Each question names the document sections that contain its answer (``relevant``) and the
document versions that must never be returned for it (``forbidden``: an outdated version,
another tenant's policy). Unanswerable questions have no relevant sections at all.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.rag.retrieval import SearchHit

DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "evaluation" / "retrieval.yaml"

Category = Literal[
    "factual",
    "exact-code",
    "cross-lingual",
    "outdated-trap",
    "point-in-time",
    "other-tenant",
    "unanswerable",
]


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SectionRef(_Item):
    doc_key: str
    version: str
    section: str

    def matches(self, hit: SearchHit) -> bool:
        return (
            hit.doc_key == self.doc_key
            and hit.version == self.version
            and self.section in hit.section_path
        )


class VersionRef(_Item):
    doc_key: str
    version: str

    def matches(self, hit: SearchHit) -> bool:
        return (hit.doc_key, hit.version) == (self.doc_key, self.version)


class Question(_Item):
    id: str
    tenant: str
    category: Category
    question: str
    as_of: date | None = None
    relevant: list[SectionRef]
    forbidden: list[VersionRef]
    answer: str | None  # reference answer; None when the documents do not answer it

    @property
    def answerable(self) -> bool:
        return bool(self.relevant)


class Dataset(_Item):
    questions: list[Question]


def load_dataset(path: Path = DATASET_PATH) -> list[Question]:
    return Dataset(**yaml.safe_load(path.read_text(encoding="utf-8"))).questions
