import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile

import numpy as np
from pypdf import PdfWriter

from kbtool.core import (build, discover, inspect_package, make_chunks, parse_document,
                         search, text_blocks)
from kbtool.embedding import Cancelled, Encoder, sha256


class BuilderTests(unittest.TestCase):
    """Integration tests use the real pinned ONNX model, not random/fake embeddings."""
    @classmethod
    def setUpClass(cls):
        cls.encoder = Encoder()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.doc = self.source / 'project.md'
        self.doc.write_text('# 客户项目\n\n## 订单缓存\n\n订单详情使用 Redis，缓存有效期为 120 秒。\n\n'
                            '## 提醒故障\n\n数据库事务提交后消息发送失败。使用 outbox 同事务写入，'
                            '后台补发并按 event_id 幂等处理。\n', encoding='utf-8')
        self.package = self.root / 'customer.wlkb'

    def build(self, **options):
        return build(self.source, self.package, options.pop('customer_id', 'customer-a'),
                     self.encoder, progress=lambda _: None, **options)

    def test_real_embeddings_are_normalized_and_semantic(self):
        docs = self.encoder.encode(['订单详情缓存的有效时间是两分钟。', '用户头像可以上传蓝色图片。'])
        query = self.encoder.encode(['订单缓存过多久失效？'], query=True)[0]
        self.assertEqual(docs.shape, (2, 512))
        np.testing.assert_allclose(np.linalg.norm(docs, axis=1), 1, atol=1e-5)
        self.assertGreater(float(docs[0] @ query), float(docs[1] @ query))

    def test_build_and_hybrid_retrieval(self):
        manifest = self.build()
        self.assertEqual(manifest['chunk_count'], 2)
        result = search(self.package, '数据保存了但是提醒没发出去怎么办', self.encoder, top_k=1)
        self.assertIn('提醒故障', result['matches'][0]['section'])
        self.assertIn('outbox', result['matches'][0]['text'])
        self.assertEqual(inspect_package(self.package)['customer_id'], 'customer-a')

    def test_unchanged_build_preserves_file_and_version(self):
        first = self.build()
        before = sha256(self.package)
        second = self.build()
        self.assertTrue(second['unchanged'])
        self.assertEqual(first['version'], second['version'])
        self.assertEqual(sha256(self.package), before)

    def test_build_requires_wlkb_extension(self):
        for extension in ('.wenlukb', '.sqlite', ''):
            with self.subTest(extension=extension):
                output = self.package.with_suffix(extension)
                with self.assertRaisesRegex(ValueError, '扩展名必须为 .wlkb'):
                    build(self.source, output, 'customer-a', self.encoder, progress=lambda _: None)
                self.assertFalse(output.exists())

    def test_changed_content_reuses_unmodified_vectors(self):
        self.build()
        self.doc.write_text(self.doc.read_text(encoding='utf-8').replace('120 秒', '90 秒'), encoding='utf-8')
        result = self.build()
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['new_vectors'], 1)
        self.assertEqual(result['reused_chunks'], 1)
        matches = search(self.package, '订单缓存的有效期', self.encoder)['matches']
        self.assertTrue(any('90 秒' in item['text'] for item in matches))
        self.assertFalse(any('120 秒' in item['text'] for item in matches))

    def test_deleted_document_does_not_survive_rebuild(self):
        other = self.source / 'other.txt'
        other.write_text('唯一测试标识 ZXQREMOVED 是旧资料。', encoding='utf-8')
        self.build()
        other.unlink()
        self.build()
        result = search(self.package, 'ZXQREMOVED', self.encoder, mode='keyword')
        self.assertEqual(result['matches'], [])

    def test_failure_preserves_existing_package(self):
        self.build()
        before = sha256(self.package)
        (self.source / 'broken.docx').write_bytes(b'not a zip')
        with self.assertRaisesRegex(ValueError, '未发布'):
            self.build()
        self.assertEqual(sha256(self.package), before)
        self.assertFalse(self.package.with_suffix('.wlkb.lock').exists())

    def test_customer_isolation(self):
        self.build()
        with self.assertRaisesRegex(ValueError, '其他客户'):
            self.build(customer_id='customer-b')
        with self.assertRaisesRegex(ValueError, '客户 ID'):
            search(self.package, '订单', self.encoder, customer_id='customer-b')

    def test_cancellation_preserves_existing_package(self):
        self.build()
        before = sha256(self.package)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(Cancelled):
            self.build(cancel=cancelled)
        self.assertEqual(sha256(self.package), before)
        self.assertFalse(self.package.with_suffix('.wlkb.lock').exists())

    def test_model_mismatch_rejected(self):
        self.build()
        with closing(sqlite3.connect(self.package)) as connection:
            manifest = json.loads(connection.execute('SELECT value FROM metadata').fetchone()[0])
            manifest['embedding']['revision'] = 'different-model'
            connection.execute('UPDATE metadata SET value=?', (json.dumps(manifest),))
            connection.commit()
        with self.assertRaisesRegex(ValueError, '模型不匹配'):
            search(self.package, '订单', self.encoder)

    def test_long_documents_chunk_without_silent_truncation(self):
        text = '# ' + '超长标题' * 80 + '\n\n' + '订单事务需要正确处理重复请求。' * 90
        chunks = make_chunks(text_blocks(text, '长文'), 'long.md', self.encoder)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(self.encoder.token_count(c['embedding_text']) + 2 <= 512 for c in chunks))
        self.assertEqual(self.encoder.encode([c['embedding_text'] for c in chunks]).shape[0], len(chunks))
        with self.assertRaisesRegex(ValueError, '上限'):
            self.encoder.encode(['这是一段超过长度的文字。' * 100], query=True)

    def test_evaluation_and_hidden_folders_excluded(self):
        for name in ('evaluation', '.secrets', 'tests'):
            folder = self.source / name
            folder.mkdir()
            (folder / 'answer.md').write_text('不要入库的验收答案', encoding='utf-8')
        _, files, _ = discover(self.source)
        self.assertEqual(files, [self.doc])

    def test_source_code_files_are_discovered_and_keep_line_ranges(self):
        vue = self.source / 'src' / 'App.vue'
        vue.parent.mkdir()
        vue.write_text('<template>\n  <Panel />\n</template>\n\n'
                       '<script setup lang="ts">\nconst title = "订单"\n</script>\n',
                       encoding='utf-8')
        (self.source / 'main.cpp').write_text(
            '#include <string>\n\nint main() { return 0; }\n', encoding='utf-8')
        (self.source / 'client.js').write_text(
            'export function load() {\n  return fetch("/api");\n}\n', encoding='utf-8')
        (self.source / 'Program.cs').write_text(
            'class Program {\n    static void Main() { }\n}\n', encoding='utf-8')

        _, files, _ = discover(self.source)
        self.assertEqual([path.name for path in files],
                         ['Program.cs', 'client.js', 'main.cpp', 'project.md', 'App.vue'])
        blocks = parse_document(vue)
        self.assertEqual(blocks[0].line_start, 1)
        self.assertEqual(blocks[0].line_end, 3)
        self.assertIn('Panel', blocks[0].text)
        self.assertEqual(blocks[1].line_start, 5)
        self.assertEqual(blocks[1].line_end, 7)
        self.assertIn('订单', blocks[1].text)

    def test_docx_extracts_heading_and_text(self):
        docx = self.source / 'resume.docx'
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
               '<w:r><w:t>项目职责</w:t></w:r></w:p><w:p><w:r><w:t>本人负责订单查询。</w:t>'
               '</w:r></w:p></w:body></w:document>')
        with zipfile.ZipFile(docx, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        blocks = parse_document(docx)
        self.assertEqual(blocks[0].section, '项目职责')
        self.assertEqual(blocks[0].text, '本人负责订单查询。')

    def test_pdf_text_and_page_metadata(self):
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        pdf = self.source / 'document.pdf'
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b'BT /F1 12 Tf 10 200 Td (Order cache expires after 120 seconds.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(content)
        writer.write(pdf)
        blocks = parse_document(pdf)
        self.assertEqual(blocks[0].page, 1)
        self.assertIn('120 seconds', blocks[0].text)

    def test_blank_pdf_rejected_instead_of_silent_omission(self):
        pdf = self.source / 'scan.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(pdf)
        with self.assertRaisesRegex(ValueError, 'OCR'):
            parse_document(pdf)

    def test_keyword_identifier_and_fts_syntax_are_safe(self):
        self.build()
        result = search(self.package, 'event_id', self.encoder, mode='keyword')
        self.assertIn('提醒故障', result['matches'][0]['section'])
        search(self.package, '" OR NEAR(*) - ^', self.encoder, mode='keyword')


if __name__ == '__main__':
    unittest.main()
