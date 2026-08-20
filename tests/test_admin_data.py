import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from walkie_dokie.admin.data import (
    list_eval_reports,
    read_costs,
    read_eval_report,
    read_memory,
    read_turns,
)
from walkie_dokie.agents.base import ExecutionAgent, ExecutionReport
from walkie_dokie.main_agent.base import MainAgent, MainAgentDecision
from walkie_dokie.main_agent.memory import JsonMemoryRepository
from walkie_dokie.orchestrator import build_graph


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


# --- read_memory ---------------------------------------------------------
# checkpoint 是 langgraph 写的真实 sqlite，不手搓 BLOB：手搓的 payload 只能证明
# 我们自己的反序列化跟自己的序列化对得上，证明不了能读懂 bot 真正写下的东西。


class _ReplyOnlyMainAgent(MainAgent):
    """只回聊天、不提任务的假主 Agent（照 tests/test_graph.py 的 Fake 模式）。"""

    async def decide(self, context):
        return MainAgentDecision(intent="chat", action="reply", user_message="知道了")

    async def finalize(self, context):
        raise AssertionError("本测试不应触发 finalize")

    async def judge_confirmation(self, context):
        raise AssertionError("本测试不应触发确认判定")


class _UnusedExecutionAgent(ExecutionAgent):
    async def run(self, instruction, input_paths, input_filenames, workdir):
        raise AssertionError("本测试不应触发执行 Agent")


async def _seed_checkpoint(
    db_path: Path,
    memory_dir: Path,
    *,
    platform: str,
    user_id: str,
    summary: list[dict],
    pending: list[dict],
):
    """跑一轮真实 graph，把预置的摘要/待压缩状态落进 tmp checkpoint db。"""
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = build_graph(
            _ReplyOnlyMainAgent(),
            _UnusedExecutionAgent(),
            JsonMemoryRepository(memory_dir),
            checkpointer=saver,
        )
        await graph.ainvoke(
            {
                "platform": platform,
                "user_id": user_id,
                "new_text": "在吗",
                "new_file": None,
                "conversation_summary": summary,
                "pending_compaction": pending,
            },
            config={"configurable": {"thread_id": f"{platform}:{user_id}"}},
        )


def _write_profile(memory_dir: Path, platform: str, user_id: str, facts: dict):
    """走真实仓库的写入路径，保证文件名算法与被测的读取路径同源。"""
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = JsonMemoryRepository(memory_dir)._path(platform, user_id)
    path.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
    return path


def _find_user(result, platform, user_id):
    matches = [
        u
        for u in result["users"]
        if u["platform"] == platform and u["user_id"] == user_id
    ]
    assert len(matches) == 1, f"{platform}:{user_id} 未唯一出现于 {result['users']}"
    return matches[0]


def _find_profile_only_user(result, memory_dir, platform, user_id):
    """没有 checkpoint 可以对上的档案：sha256 逆不回来，只能按文件名找。"""
    file_name = JsonMemoryRepository(memory_dir)._path(platform, user_id).name
    return _find_user(result, "", file_name)


async def test_read_memory_merges_profiles_and_checkpoint_summary(tmp_path):
    memory_dir = tmp_path / "memory"
    db = tmp_path / "cp.db"
    _write_profile(memory_dir, "feishu", "ou_alice", {"name": "浮瓜", "department": "研发"})
    await _seed_checkpoint(
        db,
        memory_dir,
        platform="feishu",
        user_id="ou_alice",
        summary=[{"fact": "孙女叫小雨", "evidence": ["我孙女小雨"]}],
        pending=[
            {"role": "user", "content": "旧消息0"},
            {"role": "assistant", "content": "旧消息1"},
        ],
    )

    result = read_memory(memory_dir, db)

    assert result["checkpoint_error"] is None
    user = _find_user(result, "feishu", "ou_alice")
    assert user["profile"] == {"name": "浮瓜", "department": "研发"}
    assert user["summary"] == [{"fact": "孙女叫小雨", "evidence": ["我孙女小雨"]}]
    assert user["pending_compaction"] == 2


