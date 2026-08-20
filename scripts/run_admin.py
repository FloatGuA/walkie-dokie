"""本机只读观测台的启动入口。

用法（从仓库根运行）：
    python3 -m scripts.run_admin                # http://127.0.0.1:8788
    python3 -m scripts.run_admin --port 9000    # 换端口

依赖在 ``[admin]`` extra 里：``pip install -e '.[admin]'``。Ctrl+C 停止。

host 写死 127.0.0.1，不给命令行开口子：面板把回合日志、用户档案、会话摘要全部
明文摊开，一个 ``--host 0.0.0.0`` 就等于把同网段所有人变成管理员，而它没有任何
鉴权。要远程看就自己开 SSH 端口转发，别改这里。
"""

import argparse

import uvicorn

from walkie_dokie.admin.app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="walkie-dokie 只读观测台")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（默认 8788）")
    args = parser.parse_args()

    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
