"""公共异常类型、错误转换与入口退出策略；不依赖业务或 CLI 模块。"""
from enum import Enum
import errno
import socket
import ssl
import json
import subprocess
from http.client import IncompleteRead
import urllib.error

class SubtitleState(str, Enum):
    CHINESE = "chinese"
    OTHER_LANGUAGES = "other_languages"
    NO_SUBTITLES = "no_subtitles"
    ACCESS_DENIED = "access_denied"
    REQUEST_FAILED = "request_failed"


class BilibiliError(Exception):
    pass


class AccessDenied(BilibiliError):
    state = SubtitleState.ACCESS_DENIED


class RequestFailed(BilibiliError):
    state = SubtitleState.REQUEST_FAILED


class ConfigurationError(BilibiliError):
    """启动配置或客户端初始化失败，不属于可重试的业务请求。"""


class InvalidVideoURL(RequestFailed):
    """当前任务的输入错误，不终止调用方进程。"""


class TransientRequestFailed(RequestFailed):
    """仅用于可重试的网络故障。"""


class SubtitleError(Exception):
    """可以直接展示给用户的提取错误。"""


class SummaryError(Exception):
    """总结失败，不将错误响应保存为总结。"""


def bilibili_http_error(exc, label):
    status = exc.code
    exc.close()
    error = (TransientRequestFailed if status in {408, 500, 502, 503, 504} else
             AccessDenied if status in {401, 403} else RequestFailed)
    raise error(f"{label}请求失败：HTTP {status}。") from None

def bilibili_network_error(exc, label):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    transient = (not isinstance(reason, ssl.SSLError) and (
        isinstance(reason, (TimeoutError, ConnectionError, IncompleteRead)) or
        isinstance(reason, socket.gaierror) and reason.errno == socket.EAI_AGAIN or
        isinstance(reason, OSError) and reason.errno in {
            errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE}))
    error = TransientRequestFailed if transient else RequestFailed
    raise error(f"{label}网络请求失败或超时。") from None


def api_http_error(exc):
    status = exc.code
    exc.close()
    hints = {400: "请求参数不被服务支持，请检查模型和接口兼容性",
             401: "认证失败，请检查 API Key 是否有效",
             403: "无访问权限，请检查 Key 和模型权限",
             404: "接口或模型不存在，请检查 base_url 和模型名",
             429: "请求受限或额度不足，请检查服务限额"}
    hint = ("服务返回重定向，请配置最终 base_url" if 300 <= status < 400 else
            hints.get(status, "服务端错误，请稍后重试" if status >= 500 else "请检查服务配置"))
    # 不输出服务端响应、URL 或异常原文，避免其中回显认证信息。
    raise SummaryError(f"API 请求失败（HTTP {status}）：{hint}。") from None

def ollama_http_error(exc, model):
    detail = ""
    try:
        error = json.loads(exc.read(4096))
        if isinstance(error, dict):
            detail = str(error.get("error", ""))[:500]
    except (ValueError, OSError):
        pass
    finally:
        exc.close()
    if exc.code == 404 and "model" in detail.lower() and "not found" in detail.lower():
        raise SummaryError(f"Ollama 模型不存在：{model}。请指定本机已安装的模型。") from exc
    raise SummaryError(f"Ollama 请求失败（HTTP {exc.code}）：{detail or exc.reason}") from exc

def summary_network_error(exc, provider, timeout):
    if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
        raise SummaryError(f"{provider} 请求超时（{timeout:g} 秒），未保存总结。") from None
    message = ("API 连接失败：请检查网络、TLS 证书、代理及服务地址。" if provider == "API" else
               "无法连接本机 Ollama：请确认服务已启动，且 --ollama-url 地址正确。")
    raise SummaryError(message) from None


def external_error(exc, stage, timeout):
    if isinstance(exc, subprocess.TimeoutExpired):
        raise SubtitleError(f"{stage}失败：超过 {timeout:g} 秒。") from exc
    raise SubtitleError(f"{stage}失败：无法启动程序（{exc}）。") from exc


def check_external_result(result, stage):
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        raise SubtitleError(f"{stage}失败（退出码 {result.returncode}）：{detail}")


def cli_error(exc):
    """仅由入口调用；未知程序错误不在可展示错误列表中。"""
    return ("已取消。", 130) if isinstance(exc, KeyboardInterrupt) else (str(exc), 1)


TASK_ERRORS = (SubtitleError, BilibiliError, SummaryError, OSError, EOFError)
