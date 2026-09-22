"""Real headless-browser regressions on a local JavaScript documentation fixture."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx

from kbtool.browser_sources import launch_browser, render_document
from kbtool.core import build
from kbtool.embedding import Cancelled, Encoder, sha256
from kbtool.web_sources import fetch_documents, normalize_urls
import test_web_sources as web_fixtures

HTML = web_fixtures.HTML
BODY = HTML.split('<main>', 1)[1].split('</main>', 1)[0]
SHELL = '''<html><head><title>Loading</title></head><body><div id="app"></div>
<script>setTimeout(() => fetch('/content').then(r => r.json()).then(data => {
document.querySelector('#app').innerHTML = '<main>' + data.body + '</main>';
document.title = location.hash ? '路由文档' : '动态文档';
}), 400);</script></body></html>'''


@contextmanager
def fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/content':
                content = json.dumps({'body': BODY}, ensure_ascii=False).encode('utf-8')
                kind = 'application/json'
            else:
                content = (SHELL if self.path == '/' else '<html><body><div id="app"></div></body></html>').encode('utf-8')
                kind = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class BrowserSourceTests(unittest.TestCase):
    def test_automatic_fallback_waits_for_javascript_and_keeps_hash_route(self):
        with fixture_server() as url:
            docs = list(fetch_documents([url + '#/editor'], progress=lambda _: None))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].metadata['fetch_method'], 'browser')
        self.assertTrue(docs[0].metadata['final_url'].endswith('#/editor'))
        self.assertEqual(docs[0].metadata['title'], '路由文档')
        self.assertIn('outbox', docs[0].text)
        self.assertNotIn('Loading', docs[0].text)
        self.assertEqual(len(normalize_urls([url + '#/one', url + '#/two', url + '#heading'])), 3)

    def test_renderer_timeout_closes_browser(self):
        browsers = []
        def launch(*args, **kwargs):
            browser = launch_browser(*args, **kwargs)
            browsers.append(browser)
            return browser
        with fixture_server() as url, patch('kbtool.browser_sources.launch_browser', side_effect=launch), \
                patch('kbtool.browser_sources.RENDER_SECONDS', .5):
            with self.assertRaisesRegex(ValueError, '限定时间'):
                render_document(url + 'empty', progress=lambda _: None)
        self.assertTrue(browsers)
        self.assertTrue(all(not browser.is_connected() for browser in browsers))

    def test_renderer_cancellation_closes_browser(self):
        cancel = threading.Event()
        browsers = []
        def launch(*args, **kwargs):
            browser = launch_browser(*args, **kwargs)
            browsers.append(browser)
            cancel.set()
            return browser
        with fixture_server() as url, patch('kbtool.browser_sources.launch_browser', side_effect=launch):
            with self.assertRaises(Cancelled):
                render_document(url, cancel, progress=lambda _: None)
        self.assertTrue(all(not browser.is_connected() for browser in browsers))

    def test_browser_unavailable_has_install_instruction(self):
        from playwright.sync_api import Error
        playwright = MagicMock()
        playwright.chromium.launch.side_effect = Error('Not installed')
        with self.assertRaisesRegex(ValueError, 'playwright install chromium'):
            launch_browser(playwright)
        self.assertEqual(playwright.chromium.launch.call_count, 3)

    def test_static_and_login_pages_never_launch_browser(self):
        for html, allowed in ((HTML, True), ('<html><title>登录</title><input type="password"></html>', False)):
            with web_fixtures.WebSourceTests().transport(lambda _: httpx.Response(200, text=html, headers={'content-type': 'text/html'})), \
                    patch('kbtool.browser_sources.render_document') as render:
                if allowed:
                    docs = list(fetch_documents(['https://example.com'], progress=lambda _: None))
                    self.assertEqual(docs[0].metadata['fetch_method'], 'http')
                else:
                    with self.assertRaisesRegex(ValueError, '登录'):
                        list(fetch_documents(['https://example.com'], progress=lambda _: None))
                render.assert_not_called()

    def test_render_failure_preserves_old_package(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            doc = path / 'input.md'
            doc.write_text('# 原始资料\n\n订单缓存有效期为两分钟，后台消息重试三次。', encoding='utf-8')
            package = path / 'old.wlkb'
            encoder = Encoder()
            build(doc, package, 'c', encoder, progress=lambda _: None)
            before = sha256(package)
            with web_fixtures.WebSourceTests().transport(lambda _: httpx.Response(200, text=SHELL, headers={'content-type': 'text/html'})), \
                    patch('kbtool.browser_sources.render_document', side_effect=ValueError('动态网页加载超时')):
                with self.assertRaisesRegex(ValueError, '动态网页加载超时'):
                    build(None, package, 'c', encoder, urls=['https://example.com'], progress=lambda _: None)
            self.assertEqual(sha256(package), before)
            self.assertFalse(package.with_suffix('.wlkb.lock').exists())
