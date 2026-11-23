"""The return window enforced in code must match each tenant's active returns policy."""

import re
from pathlib import Path

import yaml

from app.core.config import Settings
from scripts.generate_pdfs import SOURCE_DIR

WINDOW = re.compile(r"within \*\*(\d+) calendar days of the delivery date\*\*")


def active_return_windows() -> dict[str, int]:
    windows: dict[str, int] = {}
    for path in Path(SOURCE_DIR, "documents").rglob("returns-policy*.yaml"):
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        if document["status"] == "active":
            match = WINDOW.search(text)
            assert match, f"{path.name}: no return window found"
            windows[document["tenant_id"]] = int(match.group(1))
    return windows


def test_the_enforced_window_matches_the_active_policies() -> None:
    assert Settings(_env_file=None).return_window_days == active_return_windows()  # type: ignore[call-arg]
