"""Explicit URL ingestion with automatic fallback for public JavaScript pages."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .embedding import check_cancel

MAX_URLS = 50
MAX_BYTES = 5_000_000
MAX_TOTAL_BYTES = 100_000_000
MAX_SECONDS = 60
USER_AGENT = 'WenluKnowledgeBuilder/1.0 (webpage import)'


class NoPageContent(ValueError):
    """Only missing content may trigger rendering; access/size errors must not."""


def normalize_url(value):
    value = value.strip()
    if not value or len(value) > 2048 or re.search(r'[\s\x00-\x1f\x7f]', value):
        raise ValueError('网页地址为空、过长或含空白字符，请每行填写一个完整网址。')
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {'http', 'https'} or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None:
            raise ValueError()
        port = parts.port
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        if port and (parts.scheme.lower(), port) not in {('http', 80), ('https', 443)}:
            host += ':' + str(port)
        # Keep SPA hash routes (Docsify/Vue); ordinary #heading anchors share one page.
        fragment = parts.fragment if parts.fragment.startswith(('/', '!/')) else ''
        return urlunsplit((parts.scheme.lower(), host, parts.path or '/', parts.query, fragment))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('请使用完整的 http:// 或 https:// 网址，不支持带账号密码的链接。') from exc


def normalize_urls(values):
    if isinstance(values, str):
        values = values.splitlines()
    result = list(dict.fromkeys(normalize_url(value) for value in (values or []) if value.strip()))
    if len(result) > MAX_URLS:
        raise ValueError(f'每次最多导入 {MAX_URLS} 个不同网页，请分批生成知识包。')
    return result


@dataclass
class WebDocument:
    source: str
    text: str
    metadata: dict


def extract_page(raw, url, content_type=''):
    # Loaded lazily so existing local-only installations can still open the GUI.
    try:
        import trafilatura
        from trafilatura.utils import load_html
    except ImportError as exc:
        raise ValueError('网页功能依赖尚未安装，请关闭工具并运行 setup.cmd 后重试。') from exc
    # Honor declared legacy encodings; otherwise the extractor detects meta charset/BOM.
    match = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
    if match:
        try:
            raw = raw.decode(match[1])
        except (LookupError, UnicodeError):
            pass
    tree = load_html(raw)
    if tree is None:
        raise ValueError('无法解析网页 HTML。')
    titles = tree.xpath('//title/text() | //h1//text()')
    title = (re.sub(r'\s+', ' ', titles[0]).strip()[:200] if titles else '') or urlsplit(url).hostname
    if tree.xpath('//input[translate(@type,"PASSWORD","password")="password"]') or re.search(
            r'^(just a moment|access denied|sign in|log in|登录|安全验证|人机验证)', title, re.I):
        raise ValueError('网页需要登录或安全验证，请在浏览器中导出正文后作为本地资料导入。')
    text = trafilatura.extract(
        tree, url=url, output_format='markdown', include_comments=False,
        include_tables=True, include_links=False, include_images=False,
        include_formatting=True, favor_precision=True,
        prune_xpath=['//nav', '//header', '//footer', '//aside', '//form', '//script', '//style', '//noscript',
                     '//a[contains(concat(" ", normalize-space(@class), " "), " headerlink ") or '
                     'contains(concat(" ", normalize-space(@class), " "), " toc-anchor ")]'])
    if not text or len(re.sub(r'\s', '', text)) < 40:
        raise NoPageContent('未提取到足够正文；网页可能依赖 JavaScript、需要登录或仅含图片。请导出 TXT、Markdown 或文字 PDF 后导入。')
    if len(text) > 2_000_000:
        raise ValueError('网页提取正文超过 200 万字符，请拆分资料。')
    return title, '# ' + title + '\n\n' + text.strip() + '\n'


def _download(client, url, cancel, deadline):
    current = url
    for _ in range(6):
        check_cancel(cancel)
        if time.monotonic() >= deadline:
            raise ValueError('抓取网页超过 60 秒，请稍后重试。')
        with client.stream('GET', current) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get('location')
                if not location:
                    raise ValueError('网页重定向缺少目标地址。')
                current = normalize_url(str(response.url.join(location)))
                continue
            response.raise_for_status()
            kind = response.headers.get('content-type', '')
            if kind.split(';', 1)[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}:
                raise ValueError('链接未返回 HTML 网页；PDF 等文件请先下载，再通过本地资料导入。')
            length = response.headers.get('content-length', '')
            if length.isdigit() and int(length) > MAX_BYTES:
                raise ValueError('网页超过 5 MB，请导出正文后导入。')
            data = bytearray()
            for part in response.iter_bytes():
                check_cancel(cancel)
                if time.monotonic() >= deadline:
                    raise ValueError('抓取网页超过 60 秒，请稍后重试。')
                data.extend(part)
                if len(data) > MAX_BYTES:
                    raise ValueError('网页超过 5 MB，请导出正文后导入。')
            return bytes(data), current, kind
    raise ValueError('网页重定向超过 5 次，请使用最终页面地址。')


def fetch_documents(urls, cancel=None, progress=print):
    urls = normalize_urls(urls)
    total_bytes = 0
    with httpx.Client(timeout=httpx.Timeout(10, connect=10), follow_redirects=False,
                      headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml'}) as client:
        for index, url in enumerate(urls, 1):
            check_cancel(cancel)
            progress(f'正在抓取 {index}/{len(urls)}：{url}')
            deadline = time.monotonic() + MAX_SECONDS
            for attempt in range(2):
                try:
                    raw, final_url, kind = _download(client, url, cancel, deadline)
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if attempt == 0 and code in {502, 503, 504}:
                        progress(f'网页暂时不可用，重试：{url}')
                        continue
                    raise ValueError(f'网页抓取失败（HTTP {code}）：{url}；请检查页面是否公开可访问。') from exc
                except httpx.TransportError as exc:
                    check_cancel(cancel)
                    if attempt == 0:
                        progress(f'网页连接失败，重试：{url}')
                        continue
                    raise ValueError(f'网页连接失败或超时：{url}；请检查网络或导出正文后导入。') from exc
            page_bytes = len(raw)
            check_cancel(cancel)
            method = 'http'
            try:
                try:
                    if urlsplit(final_url).fragment.startswith(('/', '!/')):
                        raise NoPageContent('需要渲染页面路由。')
                    title, text = extract_page(raw, final_url, kind)
                except NoPageContent:
                    from .browser_sources import render_document
                    progress('静态页面没有正文，切换浏览器加载。')
                    title, text, final_url, rendered_bytes = render_document(final_url, cancel, progress)
                    page_bytes += rendered_bytes
                    method = 'browser'
            except ValueError as exc:
                raise ValueError(f'{url}\n{exc}') from exc
            total_bytes += page_bytes
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError('本批网页正文累计超过 100 MB，请分批导入。')
            check_cancel(cancel)
            source = 'web/' + hashlib.sha256(url.encode('utf-8')).hexdigest()[:24] + '.md'
            metadata = {'source_url': url, 'final_url': final_url, 'title': title,
                        'fetched_at': datetime.now(timezone.utc).isoformat(),
                        'location_kind': 'web_extracted_lines', 'extractor': 'trafilatura-markdown-v1',
                        'fetch_method': method}
            progress(f'抓取完成 {index}/{len(urls)}：{title}')
            yield WebDocument(source, text, metadata)
