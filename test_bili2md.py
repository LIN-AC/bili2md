import subprocess
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bili2md
from bilibili_info import AccessDenied, RequestFailed, SubtitleState
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
            with self.assertRaisesRegex(bili2md.SubtitleError, "Cookie 路径配置错误"):
                bili2md.cookie_path(None, settings)


    def test_link_validation_and_part_filename(self):
        for invalid in ["", "https://example.com/video/BV1zvnb6eEMP", URL + "?p=0",
                        "file:///video/BV1zvnb6eEMP", URL + "?p=abc"]:
            with self.subTest(url=invalid), self.assertRaises(bili2md.SubtitleError):
                bili2md.validate_url(invalid)
        self.assertEqual(bili2md.validate_url(URL + "?p=2"), URL + "?p=2")
        self.assertEqual(bili2md.default_output(URL + "?p=2").name, "BV1zvnb6eEMP_p2.txt")
        self.assertEqual(bili2md.validate_url("https://b23.tv/abc"), "https://b23.tv/abc")


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
                    self.assertEqual(client.return_value.require_download_cookie.called, asr_expected)
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
             patch("bili2md.transcribe_audio") as asr:
            client.return_value.fetch.return_value = make_result("", SubtitleState.OTHER_LANGUAGES)
            client.return_value.require_download_cookie.side_effect = AccessDenied("Cookie 无效")
            self.assertEqual(bili2md.main(), 1)
            asr.assert_not_called()

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
