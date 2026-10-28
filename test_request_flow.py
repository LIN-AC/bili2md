"""请求边界、有限重试和真实 CC 样本的离线回归。"""
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from urllib.parse import urlparse
from unittest.mock import MagicMock, patch

import bili2md
import media_tools
from bilibili_info import (AccessDenied, BilibiliClient, ConfigurationError, InvalidVideoURL,
                           RequestFailed, SubtitleState, TransientRequestFailed, parse_video_url)
from ollama_summary import SummaryError
from test_bilibili_info import body_data


FIXTURE = json.loads((Path(__file__).parent / "test_fixtures/cc_BV1YRnW6LESu.json").read_text(encoding="utf-8"))
VIEW, DM, NAV = "/x/web-interface/view", "/x/v2/dm/view", "/x/web-interface/nav"
CC_PATH = urlparse(FIXTURE["dm"]["subtitle"]["subtitles"][0]["subtitle_url"]).path
AI_PATH = urlparse(FIXTURE["dm"]["subtitle"]["subtitles"][1]["subtitle_url"]).path


def http_error(status, location=None):
    return urllib.error.HTTPError("https://example.invalid/?auth_key=SECRET", status, "SECRET",
                                  {"Location": location} if location else {}, io.BytesIO())


class RequestFlowTests(unittest.TestCase):
    def setUp(self):
        self.calls = Counter()
        self.failures = defaultdict(list)
        self.payloads = {VIEW: {"code": 0, "data": copy.deepcopy(FIXTURE["view"])},
                         DM: {"code": 0, "data": copy.deepcopy(FIXTURE["dm"])},
                         NAV: {"code": 0, "data": {"isLogin": True}},
                         CC_PATH: copy.deepcopy(FIXTURE["body"]), AI_PATH: body_data()}
        self.client = BilibiliClient()

        def response(request, timeout):
            path = urlparse(request.full_url).path
            self.calls[path] += 1
            if self.failures[path]:
                raise self.failures[path].pop(0)
            payload = self.payloads[path]
            return io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

        self.client.opener.open = MagicMock(side_effect=response)
        self.client.body_opener.open = MagicMock(side_effect=response)

    def run_cli(self, output):
        with patch("bili2md.BilibiliClient", return_value=self.client), \
             patch("bili2md.cookie_path", return_value=None), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return bili2md.main([FIXTURE["url"], "-o", str(output)])

    def test_cc_sample_saves_text_and_skips_asr(self):
        with tempfile.TemporaryDirectory() as folder, patch("bili2md.transcribe_audio") as asr:
            target = Path(folder) / "cc.txt"
            self.assertEqual(self.run_cli(target), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "第一段 CC 字幕\n第二段 CC 字幕\n")
            metadata = json.loads(target.with_suffix(".meta.json").read_text(encoding="utf-8"))
            self.assertEqual((metadata["bvid"], metadata["cid"], metadata["page"]),
                             ("BV1YRnW6LESu", 42964550828, 1))
            self.assertEqual(metadata["text_source"], "subtitle")
            asr.assert_not_called()
        self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1, CC_PATH: 1}))
        self.client.body_opener.open.assert_called_once()

    def test_continuous_dm_failure_only_retries_dm_and_never_starts_asr(self):
        self.failures[DM] = [TimeoutError(), TimeoutError()]
        with tempfile.TemporaryDirectory() as folder, patch("bili2md.transcribe_audio") as asr, \
             patch("bilibili_info.parse_video_url", wraps=parse_video_url) as parse:
            target = Path(folder) / "existing.txt"
            target.write_text("原有文本", encoding="utf-8")
            self.assertEqual(self.run_cli(target), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "原有文本")
            self.assertFalse(target.with_suffix(".meta.json").exists())
            parse.assert_called_once_with(FIXTURE["url"])
            asr.assert_not_called()
        self.assertEqual(self.calls, Counter({VIEW: 1, DM: 2}))

    def test_transient_dm_failure_recovers_without_replaying_view(self):
        self.failures[DM] = [http_error(503)]
        with patch("bilibili_info.parse_video_url", wraps=parse_video_url) as parse:
            self.assertEqual(self.client.fetch(FIXTURE["url"]).state, SubtitleState.CHINESE)
            parse.assert_called_once()
        self.assertEqual(self.calls, Counter({VIEW: 1, DM: 2, CC_PATH: 1}))

    def test_body_failure_has_explicit_limit_without_replaying_metadata(self):
        self.failures[CC_PATH] = [TimeoutError(), TimeoutError()]
        with self.assertRaisesRegex(TransientRequestFailed, "已尝试 2 次"):
            self.client.fetch(FIXTURE["url"])
        self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1, CC_PATH: 2}))

    def test_nonretryable_http_and_tls_errors_fail_once(self):
        for failure, error in [(http_error(404), RequestFailed), (http_error(412), RequestFailed),
                               (http_error(429), RequestFailed), (http_error(403), AccessDenied),
                               (urllib.error.URLError(ssl.SSLCertVerificationError("SECRET")), RequestFailed)]:
            with self.subTest(failure=type(failure).__name__):
                self.calls.clear()
                self.failures[DM] = [failure]
                with self.assertRaises(error) as caught:
                    self.client.fetch(FIXTURE["url"])
                self.assertNotIsInstance(caught.exception, TransientRequestFailed)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1}))

    def test_business_and_schema_errors_are_not_retried(self):
        for payload in [{"code": -404}, {"code": -101}, b"not JSON", {"code": 0, "data": {}},
                        {"code": 0, "data": {"subtitle": {"subtitles": "invalid"}}}]:
            with self.subTest(payload=payload):
                self.calls.clear()
                self.payloads[DM] = payload
                with self.assertRaises((RequestFailed, AccessDenied)):
                    self.client.fetch(FIXTURE["url"])
                self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1}))

    def test_login_retry_only_repeats_login(self):
        self.payloads[DM]["data"]["subtitle"]["subtitles"] = []
        self.failures[NAV] = [TimeoutError(), TimeoutError()]
        with self.assertRaises(TransientRequestFailed):
            self.client.fetch(FIXTURE["url"])
        self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1, NAV: 2}))

    def test_empty_subtitles_preserve_login_error_and_stop_before_asr(self):
        for payload, message in [({"code": -101}, "要求有效登录"),
                                 ({"code": 0, "data": {"isLogin": False}}, "Cookie 未登录或已失效")]:
            with self.subTest(payload=payload):
                self.calls.clear()
                self.payloads[DM]["data"]["subtitle"]["subtitles"] = []
                self.payloads[NAV] = payload
                with tempfile.TemporaryDirectory() as folder, patch("bili2md.transcribe_audio") as asr, \
                     patch("bili2md.BilibiliClient", return_value=self.client), \
                     patch("bili2md.cookie_path", return_value=None), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    target = Path(folder) / "failed.txt"
                    self.assertEqual(bili2md.main([FIXTURE["url"], "-o", str(target)]), 1)
                    self.assertIn(message, errors.getvalue())
                    self.assertNotIn("无法确认是否真的没有字幕", errors.getvalue())
                    self.assertFalse(target.exists())
                    asr.assert_not_called()
                self.assertEqual(self.calls, Counter({VIEW: 1, DM: 1, NAV: 1}))

    def test_cc_wrong_identity_language_or_content_never_becomes_asr(self):
        for location, key, value in [("dm", "cid", 99), ("dm", "oid", 99), ("track", "cid", 99),
                                     ("body", "bvid", "BV1zvnb6eEMP"), ("body", "lang", "en"),
                                     ("body", "body", []),
                                     ("body", "body", [{"from": 0, "to": 1, "content": " "}]),
                                     ("body", "body", [{"from": 0, "to": 9999, "content": "错配"}])]:
            with self.subTest(location=location, key=key):
                self.setUp()
                targets = {"dm": self.payloads[DM]["data"],
                           "track": self.payloads[DM]["data"]["subtitle"]["subtitles"][0],
                           "body": self.payloads[CC_PATH]}
                targets[location][key] = value
                with tempfile.TemporaryDirectory() as folder, patch("bili2md.transcribe_audio") as asr:
                    self.assertEqual(self.run_cli(Path(folder) / "bad.txt"), 1)
                    asr.assert_not_called()
                self.assertTrue(all(count == 1 for count in self.calls.values()))

    def test_cc_uses_requested_part_cid_and_rejects_other_part_body(self):
        self.payloads[VIEW]["data"]["pages"].append({"page": 2, "cid": 12345, "duration": 20})
        self.payloads[CC_PATH]["cid"] = 12345
        result = self.client.fetch(FIXTURE["url"] + "?p=2")
        self.assertEqual((result.info.page, result.info.cid), (2, 12345))
        request = self.client.opener.open.call_args.args[0]
        self.assertIn("oid=12345", request.full_url)
        self.payloads[CC_PATH]["cid"] = FIXTURE["view"]["cid"]
        with self.assertRaisesRegex(RequestFailed, "不匹配"):
            self.client.fetch(FIXTURE["url"] + "?p=2")

    def test_track_type_controls_readiness_without_requiring_ai_prefix(self):
        tracks = self.payloads[DM]["data"]["subtitle"]["subtitles"]
        tracks.reverse()
        tracks[0]["ai_status"] = 1
        self.assertEqual(self.client.fetch(FIXTURE["url"]).text, "第一段 CC 字幕\n第二段 CC 字幕")
        tracks[:] = [tracks[0]]
        tracks[0].update(lan="zh", ai_status=2)
        self.assertEqual(self.client.fetch(FIXTURE["url"]).state, SubtitleState.CHINESE)
        # AI 正文缺失语言仍然失败，CC 的兼容处理不会放宽 AI 校验。
        self.payloads[AI_PATH].pop("lang")
        with self.assertRaises(RequestFailed):
            self.client.fetch(FIXTURE["url"])

    def test_short_link_retries_only_failed_hop_and_validates_each_target_once(self):
        first, second = "https://b23.tv/first", "https://b23.tv/second"
        self.client.body_opener.open.side_effect = [http_error(302, second), TimeoutError(),
                                                   http_error(302, FIXTURE["url"])]
        with patch("bilibili_info.parse_video_url", wraps=parse_video_url) as parse:
            self.assertEqual(self.client._resolve(first), ("BV1YRnW6LESu", 1))
        self.assertEqual([c.args[0] for c in parse.call_args_list], [first, second, FIXTURE["url"]])
        self.assertEqual([c.args[0].full_url for c in self.client.body_opener.open.call_args_list], [first, second, second])

    def test_short_link_loop_and_hop_limit_are_bounded(self):
        with patch.object(self.client, "_redirect_target", return_value="https://b23.tv/loop") as follow:
            with self.assertRaisesRegex(RequestFailed, "循环"):
                self.client._resolve("https://b23.tv/loop")
            follow.assert_called_once()
        with patch("bilibili_info.MAX_REDIRECTS", 2), \
             patch.object(self.client, "_redirect_target", side_effect=["https://b23.tv/1", "https://b23.tv/2"]) as follow:
            with self.assertRaisesRegex(RequestFailed, "上限"):
                self.client._resolve("https://b23.tv/0")
            self.assertEqual(follow.call_count, 2)

    def test_invalid_short_link_target_is_task_error_without_retry(self):
        self.client.body_opener.open.side_effect = http_error(302, "https://[invalid")
        with self.assertRaises(InvalidVideoURL):
            self.client._resolve("https://b23.tv/example")
        self.client.body_opener.open.assert_called_once()


