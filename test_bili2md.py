import subprocess
import asyncio
import json
import os
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bili2md
import chinese_subtitle_fetch


URL = "https://www.bilibili.com/video/BV1zvnb6eEMP"


class SubtitleTests(unittest.TestCase):
    def test_filter_chinese_tracks_before_selection(self):
        for tracks, expected in [
            ([{"lan": "ai-en"}], []),
            ([], []),
            ([{"lan": "en"}, {"lan": "ai-zh"}, {"lan": "zh-CN"}], ["ai-zh", "zh-CN"]),
            ([{"lan": "zh-Hant"}, {"lan": "ai-zh-CN"}, {"lan": "ja"}], ["zh-Hant", "ai-zh-CN"]),
        ]:
            async def original(*args, **kwargs):
                return tracks
            server = SimpleNamespace(fetch_available_subtitles=original)
            chinese_subtitle_fetch.install_chinese_filter(server)
            result = asyncio.run(server.fetch_available_subtitles(aid=1, cid=2))
            self.assertEqual([t["lan"] for t in result], expected)

    def test_language_metadata_request_error_propagates(self):
        async def original(*args, **kwargs):
            raise RuntimeError("HTTP 412")
        server = SimpleNamespace(fetch_available_subtitles=original)
        chinese_subtitle_fetch.install_chinese_filter(server)
        with self.assertRaisesRegex(RuntimeError, "HTTP 412"):
            asyncio.run(server.fetch_available_subtitles())

    def test_cookie_path_settings_and_overrides(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"cookies": "custom-name.txt"}), encoding="utf-8")
            self.assertEqual(bili2md.cookie_path(None, settings), Path(temp) / "custom-name.txt")
            with patch.dict(os.environ, {"BILI2MD_COOKIES": "env-cookies.txt"}):
                self.assertEqual(bili2md.cookie_path(None, settings), Path("env-cookies.txt").resolve())
                self.assertEqual(bili2md.cookie_path(Path("cli.txt"), settings), Path("cli.txt").resolve())
            settings.write_text('{}', encoding="utf-8")
            with self.assertRaisesRegex(bili2md.SubtitleError, "Cookie 路径配置错误"):
                bili2md.cookie_path(None, settings)

    def test_success_saves_utf8_and_disables_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "nested" / "字幕.txt"
            response = subprocess.CompletedProcess([], 0, "这是字幕\n第二行\n", "")
            with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                 patch("bili2md.find_fetcher", return_value="fetcher"), \
                 patch("bili2md.subprocess.run", return_value=response) as run:
                self.assertEqual(bili2md.main(), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "这是字幕\n第二行\n")
            command = run.call_args.args[0]
            self.assertIn("--no-asr", command)
            self.assertIn("--no-clipboard", command)
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_retry_recovers(self):
        responses = [subprocess.CompletedProcess([], 1, "", "HTTP Status 412"),
                     subprocess.CompletedProcess([], 0, "重试后的字幕", "")]
        with patch("bili2md.subprocess.run", side_effect=responses) as run:
            self.assertEqual(bili2md.fetch_subtitle(URL, "fetcher"), "重试后的字幕")
            self.assertEqual(run.call_count, 2)

    def test_no_subtitle_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "existing.txt"
            output.write_text("已有内容", encoding="utf-8")
            response = subprocess.CompletedProcess([], 0, "Info: No subtitles available", "")
            with patch("sys.argv", ["bili2md.py", URL, "--no-asr", "-o", str(output)]), \
                 patch("bili2md.find_fetcher", return_value="fetcher"), \
                 patch("bili2md.subprocess.run", return_value=response) as run:
                self.assertEqual(bili2md.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "已有内容")
            self.assertEqual(run.call_count, 1)

    def test_timeout_is_retried_and_reported(self):
        with patch("bili2md.subprocess.run", side_effect=subprocess.TimeoutExpired("fetcher", 1)) as run:
            with self.assertRaisesRegex(bili2md.SubtitleError, "网络/HTTPS 错误"):
                bili2md.fetch_subtitle(URL, "fetcher", timeout=1)
            self.assertEqual(run.call_count, 2)

    def test_empty_content_and_errors_are_never_subtitles(self):
        for code, stdout, stderr in [(0, "", ""), (0, "Info: Subtitle file is empty.", ""),
                                     (1, "", "Error: failed"), (0, "Error: failed", "")]:
            with self.subTest(stdout=stdout), patch("bili2md.subprocess.run", return_value=
                    subprocess.CompletedProcess([], code, stdout, stderr)):
                with self.assertRaises(bili2md.SubtitleError):
                    bili2md.fetch_subtitle(URL, "fetcher")

    def test_link_validation_and_part_filename(self):
        for invalid in ["", "https://example.com/video/BV1zvnb6eEMP", URL + "?p=0",
                        "file:///video/BV1zvnb6eEMP", URL + "?p=abc"]:
            with self.subTest(url=invalid), self.assertRaises(bili2md.SubtitleError):
                bili2md.validate_url(invalid)
        self.assertEqual(bili2md.validate_url(URL + "?p=2"), URL + "?p=2")
        self.assertEqual(bili2md.default_output(URL + "?p=2").name, "BV1zvnb6eEMP_p2.txt")
        self.assertEqual(bili2md.validate_url("https://b23.tv/abc"), "https://b23.tv/abc")

    def test_asr_only_after_no_subtitles_and_valid_login(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.txt"
            with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                 patch("bili2md.find_fetcher", return_value="fetcher"), \
                 patch("bili2md.fetch_subtitle", side_effect=bili2md.NoSubtitles()), \
                 patch("bili2md.verify_subtitle_login") as login, \
                 patch("bili2md.transcribe_audio", return_value="中文转录") as asr:
                self.assertEqual(bili2md.main(), 0)
                login.assert_called_once()
                asr.assert_called_once()
            self.assertEqual(output.read_text(encoding="utf-8"), "中文转录\n")

    def test_network_and_cookie_failure_never_start_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.txt"
            for error in [bili2md.SubtitleError("HTTP 412"), bili2md.NoSubtitles()]:
                with self.subTest(error=type(error)), \
                     patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                     patch("bili2md.find_fetcher", return_value="fetcher"), \
                     patch("bili2md.fetch_subtitle", side_effect=error), \
                     patch("bili2md.verify_subtitle_login", side_effect=bili2md.SubtitleError("Cookie 失效")), \
                     patch("bili2md.transcribe_audio") as asr:
                    self.assertEqual(bili2md.main(), 1)
                    asr.assert_not_called()
                    self.assertFalse(output.exists())

    def test_supported_formats_skip_ffmpeg(self):
        with patch("bili2md.run_external") as run:
            for suffix in ["flac", "mp3", "ogg", "wav", "MP3"]:
                audio = Path("audio." + suffix)
                self.assertEqual(bili2md.prepare_audio(audio, Path("."), "ffmpeg", 10), audio)
            run.assert_not_called()

    def test_conversion_parameters_and_missing_output(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("bili2md.require_program", return_value="ffmpeg"), \
             patch("bili2md.run_external") as run:
            with self.assertRaisesRegex(bili2md.SubtitleError, "未生成有效 WAV"):
                bili2md.prepare_audio(Path("audio.m4a"), Path(temp), "ffmpeg", 10)
            command = run.call_args.args[0]
            self.assertIn("16000", command)
            self.assertIn("pcm_s16le", command)
            self.assertEqual(command[command.index("-ac") + 1], "1")

    def test_external_failures_and_timeouts(self):
        for stage in ["yt-dlp 音频下载", "FFmpeg 音频转换", "whisper-cli 中文转录"]:
            with self.subTest(stage=stage), patch("bili2md.subprocess.run", return_value=
                    subprocess.CompletedProcess([], 7, "", "failed")):
                with self.assertRaisesRegex(bili2md.SubtitleError, stage + "失败"):
                    bili2md.run_external(["tool"], stage, 10)
        with patch("bili2md.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 1)):
            with self.assertRaisesRegex(bili2md.SubtitleError, "超过"):
                bili2md.run_external(["tool"], "测试", 1)

    def test_asr_checks_download_and_transcript_outputs(self):
        import http.cookiejar
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            cookies = folder / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tFALSE\t0\tDUMMY\tTEST\n")
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

            with patch("bili2md.require_program", side_effect=lambda value, name: value), \
                 patch("bili2md.run_external", side_effect=external):
                with self.assertRaisesRegex(bili2md.SubtitleError, "没有生成非空转录文本"):
                    bili2md.transcribe_audio(URL, cookies, "yt-dlp", "ffmpeg", "whisper", model, 10)

    def test_login_response_must_confirm_authenticated(self):
        with patch("bili2md.require_program", return_value="python"), \
             patch("bili2md.run_external", return_value=subprocess.CompletedProcess([], 0, '{}', '')):
            with self.assertRaisesRegex(bili2md.SubtitleError, "未确认登录有效"):
                bili2md.verify_subtitle_login("tool", None, 10, "python")

    def test_expired_default_cookie_rechecks_subtitles_before_asr(self):
        for subtitles in ["重新找到的字幕", bili2md.NoSubtitles()]:
            with self.subTest(subtitles=type(subtitles)), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "result.txt"
                with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                     patch("bili2md.find_fetcher", return_value="fetcher"), \
                     patch("bili2md.fetch_subtitle", side_effect=[bili2md.NoSubtitles(), subtitles]), \
                     patch("bili2md.verify_subtitle_login", side_effect=[
                         bili2md.InvalidSubtitleCredential(), None]), \
                     patch("bili2md.cookie_subtitle_config", return_value=Path("temp.toml")), \
                     patch("bili2md.transcribe_audio", return_value="本地转录") as asr:
                    self.assertEqual(bili2md.main(), 0)
                if isinstance(subtitles, str):
                    asr.assert_not_called()
                    self.assertEqual(output.read_text(encoding="utf-8"), subtitles + "\n")
                else:
                    asr.assert_called_once()

    def test_asr_failure_keeps_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.txt"
            output.write_text("原有字幕", encoding="utf-8")
            with patch("sys.argv", ["bili2md.py", URL, "-o", str(output)]), \
                 patch("bili2md.find_fetcher", return_value="fetcher"), \
                 patch("bili2md.fetch_subtitle", side_effect=bili2md.NoSubtitles()), \
                 patch("bili2md.verify_subtitle_login"), \
                 patch("bili2md.transcribe_audio", side_effect=bili2md.SubtitleError("转录失败")):
                self.assertEqual(bili2md.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "原有字幕")


if __name__ == "__main__":
    unittest.main()
