from pathlib import Path

from scripts.generate_pdfs import SOURCE_DIR, generate


def test_generation_is_deterministic(tmp_path: Path) -> None:
    first = generate(SOURCE_DIR, tmp_path / "run1")
    second = generate(SOURCE_DIR, tmp_path / "run2")

    assert [d["sha256"] for d in first] == [d["sha256"] for d in second]


def test_manifest_describes_every_document(tmp_path: Path) -> None:
    manifest = generate(SOURCE_DIR, tmp_path)

    for entry in manifest:
        assert (tmp_path / entry["file"]).is_file()
        assert entry["pages"] >= 2
        assert {"doc_id", "version", "effective_date", "status", "classification"} <= entry.keys()


def test_outdated_revision_is_kept_as_superseded(tmp_path: Path) -> None:
    manifest = generate(SOURCE_DIR, tmp_path)

    returns = {d["version"]: d for d in manifest if d["doc_id"] == "NSI-POL-RET-003"}
    assert returns["3.0"]["status"] == "active"
    assert returns["2.1"]["status"] == "superseded"
    assert returns["2.1"]["sha256"] != returns["3.0"]["sha256"]
