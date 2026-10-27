"""最小 OpenAI-compatible Chat Completions 总结适配，仅从环境变量读取 Key。"""
from http.client import HTTPException
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ollama_summary import SUMMARY_SYSTEM, SummaryError, summary_prompt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 防止重定向把 Authorization 发送到其他地址。
        return None


def api_config(model: str, base_url: str, api_key_env: str) -> tuple[str, str]:
    if not model or not model.strip():
        raise SummaryError("API 配置错误：请使用 --api-model 指定模型。")
    if not base_url or not base_url.strip():
        raise SummaryError("API 配置错误：请使用 --api-base-url 指定服务根地址（通常包含 /v1）。")
    try:
        parsed = urlparse(base_url)
        valid = (parsed.hostname and not parsed.username and not parsed.password and
                 not parsed.query and not parsed.fragment and
                 not any(c.isspace() or ord(c) < 32 for c in base_url) and
                 (parsed.scheme == "https" or
                  (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})))
        parsed.port  # 同时验证端口格式。
    except ValueError:
        valid = False
    if not valid:
        raise SummaryError("API 地址无效：使用 HTTPS base_url，不包含账号、密码、查询参数或片段；本机模拟可用 HTTP。")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise SummaryError("API 地址应为 base_url，不要包含 /chat/completions。")
    if not api_key_env or not api_key_env.isidentifier():
        raise SummaryError("API 配置错误：--api-key-env 必须是环境变量名称，不是 Key 本身。")
    key = os.environ.get(api_key_env, "").strip()
    if not key:
        raise SummaryError("API 认证未配置：请在指定的环境变量中设置 API Key。")
    if not key.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise SummaryError("API 认证配置无效：环境变量中的 Key 含有非法字符。")
    return base_url.rstrip("/") + "/chat/completions", key


def generate_summary(text: str, model: str, base_url: str, timeout: float,
                     *, api_key_env: str = "BILI2MD_API_KEY") -> str:
    endpoint, key = api_config(model, base_url, api_key_env)
    if not text.strip():
        raise SummaryError("总结失败：字幕文本为空。")
    payload = {"model": model.strip(), "stream": False, "messages": [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": summary_prompt(text)},
    ]}
    try:
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST")
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
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
    except (TimeoutError, urllib.error.URLError, OSError, HTTPException) as exc:
        if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
            raise SummaryError(f"API 请求超时（{timeout:g} 秒），未保存总结。") from None
        raise SummaryError("API 连接失败：请检查网络、TLS 证书、代理及服务地址。") from None
    except (ValueError, UnicodeError):
        raise SummaryError("API 请求失败：响应不是有效 JSON 或请求配置无效。") from None
    if not isinstance(result, dict) or result.get("error"):
        raise SummaryError("API 返回错误或异常响应，未保存总结。")
    try:
        choice = result["choices"][0]
        finish = choice["finish_reason"]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SummaryError("API 响应格式异常：缺少 choices 或总结正文。") from None
    if finish == "length":
        raise SummaryError("API 总结达到生成长度限制，未保存不完整总结。")
    if finish != "stop" or not isinstance(content, str):
        raise SummaryError("API 未返回完整、非空的总结正文。")
    content = content.strip()
    if not content:
        raise SummaryError("API 未返回完整、非空的总结正文。")
    if key in content:
        raise SummaryError("API 返回内容包含认证信息，已拒绝保存。")
    return content
