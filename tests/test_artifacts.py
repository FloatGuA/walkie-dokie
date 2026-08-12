from pathlib import Path

import pytest

import walkie_dokie.artifacts as artifact_store
from walkie_dokie.platforms.base import IncomingFile


def test_incoming_bytes_are_persisted_outside_graph_state(monkeypatch, tmp_path):
    root = tmp_path / "inputs"
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", root)
    reference = artifact_store.store_incoming_file(
        "test",
        "u1",
        IncomingFile("../report.docx", b"secret", "application/test"),
    )
    assert set(reference) == {"kind", "path", "filename", "mime_type"}
    assert reference["filename"] == "report.docx"
    assert "content" not in reference
    assert artifact_store.resolve_artifact_reference(reference).read_bytes() == b"secret"


def test_reference_cannot_cross_its_declared_storage_root(monkeypatch, tmp_path):
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"x")
    monkeypatch.setattr(artifact_store, "INPUT_ARTIFACTS_ROOT", input_root)
    with pytest.raises(RuntimeError, match="越过"):
        artifact_store.resolve_artifact_reference(
            {
                "kind": "input",
                "path": str(outside),
                "filename": outside.name,
                "mime_type": "application/octet-stream",
            }
        )


def test_output_reference_validates_filename_and_regular_file(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    artifact = workspace_root / "result.docx"
    artifact.write_bytes(b"x")
    monkeypatch.setattr(artifact_store, "WORKSPACES_ROOT", workspace_root)
    reference = artifact_store.output_artifact_reference(artifact, artifact.name)
    assert artifact_store.resolve_artifact_reference(reference) == artifact.resolve()

    with pytest.raises(RuntimeError, match="不一致"):
        artifact_store.resolve_artifact_reference(
            {**reference, "filename": "different.docx"}
        )
