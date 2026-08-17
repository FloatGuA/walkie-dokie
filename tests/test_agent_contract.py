import pytest
from docx import Document

from walkie_dokie.agents.base import (
    ExecutionArtifact,
    ExecutionReport,
    resolve_output_file,
    safe_input_filename,
    stage_execution_inputs,
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


def test_execution_artifact_enforces_metadata_invariants(tmp_path):
    artifact = tmp_path / "result.docx"
    artifact.write_bytes(b"x")
    ea = ExecutionArtifact(artifact, artifact.name)
    assert ea.path == artifact

    with pytest.raises(ValueError, match="不一致"):
        ExecutionArtifact(artifact, "other.docx")
    with pytest.raises(ValueError, match="普通文件"):
        ExecutionArtifact(tmp_path / "missing.docx", "missing.docx")


def test_execution_report_defaults_to_no_artifacts():
    report = ExecutionReport("done")
    assert report.artifacts == ()
    assert report.warnings == ()


def test_execution_report_accepts_multiple_artifacts(tmp_path):
    a = tmp_path / "a.docx"
    a.write_bytes(b"x")
    b = tmp_path / "b.xlsx"
    b.write_bytes(b"y")
    report = ExecutionReport(
        "done", artifacts=(ExecutionArtifact(a, "a.docx"), ExecutionArtifact(b, "b.xlsx"))
    )
    assert [item.filename for item in report.artifacts] == ["a.docx", "b.xlsx"]


def test_execution_report_rejects_duplicate_artifact_filenames(tmp_path):
    a = tmp_path / "a.docx"
    a.write_bytes(b"x")
    with pytest.raises(ValueError, match="重复"):
        ExecutionReport(
            "done", artifacts=(ExecutionArtifact(a, "a.docx"), ExecutionArtifact(a, "a.docx"))
        )


def test_execution_report_rejects_non_tuple_artifacts(tmp_path):
    a = tmp_path / "a.docx"
    a.write_bytes(b"x")
    with pytest.raises(ValueError, match="tuple"):
        ExecutionReport("done", artifacts=[ExecutionArtifact(a, "a.docx")])


def _write_docx(path):
    document = Document()
    document.add_paragraph("安全内容")
    document.save(path)


def test_stage_execution_inputs_empty_is_not_an_error(tmp_path):
    staged, warnings = stage_execution_inputs((), (), tmp_path)
    assert staged == ()
    assert warnings == ()


def test_stage_execution_inputs_copies_valid_files(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    a = source_dir / "a.docx"
    _write_docx(a)
    staged, warnings = stage_execution_inputs((a,), ("a.docx",), workdir)
    assert staged == ("a.docx",)
    assert warnings == ()
    assert (workdir / "a.docx").is_file()


def test_stage_execution_inputs_excludes_invalid_file_and_continues(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    good = source_dir / "good.docx"
    _write_docx(good)
    bad = source_dir / "bad.docx"
    bad.write_text("不是合法的 docx")
    staged, warnings = stage_execution_inputs(
        (good, bad), ("good.docx", "bad.docx"), workdir
    )
    assert staged == ("good.docx",)
    assert len(warnings) == 1
    assert "bad.docx" in warnings[0]
    assert (workdir / "good.docx").is_file()
    assert not (workdir / "bad.docx").exists()


def test_stage_execution_inputs_all_invalid_raises(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    bad = source_dir / "bad.docx"
    bad.write_text("不是合法的 docx")
    with pytest.raises(RuntimeError, match="全部输入文件都未通过"):
        stage_execution_inputs((bad,), ("bad.docx",), workdir)
