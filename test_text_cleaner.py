import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import bili2md
import text_cleaner as cleaner
from exception import CleaningError, ConfigurationError
from test_bilibili_info import make_result


class CleanerTests(unittest.TestCase):
    def opener(self, transform=lambda text: text):
        opener = MagicMock()
        def response(request, timeout):
            text = json.loads(json.loads(request.data)['prompt'])['text']
            return io.BytesIO(json.dumps({'done': True, 'done_reason': 'stop',
                'response': json.dumps({'text': transform(text)}, ensure_ascii=False)}).encode())
        opener.open.side_effect = response
        return opener

    def test_independent_config_and_payload(self):
        config = cleaner.CleanerConfig(model='qwen-test', base_url='http://127.0.0.1:1234', timeout=25,
                                        num_ctx=10000, num_predict=3000, temperature=0.1, top_p=0.8, seed=9)
        opener = self.opener()
        with patch('text_cleaner.urllib.request.build_opener', return_value=opener):
            self.assertEqual(cleaner.clean_text('不能删除 12 GB。', config), '不能删除 12 GB。')
        req = opener.open.call_args.args[0]
        data = json.loads(req.data)
        self.assertEqual(req.full_url, 'http://127.0.0.1:1234/api/generate')
        self.assertEqual(data['model'], 'qwen-test')
        self.assertEqual(data['options'], dict(num_ctx=10000, num_predict=3000, temperature=0.1, top_p=0.8, seed=9))
        self.assertFalse(data['think'])
        self.assertFalse(data['stream'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 25)

    def test_config_validation(self):
        for changes in [dict(base_url='https://remote.example'), dict(base_url='http://localhost/?secret=x'),
                        dict(num_ctx=1024), dict(num_predict=-1), dict(timeout=float('nan')), dict(model='x\nsecret')]:
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                cleaner.CleanerConfig(**changes)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'config.json'
            path.write_text('{"model":"custom-qwen","temperature":0.2}')
            self.assertEqual(cleaner.load_config(path).model, 'custom-qwen')
            path.write_text('{"api_model":"summary"}')
            with self.assertRaises(ConfigurationError):
                cleaner.load_config(path)

    def test_empty_input_makes_no_request(self):
        with patch('text_cleaner.urllib.request.build_opener') as opener, self.assertRaises(CleaningError):
            cleaner.clean_text(' \n', cleaner.CleanerConfig())
        opener.assert_not_called()

    def test_long_input_preserves_order_and_boundary_whitespace(self):
        text = ('不要删除 123。\n\n' * 400) + '长句' * 1000
        opener = self.opener()
        with patch('text_cleaner.urllib.request.build_opener', return_value=opener):
            self.assertEqual(cleaner.clean_text(text, cleaner.CleanerConfig()), text)
        self.assertGreater(opener.open.call_count, 1)
        self.assertEqual(''.join(cleaner.chunks(text, 300)), text)
        self.assertTrue(all(len(c.encode()) <= 300 for c in cleaner.chunks(text, 300)))

    def test_timeout_unavailable_and_http_fail_once_without_sensitive_logs(self):
        for exc in [TimeoutError('SECRET'), urllib.error.URLError(ConnectionRefusedError('SECRET')),
                    urllib.error.HTTPError('http://localhost/?SECRET', 404, 'SECRET', {}, io.BytesIO(b'SECRET'))]:
            opener = MagicMock()
            opener.open.side_effect = exc
            with patch('text_cleaner.urllib.request.build_opener', return_value=opener), redirect_stderr(io.StringIO()) as log:
                with self.assertRaises(CleaningError):
                    cleaner.clean_text('测试。', cleaner.CleanerConfig())
            opener.open.assert_called_once()
            self.assertNotIn('SECRET', log.getvalue())
            self.assertIn('elapsed=', log.getvalue())
            self.assertIn('model=qwen3:14b-q4_K_M', log.getvalue())

    def test_invalid_truncated_and_empty_response(self):
        for data in [[], {'done': False}, {'done': True, 'done_reason': 'length'},
                     {'done': True, 'done_reason': 'stop', 'response': '{}'},
                     {'done': True, 'done_reason': 'stop', 'response': '{"text":""}'}]:
            opener = MagicMock()
            opener.open.return_value = io.BytesIO(json.dumps(data).encode())
            with patch('text_cleaner.urllib.request.build_opener', return_value=opener), self.assertRaises(CleaningError):
                cleaner.clean_text('不要删除。', cleaner.CleanerConfig())

    def test_rejects_deleted_numbers_negation_identifiers_and_large_rewrites(self):
        for a, b in [('不能删除 12 GB。', '能删除 12 GB。'), ('使用 12 GB。', '使用 13 GB。'),
                     ('使用 PyTorch。', '使用 TensorFlow。'), ('不要删除。不要删除。', '不要删除。'),
                     ('甲说春天到了，乙说可以出发。', '总结：出发。')]:
            with self.subTest(a=a), self.assertRaises(CleaningError):
                cleaner.validate_output(a,b)
        cleaner.validate_output('今天我们讨论这个问题然后开始测试', '今天我们讨论这个问题，然后开始测试。')

    def test_summary_model_changes_do_not_change_cleaner_and_body_is_forwarded(self):
        for summary_args, target in [(['--ollama-model','summary-a'], 'ollama_summary.generate_summary'),
                                     (['--api-model','summary-b','--api-base-url','https://example.invalid/v1'], 'api_summary.generate_summary')]:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'raw.txt'
                path.write_text('  今天我们讨论这个问题然后开始测试 ', encoding='utf-8')
                output = Path(temp)/'summary.md'
                opener = self.opener(lambda t: t.replace('问题然后', '问题，然后'))
                with patch('text_cleaner.urllib.request.build_opener', return_value=opener), \
                     patch(target, return_value='总结') as generator, patch('bili2md.api_summary.api_config'), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(bili2md.main(['--input-text',str(path),'--summary-output',str(output),'--llm-clean',*summary_args]),0)
                self.assertEqual(json.loads(opener.open.call_args.args[0].data)['model'], 'qwen3:14b-q4_K_M')
                self.assertEqual(generator.call_args.args[0], '今天我们讨论这个问题，然后开始测试')
                self.assertTrue(path.with_name('raw.qwen.cleaned.txt').exists())
                self.assertEqual(path.read_text(encoding='utf-8'), '  今天我们讨论这个问题然后开始测试 ')

    def test_failure_preserves_basic_copy_and_stops_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'raw.txt'
            source.write_text('不要 删除 123', encoding='utf-8')
            with patch('text_cleaner.clean_text', side_effect=CleaningError('模拟超时')), \
                 patch('ollama_summary.generate_summary') as generate, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(bili2md.main(['--input-text', str(source),'--ollama-model','summary','--llm-clean']),1)
            generate.assert_not_called()
            self.assertTrue(source.with_name('raw.cleaned.txt').exists())
            self.assertFalse(source.with_name('raw.qwen.cleaned.txt').exists())

    def test_later_chunk_failure_never_saves_partial_success(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)/'raw.txt'
            text = '不能删除 123。' * 300
            source.write_text(text, encoding='utf-8')
            opener = self.opener()
            response = opener.open.side_effect
            calls = []
            def fail_second(request, timeout):
                calls.append(request)
                if len(calls) == 2:
                    raise TimeoutError()
                return response(request, timeout)
            opener.open.side_effect = fail_second
            with patch('text_cleaner.urllib.request.build_opener', return_value=opener), self.assertRaises(CleaningError):
                cleaner.clean_and_save(text, source=source, config=cleaner.CleanerConfig())
            self.assertEqual(len(calls), 2)
            self.assertFalse(source.with_name('raw.qwen.cleaned.txt').exists())
            self.assertEqual(source.read_text(encoding='utf-8'), text)

    def test_video_without_summary_can_clean_and_disabled_never_requests_llm(self):
        for enabled in [False, True]:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'video.txt'
                result = make_result('测试文本')
                with patch('bili2md.BilibiliClient') as client, patch('bili2md.cookie_path', return_value=None), \
                     patch('text_cleaner.clean_text', return_value='测试文本') as clean, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    client.return_value.fetch.return_value = result
                    self.assertEqual(bili2md.main([result.info.url,'-o',str(path), *(['--llm-clean'] if enabled else [])]),0)
                self.assertEqual(clean.call_count, int(enabled))


if __name__ == '__main__':
    unittest.main()
