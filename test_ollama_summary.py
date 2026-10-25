import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import bili2md
from ollama_summary import SummaryError, generate_summary, summarize_file


class SummaryTests(unittest.TestCase):
    def opener(self, payload):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return opener

    def test_generate_uses_requested_model_and_nonstreaming_local_api(self):
        opener = self.opener({"done": True, "response": "## 概述\n中文总结"})
        with patch("ollama_summary.urllib.request.build_opener", return_value=opener):
            text = generate_summary("原始字幕", "custom-model:7b", "http://127.0.0.1:11434", 60)
        request = opener.open.call_args.args[0]
        data = json.loads(request.data)
        self.assertEqual(data["model"], "custom-model:7b")
        self.assertFalse(data["stream"])
        self.assertIn("原始字幕", data["prompt"])
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/generate")
        self.assertIn("中文总结", text)

    def test_http_missing_model_and_request_failure(self):
        for code, error, expected in [(404, "model 'missing' not found", "模型不存在"),
                                      (500, "failed to load model", "HTTP 500")]:
            with self.subTest(code=code):
                opener = MagicMock()
                opener.open.side_effect = urllib.error.HTTPError(
                    "http://localhost/api/generate", code, "error", {},
                    io.BytesIO(json.dumps({"error": error}).encode()))
                with patch("ollama_summary.urllib.request.build_opener", return_value=opener):
                    with self.assertRaisesRegex(SummaryError, expected):
                        generate_summary("字幕", "missing", "http://localhost:11434", 10)

    def test_connection_and_timeout_errors(self):
        for error, expected in [(urllib.error.URLError(ConnectionRefusedError()), "无法连接"),
                                (TimeoutError(), "请求超时")]:
            opener = MagicMock()
            opener.open.side_effect = error
            with patch("ollama_summary.urllib.request.build_opener", return_value=opener):
                with self.assertRaisesRegex(SummaryError, expected):
                    generate_summary("字幕", "model", "http://localhost:11434", 10)

    def test_empty_incomplete_or_error_response_never_succeeds(self):
        for payload in [{"done": True, "response": ""}, {"done": False, "response": "部分内容"},
                        {"error": "load failed"}, [], {"done": True, "response": 42},
                        {"done": True, "response": "截断内容", "done_reason": "length"}]:
            with self.subTest(payload=payload), patch("ollama_summary.urllib.request.build_opener",
                                                     return_value=self.opener(payload)):
                with self.assertRaises(SummaryError):
                    generate_summary("字幕", "model", "http://localhost:11434", 10)

    def test_invalid_json_and_empty_source(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b"invalid JSON")
        with patch("ollama_summary.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(SummaryError, "JSON"):
                generate_summary("字幕", "model", "http://localhost:11434", 10)
        with self.assertRaisesRegex(SummaryError, "字幕文本为空"):
            generate_summary(" ", "model", "http://localhost:11434", 10)

    def test_save_markdown_preserves_subtitle_and_existing_summary_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "video.txt"
            target = Path(folder) / "output" / "video.md"
            source.write_bytes("原始字幕\n".encode("utf-8"))
            original = source.read_bytes()
            with patch("ollama_summary.generate_summary", return_value="## 概述\n测试总结"):
                result = summarize_file(source, "my-model", target, video_url="https://www.bilibili.com/video/BV1BgNb6oEnk")
            self.assertEqual(result, target)
            self.assertEqual(source.read_bytes(), original)
            saved = target.read_bytes()
            self.assertIn("my-model", saved.decode("utf-8"))
            with patch("ollama_summary.generate_summary", side_effect=SummaryError("服务未启动")):
                with self.assertRaises(SummaryError):
                    summarize_file(source, "my-model", target)
            self.assertEqual(target.read_bytes(), saved)
            self.assertEqual(source.read_bytes(), original)
            with self.assertRaisesRegex(SummaryError, "原始字幕相同"):
                summarize_file(source, "my-model", source)

    def test_input_text_cli_does_not_fetch_video(self):
        with patch("sys.argv", ["bili2md.py", "--input-text", "existing.txt", "--ollama-model", "my-model"]), \
             patch("bili2md.summarize_file", return_value=Path("summaries/existing.md")) as summarize, \
             patch("bili2md.fetch_subtitle") as fetch:
            self.assertEqual(bili2md.main(), 0)
        fetch.assert_not_called()
        self.assertEqual(summarize.call_args.args[1], "my-model")

    def test_link_summary_failure_keeps_saved_subtitle(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "subtitle.txt"
            with patch("sys.argv", ["bili2md.py", "https://www.bilibili.com/video/BV1BgNb6oEnk",
                                    "-o", str(output), "--ollama-model", "my-model"]), \
                 patch("bili2md.find_fetcher", return_value="fetcher"), \
                 patch("bili2md.fetch_subtitle", return_value="已提取的字幕"), \
                 patch("bili2md.summarize_file", side_effect=SummaryError("模型不存在")):
                self.assertEqual(bili2md.main(), 1)
            self.assertEqual(output.read_text(encoding="utf-8"), "已提取的字幕\n")


if __name__ == "__main__":
    unittest.main()
