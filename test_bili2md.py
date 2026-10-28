import subprocess
from dataclasses import replace
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bili2md
import media_tools
from bilibili_info import AccessDenied, BilibiliClient, ConfigurationError, InvalidVideoURL, RequestFailed, SubtitleState, parse_video_url
from test_bilibili_info import make_result

URL = "https://www.bilibili.com/video/BV1zvnb6eEMP"


class SubtitleTests(unittest.TestCase):
    def test_cookie_path_settings_and_overrides(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"cookies": "custom-name.txt"}), encoding="utf-8")
            self.assertEqual(bili2md.cookie_path(None, settings), Path(temp) / "custom-name.txt")
            with patch.dict(os.environ, {"BILI2MD_COOKIES": "env-cookies.txt"}):
                self.assertEqual(bili2md.cookie_path(None, settings), Path("env-cookies.txt").resolve())
                self.assertEqual(bili2md.cookie_path(Path("cli.txt"), settings), Path("cli.txt").resolve())
            settings.write_text('{}', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "Cookie 路径配置错误"):
                bili2md.cookie_path(None, settings)


    def test_link_validation_and_part_filename(self):
        for invalid in ["", "https://example.com/video/BV1zvnb6eEMP", URL + "?p=0",
                        "file:///video/BV1zvnb6eEMP", URL + "?p=abc"]:
            with self.subTest(url=invalid), self.assertRaises(InvalidVideoURL):
                parse_video_url(invalid)
        self.assertEqual(parse_video_url(URL + "?p=2")[2], 2)
        self.assertEqual(bili2md.default_output(replace(make_result().info, page=2)).name, "测试标题.txt")
        self.assertEqual(parse_video_url("https://b23.tv/abc")[0].geturl(), "https://b23.tv/abc")

    def test_url_rules_match_client_and_report_input_errors(self):
        invalid = [URL + suffix for suffix in ["?p=", "?p=1&p=2", "?p=%2B2", "?p=²", "?p=２", "?p=" + "9" * 5000]]
        invalid += ["https://[invalid/video/BV1zvnb6eEMP", URL.replace(".com", ".com:bad"),
                    URL.replace(".com", ".com:444"), URL.replace("www.", "user@www."),
                    URL.replace("www.", "www.\n"), URL + ";extra"]
        client = BilibiliClient()
        for url in invalid:
            with self.subTest(url=url[:100]):
                with self.assertRaises(InvalidVideoURL):
                    parse_video_url(url)
                with self.assertRaises(RequestFailed):
                    client._resolve(url)
        self.assertEqual(client._resolve("  " + URL + "?p=02  "), ("BV1zvnb6eEMP", 2))


    def test_supported_formats_skip_ffmpeg(self):
        with patch("media_tools.run_external") as run:
            for suffix in ["flac", "mp3", "ogg", "wav", "MP3"]:
                audio = Path("audio." + suffix)
                self.assertEqual(media_tools.prepare_audio(audio, Path("."), "ffmpeg", 10), audio)
            run.assert_not_called()


    def test_conversion_parameters_and_missing_output(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("media_tools.require_program", return_value="ffmpeg"), \
             patch("media_tools.run_external") as run:
            with self.assertRaisesRegex(bili2md.SubtitleError, "未生成有效 WAV"):
                media_tools.prepare_audio(Path("audio.m4a"), Path(temp), "ffmpeg", 10)
            command = run.call_args.args[0]
            self.assertIn("16000", command)
            self.assertIn("pcm_s16le", command)
            self.assertEqual(command[command.index("-ac") + 1], "1")


    def test_external_failures_and_timeouts(self):
        for stage in ["yt-dlp 音频下载", "FFmpeg 音频转换", "whisper-cli 中文转录"]:
            with self.subTest(stage=stage), patch("media_tools.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 7, "", "failed")):
                with self.assertRaisesRegex(bili2md.SubtitleError, stage + "失败"):
                    media_tools.run_external(["tool"], stage, 10)
        with patch("media_tools.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 1)):
            with self.assertRaisesRegex(bili2md.SubtitleError, "超过"):
                media_tools.run_external(["tool"], "测试", 1)


    def test_asr_checks_download_and_transcript_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            cookies = folder / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tDUMMY\tTEST\n")
            client = BilibiliClient(cookies)
            model = folder / "model.bin"
            model.write_bytes(b"test")

            def external(command, stage, timeout):
                if stage.startswith("yt-dlp"):
                    manifest = Path(command[command.index("--print-to-file") + 2])
                    audio = manifest.parent / "audio.wav"
                    audio.write_bytes(b"test")
                    manifest.write_text(str(audio) + "\n", encoding="utf-8")
                # Whisper returns 0 but writes no transcript: must fail.
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("media_tools.require_program", side_effect=lambda value, name: value), \
                 patch.object(client, "_api", return_value={"isLogin": True}), \
                 patch("media_tools.run_external", side_effect=external):
                with self.assertRaisesRegex(bili2md.SubtitleError, "没有生成非空转录文本"):
                    bili2md.transcribe_audio(make_result().info, client, "yt-dlp", "ffmpeg", "whisper", model, 10)

    def test_asr_exports_authenticated_cookie_snapshot_and_uses_resolved_page(self):
        import http.cookiejar
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            cookies, model = folder / "cookies.txt", folder / "model.bin"
            cookies.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t4102444800\tDUMMY\tINITIAL\n"
                               ".bilibili.com\tTRUE\t/\tFALSE\t1\tOLD\tEXPIRED\n")
            model.write_bytes(b"test")
            client = BilibiliClient(cookies)
            # 模拟磁盘文件被替换，以及登录响应刷新内存 Cookie。
            cookies.write_text("replaced source file")
            info = replace(make_result().info, page=2, url=URL + "/?p=2")

            def login(*args):
                next(iter(client.jar)).value = "REFRESHED"
                return {"isLogin": True}

            def external(command, stage, timeout):
                if stage.startswith("yt-dlp"):
                    exported = Path(command[command.index("--cookies") + 1])
                    jar = http.cookiejar.MozillaCookieJar(str(exported))
                    jar.load(ignore_discard=True, ignore_expires=True)
                    self.assertEqual([(c.name, c.value) for c in jar], [("DUMMY", "REFRESHED")])
                    self.assertEqual(command[command.index("--playlist-items") + 1], "2")
                    self.assertEqual(command[-1], info.url)
                    self.assertNotEqual(exported, cookies)
                    exported.write_text("downloader rewrote its copy")
                    manifest = Path(command[command.index("--print-to-file") + 2])
                    audio = manifest.parent / "audio.wav"
                    audio.write_bytes(b"audio")
                    manifest.write_text(str(audio) + "\n", encoding="utf-8")
                else:
                    prefix = Path(command[command.index("-of") + 1])
                    prefix.with_suffix(".txt").write_text("\ufeff 中文转录 \n", encoding="utf-8")

            with patch("media_tools.require_program", side_effect=lambda value, name: value), \
                 patch.object(client, "_api", side_effect=login), \
                 patch("media_tools.run_external", side_effect=external):
                self.assertEqual(bili2md.transcribe_audio(info, client, "yt-dlp", "ffmpeg", "whisper", model, 10), "中文转录")
            self.assertEqual(cookies.read_text(), "replaced source file")



    def test_all_five_states_control_asr_and_preserve_failures(self):
        cases = [
            (make_result("中文字幕"), True, False),
            (make_result("", SubtitleState.OTHER_LANGUAGES), True, True),
            (make_result("", SubtitleState.NO_SUBTITLES), True, True),
            (AccessDenied("权限不足"), False, False),
            (RequestFailed("请求失败"), False, False),
        ]
        for result, success, asr_expected in cases:
            with self.subTest(state=type(result).__name__), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "output.txt"
                output.write_text("原有字幕", encoding="utf-8")
                with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                     patch("bili2md.BilibiliClient") as client, \
                     patch("bili2md.transcribe_audio", return_value="ASR 文本") as asr:
                    if isinstance(result, Exception):
                        client.return_value.fetch.side_effect = result
                    else:
                        client.return_value.fetch.return_value = result
                    self.assertEqual(bili2md.main(), 0 if success else 1)
                    self.assertEqual(asr.called, asr_expected)
                    if asr_expected:
                        self.assertEqual(asr.call_args.args[:2], (result.info, client.return_value))
                if success:
                    meta = json.loads(output.with_suffix(".meta.json").read_text(encoding="utf-8"))
                    self.assertEqual(meta["title"], "测试标题")
                    self.assertEqual(meta["desc"], "测试简介")
                    self.assertNotIn("subtitle_url", meta)
                else:
                    self.assertEqual(output.read_text(encoding="utf-8"), "原有字幕")
                    self.assertFalse(output.with_suffix(".meta.json").exists())

    def test_no_asr_flag_stops_download(self):
        with patch("sys.argv", ["bili2md.py", URL, "--no-asr"]), \
             patch("bili2md.BilibiliClient") as client, \
             patch("bili2md.transcribe_audio") as asr:
            client.return_value.fetch.return_value = make_result("", SubtitleState.NO_SUBTITLES)
            self.assertEqual(bili2md.main(), 1)
            asr.assert_not_called()
            client.return_value.require_download_cookie.assert_not_called()

    def test_failed_download_cookie_validation_never_runs_asr(self):
        with patch("sys.argv", ["bili2md.py", URL]), \
             patch("bili2md.BilibiliClient") as client, \
             patch("media_tools.run_external") as external:
            client.return_value.fetch.return_value = make_result("", SubtitleState.OTHER_LANGUAGES)
            client.return_value.require_download_cookie.side_effect = AccessDenied("Cookie 无效")
            self.assertEqual(bili2md.main(), 1)
            external.assert_not_called()

    def test_asr_failure_keeps_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.txt"
            output.write_text("原有字幕", encoding="utf-8")
            with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                 patch("bili2md.BilibiliClient") as client, \
                 patch("bili2md.transcribe_audio", side_effect=bili2md.SubtitleError("转录失败")):
                client.return_value.fetch.return_value = make_result("", SubtitleState.NO_SUBTITLES)
                self.assertEqual(bili2md.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "原有字幕")


if __name__ == "__main__":
    unittest.main()
