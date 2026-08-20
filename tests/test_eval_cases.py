from pathlib import Path

import pytest

from walkie_dokie.evals.cases import load_cases


def _write(tmp_path: Path, name: str, text: str) -> Path:
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir(exist_ok=True)
    (cases_dir / name).write_text(text, encoding="utf-8")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "simple.docx").write_bytes(b"stub")
    return cases_dir


def test_load_cases_parses_turns_and_final(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: 方法咨询直接回复
  turns:
    - user: "Word里怎么调行距？"
      expect: {action: reply}
- id: intent-002
  description: 文件任务确认后执行
  turns:
    - user: "转成表格"
      files: [simple.docx]
      expect: {action: propose_task, intent: document_task}
    - user: "是"
      expect: {executed: true}
  final:
    reply_must_not_contain: ["Claude"]
""",
    )
    cases = load_cases(cases_dir, tmp_path / "fixtures")
    assert cases[0].category == "intent_routing"
    assert cases[0].turns[0].expect.action == "reply"
    assert cases[1].turns[0].files == ("simple.docx",)
    assert cases[1].final.reply_must_not_contain == ("Claude",)


def test_blacklist_is_appended_to_every_case(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: x
  turns:
    - user: "hi"
      expect: {action: reply}
""",
    )
    cases = load_cases(
        cases_dir, tmp_path / "fixtures", extra_reply_blacklist=("someone@example.com",)
    )
    assert "someone@example.com" in cases[0].final.reply_must_not_contain


def test_case_without_any_assertion_is_rejected(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: 空样本假绿
  turns:
    - user: "hi"
""",
    )
    with pytest.raises(ValueError, match="intent-001"):
        load_cases(cases_dir, tmp_path / "fixtures")


def test_intent_expect_requires_propose_task(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: intent 只在 interrupt 时可观测
  turns:
    - user: "hi"
      expect: {action: reply, intent: chat}
""",
    )
    with pytest.raises(ValueError, match="intent"):
        load_cases(cases_dir, tmp_path / "fixtures")


def test_missing_fixture_and_duplicate_id_are_rejected(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: x
  turns:
    - user: "hi"
      files: [nope.docx]
      expect: {action: reply}
""",
    )
    with pytest.raises(ValueError, match="nope.docx"):
        load_cases(cases_dir, tmp_path / "fixtures")
    _write(
        tmp_path,
        "memory_boundary.yaml",
        """
- id: intent-001
  description: 与另一文件撞 id
  turns:
    - user: "hi"
      expect: {action: reply}
""",
    )
    (tmp_path / "cases" / "intent_routing.yaml").write_text(
        """
- id: intent-001
  description: x
  turns:
    - user: "hi"
      expect: {action: reply}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_cases(tmp_path / "cases", tmp_path / "fixtures")


def test_null_expect_is_rejected_with_case_id(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: expect 键写了但值为空
  turns:
    - user: "hi"
      expect:
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_cases(cases_dir, tmp_path / "fixtures")
    assert "intent-001" in str(excinfo.value)
    assert "expect" in str(excinfo.value)


def test_null_final_is_rejected_with_case_id(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: final 键写了但值为空
  turns:
    - user: "hi"
      expect: {action: reply}
  final:
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_cases(cases_dir, tmp_path / "fixtures")
    assert "intent-001" in str(excinfo.value)
    assert "final" in str(excinfo.value)


def test_turn_without_user_is_rejected_with_case_id(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: 某轮漏了 user
  turns:
    - expect: {action: reply}
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_cases(cases_dir, tmp_path / "fixtures")
    assert "intent-001" in str(excinfo.value)
    assert "user" in str(excinfo.value)


def test_invalid_intent_value_is_rejected(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: intent 拼错
  turns:
    - user: "转成表格"
      files: [simple.docx]
      expect: {action: propose_task, intent: chatt}
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_cases(cases_dir, tmp_path / "fixtures")
    assert "intent-001" in str(excinfo.value)
    assert "chatt" in str(excinfo.value)


def test_non_bool_executed_is_rejected(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: executed 写成字符串
  turns:
    - user: "是"
      expect: {executed: "true"}
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_cases(cases_dir, tmp_path / "fixtures")
    assert "intent-001" in str(excinfo.value)
    assert "executed" in str(excinfo.value)


def test_bool_executed_is_accepted(tmp_path):
    cases_dir = _write(
        tmp_path,
        "intent_routing.yaml",
        """
- id: intent-001
  description: executed 正常布尔值
  turns:
    - user: "是"
      expect: {executed: true}
    - user: "不用了"
      expect: {executed: false}
""",
    )
    cases = load_cases(cases_dir, tmp_path / "fixtures")
    assert cases[0].turns[0].expect.executed is True
    assert cases[0].turns[1].expect.executed is False
