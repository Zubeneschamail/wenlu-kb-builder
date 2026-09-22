"""Offline HTTP fixtures with real HTML extraction and real BGE package/retrieval."""
import gzip
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from kbtool.core import build, inspect_package, search
from kbtool.embedding import Cancelled, Encoder, sha256
from kbtool.web_sources import extract_page, fetch_documents, normalize_urls

HTML = '''<!doctype html><html><head><meta charset="utf-8"><title>通知系统设计</title></head>
<body><nav>导航广告 NAV_NOISE <a href="/login">登录</a></nav><main><article>
<h1>通知系统设计</h1><h2>消息可靠性<a class="headerlink" href="#message">¶</a></h2><p>数据库提交后消息没有发出，采用 outbox 同事务记录，
由后台补发并按 event_id 幂等处理。发生暂时故障时最多重试三次，之后转入人工处理队列，避免漏发。</p>
<h2>订单缓存</h2><p>订单详情使用 Redis 缓存，有效期为 120 秒。订单变更后清理对应缓存，
查询未命中时回源数据库并重新写入缓存，从而降低数据库压力并控制数据延迟。</p>
</article></main><footer>版权页脚 FOOTER_NOISE</footer><script>SECRET_SCRIPT</script></body></html>'''


class WebSourceTests(unittest.TestCase):
    def transport(self, handler):
        real_client = httpx.Client
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        return patch('kbtool.web_sources.httpx.Client', side_effect=client)

    def test_normalization_validation_and_limit(self):
        self.assertEqual(normalize_urls('HTTPS://EXAMPLE.COM:443/a#one\nhttps://example.com/a#two\n'),
                         ['https://example.com/a'])
        for url in ('file:///a', 'ftp://example.com', 'https://u:p@example.com', 'https://example.com:xyz',
                    'https://exa mple.com', 'https://example.com/\x00a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_urls([url])
        with self.assertRaisesRegex(ValueError, '50'):
            normalize_urls([f'https://example.com/{i}' for i in range(51)])

    def test_real_extraction_strips_chrome_keeps_body_and_chinese_encoding(self):
        for raw, kind in ((HTML.encode(), 'text/html'), (HTML.encode('gb18030'), 'text/html; charset=gb18030')):
            title, text = extract_page(raw, 'https://example.com/article', kind)
            self.assertEqual(title, '通知系统设计')
            self.assertIn('outbox', text)
            self.assertIn('120 秒', text)
            for noise in ('NAV_NOISE', 'FOOTER_NOISE', 'SECRET_SCRIPT', '¶'):
                self.assertNotIn(noise, text)

    def test_empty_dynamic_and_login_pages_rejected(self):
        for html in ('<html><body><div id="app"></div><script>load()</script></body></html>',
                     '<html><title>登录</title><form><input type="password"></form></html>'):
            with self.assertRaisesRegex(ValueError, '导入'):
                extract_page(html.encode(), 'https://example.com')

    def test_redirect_dedup_and_provenance(self):
        calls, log = [], []
        def handler(request):
            calls.append(str(request.url))
            if request.url.path == '/old':
                return httpx.Response(302, headers={'location': '/article'})
            return httpx.Response(200, text=HTML, headers={'content-type': 'text/html'})
        with self.transport(handler):
            docs = list(fetch_documents(['https://example.com/old#x', 'https://example.com/old#y'], progress=log.append))
        self.assertEqual(len(docs), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(docs[0].metadata['source_url'], 'https://example.com/old')
        self.assertEqual(docs[0].metadata['final_url'], 'https://example.com/article')
        self.assertNotIn(':', docs[0].source)
        self.assertIn('抓取完成 1/1', log[-1])

    def test_http_errors_retries_and_type(self):
        for code, kind, expected, count in ((403, 'text/html', 'HTTP 403', 1),
                                          (503, 'text/html', 'HTTP 503', 2),
                                          (200, 'application/pdf', 'HTML', 1)):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(code, content=b'bad', headers={'content-type': kind})
            with self.transport(handler), self.assertRaisesRegex(ValueError, expected):
                list(fetch_documents(['https://example.com'], progress=lambda _: None))
            self.assertEqual(len(calls), count)

    def test_timeout_is_actionable_and_retry_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('timed out', request=request)
        with self.transport(handler), self.assertRaisesRegex(ValueError, '网页连接失败或超时'):
            list(fetch_documents(['https://example.com'], progress=lambda _: None))
        self.assertEqual(len(calls), 2)

    def test_size_limit_including_compressed_response(self):
        for headers, content in (({'content-length': '5000001'}, b'x'),
                                 ({'content-encoding': 'gzip'}, gzip.compress(b'x' * 5_000_001))):
            with self.transport(lambda _: httpx.Response(200, content=content,
                    headers=dict(headers, **{'content-type': 'text/html'}))), self.assertRaisesRegex(ValueError, '5 MB'):
                list(fetch_documents(['https://example.com'], progress=lambda _: None))

    def test_redirect_loop_and_invalid_scheme_are_rejected(self):
        for location in ('/again', 'file:///secret'):
            with self.transport(lambda _: httpx.Response(302, headers={'location': location})), self.assertRaises(ValueError):
                list(fetch_documents(['https://example.com'], progress=lambda _: None))

    def test_cancel_while_reading(self):
        cancel = threading.Event()
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'x' * 65536
                cancel.set()
                yield b'y' * 65536
        with self.transport(lambda _: httpx.Response(200, stream=Stream(), headers={'content-type': 'text/html'})), \
                self.assertRaises(Cancelled):
            list(fetch_documents(['https://example.com'], cancel, progress=lambda _: None))


class WebPackageTests(unittest.TestCase):
    transport = WebSourceTests.transport
    @classmethod
    def setUpClass(cls):
        cls.encoder = Encoder()

    def test_web_build_search_refresh_and_failure_preserves_package(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'web.wlkb'
            body = HTML
            def handler(request):
                if request.url.path == '/missing':
                    return httpx.Response(404)
                return httpx.Response(200, text=body, headers={'content-type': 'text/html'})
            def generate(urls=None, **kwargs):
                return build(None, package, 'web-customer', self.encoder, urls=urls or ['https://example.com/article'],
                             progress=lambda _: None, **kwargs)
            with self.transport(handler):
                first = generate()
                self.assertEqual(first['documents'][0]['location_kind'], 'web_extracted_lines')
                self.assertIn('fetched_at', first['documents'][0])
                before = sha256(package)
                self.assertTrue(generate()['unchanged'])
                self.assertEqual(sha256(package), before)
                hit = search(package, '通知失败如何补发', self.encoder, top_k=1)['matches'][0]
                self.assertIn('outbox', hit['text'])
                self.assertEqual(hit['source_url'], 'https://example.com/article')
                with self.assertRaisesRegex(ValueError, '404'):
                    generate(['https://example.com/article', 'https://example.com/missing'])
                self.assertEqual(sha256(package), before)
                self.assertFalse(package.with_suffix('.wlkb.lock').exists())
                cancel = threading.Event()
                cancel.set()
                with self.assertRaises(Cancelled):
                    generate(cancel=cancel)
                self.assertEqual(sha256(package), before)
                body = HTML.replace('120 秒', '90 秒')
                updated = generate()
                self.assertEqual(updated['version'], 2)
                self.assertGreater(updated['reused_chunks'], 0)
                self.assertGreater(updated['new_vectors'], 0)
                hits = search(package, '订单缓存', self.encoder)['matches']
                self.assertTrue(any('90 秒' in item['text'] for item in hits))
                self.assertFalse(any('120 秒' in item['text'] for item in hits))
                self.assertEqual(inspect_package(package)['customer_id'], 'web-customer')

    def test_mixed_local_and_web_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'resume.md'
            source.write_text('# 简历\n\n本人负责后台通知服务的消息可靠性和补发机制。', encoding='utf-8')
            with self.transport(lambda _: httpx.Response(200, text=HTML, headers={'content-type': 'text/html'})):
                manifest = build(source, Path(directory) / 'mixed.wlkb', 'c', self.encoder,
                                 urls=['https://example.com/article'], progress=lambda _: None)
            self.assertEqual(len(manifest['documents']), 2)
            self.assertEqual(manifest['documents'][0]['source'], 'resume.md')
            self.assertEqual(manifest['documents'][1]['title'], '通知系统设计')

    def test_cli_url_list_and_invalid_input(self):
        from kbtool.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            listing = Path(directory) / 'urls.txt'
            listing.write_text('https://example.com/article\n\n', encoding='utf-8-sig')
            output = Path(directory) / 'cli.wlkb'
            args = ['kbtool', 'build', '--urls-file', str(listing), '--url', 'https://example.com/article#duplicate',
                    '--output', str(output), '--customer-id', 'cli-customer']
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.transport(lambda _: httpx.Response(200, text=HTML, headers={'content-type': 'text/html'})), \
                    patch('sys.argv', args), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(), 0)
            self.assertEqual(len(json.loads(stdout.getvalue())['documents']), 1)
            self.assertIn('抓取完成 1/1', stderr.getvalue())
            with patch('sys.argv', ['kbtool', 'build', '--output', str(output), '--customer-id', 'c']), \
                    patch('kbtool.__main__.Encoder', side_effect=AssertionError('must validate first')), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 1)