class ErrorBoundaryTests(unittest.TestCase):
    def test_startup_config_errors_prevent_network(self):
        with tempfile.TemporaryDirectory() as folder:
            cookies = Path(folder) / "cookies.txt"
            cookies.write_text("invalid Netscape file")
            with patch("urllib.request.OpenerDirector.open") as network, self.assertRaises(ConfigurationError):
                BilibiliClient(cookies)
            network.assert_not_called()
        with patch("bilibili_info.urllib.request.build_opener", side_effect=OSError("init failed")):
            with self.assertRaises(ConfigurationError):
                BilibiliClient()
        args = bili2md.parse_args([FIXTURE["url"], "--api-model", "model"])
        with patch("bili2md.BilibiliClient") as client, self.assertRaises(ConfigurationError):
            bili2md.run_task(args)
        client.assert_not_called()

    def test_bad_url_raises_task_error_and_cli_returns_code(self):
        for url in ["bad", "https://[invalid", FIXTURE["url"] + "?p=²"]:
            with self.subTest(url=url), patch("bili2md.cookie_path", return_value=None), \
                 patch("urllib.request.OpenerDirector.open") as network:
                args = bili2md.parse_args([url])
                with self.assertRaises(InvalidVideoURL):
                    bili2md.run_task(args)
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(bili2md.main([url]), 1)
                network.assert_not_called()

    def test_missing_video_is_task_error_without_retry_or_exit(self):
        client = BilibiliClient()
        with patch.object(client, "_json", return_value={"code": -404}) as request:
            with self.assertRaises(RequestFailed):
                client.fetch(FIXTURE["url"])
            request.assert_called_once()

    def test_runtime_model_error_propagates_and_next_task_can_run(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "text.txt", Path(folder) / "summary.md"
            source.write_text("字幕正文", encoding="utf-8")
            args = bili2md.parse_args(["--input-text", str(source), "--ollama-model", "missing",
                                      "--summary-output", str(target)])
            with patch("ollama_summary.generate_summary", side_effect=SummaryError("模型不存在")):
                with self.assertRaisesRegex(SummaryError, "模型不存在"):
                    bili2md.run_task(args)
                self.assertEqual(bili2md.main(["--input-text", str(source), "--ollama-model", "missing"]), 1)
            self.assertFalse(target.exists())
            with patch("ollama_summary.generate_summary", return_value="成功总结"):
                self.assertEqual(bili2md.run_task(args), 0)
            self.assertTrue(target.is_file())

    def test_cli_syntax_errors_and_help_return_exit_codes(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(bili2md.main(["--help"]), 0)
            self.assertEqual(bili2md.main(["--unknown-option"]), 2)


if __name__ == "__main__":
    unittest.main()
