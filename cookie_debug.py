"""临时 Cookie 审查日志；仅在 BILI2MD_DEBUG_COOKIE=1 时启用，不记录凭证。"""
from contextvars import ContextVar
import os
import sys


_checks = ContextVar("cookie_debug_checks", default=0)


def reset_cookie_debug():
    """每次 CLI 处理从零计数；单独调用模块时也可显式重置。"""
    _checks.set(0)


def cookie_check(function, kind, action):
    """统计即将实际执行的检查；保持返回值、异常和调用次数不变。"""
    if os.environ.get("BILI2MD_DEBUG_COOKIE") == "1":
        _checks.set(_checks.get() + 1)
        print(f"[cookie-debug] event=check checks={_checks.get()} function={function} type={kind}", file=sys.stderr)
    return action()


def cookie_operation(function, kind):
    """正常配置读取、传输筛选或文件复制，不增加校验计数。"""
    if os.environ.get("BILI2MD_DEBUG_COOKIE") == "1":
        print(f"[cookie-debug] event=operation checks={_checks.get()} function={function} type={kind}", file=sys.stderr)
