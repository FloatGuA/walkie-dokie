from pathlib import Path

from walkie_dokie.agents.security import validate_office_artifact
from walkie_dokie.evals.fake_execution import (
    FakeExecutionAgent,
    RecordingExecutionAgent,
)

FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "fake_output.docx"


async def test_fake_agent_writes_valid_artifact_into_workdir(tmp_path):
    agent = FakeExecutionAgent(output_fixture=FIXTURE)
    report = await agent.run(
        instruction="转成表格",
        input_paths=(),
        input_filenames=(),
        workdir=tmp_path,
    )
    artifact = report.artifacts[0]
    assert artifact.path.parent == tmp_path
    # 必须能过 graph 的 OOXML 校验（role 是 security.py 里的必填关键字参数）
    validate_office_artifact(artifact.path, role="eval 产物")


async def test_recording_agent_records_then_delegates(tmp_path):
    inner = FakeExecutionAgent(output_fixture=FIXTURE)
    recorder = RecordingExecutionAgent(inner)
    report = await recorder.run(
        instruction="改标题",
        input_paths=(),
        input_filenames=("a.docx",),
        workdir=tmp_path,
    )
    assert recorder.calls == [{"instruction": "改标题", "input_filenames": ("a.docx",)}]
    assert report.summary
