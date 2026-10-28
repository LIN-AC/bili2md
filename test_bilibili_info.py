import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from bilibili_info import AccessDenied, BilibiliClient, RequestFailed, SubtitleState, VideoInfo, VideoResult


BV = "BV1zvnb6eEMP"
URL = f"https://www.bilibili.com/video/{BV}/?p=1"


def make_result(text="中文字幕", state=SubtitleState.CHINESE):
    return VideoResult(VideoInfo(BV, 117489266662063, 42980871939, 1,
                                 "测试标题", "测试简介", 300, URL), state, text)


def view_data():
    return {"bvid": BV, "aid": 117489266662063, "cid": 42980871939,
            "title": "测试标题", "desc": "测试简介", "duration": 300,
            "pages": [{"page": 1, "cid": 42980871939, "duration": 300}],
            "is_upower_exclusive": False, "is_upower_play": False,
            "rights": {"pay": 0, "ugc_pay": 0, "arc_pay": 0}}


def zh_track(**changes):
    return {"lan": "ai-zh", "lan_doc": "中文（自动生成）", "type": 1,
            "ai_type": 0, "ai_status": 2,
            "subtitle_url": "http://aisubtitle.hdslb.com/test?auth_key=MASKED", **changes}


def body_data():
    return {"lang": "zh", "type": "AIsubtitle", "body": [{"from": 0.0, "to": 2.62, "content": "中文字幕"}]}


