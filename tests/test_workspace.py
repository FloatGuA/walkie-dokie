import pytest

from walkie_dokie import workspace


def test_workspace_user_id_cannot_escape_root(monkeypatch, tmp_path):
    monkeypatch.setattr(workspace, "WORKSPACES_ROOT", tmp_path)
    workdir = workspace.create_workspace_dir("../platform", "../../user")
    assert workdir.resolve().is_relative_to(tmp_path.resolve())
    assert ".." not in workdir.relative_to(tmp_path).parts


def test_artifact_reference_must_stay_in_workspace(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(workspace, "WORKSPACES_ROOT", root)
    artifact = root / "result.docx"
    artifact.write_bytes(b"x")
    assert workspace.resolve_artifact_reference(str(artifact)) == artifact.resolve()

    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="越过"):
        workspace.resolve_artifact_reference(str(outside))
