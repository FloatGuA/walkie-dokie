import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """全项目统一的日志初始化，入口脚本启动时调一次。

    顺带修正 Windows 控制台默认编码（GBK 等）导致中文日志乱码的问题——
    stdout/stderr 显式转成 UTF-8 再挂 handler。
    """
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