class BilibiliInfoTests(unittest.TestCase):
    def client(self, tracks=None, view=None, body=None):
        client = BilibiliClient()
        data = view if view is not None else view_data()

        def respond(url, label, body=False):
            path = urlparse(url).path
            if path == "/x/web-interface/view":
                return {"code": 0, "data": copy.deepcopy(data)}
            if path == "/x/v2/dm/view":
                return {"code": 0, "data": {"subtitle": {"subtitles": copy.deepcopy(tracks)}}}
            if path == "/x/web-interface/nav":
                return {"code": 0, "data": {"isLogin": True}}
            if body:
                return copy.deepcopy(self.body if hasattr(self, "body") else body_data())
            raise AssertionError("Unexpected endpoint: " + path)

        if body is not None:
            self.body = body
        client._json = MagicMock(side_effect=respond)
        return client

    def test_real_chinese_shape_is_usable_anonymously_without_player(self):
        client = self.client([zh_track(), {"lan": "ai-en", "ai_type": 1}])
        result = client.fetch(URL)
        self.assertEqual(result.state, SubtitleState.CHINESE)
        self.assertEqual(result.text, "中文字幕")
        paths = [urlparse(c.args[0]).path for c in client._json.call_args_list]
        self.assertNotIn("/x/player/v2", paths)
        self.assertNotIn("/x/web-interface/nav", paths)
        self.assertTrue(client._json.call_args.kwargs["body"])
        self.assertTrue(client._json.call_args.args[0].startswith("https://"))

    def test_english_only_uses_dm_and_never_downloads_english_or_player_body(self):
        client = self.client([{"lan": "ai-en", "type": 1, "ai_type": 1, "ai_status": 2}])
        result = client.fetch(URL)
        self.assertEqual(result.state, SubtitleState.OTHER_LANGUAGES)
        self.assertEqual(result.languages, ("ai-en",))
        self.assertEqual(client._json.call_count, 2)

    def test_preferred_language_normalizes_case_and_separator(self):
        client = self.client([zh_track(lan="zh-TW"), zh_track(lan="zh_CN"), zh_track(lan="ai-zh")])
        for preferred, expected in [(" zh-cn ", "zh_CN"), ("AI_ZH", "ai-zh")]:
            with patch.object(client, "_body", return_value="中文") as body:
                client.fetch(URL, preferred)
                self.assertEqual(body.call_args.args[0]["lan"], expected)

    def test_short_link_target_uses_same_url_rules(self):
        for target in [URL.replace("p=1", "p=%2B2"), URL.replace("p=1", "p="),
                       URL.replace("www.bilibili.com", "other.invalid")]:
            with self.subTest(target=target), patch("bilibili_info.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.url = target
                with self.assertRaises(RequestFailed):
                    BilibiliClient()._resolve("https://b23.tv/example")

    def test_null_and_empty_confirm_no_subtitles_with_login(self):
        for tracks in [None, []]:
            client = self.client(tracks)
            self.assertEqual(client.fetch(URL).state, SubtitleState.NO_SUBTITLES)
            self.assertEqual(client._json.call_count, 3)

    def test_empty_list_with_invalid_login_is_not_no_subtitles(self):
        for payload, message in [({"code": -101}, "要求有效登录"),
                                 ({"code": 0, "data": {"isLogin": False}}, "Cookie 未登录或已失效")]:
            with self.subTest(payload=payload):
                client = self.client([])
                original = client._json.side_effect
                client._json.side_effect = lambda url, label, **kw: payload if urlparse(url).path.endswith("/nav") else original(url, label, **kw)
                with self.assertRaisesRegex(AccessDenied, message):
                    client.fetch(URL)
                self.assertIsNone(client._login_checked_at)
                paths = [urlparse(c.args[0]).path for c in client._json.call_args_list]
                self.assertEqual(paths, ["/x/web-interface/view", "/x/v2/dm/view", "/x/web-interface/nav"])

    def test_missing_permission_fields_and_metadata_ids_fail_closed(self):
        for key, value in [("is_upower_exclusive", None), ("bvid", "BV1BgNb6oEnk"),
                           ("desc", None), ("pages", None), ("duration", 0)]:
            data = view_data()
            data[key] = value
            if key == "duration":
                data["pages"][0]["duration"] = 0
            with self.subTest(key=key), self.assertRaises(RequestFailed):
                self.client([zh_track()], data).fetch(URL)

    def test_real_paid_preview_flags_stop_before_subtitle_request(self):
        data = view_data()
        data.update(is_upower_exclusive=True, is_upower_play=False, is_upower_preview=True)
        client = self.client(None, data)
        with self.assertRaises(AccessDenied):
            client.fetch(URL)
        self.assertEqual(client._json.call_count, 1)

    def test_authorized_exclusive_content_allowed_but_unverified_purchase_fails(self):
        data = view_data()
        data.update(is_upower_exclusive=True, is_upower_play=True)
        self.assertEqual(self.client([zh_track()], data).fetch(URL).state, SubtitleState.CHINESE)
        data["rights"]["pay"] = 1
        client = self.client(None, data)
        with self.assertRaises(RequestFailed):
            client.fetch(URL)
        self.assertEqual(client._json.call_count, 1)

    def test_malformed_list_is_never_classified_as_empty(self):
        for value in [{}, "", False, 3, [{"lan": ""}], [{"lan": None}]]:
            with self.subTest(value=value):
                client = self.client(value)
                with self.assertRaises(RequestFailed):
                    client.fetch(URL)
                self.assertEqual(client._json.call_count, 2)
        client = self.client([])
        original = client._json.side_effect
        client._json.side_effect = lambda url, label, **kw: {"code": 0, "data": {"subtitle": {}}} if urlparse(url).path.endswith("/dm/view") else original(url, label, **kw)
        with self.assertRaises(RequestFailed):
            client.fetch(URL)

    def test_chinese_without_url_and_nonready_status_do_not_become_asr(self):
        for changes in [{"subtitle_url": ""}, {"ai_status": 1}, {"subtitle_url": "https://evil.invalid/subtitle"},
                        {"subtitle_url": "https://aisubtitle.hdslb.com:bad/test"}]:
            with self.subTest(changes=changes):
                with self.assertRaises(RequestFailed):
                    self.client([zh_track(**changes)]).fetch(URL)
        with self.assertRaises(AccessDenied):
            self.client([zh_track(is_lock=True)]).fetch(URL)

    def test_body_language_schema_content_time_or_ids_must_be_valid(self):
        variants = [{"lang": "en", "body": [{"content": "text"}]}, {"lang": "zh", "body": []},
                    {"lang": "zh", "body": [{}]},
                    {"lang": "zh", "body": [{"from": 0, "to": 1, "content": " "}]},
                    {"lang": "zh", "body": [{"from": 0, "to": 999, "content": "不匹配"}]},
                    {**body_data(), "cid": 999}]
        for body in variants:
            with self.subTest(body=body), self.assertRaises(RequestFailed):
                self.client([zh_track()], body=body).fetch(URL)

    def test_page_resolution_uses_requested_cid_and_missing_page_fails(self):
        data = view_data()
        data["pages"].append({"page": 2, "cid": 998, "duration": 100})
        client = self.client([zh_track()], data)
        result = client.fetch(URL.replace("p=1", "p=2"))
        self.assertEqual(result.info.cid, 998)
        dm_url = client._json.call_args_list[1].args[0]
        self.assertEqual(parse_qs(urlparse(dm_url).query)["oid"], ["998"])
        with self.assertRaises(RequestFailed):
            client.fetch(URL.replace("p=1", "p=3"))

    def test_http_business_errors_and_signed_urls_are_not_leaked(self):
        client = BilibiliClient()
        client.opener = MagicMock()
        client.opener.open.side_effect = urllib.error.HTTPError("https://test/?auth_key=SECRET", 412, "SECRET", {}, io.BytesIO())
        with self.assertRaises(RequestFailed) as err:
            client._json("https://test/?auth_key=SECRET", "字幕")
        self.assertIn("412", str(err.exception))
        self.assertNotIn("SECRET", str(err.exception))
        for response in [{"code": -400}, {"data": {}}, {"code": 0, "data": None}]:
            with patch.object(client, "_json", return_value=response), self.assertRaises(RequestFailed):
                client._api("/x/web-interface/view")

    def test_download_requires_cookie_and_valid_login(self):
        with self.assertRaises(AccessDenied):
            BilibiliClient().require_download_cookie()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tDUMMY\tTEST\n")
            client = BilibiliClient(path)
            with patch.object(client, "_login") as login:
                client.require_download_cookie()
                login.assert_called_once()

    def test_login_reuse_expires_and_resets_for_next_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tDUMMY\tTEST\n")
            client = BilibiliClient(path)
            with patch.object(client, "_api", return_value={"isLogin": True}) as api, \
                 patch("bilibili_info.time.monotonic", return_value=100) as clock:
                client._login()
                clock.return_value = 159
                client.require_download_cookie()
                self.assertEqual(api.call_count, 1)
                clock.return_value = 160
                client.require_download_cookie()
                self.assertEqual(api.call_count, 2)
                with patch.object(client, "_resolve", side_effect=RequestFailed("输入错误")), self.assertRaises(RequestFailed):
                    client.fetch(URL)
                client.require_download_cookie()
                self.assertEqual(api.call_count, 3)
                # 失败不缓存成功状态，也不能在下一次调用时跳过登录。
                api.return_value = {"isLogin": False}
                with self.assertRaises(AccessDenied):
                    client._login()
                with self.assertRaises(AccessDenied):
                    client.require_download_cookie()
                self.assertEqual(api.call_count, 5)

    def test_expired_or_other_domain_cookie_cannot_start_download(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            for domain, expiry in [(".bilibili.com", "1"), (".example.com", "4102444800")]:
                path.write_text(f"# Netscape HTTP Cookie File\n{domain}\tTRUE\t/\tFALSE\t{expiry}\tDUMMY\tTEST\n")
                client = BilibiliClient(path)
                with patch.object(client, "_login") as login, self.assertRaises(AccessDenied):
                    client.require_download_cookie()
                login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