async def test_read_memory_shows_checkpoint_user_without_profile(tmp_path):
    """并集展示的另一半：有会话没档案的用户也要出现，档案为空而不是被丢掉。"""
    memory_dir = tmp_path / "memory"
    db = tmp_path / "cp.db"
    _write_profile(memory_dir, "feishu", "ou_alice", {"name": "浮瓜"})
    await _seed_checkpoint(
        db, memory_dir, platform="feishu", user_id="ou_bob", summary=[], pending=[]
    )

    result = read_memory(memory_dir, db)

    assert result["checkpoint_error"] is None
    bob = _find_user(result, "feishu", "ou_bob")
    assert bob["profile"] == {}
    assert bob["summary"] == []
    assert bob["pending_compaction"] == 0
    # 另一半：alice 有档案但这库里没有她的 thread，同样必须在列表里
    alice = _find_profile_only_user(result, memory_dir, "feishu", "ou_alice")
    assert alice["profile"] == {"name": "浮瓜"}
    assert alice["summary"] == []


def test_read_memory_without_db_returns_profiles_only(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_profile(memory_dir, "feishu", "ou_alice", {"name": "浮瓜"})

    result = read_memory(memory_dir, tmp_path / "absent.db")

    assert result["checkpoint_error"] is None
    assert len(result["users"]) == 1
    user = result["users"][0]
    assert user["profile"] == {"name": "浮瓜"}
    assert user["summary"] == []
    assert user["pending_compaction"] == 0
    # 文件名里的 platform/user_id 是 sha256，不可逆。没有 checkpoint 可以对上时
    # 只能原样展示文件名，绝不编一个看起来像 ID 的东西出来。
    assert user["platform"] == ""
    assert user["user_id"] == JsonMemoryRepository(memory_dir)._path("feishu", "ou_alice").name


def test_read_memory_without_memory_dir_is_empty_state(tmp_path):
    assert read_memory(tmp_path / "absent-memory", tmp_path / "absent.db") == {
        "users": [],
        "checkpoint_error": None,
    }


def test_read_memory_db_without_checkpoints_table_is_not_an_error(tmp_path):
    """空态不是错误：db 刚建好还没跑过图时，观测台该显示"没有会话"而不是报错。"""
    memory_dir = tmp_path / "memory"
    _write_profile(memory_dir, "feishu", "ou_alice", {"name": "浮瓜"})
    db = tmp_path / "fresh.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE unrelated (x TEXT)")
    connection.commit()
    connection.close()

    result = read_memory(memory_dir, db)

    assert result["checkpoint_error"] is None
    alice = _find_profile_only_user(result, memory_dir, "feishu", "ou_alice")
    assert alice["profile"] == {"name": "浮瓜"}
    assert alice["summary"] == []


def test_read_memory_db_error_is_reported_not_raised(tmp_path):
    memory_dir = tmp_path / "memory"
    _write_profile(memory_dir, "feishu", "ou_alice", {"name": "浮瓜"})
    db = tmp_path / "garbage.db"
    db.write_text("这不是 sqlite 文件", encoding="utf-8")

    result = read_memory(memory_dir, db)

    assert result["checkpoint_error"]
    profile_only = _find_profile_only_user(result, memory_dir, "feishu", "ou_alice")
    assert profile_only["profile"] == {"name": "浮瓜"}


async def test_read_memory_reads_non_wal_db_without_writing(tmp_path):
    """备份/拷贝出来的 db 常是 delete journal 模式。

    langgraph 的 SqliteSaver.setup() 会执行 PRAGMA journal_mode=WAL + CREATE TABLE，
    在只读连接上直接抛 "attempt to write a readonly database"。这个测试钉住"读取
    路径绝不触发 setup"，别人删掉那行绕过时会立刻红。
    """
    memory_dir = tmp_path / "memory"
    db = tmp_path / "cp.db"
    await _seed_checkpoint(
        db,
        memory_dir,
        platform="feishu",
        user_id="ou_alice",
        summary=[{"fact": "喜欢喝茶", "evidence": ["我爱喝茶"]}],
        pending=[],
    )
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.close()
    assert not (tmp_path / "cp.db-wal").exists()
    before = db.stat().st_mtime_ns

    result = read_memory(memory_dir, db)

    assert result["checkpoint_error"] is None
    assert _find_user(result, "feishu", "ou_alice")["summary"] == [
        {"fact": "喜欢喝茶", "evidence": ["我爱喝茶"]}
    ]
    assert db.stat().st_mtime_ns == before
    assert not (tmp_path / "cp.db-wal").exists()


def test_read_memory_keeps_unreadable_profile_as_empty(tmp_path):
    """档案文件坏了也要让这个用户出现在列表里——静默消失比显示空档案更糟。"""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "v2_aaa_bbb.json").write_text("{坏掉的 JSON", encoding="utf-8")

    result = read_memory(memory_dir, tmp_path / "absent.db")

    assert result["checkpoint_error"] is None
    assert len(result["users"]) == 1
    assert result["users"][0]["profile"] == {}
    assert result["users"][0]["user_id"] == "v2_aaa_bbb.json"


