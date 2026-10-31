import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import bili2md
import ollama_summary as summary
from test_bilibili_info import make_result


class SourceStructureTests(unittest.TestCase):
    def test_saved_structure_is_exact_summary_input_with_or_without_sidecar(self):
        for sidecar in (False, True):
            with self.subTest(sidecar=sidecar), tempfile.TemporaryDirectory() as folder:
                info = replace(make_result().info, title='PyTorch 训练', desc='RTX 4090\n显存与批大小')
                body = '  不要删除\r\n\r\n===== 标题 =====\n正文中的标题标记\n'
                path = bili2md.save_text(info, body.encode(), Path(folder)/'source.txt')
                saved = path.read_bytes()
                self.assertEqual(saved.decode(), summary.format_source(info.title, info.desc, body))
                if sidecar:
                    metadata = info.metadata()
                    metadata['text_sha256'] = hashlib.sha256(saved).hexdigest()
                    path.with_suffix('.meta.json').write_text(json.dumps(metadata), encoding='utf-8')
                generate = MagicMock(return_value='总结')
                summary.summarize_file(path, 'local', Path(folder)/'summary.md', generator=generate)
                self.assertEqual(generate.call_args.args[0], saved.decode())
                self.assertEqual(summary._source_sections(saved.decode()), (info.title, info.desc, body))
                prompt = summary.summary_prompt(saved.decode())
                self.assertIn('<hotwords>\n标题：PyTorch 训练\n简介：RTX 4090\n显存与批大小', prompt)
                self.assertIn('<transcript>\n' + body + '\n</transcript>', prompt)
                self.assertEqual(path.read_bytes(), saved)

    def test_empty_body_fails_even_with_title_and_description(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.txt'
            path.write_text(summary.format_source('标题', '简介', ' \n'), encoding='utf-8')
            generate = MagicMock()
            with self.assertRaises(summary.SummaryError):
                summary.summarize_file(path, 'local', Path(folder)/'summary.md', generator=generate)
            generate.assert_not_called()

    def test_empty_metadata_fields_and_plain_input(self):
        text = summary.format_source('', '', '正文\n')
        self.assertEqual(summary._source_sections(text), ('', '', '正文\n'))
        self.assertIn('<transcript>\n正文\n', summary.summary_prompt(text))
        self.assertIsNone(summary._source_sections('普通正文\n===== 正文 =====\n保留'))


@unittest.skipUnless(os.environ.get('BILI2MD_SUMMARY_LOCAL_MODEL'), '未启用本地 Ollama 结构集成测试')
class LocalSummaryIntegration(unittest.TestCase):
    def test_saved_tech_text_to_local_ollama(self):
        root = Path('temp_test')
        root.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='summary_structure_', dir=root))
        info = replace(make_result().info, title='PyTorch 与 RTX 4090：训练显存控制',
                       desc='热词：PyTorch、RTX 4090、batch size、混合精度。背景术语：TensorRT。')
        body = ('今天讨论派托奇训练时的显存问题。使用 RTX 4090 的 24 GB 显存。'
                '把 batch size 从 8 降到 4 可以降低显存占用，但不能保证训练速度提高。'
                '混合精度可能减少显存占用，必须验证数值稳定性。我们没有测试推理性能。\n')
        source = bili2md.save_text(info, body.encode(), folder/'source.txt')
        raw = source.read_bytes()
        metadata = info.metadata()
        metadata['text_sha256'] = hashlib.sha256(raw).hexdigest()
        source.with_suffix('.meta.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        model = os.environ['BILI2MD_SUMMARY_LOCAL_MODEL']
        with patch('ollama_summary.generate_summary', wraps=summary.generate_summary) as generate:
            output = summary.summarize_file(source, model, folder/'summary.md', timeout=180)
        self.assertEqual(generate.call_args.args[0], raw.decode())
        self.assertEqual(source.read_bytes(), raw)
        self.assertTrue(output.read_text(encoding='utf-8').strip())
        (folder/'prompt.txt').write_text(summary.summary_prompt(raw.decode()), encoding='utf-8')
        print(f'本地 Ollama 集成产物：{folder.resolve()}，模型：{model}')


if __name__ == '__main__':
    unittest.main()
