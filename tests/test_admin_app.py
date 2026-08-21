"""观测台 HTTP 层：只读端点、错误映射、静态首页。

fastapi 是 ``[admin]`` extra，不装的环境直接跳过整个文件——bot 本体不依赖它，
不该因为没装观测台就让全量测试变红。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from walkie_dokie.admin import app as app_module  # noqa: E402
from walkie_dokie.admin.data import (  # noqa: E402
    list_eval_reports,
    list_sessions,
    read_costs,
    read_memory,
    read_turns,
)

# 真实的前端交付物。不从 ``app_module.INDEX_HTML_PATH`` 取：那个常量会被 paths
# fixture 改指到 tmp，下面几条 smoke 断言的恰恰是仓库里那一份。
REAL_INDEX_HTML = Path(app_module.__file__).with_name("index.html")


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
    # base_url 必须是真实的本机 host：TrustedHostMiddleware 只认 127.0.0.1 /
    # localhost，TestClient 默认的 "testserver" 会被挡成 400。
    return TestClient(app_module.create_app(), base_url="http://127.0.0.1")


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
                # 字段名照 ModelCallRecord：写错名字的话 aggregate 会当成"没拿到
                # usage"，calls 照样是 1，断言却什么都没验证。
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "platform": "feishu",
                "user_id": "u1",
            }
        ],
    )
    response = client.get("/api/costs", params={"days": 3})
    assert response.status_code == 200
    assert response.json() == read_costs(paths.model_calls, days=3)
    aggregate = response.json()["aggregate"]
    assert aggregate["totals"]["calls"] == 1
    assert aggregate["totals"]["tokens"] == 120
    assert aggregate["unknown_token_calls"] == 0
    # 成本页的"按用户"小表直接吃这一段，形状要钉住。
    row = aggregate["by_user"][0]
    assert row["owner"] == "feishu:u1"
    assert row["calls"] == 1
    assert row["tokens"] == 120
    assert row["cost_usd"] > 0


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


def test_sessions_endpoint_passes_through(client, paths):
    from datetime import datetime

    _write_jsonl(
        paths.turns,
        [
            {"timestamp": "2026-08-20T09:00:00", "platform": "feishu", "user_id": "ou_alice",
             "record_type": "conversation", "success": True},
            {"timestamp": "2026-08-20T09:30:00", "platform": "feishu", "user_id": "ou_alice",
             "record_type": "conversation", "success": False, "error": "炸了"},
            {"timestamp": "2026-08-20T08:00:00", "platform": "eval", "user_id": "t-x1",
             "record_type": "conversation", "success": True},
        ],
    )
    _write_jsonl(
        paths.model_calls,
        [
            {
                "timestamp": datetime.now().isoformat(),
                "provider": "deepseek",
                "model": "deepseek-chat",
                "purpose": "decide",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "platform": "feishu",
                "user_id": "ou_alice",
            }
        ],
    )
    response = client.get("/api/sessions")
    assert response.status_code == 200
    # 薄透传：HTTP 层不重新包装字段，响应就是 data 层返回的那个 dict。
    assert response.json() == list_sessions(
        paths.turns, paths.memory, paths.checkpoint, paths.model_calls
    )
    sessions = response.json()["sessions"]
    assert [s["user_id"] for s in sessions] == ["ou_alice", "t-x1"]
    assert sessions[0]["platform"] == "feishu"
    assert sessions[0]["turn_count"] == 2
    assert sessions[0]["failed_count"] == 1
    assert sessions[0]["cost_usd"] > 0


def test_sessions_endpoint_empty_state(client):
    response = client.get("/api/sessions")
    assert response.status_code == 200
    assert response.json() == {
        "sessions": [],
        "skipped_lines": 0,
        "checkpoint_error": None,
    }


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


# ------------------------------------------------------- index.html smoke
#
# 这几条只读仓库里那份真实 index.html 的源码。前端没有构建步骤、没有单测框架，
# 字符串断言是唯一能钉住"结构还在"的手段：视图入口、固定 purpose 色映射、零外部
# 资源、以及最起码的转义意识。它们挡不住渲染 bug，但挡得住"某次编辑把整块删了"。
#
# session 式改版把四个平级 tab 换成了"侧栏 + 三个视图"，旧断言逐条换成了等价物：
#   data-tab="turns"  -> data-view="session"（回合改成 session 详情里的对话回放）
#   data-tab="memory" -> 无独立视图，档案/摘要并进 session 信息卡，改由
#                        test_index_html_consumes_every_read_endpoint 钉住
#                        /api/memory 仍被消费
#   data-tab="costs"  -> data-view="costs"
#   data-tab="evals"  -> data-view="evals"
#   '错误'（回合表的错误列）-> 'bubble-error'（气泡下的红色错误行）


def _index_source() -> str:
    return REAL_INDEX_HTML.read_text(encoding="utf-8")


@pytest.mark.parametrize("view", ["session", "costs", "evals"])
def test_index_html_has_all_three_views(view):
    assert f'data-view="{view}"' in _index_source()


def test_index_html_has_the_session_sidebar():
    """侧栏是整个改版的骨架：没有它就退回成了一堆孤立的表。"""
    source = _index_source()
    assert 'class="sidebar"' in source
    assert 'id="session-list"' in source


@pytest.mark.parametrize(
    "endpoint",
    ["/api/sessions", "/api/turns", "/api/memory", "/api/costs", "/api/evals"],
)
def test_index_html_consumes_every_read_endpoint(endpoint):
    """五个只读端点一个都不能在改版里掉队（记忆那一份最容易被顺手删掉）。"""
    assert endpoint in _index_source()


@pytest.mark.parametrize("channel", ["飞书", "评估", "测试"])
def test_index_html_maps_platform_to_chinese_channel(channel):
    """平台标识是英文代号，看板上一律显示中文渠道名。"""
    assert channel in _index_source()


def test_index_html_keeps_the_conversation_timeline():
    """对话回放时间轴是这一版唯一的签名元素，退回裸表格就等于改版没做。"""
    source = _index_source()
    assert 'class="timeline"' in source or "'timeline'" in source
    assert "bubble-user" in source
    assert "bubble-bot" in source


@pytest.mark.parametrize(
    "color", ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
)
def test_index_html_carries_fixed_purpose_palette(color):
    """5 个 purpose 色号照抄报表脚本，两处必须一致，否则两张图没法对着看。"""
    assert color in _index_source()


@pytest.mark.parametrize("scheme", ["http://", "https://"])
def test_index_html_has_no_external_resources(scheme):
    """单文件、零外部资源：没有 CDN、没有 web 字体。观测台要能离线开着看。"""
    assert scheme not in _index_source()


def test_index_html_escapes_api_text():
    """回合输入输出、摘要 evidence 都是用户/模型产出的文本，绝不能裸拼进 DOM。"""
    source = _index_source()
    assert "textContent" in source
    assert "function esc(" in source


@pytest.mark.parametrize(
    "needle",
    [
        # evidence 是 list[str]：数组必须按数组渲染，退回 '原文：' + entry.evidence
        # 会把多条原文用逗号粘成一串、空数组渲染出一个孤零零的"原文："。
        "Array.isArray",
        # 失败回合的原因不能只留一个红点。旧版是回合表的"错误"列，改版后是气泡
        # 下面那行红字，标记随之从 '错误' 换成它的 class。
        "bubble-error",
        "by_user",     # 总成本视图的"按用户"小表，直接吃聚合里现成的这一段
    ],
)
def test_index_html_keeps_review_fixes(needle):
    assert needle in _index_source()


def test_root_serves_the_real_index_html(monkeypatch):
    monkeypatch.setattr(app_module, "INDEX_HTML_PATH", REAL_INDEX_HTML)
    response = TestClient(app_module.create_app(), base_url="http://127.0.0.1").get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "观测台" in response.text


def test_rejects_requests_with_foreign_host_header(paths):
    """只绑 127.0.0.1 挡不住 DNS rebinding：浏览器照样会把请求发到本机端口上。

    Host 头不是本机名的一律 400——这台面板无鉴权且把用户档案全摊开，多这一道
    比什么都便宜。
    """
    client = TestClient(app_module.create_app(), base_url="http://evil.example.com")
    for path in ["/", "/api/turns", "/api/memory"]:
        assert client.get(path).status_code == 400


def test_app_exposes_no_write_routes():
    """全只读。任何 POST/PUT/DELETE 都是设计事故，钉死在测试里。"""
    methods = set()
    for route in app_module.create_app().routes:
        methods |= set(getattr(route, "methods", None) or set())
    assert methods <= {"GET", "HEAD"}
