"""观测台的 HTTP 层：把 ``admin.data`` 的六个读取函数暴露成七个只读端点。

全只读，没有任何 POST/PUT/DELETE——这个面板只看被观测的 bot，绝不改它的数据。
路由只做三件事：读模块常量拿路径、调 data 层、把返回的 dict 原样交出去。
刻意不重新包装字段：页面拿到的形状就是 data 层的形状，多一层映射就多一处会
悄悄对不上的地方。

六个路径常量是模块级的，处理函数在请求时才查全局名，所以测试 monkeypatch 这些
常量即可把整台观测台指到 tmp。有归属的路径一律从属主模块拿（turn_log /
model_call_log / memory），绝不在这里抄一份字面量：抄的那份在属主改路径的那天
会静默指向一个空文件，看板上就是"最近没有任何回合"。
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from walkie_dokie import model_call_log, turn_log
from walkie_dokie.admin.data import (
    list_eval_reports,
    list_sessions,
    read_costs,
    read_eval_report,
    read_memory,
    read_turns,
)
from walkie_dokie.main_agent import memory as memory_module

TURNS_PATH = turn_log.TURN_LOG_PATH
MODEL_CALLS_PATH = model_call_log.MODEL_CALL_LOG_PATH
MEMORY_DIR = memory_module.MEMORY_DIR

# checkpoint 库与 eval 报告目录没有属主常量（一个在 run_mvp.py 里，导进来会
# 拖进整套 SDK/飞书依赖；另一个是 write_report 的默认参数且是相对路径），
# 所以从 MEMORY_DIR 反推 var/ 根，至少不重复推导目录层级。
_VAR_ROOT = MEMORY_DIR.parent
CHECKPOINT_DB = _VAR_ROOT / "checkpoints-v2.db"
EVALS_DIR = _VAR_ROOT / "evals"

INDEX_HTML_PATH = Path(__file__).parent / "index.html"


def create_app() -> FastAPI:
    """组装只读观测台。工厂而不是模块级单例：测试可以按需拿一份干净的 app。"""
    app = FastAPI(title="walkie-dokie 观测台", docs_url=None, redoc_url=None)

    # 只绑 127.0.0.1 挡不住 DNS rebinding：恶意页面把自己的域名解析到 127.0.0.1，
    # 用户浏览器就会带着那个域名的 Host 头请求这个无鉴权面板，把回合日志和用户
    # 档案读走。校验 Host 头是这一类攻击的标准解，starlette 自带，零新依赖。
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]
    )

    @app.get("/")
    def index() -> FileResponse:
        # 先自己判存在：直接交给 FileResponse 的话，文件缺失要到发响应时才炸成
        # RuntimeError（500），而"前端还没构建"是 404 语义。
        if not INDEX_HTML_PATH.is_file():
            raise HTTPException(status_code=404, detail="index.html 不存在")
        return FileResponse(INDEX_HTML_PATH)

    @app.get("/api/sessions")
    def sessions() -> dict:
        return list_sessions(TURNS_PATH, MEMORY_DIR, CHECKPOINT_DB, MODEL_CALLS_PATH)

    @app.get("/api/turns")
    def turns(limit: int = 50, user: str | None = None) -> dict:
        return read_turns(TURNS_PATH, limit=limit, user=user)

    @app.get("/api/costs")
    def costs(days: int = 7) -> dict:
        return read_costs(MODEL_CALLS_PATH, days=days)

    @app.get("/api/memory")
    def memory() -> dict:
        return read_memory(MEMORY_DIR, CHECKPOINT_DB)

    @app.get("/api/evals")
    def evals() -> dict:
        return list_eval_reports(EVALS_DIR)

    @app.get("/api/evals/{name}")
    def eval_report(name: str) -> dict:
        try:
            return read_eval_report(EVALS_DIR, name)
        # JSONDecodeError 必须排在 ValueError 前面——它是 ValueError 的子类，顺序
        # 反过来会把"报告文件写坏了"误报成"这个名字不合法"，把唯一的线索抹掉。
        # 坏文件是服务端的数据完整性问题，不是客户端请求错误，所以是 500 而不是
        # 4xx；detail 带上文件名，人才知道该去删哪一份。
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=500, detail=f"eval 报告不是合法 JSON: {name} ({exc})"
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
