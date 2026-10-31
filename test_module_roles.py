from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bili_cli
import bili2md
import bilibili_info
import exception
import media_tools
import ollama_summary
import test_bilibili_info
from test_bilibili_info import make_result, zh_track, view_data


class ModuleRoleTests(unittest.TestCase):
    def test_exception_identity_and_business_failures_are_preserved(self):
        self.assertIs(bilibili_info.RequestFailed, exception.RequestFailed)
        self.assertIs(ollama_summary.SummaryError, exception.SummaryError)
        self.assertIs(media_tools.SubtitleError, exception.SubtitleError)
        with patch('bili2md.read_input', return_value=object()), patch('bili2md.run_task', side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError):
                bili2md.main([])

    def test_interactive_and_argument_inputs_share_cli(self):
        url = 'https://www.bilibili.com/video/BV1YRnW6LESu'
        with patch('builtins.input', return_value=url) as read:
            self.assertEqual(bili_cli.read_input([]).url, url)
            read.assert_called_once()
        with patch('builtins.input') as read:
            self.assertEqual(bili_cli.read_input([url]).url, url)
            bili_cli.read_input(['--input-text', 'text.txt', '--ollama-model', 'model'])
            read.assert_not_called()
        with patch('builtins.input', side_effect=EOFError()), redirect_stderr(io.StringIO()):
            self.assertEqual(bili2md.main([]), 1)

    def test_safe_titles_and_fallback_names(self):
        info = make_result().info
        for title in ['中文标题', 'a<>:"/\\|?*b\x00\n', 'CON', 'con.txt', 'LPT¹', 'x' * 500, '😀' * 100]:
            with self.subTest(title=title):
                name = bili2md.default_output(replace(info, title=title)).name
                self.assertNotRegex(name, r'[<>:"/\\|?*\x00-\x1f]')
                self.assertLess(len(name.encode('utf-16-le')), 400)
                self.assertNotIn(name.lower(), ['con.txt', 'con.txt.txt', 'lpt¹.txt'])
        for title in ['', '   ', '...', '<>:?*']:
            self.assertEqual(bili2md.default_output(replace(info, title=title)).name, f'{info.bvid}_p1.txt')

    def test_empty_title_metadata_still_allows_subtitle(self):
        data = view_data()
        data['title'] = ''
        result = test_bilibili_info.BilibiliInfoTests().client([zh_track()], view=data).fetch(make_result().info.url)
        self.assertEqual(result.text, '中文字幕')

    def test_collisions_preserve_text_metadata_and_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / '同名.txt'
            base.write_bytes(b'existing')
            base.with_name('同名 (2).meta.json').write_bytes(b'metadata')
            with patch('bili2md.default_output', return_value=base):
                first = bili2md.save_text(make_result().info, b'first')
                second = bili2md.save_text(make_result().info, b'second')
            self.assertEqual(first.name, '同名 (3).txt')
            self.assertEqual(second.name, '同名 (4).txt')
            self.assertEqual(base.read_bytes(), b'existing')
            self.assertEqual(first.read_bytes(), ollama_summary.format_source(make_result().info.title, make_result().info.desc, 'first').encode())
            self.assertEqual(base.with_name('同名 (2).meta.json').read_bytes(), b'metadata')

    def test_summary_collision_and_exclusive_create_race(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / 'title.txt'
            original_exists = Path.exists
            def exists(path):
                return str(path) == str(Path('summaries/title.md')) or original_exists(path)
            with patch('bili2md.default_output', return_value=base), patch.object(Path, 'exists', exists):
                self.assertEqual(bili2md.save_text(make_result().info, b'new').name, 'title (2).txt')
            original_open = Path.open
            def racing_open(path, mode='r', *args, **kwargs):
                if path == base and mode == 'xb':
                    with original_open(path, 'wb') as file:
                        file.write(b'concurrent writer')
                return original_open(path, mode, *args, **kwargs)
            with patch('bili2md.default_output', return_value=base), patch.object(Path, 'open', racing_open):
                self.assertEqual(bili2md.save_text(make_result().info, b'new').name, 'title (3).txt')
            self.assertEqual(base.read_bytes(), b'concurrent writer')

    def test_title_output_uses_existing_info_and_saves_matching_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / '视频标题.txt'
            result = make_result('实际正文')
            with patch('bili2md.default_output', return_value=base), patch('bili2md.cookie_path', return_value=None), \
                 patch('bili2md.BilibiliClient') as client, patch('bili2md.transcribe_audio') as asr:
                client.return_value.fetch.return_value = result
                self.assertEqual(bili2md.main([result.info.url]), 0)
                self.assertEqual(bili2md.main([result.info.url]), 0)
                self.assertEqual(client.return_value.fetch.call_count, 2)
                asr.assert_not_called()
            self.assertEqual(base.read_bytes(), ollama_summary.format_source(result.info.title, result.info.desc, '实际正文\n').encode())
            numbered = base.with_name('视频标题 (2).txt')
            self.assertEqual(numbered.read_bytes(), base.read_bytes())
            self.assertEqual(json.loads(numbered.with_suffix('.meta.json').read_text(encoding='utf-8'))['title'], result.info.title)

    def test_missing_cookie_keeps_anonymous_chinese_and_blocks_download(self):
        with tempfile.TemporaryDirectory() as temp:
            client = bilibili_info.BilibiliClient(Path(temp) / 'missing.txt')
            with patch.object(client, '_login') as login, self.assertRaises(exception.AccessDenied):
                client.require_download_cookie()
            login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
