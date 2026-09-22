"""Wenlu knowledge builder. Only the UI thread touches Tk widgets."""
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from . import ui
from .scrollbars import SlimScrollbar
from .core import build, search
from .web_sources import normalize_urls
from .embedding import Cancelled, DEFAULT_MODEL_DIR, Encoder, FILES, ROOT, prepare_model, sha256

OFFLINE_RELEASE = 'https://github.com/Zubeneschamail/wenlu-kb-builder/releases/tag/offline-model-bge-zh-v1'


class Application:
    def __init__(self, root, model_dir=DEFAULT_MODEL_DIR):
        self.root, self.model_dir = root, Path(model_dir)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = self.encoder = self.poll_timer = self.started = self.operation_kind = None
        self.busy = self.closed = False
        self.actions, self.controls = [], []
        self.source = tk.StringVar()
        self.source_mode = 'local'
        self.output = tk.StringVar(value=str(ROOT / 'output' / 'customer.wlkb'))
        self.customer = tk.StringVar(value='customer-001')
        self.name = tk.StringVar(value='客户知识库')
        self.query = tk.StringVar()
        self.status = tk.StringVar(value='选择资料后，开始生成知识包。')
        self.phase = tk.StringVar(value='准备就绪')
        self.progress_detail = tk.StringVar(value='')
        self.elapsed = tk.StringVar(value='')
        self.model_status = tk.StringVar()
        ui.setup(root)
        root.title('闻录 · 知识库')
        root.geometry(f'960x{min(760, root.winfo_screenheight() - 100)}')
        root.minsize(860, 680)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Destroy>', self.destroyed, add='+')
        assets = ROOT / 'assets'
        self.logo = tk.PhotoImage(file=str(assets / 'knowledge-24.png'))
        self.window_icon = tk.PhotoImage(file=str(assets / 'knowledge-256.png'))
        root.iconphoto(True, self.window_icon)
        if sys.platform == 'win32':
            root.iconbitmap(str(assets / 'knowledge.ico'))
        self.build_ui()
        self.refresh_model_status()
        self.poll_timer = root.after(80, self.poll)

    def build_ui(self):
        page = tk.Frame(self.root, bg=ui.BG, padx=20, pady=16)
        page.pack(fill='both', expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)
        header = tk.Frame(page, bg=ui.BG)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 14))
        tk.Label(header, image=self.logo, bg=ui.BG, bd=0).pack(side='left', padx=(0, 10))
        ui.label(header, '知识库', size=14, color=ui.TEXT, bold=True).pack(side='left')
        ui.label(header, '闻录', size=10, color=ui.MUTED).pack(side='right')

        form = self.card(page, row=1)
        source_header = tk.Frame(form, bg=ui.SURFACE)
        source_header.pack(fill='x')
        ui.label(source_header, '生成知识包', size=10, color=ui.TEXT, bold=True).pack(side='left')
        self.web_mode_button = self.button(source_header, '网页链接', lambda: self.select_source_mode('web'))
        self.web_mode_button.pack(side='right', padx=(6, 0))
        self.local_mode_button = self.button(source_header, '本地资料', lambda: self.select_source_mode('local'))
        self.local_mode_button.pack(side='right')
        for button, mode in ((self.local_mode_button, 'local'), (self.web_mode_button, 'web')):
            button.configure(pady=3)
            button.bind('<Leave>', lambda _, b=button, m=mode: b.configure(
                bg=ui.HOVER if self.source_mode == m else ui.BG))
        fields = tk.Frame(form, bg=ui.SURFACE)
        fields.pack(fill='x', pady=(12, 0))
        fields.columnconfigure(0, minsize=78)
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(2, minsize=168)
        ui.label(fields, '客户 ID', size=9).grid(row=0, column=0, sticky='w')
        metadata = tk.Frame(fields, bg=ui.SURFACE)
        metadata.grid(row=0, column=1, columnspan=2, sticky='ew', pady=(0, 8))
        metadata.columnconfigure(0, weight=1, uniform='metadata')
        metadata.columnconfigure(2, weight=1, uniform='metadata')
        self.customer_entry = self.entry(metadata, self.customer, width=14)
        self.customer_entry.grid(row=0, column=0, sticky='ew')
        ui.label(metadata, '知识库名称', size=9).grid(row=0, column=1, padx=(20, 12))
        self.entry(metadata, self.name, width=16).grid(row=0, column=2, sticky='ew')

        self.source_label = ui.label(fields, '资料位置', size=9)
        self.source_label.grid(row=1, column=0, sticky='w')
        self.source_entry = self.entry(fields, self.source)
        self.source_entry.grid(row=1, column=1, sticky='ew', pady=(0, 8))
        choices = tk.Frame(fields, bg=ui.SURFACE)
        choices.grid(row=1, column=2, sticky='nsew', padx=(8, 0), pady=(0, 8))
        choices.columnconfigure((0, 1), weight=1, uniform='choices')
        self.button(choices, '文件', self.pick_file).grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        self.button(choices, '文件夹', self.pick_folder).grid(row=0, column=1, sticky='nsew', padx=(4, 0))
        choices.rowconfigure(0, weight=1)
        self.local_choices = choices
        self.web_input = tk.Frame(fields, bg=ui.SURFACE)
        self.web_input.grid(row=1, column=1, columnspan=2, sticky='ew', pady=(0, 8))
        self.url_text = tk.Text(self.web_input, height=2, width=1, wrap='char',
                                font=(ui.FONT, 9), bg=ui.SURFACE, fg=ui.TEXT,
                                bd=0, highlightthickness=1, highlightbackground=ui.BORDER,
                                highlightcolor=ui.ACCENT, padx=9, pady=6, undo=True)
        self.url_text.pack(fill='x')
        self.url_scrollbar = SlimScrollbar(self.url_text, overlay_parent=self.web_input)
        self.controls.append(self.url_text)
        self.url_text.bind('<Tab>', lambda _: (self.output_entry.focus_set(), 'break')[1])
        ui.label(self.web_input, '每行一个网址，最多 50 个；动态页面自动通过浏览器加载。', size=9, color=ui.MUTED).pack(anchor='w', pady=(4, 0))
        self.web_input.grid_remove()
        self.local_mode_button.configure(bg=ui.HOVER, fg=ui.ACCENT)

        ui.label(fields, '保存位置', size=9).grid(row=2, column=0, sticky='w')
        self.output_entry = self.entry(fields, self.output)
        self.output_entry.grid(row=2, column=1, sticky='ew')
        self.button(fields, '选择位置', self.pick_output).grid(row=2, column=2, sticky='nsew', padx=(8, 0))

        actions = tk.Frame(form, bg=ui.SURFACE)
        actions.pack(fill='x', pady=(12, 0))
        self.generate_button = self.button(actions, '生成知识包', self.generate, primary=True)
        self.generate_button.pack(side='right')
        self.cancel_button = ui.button(actions, '取消', self.cancel_task)
        self.cancel_button.configure(state='disabled')
        self.cancel_button.pack(side='right', padx=(0, 8))
        self.model_label = ui.label(actions, variable=self.model_status, size=9, color=ui.MUTED)
        self.model_label.pack(side='left', padx=(0, 12))
        self.button(actions, '准备模型', self.prepare).pack(side='left')
        self.button(actions, '离线模型', lambda: webbrowser.open(OFFLINE_RELEASE)).pack(side='left', padx=(8, 0))

        self.spinner = ui.ProgressStrip(form)
        self.spinner.pack(fill='x', pady=(12, 6))
        progress = tk.Frame(form, bg=ui.SURFACE)
        progress.pack(fill='x')
        self.phase_label = ui.label(progress, variable=self.phase, size=9, color=ui.SECONDARY)
        self.phase_label.pack(side='left', padx=(0, 12))
        ui.label(progress, variable=self.elapsed, size=9, color=ui.MUTED).pack(side='right')
        ui.label(progress, variable=self.progress_detail, size=9, color=ui.ACCENT).pack(side='right', padx=(12, 12))
        ui.StatusLine(progress, self.status).pack(side='left', fill='x', expand=True)

        retrieval = self.card(page, row=2, top=12)
        retrieval_heading = tk.Frame(retrieval, bg=ui.SURFACE)
        retrieval_heading.pack(fill='x')
        ui.label(retrieval_heading, '检索验证', size=10, color=ui.TEXT, bold=True).pack(side='left')
        self.import_button = self.button(retrieval_heading, '一键导入闻录', self.import_to_wenlu)
        self.import_button.configure(pady=3)
        self.import_button.pack(side='right')
        query_row = tk.Frame(retrieval, bg=ui.SURFACE)
        query_row.pack(fill='x', pady=(12, 10))
        self.button(query_row, '打开知识包', self.pick_package).pack(side='right', padx=(8, 0))
        self.search_button = self.button(query_row, '检索', self.lookup, primary=True)
        self.search_button.pack(side='right', padx=(8, 0))
        self.query_entry = self.entry(query_row, self.query)
        self.query_entry.pack(side='left', fill='x', expand=True)
        self.query_entry.bind('<Return>', lambda _: self.lookup())
        self.tabs = ui.FlatTabs(retrieval)
        self.tabs.pack(fill='both', expand=True)
        self.result_page, self.results = self.text_page('检索结果')
        self.log_page, self.display = self.text_page('运行记录')
        self.set_results('输入问题，检索当前知识包。\n\n已有知识包可直接打开，无需源文件。', empty=True)

    def card(self, parent, row, top=0):
        border = tk.Frame(parent, bg=ui.SURFACE, bd=0, highlightbackground=ui.BORDER, highlightcolor=ui.BORDER, highlightthickness=1)
        border.grid(row=row, column=0, sticky='nsew', pady=(top, 0))
        content = tk.Frame(border, bg=ui.SURFACE, padx=16, pady=14)
        content.pack(fill='both', expand=True)
        return content

    def heading(self, parent, title, hint):
        row = tk.Frame(parent, bg=ui.SURFACE)
        row.pack(fill='x')
        ui.label(row, title, size=10, color=ui.TEXT, bold=True).pack(side='left')
        ui.label(row, hint, size=9, color=ui.MUTED).pack(side='right')

    def entry(self, parent, variable, **kwargs):
        entry = ttk.Entry(parent, textvariable=variable, style='WL.TEntry', **kwargs)
        self.controls.append(entry)
        return entry

    def button(self, parent, text, command, primary=False):
        button = ui.button(parent, text, command, primary)
        self.actions.append(button)
        return button

    def text_page(self, title):
        frame = tk.Frame(self.tabs.body, bg=ui.SURFACE, bd=0, highlightthickness=0)
        text = tk.Text(frame, wrap='word', font=(ui.FONT, 10), bg=ui.SURFACE, fg=ui.SECONDARY,
                       bd=0, highlightthickness=0, padx=10, pady=12, height=5, spacing1=0, spacing3=2,
                       selectbackground=ui.HOVER, selectforeground=ui.TEXT, state='disabled')
        text.pack(side='left', fill='both', expand=True)
        scrollbar = SlimScrollbar(text, overlay_parent=frame)
        text._scrollbar = scrollbar
        text.tag_configure('muted', foreground=ui.MUTED)
        text.tag_configure('heading', foreground=ui.ACCENT, font=(ui.FONT, 10, 'bold'))
        self.tabs.add(frame, text=title)
        return frame, text

    def append(self, text):
        self.display.configure(state='normal')
        self.display.insert('end', text + '\n')
        lines = int(self.display.index('end-1c').split('.')[0])
        if lines > 1200:
            self.display.delete('1.0', f'{lines - 1000}.0')
        self.display.see('end')
        self.display.configure(state='disabled')

    def set_results(self, text, empty=False):
        self.results.configure(state='normal')
        self.results.delete('1.0', 'end')
        for line in text.splitlines(keepends=True):
            tag = 'muted' if empty or line.startswith('找到 ') else 'heading' if re.match(r'^\[\d+\]', line) else ''
            self.results.insert('end', line, tag)
        self.results.configure(state='disabled')
        self.results.yview_moveto(0)

    def refresh_model_status(self):
        try:
            ready = all((self.model_dir / name).is_file() and sha256(self.model_dir / name) == expected
                        for name, expected in FILES.items())
        except OSError:
            ready = False
        self.model_status.set('本地模型就绪' if ready else '模型未就绪')
        self.model_label.configure(fg='#15803d' if ready else ui.MUTED)

    def pick_file(self):
        value = filedialog.askopenfilename(parent=self.root, title='选择资料文件', filetypes=[('支持的资料', '*.txt *.md *.rst *.pdf *.docx')])
        if value:
            self.source.set(value)

    def select_source_mode(self, mode):
        if self.busy:
            return
        self.source_mode = mode
        web = mode == 'web'
        self.source_label.configure(text='网页链接' if web else '资料位置')
        if web:
            self.source_entry.grid_remove()
            self.local_choices.grid_remove()
            self.web_input.grid()
            self.root.minsize(860, 740)
            self.url_text.focus_set()
        else:
            self.web_input.grid_remove()
            self.source_entry.grid()
            self.local_choices.grid()
            self.root.minsize(860, 680)
            self.source_entry.focus_set()
        for button, selected in ((self.local_mode_button, not web), (self.web_mode_button, web)):
            button.configure(bg=ui.HOVER if selected else ui.BG, fg=ui.ACCENT if selected else ui.MUTED)
        self.status.set('粘贴公开网页链接后生成知识包；再次生成会重新抓取。' if web else '选择资料后，开始生成知识包。')

    def pick_folder(self):
        value = filedialog.askdirectory(parent=self.root, title='选择资料文件夹')
        if value:
            self.source.set(value)

    def pick_output(self):
        value = filedialog.asksaveasfilename(parent=self.root, title='保存知识包', defaultextension='.wlkb', filetypes=[('闻录知识包', '*.wlkb')])
        if value:
            self.output.set(value)

    def pick_package(self):
        value = filedialog.askopenfilename(parent=self.root, title='打开知识包', filetypes=[('闻录知识包', '*.wlkb')])
        if value:
            self.output.set(value)
            self.status.set(f'已选择 {Path(value).name}，输入问题即可检索。')
            self.set_results('已切换知识包，请重新检索。', empty=True)
            self.tabs.select(self.result_page)
            self.query_entry.focus_set()

    def run(self, operation, title='正在处理', kind='build'):
        if self.busy:
            return
        self.busy = True
        self.operation_kind = kind
        self.cancel = threading.Event()
        self.started = time.monotonic()
        self.elapsed.set('已用时 0 秒')
        for control in self.actions + self.controls:
            control.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        if kind == 'import':
            # The receiver may already have committed; cancellation cannot roll it back.
            self.cancel_button.configure(state='disabled')
        self.set_progress(title)
        self.status.set(title + '…')
        self.append('\n' + time.strftime('%H:%M:%S') + '  ' + title)
        if kind == 'search':
            self.set_results('正在检索当前知识包…', empty=True)
            self.tabs.select(self.result_page)
        else:
            self.tabs.select(self.log_page)
        def work():
            try:
                self.events.put(('result', operation()))
            except Cancelled as exc:
                self.events.put(('cancelled', str(exc)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('done', None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def set_progress(self, phase, value=None, detail=None, state='running'):
        self.phase.set(phase)
        self.spinner.set(state, value)
        self.progress_detail.set(detail if detail is not None else f'{value:.0%}' if value is not None else '处理中')
        self.phase_label.configure(fg={'error': '#b91c1c', 'cancelled': '#b45309'}.get(state, ui.TEXT))

    def update_progress(self, text):
        if self.cancel.is_set():
            return
        match = re.search(r'^(解析完成|抓取完成|向量化：)\s*(\d+)/(\d+)', text)
        download = re.search(r'：([\d.]+) MB / ([\d.]+) MB', text)
        if match:
            done, total = int(match[2]), int(match[3])
            phase = {'解析完成': '解析资料', '抓取完成': '抓取网页', '向量化：': '生成向量'}[match[1]]
            self.set_progress(phase, done / total if total else 0, f'{done} / {total} · {done / max(total, 1):.0%}')
        elif download:
            done, total = float(download[1]), float(download[2])
            self.set_progress('下载模型', done / total if total else None, f'{done:g} / {total:g} MB')
        elif text.startswith('下载模型文件'):
            self.set_progress('下载模型')
        elif text.startswith('正在渲染网页'):
            self.set_progress('加载动态网页', detail='等待网页正文')
        elif text.startswith('正在抓取') and self.phase.get() != '抓取网页':
            self.set_progress('抓取网页', 0, '正在连接网页')
        elif text.startswith('正在解析') and self.phase.get() != '解析资料':
            self.set_progress('解析资料', 0, '正在读取资料')
        elif text.startswith('已校验'):
            self.set_progress('校验模型')
        elif text.startswith('共 '):
            self.set_progress('生成向量')
        elif text.startswith('加载'):
            self.set_progress('加载模型')
        elif text.startswith('写入'):
            self.set_progress('写入知识包')

    def log(self, text):
        self.events.put(('log', text))

    def get_encoder(self):
        if self.encoder is None:
            self.log('加载本地向量模型…')
            self.encoder = Encoder(self.model_dir)
        return self.encoder

    def prepare(self):
        def operation():
            prepare_model(self.model_dir, self.log, self.cancel)
            self.encoder = None
            return '模型校验完成，可以生成知识包。'
        self.run(operation, '准备模型', 'model')

    def generate(self):
        if self.busy:
            return
        source, output, customer, name = self.source.get().strip(), self.output.get().strip(), self.customer.get().strip(), self.name.get().strip()
        urls = []
        if self.source_mode == 'web':
            source = None
            try:
                urls = normalize_urls(self.url_text.get('1.0', 'end'))
            except ValueError as exc:
                messagebox.showerror('网页地址有误', str(exc), parent=self.root)
                return
        if (not source and not urls) or not output or not customer or not name:
            messagebox.showerror('信息不完整', '请填写客户 ID、名称、资料和输出位置。', parent=self.root)
            return
        if Path(output).suffix.lower() != '.wlkb':
            messagebox.showerror('知识包格式错误', '输出知识包的后缀必须为 .wlkb。', parent=self.root)
            return
        def operation():
            result = build(source, output, customer, self.get_encoder(), name=name, cancel=self.cancel, progress=self.log, urls=urls)
            state = '内容未变化，保留原知识包' if result.get('unchanged') else '知识包已生成'
            return f'{state} · {result["chunk_count"]} 个片段 · 版本 {result["version"]}\n{output}'
        self.run(operation, '生成知识包', 'build')

    def lookup(self):
        if self.busy:
            return
        package, query = self.output.get().strip(), self.query.get().strip()
        if not package or not query:
            messagebox.showerror('信息不完整', '请选择知识包并输入检索问题。', parent=self.root)
            return
        path = Path(package)
        if path.suffix.lower() == '.wldb' and path.with_suffix('.wlkb').is_file():
            path = path.with_suffix('.wlkb')
            self.output.set(str(path))
            self.append(f'知识包后缀已纠正为 .wlkb：{path}')
        if path.suffix.lower() != '.wlkb':
            messagebox.showerror('知识包格式错误', '请选择 .wlkb 知识包，可点击「打开知识包」重新选择。', parent=self.root)
            return
        if not path.is_file():
            messagebox.showerror('知识包不存在', f'找不到知识包：{path}\n请点击「打开知识包」选择已生成的 .wlkb 文件。', parent=self.root)
            return
        package = str(path)
        def operation():
            result = search(package, query, self.get_encoder(), cancel=self.cancel)
            sections = [f'找到 {len(result["matches"])} 个相关片段 · 客户 {result["customer_id"]}']
            for i, item in enumerate(result['matches'], 1):
                location = f'第 {item["page"]} 页，' if item['page'] else ''
                source = item.get('source_url', item['source'])
                snapshot = f'\n抓取时间：{item["fetched_at"]}' if item.get('fetched_at') else ''
                sections.append(f'[{i}] {item["section"]}\n{source} · {location}行 {item["line_start"]}—{item["line_end"]}{snapshot}\n{item["text"]}')
            return '\n\n'.join(sections) + '\n\n' + result['note']
        self.run(operation, '检索知识包', 'search')

    def import_to_wenlu(self):
        if self.busy:
            return
        path = Path(self.output.get().strip())
        if path.suffix.lower() != '.wlkb' or not path.is_file():
            messagebox.showerror('知识包未就绪', '请先生成知识包，或点击「打开知识包」选择已有 .wlkb 文件。', parent=self.root)
            return
        def operation():
            from .wenlu_import import import_package
            result = import_package(path, progress=self.log)
            return f'已导入闻录：{result["name"]}\n知识包已复制到闻录资料目录，并启用参考资料。\n{result["path"]}'
        self.run(operation, '导入闻录', 'import')

    def cancel_task(self):
        if self.busy:
            self.cancel.set()
            self.cancel_button.configure(state='disabled')
            self.set_progress('正在取消', detail='等待当前步骤结束')
            self.status.set('正在停止任务，请稍候。')

    def poll(self):
        self.poll_timer = None
        if self.closed:
            return
        if self.busy and self.started is not None:
            self.elapsed.set(f'已用时 {time.monotonic() - self.started:.0f} 秒')
        try:
            for _ in range(100):
                event, value = self.events.get_nowait()
                if event == 'done':
                    self.busy = False
                    self.spinner.stop()
                    self.cancel_button.configure(state='disabled')
                    for control in self.actions + self.controls:
                        control.configure(state='normal')
                    if self.operation_kind == 'model':
                        self.refresh_model_status()
                elif event == 'log':
                    self.status.set(value)
                    self.update_progress(value)
                    self.append(value)
                elif event in ('error', 'cancelled'):
                    title = '任务失败' if event == 'error' else '任务已取消'
                    self.set_progress(title, self.spinner.value, detail='未完成', state=event)
                    self.status.set('模型下载超时，可使用「离线模型包」。' if self.operation_kind == 'model' and 'timed out' in value.lower() else value)
                    self.append(('错误：' if event == 'error' else '已取消：') + value)
                    if self.operation_kind == 'search':
                        self.set_results(title + '，请查看运行记录。', empty=True)
                    self.tabs.select(self.log_page)
                elif event == 'result':
                    self.set_progress('检索完成' if self.operation_kind == 'search' else '任务完成', 1, state='success')
                    if self.operation_kind == 'search':
                        self.set_results(value)
                        self.status.set('已返回匹配片段，来源见下方检索结果。')
                        self.append('检索完成。')
                        self.tabs.select(self.result_page)
                    else:
                        self.status.set(value.splitlines()[0])
                        self.append(value)
        except queue.Empty:
            pass
        self.poll_timer = self.root.after(80, self.poll)

    def destroyed(self, event):
        if event.widget is self.root:
            self.closed = True
            self.cancel.set()
            if self.poll_timer is not None:
                self.root.after_cancel(self.poll_timer)
                self.poll_timer = None

    def close(self):
        if self.busy:
            if self.operation_kind == 'import':
                self.status.set('正在等待闻录确认，请导入结束后关闭窗口。')
                return
            self.cancel_task()
            self.status.set('正在取消，请任务结束后关闭窗口。')
            return
        self.root.destroy()


def launch(model_dir=DEFAULT_MODEL_DIR):
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Wenlu.KnowledgeBuilder')
    root = tk.Tk()
    Application(root, model_dir)
    root.mainloop()
