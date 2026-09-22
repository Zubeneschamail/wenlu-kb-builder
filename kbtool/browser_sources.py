"""Render public JavaScript pages in a temporary, headless browser profile."""
import time

from .embedding import check_cancel

RENDER_SECONDS = 45
SETTLE_SECONDS = 2


def launch_browser(playwright, cancel=None):
    from playwright.sync_api import Error
    for channel in ('msedge', 'chrome', None):
        check_cancel(cancel)
        try:
            return playwright.chromium.launch(headless=True, channel=channel, timeout=10000)
        except Error:
            continue
    raise ValueError('动态网页需要浏览器：未能启动 Edge、Chrome 或 Chromium。请安装 Edge/Chrome，'
                     '或在工具目录运行 .venv\\Scripts\\python.exe -m playwright install chromium 后重试。')


def render_document(url, cancel=None, progress=print):
    from .web_sources import MAX_BYTES, NoPageContent, extract_page, normalize_url
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as exc:
        raise ValueError('动态网页依赖尚未安装，请关闭工具并运行 setup.cmd 后重试。') from exc
    check_cancel(cancel)
    progress('正在渲染网页：' + url)
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright, cancel)
            try:
                check_cancel(cancel)
                context = browser.new_context(locale='zh-CN', accept_downloads=False, service_workers='block')
                pending = set()
                requests = 0

                def route_request(route):
                    nonlocal requests
                    request = route.request
                    if cancel is not None and cancel.is_set():
                        route.abort()
                        return
                    # No media is needed for text extraction. Scripts/XHR still run normally.
                    if request.resource_type in {'image', 'media', 'font'} or 'prefetch' in (
                            request.headers.get('purpose', '') + request.headers.get('sec-purpose', '')):
                        route.abort()
                        return
                    requests += 1
                    if requests > 250:
                        route.abort()
                        return
                    try:
                        normalize_url(request.url)
                    except ValueError:
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', route_request)
                page = context.new_page()
                context.on('page', lambda other: other.close() if other != page else None)
                page.on('dialog', lambda dialog: dialog.dismiss())
                page.on('request', lambda request: pending.add(request) if request.resource_type in {'script', 'xhr', 'fetch'} else None)
                page.on('requestfinished', lambda request: pending.discard(request))
                page.on('requestfailed', lambda request: pending.discard(request))
                page.set_default_timeout(5000)
                response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
                check_cancel(cancel)
                if response and response.status >= 400:
                    raise ValueError(f'动态网页返回 HTTP {response.status}，请检查页面是否公开可访问。')
                started = time.monotonic()
                deadline = started + RENDER_SECONDS
                previous = None
                stable_since = started
                while time.monotonic() < deadline:
                    check_cancel(cancel)
                    if requests > 250:
                        raise ValueError('动态网页请求过多，请导出正文后导入。')
                    # A content hash and settled script/data requests avoid capturing a loading shell.
                    raw = page.content().encode('utf-8')
                    if len(raw) > MAX_BYTES:
                        raise ValueError('动态网页正文 HTML 超过 5 MB，请导出正文后导入。')
                    final_url = normalize_url(page.url)
                    try:
                        title, text = extract_page(raw, final_url, 'text/html; charset=utf-8')
                    except NoPageContent:
                        previous = None
                        stable_since = time.monotonic()
                    else:
                        snapshot = (title, text, final_url)
                        if snapshot != previous or pending:
                            previous = snapshot
                            stable_since = time.monotonic()
                        elif time.monotonic() - stable_since >= SETTLE_SECONDS:
                            check_cancel(cancel)
                            return title, text, final_url, len(raw)
                    page.wait_for_timeout(250)
                raise ValueError('动态网页正文未能在限定时间内加载稳定；可能需要登录、验证码或访问失败，请导出正文后导入。')
            finally:
                browser.close()
    except TimeoutError as exc:
        check_cancel(cancel)
        raise ValueError('动态网页加载超时，请检查网络或导出正文后导入。') from exc
    except Error as exc:
        check_cancel(cancel)
        raise ValueError('动态网页浏览器加载失败，请检查网络和浏览器是否可用；详情：' + str(exc)[:300]) from exc
