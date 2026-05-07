"""统一日志配置。

KISS 原则：使用标准库 logging，不引入额外依赖；
后续如果需要结构化日志，再切到 structlog/loguru。
"""

import logging
import sys

_INITIALIZED = False


def setup_logging(level: str = "INFO") -> None:
    """初始化全局日志格式。

    幂等：多次调用只生效一次，避免重复挂载 handler 导致日志重复输出。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            # 包含 trace_id 占位的精简格式，便于 ELK/Loki 解析
            fmt="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(level.upper())
    # 清掉 uvicorn / 其它库默认 handler，避免重复输出
    root.handlers = [handler]

    # uvicorn 自身日志保留，但格式与业务一致
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """返回带模块名的 logger。"""
    return logging.getLogger(name)
