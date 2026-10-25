"""在字幕工具的 Python 环境中检查同一份凭证，不输出 Cookie。"""
import asyncio
import json
import sys


async def main():
    from bilibili_subtitle_fetch import server

    if len(sys.argv) > 1:
        server.CREDENTIAL_MANAGER.set_config_path(sys.argv[1])
    credential = await server.get_runtime_credential()
    async with server.create_httpx_client(cookies=credential.get_cookies()) as client:
        response = await client.get("https://api.bilibili.com/x/web-interface/nav")
        response.raise_for_status()
        payload = response.json()
    if payload.get("code") == -101:
        print(json.dumps({"logged_in": False}))
        return
    if payload.get("code") != 0:
        raise RuntimeError(f"登录检查 API 错误，code={payload.get('code')}")
    if payload.get("data", {}).get("isLogin") is not True:
        print(json.dumps({"logged_in": False}))
        return
    print(json.dumps({"logged_in": True}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # 不打印请求地址、响应正文或凭证。
        print(f"登录检查失败：{type(exc).__name__}：" +
              (str(exc) if isinstance(exc, RuntimeError) else "网络、配置或依赖异常"), file=sys.stderr)
        sys.exit(1)
