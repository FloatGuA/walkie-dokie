import json
from datetime import datetime
from pathlib import Path

from walkie_dokie.admin.data import read_costs, read_turns


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)) + "\n")


def test_read_turns_newest_first_with_limit_and_filter(tmp_path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [
        {"timestamp": "t1", "user_id": "u1", "output_text": "a"},
        {"timestamp": "t2", "user_id": "u2", "output_text": "b"},
        {"timestamp": "t3", "user_id": "u1", "output_text": "c"},
    ])
    result = read_turns(path, limit=2)
    assert [t["timestamp"] for t in result["turns"]] == ["t3", "t2"]
    filtered = read_turns(path, limit=10, user="u1")
    assert [t["timestamp"] for t in filtered["turns"]] == ["t3", "t1"]
    assert result["skipped_lines"] == 0


def test_read_turns_skips_bad_lines_and_reports_count(tmp_path):
    path = tmp_path / "turns.jsonl"
    _write_jsonl(path, [{"timestamp": "t1", "user_id": "u1"}, "不是 JSON{", {"timestamp": "t2", "user_id": "u1"}])
    result = read_turns(path)
    assert len(result["turns"]) == 2
    assert result["skipped_lines"] == 1


def test_read_turns_missing_file_is_empty_state(tmp_path):
    result = read_turns(tmp_path / "absent.jsonl")
    assert result == {"turns": [], "skipped_lines": 0}


def test_read_costs_reuses_aggregate_and_reports_disclaimer(tmp_path):
    path = tmp_path / "model_calls.jsonl"
    # read_costs 不暴露 now 注入口，窗口按真实时钟算——时间戳必须是"今天"，
    # 否则这个测试会在写死日期离开 7 天窗口后开始失败。
    _write_jsonl(path, [
        {"timestamp": datetime.now().isoformat(), "provider": "deepseek", "model": "deepseek-chat",
         "purpose": "decide", "platform": "test", "user_id": "u1",
         "prompt_tokens": 100, "completion_tokens": 20, "duration_ms": 500},
    ])
    result = read_costs(path, days=7)
    assert result["aggregate"]["totals"]["calls"] == 1
    assert "上界" in result["disclaimer"]
    assert read_costs(tmp_path / "absent.jsonl")["aggregate"]["totals"]["calls"] == 0
