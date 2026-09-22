from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kbtool.core import build, discover, search
from kbtool.embedding import Cancelled, Encoder, sha256
from kbtool.build_pipeline import ENCODE_BATCH


class LargeBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoder = Encoder()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        self.package = self.folder / 'large.wlkb'

    def build(self, **kwargs):
        return build(self.source, self.package, 'large-test', self.encoder,
                     progress=kwargs.pop('progress', lambda _: None), **kwargs)

    def test_over_1000_documents_and_bounded_skip_report(self):
        for index in range(1005):
            (self.source / f'{index}.md').write_text('# 项目\n\n订单缓存两分钟。', encoding='utf-8')
            (self.source / f'{index}.bin').write_text('excluded', encoding='utf-8')
        (self.source / 'empty.md').write_text('', encoding='utf-8')
        _, files, skipped = discover(self.source)
        self.assertEqual(len(files), 1006)
        self.assertEqual(len(skipped), 1001)
        self.assertEqual(skipped[-1]['omitted_count'], 5)
        result = self.build()
        self.assertEqual(result['chunk_count'], 1005)
        self.assertEqual(result['new_vectors'], 1)
        self.assertEqual(sum(d['chunks'] == 0 for d in result['documents']), 1)
        self.assertTrue(search(self.package, '订单缓存', self.encoder)['matches'])

    def test_embedding_batches_and_cancel_preserve_previous_package(self):
        (self.source / 'old.md').write_text('原始资料', encoding='utf-8')
        self.build()
        before = sha256(self.package)
        for index in range(ENCODE_BATCH + 3):
            (self.source / f'{index}.md').write_text(f'项目编号 {index}，通知消息需要重试。', encoding='utf-8')
        cancel = threading.Event()
        def progress(message):
            if message.startswith('向量化：'):
                cancel.set()
        with patch.object(self.encoder, 'encode', wraps=self.encoder.encode) as encode:
            with self.assertRaises(Cancelled):
                self.build(cancel=cancel, progress=progress)
            self.assertEqual(len(encode.call_args.args[0]), ENCODE_BATCH)
        self.assertEqual(sha256(self.package), before)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])
        self.assertFalse(self.package.with_suffix('.wlkb.lock').exists())
        with patch.object(self.encoder, 'encode', wraps=self.encoder.encode) as encode:
            result = self.build()
            self.assertEqual([len(c.args[0]) for c in encode.call_args_list], [ENCODE_BATCH, 3])
        self.assertEqual(result['reused_chunks'], 1)

    def test_only_empty_documents_do_not_publish(self):
        (self.source / 'empty.md').write_text('', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '没有可索引正文'):
            self.build()
        self.assertFalse(self.package.exists())
