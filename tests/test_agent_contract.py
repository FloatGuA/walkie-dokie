import pytest

from walkie_dokie.agents.base import (
    ExecutionReport,
    resolve_output_file,
    safe_input_filename,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "input"),
        ("report.docx", "report.docx"),
        ("../../secret.txt", "secret.txt"),
        (r"..\\..\\secret.txt", "secret.txt"),
        (".walkie-dokie", "input-.walkie-dokie"),
    ],
)
def test_input_filename_is_confined_to_one_basename(value, expected):
    assert safe_input_filename(value) == expected


def test_output_filename_must_be_a_single_relative_name(tmp_path):
    output = tmp_path / "result.docx"
    output.write_bytes(b"x")
    assert resolve_output_file(tmp_path, "result.docx") == output.resolve()

    for unsafe in ("../result.docx", "/tmp/result.docx", "nested/result.docx"):
        with pytest.raises(RuntimeError, match="不安全"):
            resolve_output_file(tmp_path, unsafe)


def test_output_symlink_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "result.txt"
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match="越过工作目录"):
        resolve_output_file(tmp_path, "result.txt")


def test_output_must_be_a_regular_file(tmp_path):
    (tmp_path / "directory").mkdir()
    with pytest.raises(RuntimeError, match="不是普通文件"):
        resolve_output_file(tmp_path, "directory")


def test_execution_report_enforces_artifact_metadata_invariants(tmp_path):
    artifact = tmp_path / "result.docx"
    artifact.write_bytes(b"x")
    report = ExecutionReport("done", artifact, artifact.name)
    assert report.artifact_path == artifact

    with pytest.raises(ValueError, match="同时存在"):
        ExecutionReport("done", artifact, None)
    with pytest.raises(ValueError, match="不一致"):
        ExecutionReport("done", artifact, "other.docx")
    with pytest.raises(ValueError, match="普通文件"):
        ExecutionReport("done", tmp_path / "missing.docx", "missing.docx")
