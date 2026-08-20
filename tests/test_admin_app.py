"""观测台 HTTP 层：只读端点、错误映射、静态首页。

fastapi 是 ``[admin]`` extra，不装的环境直接跳过整个文件——bot 本体不依赖它，
不该因为没装观测台就让全量测试变红。
"""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from walkie_dokie.admin import app as app_module  # noqa: E402
from walkie_dokie.admin.data import (  # noqa: E402
    list_eval_reports,
    read_costs,
    read_memory,
    read_turns,
)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """把六个模块常量指到 tmp，测试永远不碰真实 var/。"""
    fixture = SimpleNamespace(
        turns=tmp_path / "logs" / "turns.jsonl",
        model_calls=tmp_path / "logs" / "model_calls.jsonl",
        memory=tmp_path / "memory",
        checkpoint=tmp_path / "checkpoints-v2.db",
        evals=tmp_path / "evals",
        index=tmp_path / "index.html",
    )
    monkeypatch.setattr(app_module, "TURNS_PATH", fixture.turns)
    monkeypatch.setattr(app_module, "MODEL_CALLS_PATH", fixture.model_calls)
    monkeypatch.setattr(app_module, "MEMORY_DIR", fixture.memory)
    monkeypatch.setattr(app_module, "CHECKPOINT_DB", fixture.checkpoint)
    monkeypatch.setattr(app_module, "EVALS_DIR", fixture.evals)
    monkeypatch.setattr(app_module, "INDEX_HTML_PATH", fixture.index)
    return fixture


@pytest.fixture
def client(paths):
    return TestClient(app_module.create_app())


def test_root_serves_index_html(client, paths):
    paths.index.write_text("<!doctype html><title>观测台</title>", encoding="utf-8")
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "观测台" in response.text


def test_root_404_when_index_missing(client):
    # index.html 是 Task 5 的交付物；没有它时首页 404，而不是 500 或空白页。
    assert client.get("/").status_code == 404


def test_turns_endpoint_passes_through_query_params(client, paths):
    _write_jsonl(
        paths.turns,
        [
            {"timestamp": "t1", "user_id": "u1", "output_text": "a"},
            {"timestamp": "t2", "user_id": "u2", "output_text": "b"},
            {"timestamp": "t3", "user_id": "u1", "output_text": "c"},
        ],
    )
    response = client.get("/api/turns", params={"limit": 2, "user": "u1"})
    assert response.status_code == 200
    # 薄透传：HTTP 层不重新包装字段，响应就是 data 层返回的那个 dict。
    assert response.json() == read_turns(paths.turns, limit=2, user="u1")
    assert [t["timestamp"] for t in response.json()["turns"]] == ["t3", "t1"]


def test_turns_endpoint_defaults_and_empty_state(client):
    response = client.get("/api/turns")
    assert response.status_code == 200
    assert response.json() == {"turns": [], "skipped_lines": 0}


def test_costs_endpoint_passes_through_days(client, paths):
    from datetime import datetime

    today = datetime.now().isoformat(timespec="seconds")
    _write_jsonl(
        paths.model_calls,
        [
            {
                "timestamp": today,
                "provider": "deepseek",
                "model": "deepseek-chat",
                "purpose": "decide",
                "input_tokens": 100,
                "output_tokens": 20,
                "user_id": "u1",
            }
        ],
    )
    response = client.get("/api/costs", params={"days": 3})
    assert response.status_code == 200
    assert response.json() == read_costs(paths.model_calls, days=3)
    assert response.json()["aggregate"]["totals"]["calls"] == 1


def test_costs_endpoint_empty_state_keeps_disclaimer(client, paths):
    response = client.get("/api/costs")
    assert response.status_code == 200
    body = response.json()
    assert body == read_costs(paths.model_calls, days=7)
    assert body["skipped_lines"] == 0
    assert body["disclaimer"]


def test_memory_endpoint_passes_through(client, paths):
    paths.memory.mkdir(parents=True)
    (paths.memory / "v2_aaa_bbb.json").write_text(
        json.dumps({"name": "张三", "department": "行政"}, ensure_ascii=False),
        encoding="utf-8",
    )
    response = client.get("/api/memory")
    assert response.status_code == 200
    assert response.json() == read_memory(paths.memory, paths.checkpoint)
    assert response.json()["users"][0]["profile"]["name"] == "张三"


def test_memory_endpoint_empty_state(client):
    response = client.get("/api/memory")
    assert response.status_code == 200
    assert response.json() == {"users": [], "checkpoint_error": None}


def _write_report(evals_dir, name, payload):
    evals_dir.mkdir(parents=True, exist_ok=True)
    path = evals_dir / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_evals_list_endpoint_passes_through(client, paths):
    _write_report(
        paths.evals,
        "20260820T101010Z.json",
        {"status": "PASSED", "mode": "fake", "summary": {"passed": 3}, "git_commit": "abc"},
    )
    _write_report(
        paths.evals,
        "20260821T101010Z.json",
        {"status": "FAILED", "mode": "real", "summary": {"passed": 1}, "git_commit": "def"},
    )
    response = client.get("/api/evals")
    assert response.status_code == 200
    assert response.json() == list_eval_reports(paths.evals)
    assert [r["name"] for r in response.json()["reports"]] == [
        "20260821T101010Z.json",
        "20260820T101010Z.json",
    ]


def test_evals_list_endpoint_empty_state(client):
    response = client.get("/api/evals")
    assert response.status_code == 200
    assert response.json() == {"reports": [], "skipped_files": 0}


def test_eval_report_endpoint_returns_full_report(client, paths):
    payload = {"status": "PASSED", "case_results": [{"case_id": "c1", "passed": True}]}
    _write_report(paths.evals, "20260821T101010Z.json", payload)
    response = client.get("/api/evals/20260821T101010Z.json")
    assert response.status_code == 200
    assert response.json() == payload


def test_eval_report_endpoint_404_when_missing(client, paths):
    paths.evals.mkdir(parents=True)
    assert client.get("/api/evals/20260821T101010Z.json").status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "20260821T101010Z.json.bak",
        "20260821T101010Z.json%0a",  # fullmatch 必须挡住 $ 对结尾换行的宽容
        "../secret.json",
        "..%2Fsecret.json",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    ],
)
def test_eval_report_endpoint_404_on_illegal_name(client, paths, name):
    _write_report(paths.evals, "20260821T101010Z.json", {"status": "PASSED"})
    (paths.evals.parent / "secret.json").write_text("{}", encoding="utf-8")
    assert client.get(f"/api/evals/{name}").status_code == 404


def test_eval_report_endpoint_bad_json_is_500_not_404(client, paths):
    """坏报告必须报 500，绝不能被当成"非法名"报 404。

    ``json.JSONDecodeError`` 是 ``ValueError`` 的子类：两个 except 顺序写反的话，
    一份写坏的报告会被报成"这个名字不合法"，把"文件坏了"这条线索直接抹掉。
    """
    _write_report(paths.evals, "20260821T101010Z.json", "{不是 JSON")
    response = client.get("/api/evals/20260821T101010Z.json")
    assert response.status_code == 500
    assert "20260821T101010Z.json" in response.json()["detail"]


def test_app_exposes_no_write_routes():
    """全只读。任何 POST/PUT/DELETE 都是设计事故，钉死在测试里。"""
    methods = set()
    for route in app_module.create_app().routes:
        methods |= set(getattr(route, "methods", None) or set())
    assert methods <= {"GET", "HEAD"}