def _write_eval_report(evals_dir: Path, name: str, **fields) -> Path:
    evals_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "regression",
        "status": "PASSED",
        "git_commit": "abc1234",
        "summary": {"total": 1, "passed": 1, "failed": 0, "judge_clarity_avg": 3.5},
        "case_results": [{"case_id": "c1", "passed": True}],
    }
    report.update(fields)
    path = evals_dir / name
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def test_list_eval_reports_newest_first(tmp_path):
    evals_dir = tmp_path / "evals"
    _write_eval_report(evals_dir, "20260820T105503Z.json", status="FAILED_INFRA")
    _write_eval_report(
        evals_dir,
        "20260820T185856Z.json",
        mode="real-execution",
        git_commit="01ae0fc",
        summary={"total": 23, "passed": 23, "failed": 0, "judge_clarity_avg": 3.4},
    )

    result = list_eval_reports(evals_dir)

    assert [r["name"] for r in result["reports"]] == [
        "20260820T185856Z.json",
        "20260820T105503Z.json",
    ]
    newest = result["reports"][0]
    assert newest["status"] == "PASSED"
    assert newest["mode"] == "real-execution"
    assert newest["git_commit"] == "01ae0fc"
    assert newest["summary"] == {
        "total": 23,
        "passed": 23,
        "failed": 0,
        "judge_clarity_avg": 3.4,
    }
    assert result["reports"][1]["status"] == "FAILED_INFRA"
    assert result["skipped_files"] == 0
    # 列表页只放摘要字段，逐 case 的明细留给 read_eval_report——否则一页就要
    # 把几十份报告的全文都塞进来。
    assert "case_results" not in newest


def test_list_eval_reports_empty_dir(tmp_path):
    empty = tmp_path / "evals"
    empty.mkdir()
    assert list_eval_reports(empty) == {"reports": [], "skipped_files": 0}
    assert list_eval_reports(tmp_path / "absent") == {"reports": [], "skipped_files": 0}


def test_list_eval_reports_skips_broken_and_foreign_files(tmp_path):
    evals_dir = tmp_path / "evals"
    _write_eval_report(evals_dir, "20260820T111441Z.json")
    (evals_dir / "20260820T150924Z.json").write_text("{半个 JSON", encoding="utf-8")
    # 名字不合规的一律不算数，也不该计入 skipped_files：它们本来就不是报告。
    (evals_dir / "notes.txt").write_text("随手记", encoding="utf-8")
    (evals_dir / "20260820T161933Z.json.bak").write_text("{}", encoding="utf-8")

    result = list_eval_reports(evals_dir)

    assert [r["name"] for r in result["reports"]] == ["20260820T111441Z.json"]
    assert result["skipped_files"] == 1


def test_list_eval_reports_tolerates_missing_fields(tmp_path):
    evals_dir = tmp_path / "evals"
    evals_dir.mkdir(parents=True)
    (evals_dir / "20260820T111441Z.json").write_text("{}", encoding="utf-8")

    report = list_eval_reports(evals_dir)["reports"][0]

    assert report["name"] == "20260820T111441Z.json"
    assert report["status"] is None
    assert report["mode"] is None
    assert report["git_commit"] is None
    assert report["summary"] == {}


def test_read_eval_report_returns_full_document(tmp_path):
    evals_dir = tmp_path / "evals"
    _write_eval_report(evals_dir, "20260820T185856Z.json", error=None)

    result = read_eval_report(evals_dir, "20260820T185856Z.json")

    assert result["status"] == "PASSED"
    assert result["case_results"] == [{"case_id": "c1", "passed": True}]


def test_read_eval_report_rejects_path_traversal(tmp_path):
    """name 直接来自 URL，只有白名单正则挡在中间——这条防线塌了就是任意文件读。"""
    evals_dir = tmp_path / "evals"
    evals_dir.mkdir()
    (tmp_path / "secret.json").write_text('{"secret": 1}', encoding="utf-8")

    for bad in [
        "../../etc/passwd",
        "../secret.json",
        "20260820T111441Z.json.bak",
        "20260820T111441Z.json/../../secret.json",
        "/etc/passwd",
        "",
        "20260820T111441Z.json\n",
    ]:
        with pytest.raises(ValueError):
            read_eval_report(evals_dir, bad)


def test_read_eval_report_missing_file(tmp_path):
    evals_dir = tmp_path / "evals"
    evals_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        read_eval_report(evals_dir, "20260820T111441Z.json")
