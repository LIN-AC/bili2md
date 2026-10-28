from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import urlparse
from urllib.request import Request
from unittest.mock import patch

import bili2md
import media_tools
from bilibili_info import BilibiliClient, SubtitleState
from cookie_debug import cookie_check, cookie_operation, reset_cookie_debug
from test_bilibili_info import URL, body_data, view_data, zh_track


def trace_scenario(tracks):
    reset_cookie_debug()
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"BILI2MD_DEBUG_COOKIE": "1"}), redirect_stderr(io.StringIO()) as log:
        path = Path(temp) / "cookies.txt"
        path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tDUMMY\tTEST_ONLY\n", encoding="utf-8")
        model = Path(temp) / "model.bin"
        model.write_bytes(b"mock")
        client = BilibiliClient(path)

        def response(request, timeout):
            endpoint = urlparse(request.full_url).path
            if endpoint == "/x/web-interface/view":
                payload = {"code": 0, "data": view_data()}
            elif endpoint == "/x/v2/dm/view":
                payload = {"code": 0, "data": {"subtitle": {"subtitles": tracks}}}
            elif endpoint == "/x/web-interface/nav":
                payload = {"code": 0, "data": {"isLogin": True}}
            else:
                payload = body_data()
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch.object(client.opener, "open", side_effect=response), patch.object(client.body_opener, "open", side_effect=response):
            result = client.fetch(URL)
            if result.state is not SubtitleState.CHINESE:
                with patch("media_tools.require_program", side_effect=lambda value, name: value), \
                     patch("media_tools.run_external", side_effect=bili2md.SubtitleError("模拟下载入口停止")):
                    try:
                        bili2md.transcribe_audio(result.info, client, "mock-ytdlp", "mock-ffmpeg", "mock-whisper", model, 10)
                    except bili2md.SubtitleError:
                        pass
    return log.getvalue()


class CookieDebugTests(unittest.TestCase):
    def test_load_filters_expired_cookie_and_sends_session_to_nav(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n"
                            ".bilibili.com\tTRUE\t/\tTRUE\t4102444800\tSESSDATA\tTEST_ONLY\n"
                            ".bilibili.com\tTRUE\t/\tFALSE\t1\tEXPIRED\tTEST_ONLY\n", encoding="utf-8")
            client = BilibiliClient(path)
            self.assertEqual([c.name for c in client.jar], ["SESSDATA"])
            request = Request("https://api.bilibili.com/x/web-interface/nav")
            client.jar.add_cookie_header(request)
            self.assertEqual(request.get_header("Cookie"), "SESSDATA=TEST_ONLY")

    def test_disabled_logging_preserves_action_return_and_exception(self):
        with patch.dict(os.environ, {"BILI2MD_DEBUG_COOKIE": "0"}), redirect_stderr(io.StringIO()) as log:
            self.assertEqual(cookie_check("test", "format", lambda: 42), 42)
            def failing():
                raise ValueError("original")
            with self.assertRaisesRegex(ValueError, "original"):
                cookie_check("test", "format", failing)
        self.assertEqual(log.getvalue(), "")

    def test_operations_do_not_increment_checks_and_reset_is_per_flow(self):
        with patch.dict(os.environ, {"BILI2MD_DEBUG_COOKIE": "1"}), redirect_stderr(io.StringIO()) as log:
            reset_cookie_debug()
            cookie_check("test", "file_exists", lambda: True)
            cookie_operation("test", "http_cookie_processor_selection")
            cookie_check("test", "server_auth", lambda: False)
            reset_cookie_debug()
            cookie_check("test", "file_exists", lambda: True)
        self.assertEqual(re.findall(r"event=check checks=(\d+)", log.getvalue()), ["1", "2", "1"])
        self.assertIn("event=operation checks=1", log.getvalue())

    def test_counts_cover_chinese_other_languages_and_no_subtitles(self):
        for tracks, count, auth_calls in [([zh_track()], 2, 0), ([{"lan": "ai-en"}], 4, 1), (None, 4, 1)]:
            with self.subTest(count=count):
                log = trace_scenario(tracks)
                checks = [int(x) for x in re.findall(r"event=check checks=(\d+)", log)]
                self.assertEqual(checks, list(range(1, count + 1)))
                self.assertEqual(log.count("type=server_auth"), auth_calls)
                self.assertNotIn("TEST_ONLY", log)
                self.assertNotIn("auth_key", log)


if __name__ == "__main__":
    unittest.main()
