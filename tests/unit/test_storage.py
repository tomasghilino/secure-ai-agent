from pathlib import Path

import pytest

from app.ingestion.storage import LocalFileStorage, ObjectNotFoundError, upload_key


async def test_a_stored_object_can_be_read_back(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    key = upload_key("nsi-amer", "ab" * 32)

    await storage.put(key, b"%PDF-1.7 first")
    await storage.put(key, b"%PDF-1.7 second")  # same key again: replaced, never appended

    assert await storage.get(key) == b"%PDF-1.7 second"
    assert [p.name for p in (tmp_path / "uploads" / "nsi-amer").iterdir()] == [f"{'ab' * 32}.pdf"]


async def test_a_missing_object_raises_object_not_found(tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFoundError):
        await LocalFileStorage(tmp_path).get("uploads/nsi-amer/missing.pdf")


async def test_a_key_cannot_escape_the_storage_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes"):
        await storage.put("../outside.pdf", b"%PDF-")
