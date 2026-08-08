import logging
import logging.handlers
import sys
from pathlib import Path

_VAR_ROOT = Path(__file__).parent.parent.parent / "var"
LOG_FILE = _VAR_ROOT / "logs" / "walkie-dokie.log"

# 传输层的帧级细节（websocket ping/pong 之类）价值不大，只吵——真正需要看
# 连通性时，飞书 SDK 自己在 INFO 级别已经打了 connected/disconnected/
# reconnecting，够用了。这里只按 logger 名字调粗，不影响我们自己模块
# （walkie_dokie.*）和其他第三方库的 DEBUG 粒度。
_QUIET_LOGGERS = ["websockets"]


def setup_logging(console_level: int = logging.INFO, file_level: int = logging.DEBUG) -> None:
    """全项目统一的日志初始化，入口脚本启动时调一次。

    顺带修正 Windows 控制台默认编码（GBK 等）导致中文日志乱码的问题——
    stdout/stderr 显式转成 UTF-8 再挂 handler。

    项目早期要能复盘，日志落盘到项目自己的 var/logs/（不是随便一个会话
    临时目录），文件里存 DEBUG 粒度；控制台默认 INFO，别刷屏。等项目过了
    高频调试阶段，把 file_level 调粗一点就行，不用改调用点。
    """
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(fmt)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)
