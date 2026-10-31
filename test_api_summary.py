from contextlib import redirect_stderr
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import api_summary
import bili2md
from test_bilibili_info import make_result
from ollama_summary import SummaryError, summarize_file


class APISummaryTests(unittest.TestCase):
    def setUp(self):
        # 随机生成仅用于模拟测试的认证值，不使用用户的真实凭证。
        self.key = secrets.token_urlsafe(24)
        self.env = patch.dict(os.environ, {"BILI2MD_TEST_KEY": self.key})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, content="## 概述\n模拟中文总结", finish="stop"):
        return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}

    def opener(self, payload):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return opener

    def generate(self):
        return api_summary.generate_summary("测试字幕", "selected-model", "https://example.invalid/v1/", 42,
                                            api_key_env="BILI2MD_TEST_KEY")

    def test_request_parameters_and_auth_from_named_environment(self):
        opener = self.opener(self.response())
        with patch("api_summary.urllib.request.build_opener", return_value=opener):
            self.assertIn("模拟中文总结", self.generate())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "selected-model")
        self.assertFalse(payload["stream"])
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertIn("测试字幕", payload["messages"][1]["content"])
        self.assertNotIn(self.key, request.data.decode())
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 42)

    def test_http_failures_never_echo_credentials_or_error_body(self):
        for code in [302, 400, 401, 403, 404, 429, 500, 503]:
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://example.invalid/?key=" + self.key, code, self.key, {},
                io.BytesIO(json.dumps({"error": self.key}).encode()))
            with self.subTest(code=code), patch("api_summary.urllib.request.build_opener", return_value=opener):
                with self.assertRaisesRegex(SummaryError, f"HTTP {code}") as error:
                    self.generate()
                self.assertNotIn(self.key, str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_connection_timeout_and_invalid_json_are_safe(self):
        for failure, expected in [(urllib.error.URLError(self.key), "连接失败"),
                                  (TimeoutError(self.key), "请求超时")]:
            opener = MagicMock()
            opener.open.side_effect = failure
            with patch("api_summary.urllib.request.build_opener", return_value=opener):
                with self.assertRaisesRegex(SummaryError, expected) as error:
                    self.generate()
                self.assertNotIn(self.key, str(error.exception))
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(self.key.encode())
        with patch("api_summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(SummaryError, "JSON") as error:
                self.generate()
            self.assertNotIn(self.key, str(error.exception))

    def test_incomplete_empty_error_or_secret_responses_fail(self):
        for payload in [{"error": {"message": self.key}}, [], {}, {"choices": []},
                        self.response(""), self.response("内容", "length"),
                        self.response("内容", "content_filter"), self.response("内容", None),
                        self.response(self.key)]:
            with self.subTest(kind=type(payload).__name__), \
                 patch("api_summary.urllib.request.build_opener", return_value=self.opener(payload)):
                with self.assertRaises(SummaryError) as error:
                    self.generate()
                self.assertNotIn(self.key, str(error.exception))

    def test_config_validation_before_network(self):
        for url in ["https://user:password@example.invalid/v1", "http://example.invalid/v1",
                    "https://example.invalid/v1?key=anything", "https://example.invalid/#fragment",
                    "https://example.invalid:bad/v1", "https://example.invalid/v1/chat/completions"]:
            with self.subTest(url=url), self.assertRaises(SummaryError):
                api_summary.api_config("model", url, "BILI2MD_TEST_KEY")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SummaryError, "认证未配置"):
                api_summary.api_config("model", "https://example.invalid/v1", "BILI2MD_TEST_KEY")

    def test_missing_credentials_prevents_video_fetch(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["bili2md.py", "https://www.bilibili.com/video/BV1BgNb6oEnk",
                                "--api-model", "model", "--api-base-url", "https://example.invalid/v1"]), \
             patch("bili2md.BilibiliClient.fetch") as fetch, redirect_stderr(io.StringIO()) as log:
            self.assertEqual(bili2md.main(), 1)
        fetch.assert_not_called()
        self.assertIn("认证未配置", log.getvalue())

    def test_provider_options_are_mutually_exclusive(self):
        with patch("sys.argv", ["bili2md.py", "--ollama-model", "local", "--api-model", "remote"]), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(bili2md.main(), 2)

    def test_link_path_reuses_existing_subtitle_and_markdown_flow(self):
        for failure in [False, True]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                source, target = Path(temp) / "subtitle.txt", Path(temp) / "summary.md"
                response = {"error": self.key} if failure else self.response()
                with patch("sys.argv", ["bili2md.py", "https://www.bilibili.com/video/BV1BgNb6oEnk",
                                        "-o", str(source), "--summary-output", str(target),
                                        "--api-model", "mock-model", "--api-base-url", "https://example.invalid/v1",
                                        "--api-key-env", "BILI2MD_TEST_KEY"]), \
                     patch("bili2md.BilibiliClient.fetch", return_value=make_result("模拟已有中文字幕")), \
                     patch("api_summary.urllib.request.build_opener", return_value=self.opener(response)), \
                     redirect_stderr(io.StringIO()) as log:
                    self.assertEqual(bili2md.main(), 1 if failure else 0)
                info = make_result().info
                self.assertEqual(source.read_text(encoding="utf-8"),
                                 bili2md.format_source(info.title, info.desc, "模拟已有中文字幕\n"))
                self.assertEqual(target.exists(), not failure)
                self.assertNotIn(self.key, log.getvalue())

    def test_failure_protects_existing_markdown_and_subtitle(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.txt"
            target = Path(temp) / "summary.md"
            source.write_text("原文", encoding="utf-8")
            target.write_text("原有总结", encoding="utf-8")
            generator = partial(api_summary.generate_summary, api_key_env="BILI2MD_TEST_KEY")
            with patch("api_summary.urllib.request.build_opener", return_value=self.opener({"error": self.key})):
                with self.assertRaises(SummaryError):
                    summarize_file(source, "model", target, "https://example.invalid/v1", generator=generator)
            self.assertEqual(source.read_text(encoding="utf-8"), "原文")
            self.assertEqual(target.read_text(encoding="utf-8"), "原有总结")

    def test_simulated_http_cli_to_markdown_and_redirect_rejection(self):
        requests = []
        result = self.response()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), body))
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/leak")
                    self.end_headers()
                    return
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as temp:
                source, target = Path(temp) / "subtitle.txt", Path(temp) / "summary.md"
                source.write_text("本地模拟字幕", encoding="utf-8")
                with patch("sys.argv", ["bili2md.py", "--input-text", str(source),
                                        "--summary-output", str(target), "--api-model", "mock-model",
                                        "--api-base-url", base + "/v1", "--api-key-env", "BILI2MD_TEST_KEY"]), \
                     patch("bili2md.BilibiliClient.fetch") as fetch, redirect_stderr(io.StringIO()) as log:
                    self.assertEqual(bili2md.main(), 0)
                fetch.assert_not_called()
                self.assertEqual(requests[0][0], "/v1/chat/completions")
                self.assertEqual(requests[0][1], "Bearer " + self.key)
                self.assertEqual(requests[0][2]["model"], "mock-model")
                self.assertIn("模拟中文总结", target.read_text(encoding="utf-8"))
                self.assertNotIn(self.key, target.read_text(encoding="utf-8"))
                self.assertNotIn(self.key, log.getvalue())
                self.assertEqual(source.read_text(encoding="utf-8"), "本地模拟字幕")
                with self.assertRaisesRegex(SummaryError, "重定向"):
                    api_summary.generate_summary("字幕", "mock", base + "/redirect", 10,
                                                 api_key_env="BILI2MD_TEST_KEY")
                self.assertEqual(len(requests), 2)  # 没有跟随重定向到 /leak。
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
