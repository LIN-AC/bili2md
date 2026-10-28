from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import bili2md
from exception import SummaryError
from bilibili_info import SubtitleState
from ollama_summary import summarize_file
from test_bilibili_info import make_result
from text_cleaning import clean_text, save_cleaned_text


class TextCleaningTests(unittest.TestCase):
    def test_empty_and_whitespace_normalization(self):
        for value in ["", " \t\r\n\u00a0", "\ufeff\n"]:
            self.assertEqual(clean_text(value), "")
        self.assertEqual(clean_text("\ufeff  第一行\t  GPU\u00a0  8 GB \r\n\r第二行\u2028第三行  "),
                         "第一行 GPU 8 GB\n\n第二行\n第三行")

    def test_source_specific_empty_fragments_and_paragraphs(self):
        raw = "\n 第一段\n \t\n\n第二段\n\n"
        self.assertEqual(clean_text(raw, "subtitle"), "第一段\n第二段")
        for source in ["asr", "unknown", "unrecognized"]:
            self.assertEqual(clean_text(raw, source), "第一段\n\n第二段")

    def test_repetition_and_order_are_preserved(self):
        raw = "不要删除。\n不要删除。\n先启动。\n再停止。\n先启动。"
        for source in ["subtitle", "asr", "unknown"]:
            self.assertEqual(clean_text(raw, source), raw)

    def test_mixed_language_numbers_negation_and_names(self):
        raw = "不是所有 NVIDIA RTX 4090 都支持该参数。\n仅在 PyTorch 2.5、温度 -0.5°C、阈值 <= 0.01 时启用。\n不要删除 --no-asr；最多 3 次，不是 30 次。"
        self.assertEqual(clean_text(raw, "subtitle"), raw)

    def test_uncertain_markup_timestamps_and_sound_labels_are_kept(self):
        raw = "1\n00:00:01,000 --> 00:00:02,000\n<b>不是 HTML 清洗器</b>\n[音乐]\nA < B && B > 0\n技术标记\ufeff保留"
        self.assertEqual(clean_text(raw), raw)

    def test_explicit_term_mapping_has_boundaries_and_no_cascade(self):
        raw = "pytorch notpytorch 中文pytorch pytorch中文 PYTORCH\nTorch"
        terms = {"pytorch": "Torch", "Torch": "PyTorch"}
        self.assertEqual(clean_text(raw, terms=terms),
                         "Torch notpytorch 中文pytorch pytorch中文 PYTORCH\nPyTorch")
        self.assertEqual(clean_text(raw), raw)
        self.assertEqual(terms, {"pytorch": "Torch", "Torch": "PyTorch"})
        self.assertEqual(clean_text("whisper cpp", terms={"whisper": "X", "whisper cpp": "whisper.cpp"}), "whisper.cpp")
        for mapping in [{"": "x"}, {"x": ""}, {"x": "a\nb"}, {1: "x"}]:
            with self.assertRaises(ValueError):
                clean_text(raw, terms=mapping)

    def test_default_cleaning_is_idempotent(self):
        raw = "\ufeff A\t B \r\n\r\n\r\n不。\n不。\n"
        for source in ["subtitle", "asr", "unknown"]:
            result = clean_text(raw, source)
            self.assertEqual(clean_text(result, source), result)

    def test_saved_copy_preserves_raw_bytes_and_existing_cleaned_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "video.txt"
            raw = "\ufeff  不要\t删除  123\r\n\r\n".encode("utf-8")
            source.write_bytes(raw)
            cleaned = clean_text(raw.decode("utf-8"), "subtitle")
            first = save_cleaned_text(source, cleaned)
            second = save_cleaned_text(source, "第二次")
            self.assertEqual(first.name, "video.cleaned.txt")
            self.assertEqual(second.name, "video.cleaned (2).txt")
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(first.read_bytes(), "不要 删除 123\n".encode())
            self.assertEqual(second.read_text(encoding="utf-8"), "第二次\n")

    def test_summary_uses_cleaned_body_but_original_metadata_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "raw.txt", Path(temp) / "summary.md"
            raw = "  不要\t删除  12\r\n\r\n 不要删除。\r\n不要删除。".encode()
            source.write_bytes(raw)
            metadata = {"title": "原始  标题", "desc": "原始  简介", "text_source": "subtitle",
                        "text_sha256": hashlib.sha256(raw).hexdigest()}
            original_metadata = dict(metadata)
            generator = MagicMock(return_value="总结")
            summarize_file(source, "model", output, metadata=metadata, generator=generator)
            expected = "不要 删除 12\n不要删除。\n不要删除。"
            self.assertEqual(generator.call_args.args[0],
                             "视频标题：原始  标题\n视频简介：原始  简介\n\n字幕正文：\n" + expected)
            self.assertEqual(source.with_name("raw.cleaned.txt").read_text(encoding="utf-8"), expected + "\n")
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(metadata, original_metadata)

    def test_bad_hash_or_empty_text_never_calls_model_or_writes_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "raw.txt", Path(temp) / "summary.md"
            generator = MagicMock()
            for raw, metadata in [(b"text", {"title": "x", "desc": "", "text_sha256": "wrong"}),
                                  (b" \r\n", None)]:
                source.write_bytes(raw)
                with self.assertRaises(SummaryError):
                    summarize_file(source, "model", output, metadata=metadata, generator=generator)
                self.assertEqual(list(Path(temp).glob("*.cleaned*.txt")), [])
            generator.assert_not_called()

    def test_model_failure_preserves_both_text_versions_and_existing_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "raw.txt", Path(temp) / "summary.md"
            source.write_bytes(b"  do  not\r\n\r\nremove 123")
            raw = source.read_bytes()
            output.write_text("已有总结", encoding="utf-8")
            with self.assertRaises(SummaryError):
                summarize_file(source, "model", output, generator=MagicMock(side_effect=SummaryError("失败")))
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(source.with_name("raw.cleaned.txt").read_bytes(), b"do not\n\nremove 123\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "已有总结")

    def test_cleaned_output_does_not_collide_with_summary_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "raw.txt"
            source.write_text("文本", encoding="utf-8")
            target = Path(temp) / "raw.cleaned.txt"
            summarize_file(source, "model", target, generator=MagicMock(return_value="总结"))
            self.assertEqual(source.with_name("raw.cleaned (2).txt").read_text(encoding="utf-8"), "文本\n")
            self.assertIn("总结", target.read_text(encoding="utf-8"))

    def test_copy_write_failure_stops_before_model_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "raw.txt"
            source.write_bytes(b"do  not remove")
            generator = MagicMock()
            with patch("ollama_summary.save_cleaned_text", side_effect=OSError("disk full")), self.assertRaises(OSError):
                summarize_file(source, "model", Path(temp) / "summary.md", generator=generator)
            generator.assert_not_called()
            self.assertEqual(source.read_bytes(), b"do  not remove")

    def test_video_summary_cleans_once_and_preserves_asr_paragraphs(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "raw.txt", Path(temp) / "summary.md"
            result = make_result(state=SubtitleState.NO_SUBTITLES)
            raw = "  不要\t删除\n\n\n123 "
            with patch("bili2md.BilibiliClient") as client, patch("bili2md.cookie_path", return_value=None), \
                 patch("bili2md.transcribe_audio", return_value=raw), \
                 patch("ollama_summary.generate_summary", return_value="总结") as generator, \
                 patch("ollama_summary.clean_text", wraps=clean_text) as clean, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                client.return_value.fetch.return_value = result
                self.assertEqual(bili2md.main([result.info.url, "-o", str(source), "--ollama-model", "model",
                                               "--summary-output", str(target)]), 0)
                clean.assert_called_once_with(raw + "\n", "asr")
                self.assertTrue(generator.call_args.args[0].endswith("不要 删除\n\n123"))
            self.assertEqual(len(list(Path(temp).glob("*.cleaned*.txt"))), 1)
            self.assertEqual(source.read_bytes(), (raw + "\n").encode())

    def test_video_pipeline_creates_copy_without_model_for_subtitle_and_asr(self):
        for state, expected in [(SubtitleState.CHINESE, "不 删除\n结束\n"),
                                (SubtitleState.NO_SUBTITLES, "不 删除\n\n结束\n")]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                raw = "  不\t删除\n\n结束 "
                result = make_result(raw, state=state)
                source = Path(temp) / "raw.txt"
                with patch("bili2md.BilibiliClient") as client, patch("bili2md.cookie_path", return_value=None), \
                     patch("bili2md.transcribe_audio", return_value=raw), patch("bili2md.summarize_file") as summary, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    client.return_value.fetch.return_value = result
                    self.assertEqual(bili2md.main([result.info.url, "-o", str(source)]), 0)
                    summary.assert_not_called()
                self.assertEqual(source.read_bytes(), (raw + "\n").encode())
                self.assertEqual(source.with_name("raw.cleaned.txt").read_text(encoding="utf-8"), expected)
                metadata = json.loads(source.with_suffix(".meta.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["text_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_input_text_cli_cleans_for_both_providers_without_fetching(self):
        for options, provider in [(["--ollama-model", "model"], "ollama_summary.generate_summary"),
                                  (["--api-model", "model", "--api-base-url", "https://example.invalid/v1"],
                                   "api_summary.generate_summary")]:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                source, output = Path(temp) / "raw.txt", Path(temp) / "summary.md"
                raw = " 不要\t删除\n\n\n123 ".encode()
                source.write_bytes(raw)
                with patch(provider, return_value="总结") as generator, patch("bili2md.api_summary.api_config"), \
                     patch("bili2md.BilibiliClient") as client, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(bili2md.main(["--input-text", str(source), "--summary-output", str(output), *options]), 0)
                    client.assert_not_called()
                    self.assertEqual(generator.call_args.args[0], "不要 删除\n\n123")
                self.assertEqual(source.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
