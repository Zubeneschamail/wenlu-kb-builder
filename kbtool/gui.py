"""Tk desktop front end; workers never access Tk widgets."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .core import build, search
from .embedding import Cancelled, DEFAULT_MODEL_DIR, Encoder, ROOT, prepare_model


class Application:
    def __init__(self, root, model_dir=DEFAULT_MODEL_DIR):
        self.root, self.model_dir = root, model_dir
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.encoder = None
        root.title('闻录 · 独立知识库生成工具')
        root.geometry('920x760')
        root.minsize(760, 620)
        root.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='客户知识库生成工具', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='本地解析与向量化 · 原文可追溯 · 生成包后可直接试搜').pack(anchor='w', pady=(4, 12))
        self.source = tk.StringVar()
        self.output = tk.StringVar(value=str(ROOT / 'output' / 'customer.wlkb'))
        self.customer = tk.StringVar(value='customer-001')
        self.name = tk.StringVar(value='客户项目知识库')
        self.query = tk.StringVar()
        self.status = tk.StringVar(value='首次使用请准备模型；模型就绪后可离线构建。')
        self.actions = []
        self.controls = []
        form = ttk.Frame(frame)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate([('客户 ID', self.customer), ('知识库名称', self.name),
                                                ('资料文件 / 文件夹', self.source), ('输出知识包', self.output)]):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 10))
            entry = ttk.Entry(form, textvariable=variable)
            entry.grid(row=row, column=1, sticky='ew', pady=5)
            self.controls.append(entry)
        pickers = ttk.Frame(form)
        pickers.grid(row=2, column=2, padx=(8, 0))
        self.button(pickers, '选文件', self.pick_file).pack(side='left')
        self.button(pickers, '选文件夹', self.pick_folder).pack(side='left', padx=(4, 0))
        self.button(form, '选择位置', self.pick_output).grid(row=3, column=2, padx=(8, 0))
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=12)
        self.button(bar, '① 准备模型', self.prepare).pack(side='left')
        self.button(bar, '② 生成知识包', self.generate).pack(side='left', padx=8)
        self.button(bar, '填入虚构样例', self.demo).pack(side='left')
        self.cancel_button = ttk.Button(bar, text='取消任务', command=self.cancel.set, state='disabled')
        self.cancel_button.pack(side='right')
        self.spinner = ttk.Progressbar(frame, mode='indeterminate')
        self.spinner.pack(fill='x')
        ttk.Label(frame, textvariable=self.status, wraplength=820).pack(anchor='w', pady=(4, 10))
        querybar = ttk.Frame(frame)
        querybar.pack(fill='x')
        query_entry = ttk.Entry(querybar, textvariable=self.query)
        query_entry.pack(side='left', fill='x', expand=True)
        self.controls.append(query_entry)
        query_entry.bind('<Return>', lambda _: self.lookup())
        self.button(querybar, '③ 检索验证', self.lookup).pack(side='left', padx=(8, 0))
        self.button(querybar, '打开已有包', self.pick_package).pack(side='left', padx=(6, 0))
        self.display = ScrolledText(frame, wrap='word', font=('Microsoft YaHei UI', 10), state='disabled')
        self.display.pack(fill='both', expand=True, pady=(10, 0))
        self.append('只选择资料目录。evaluation、tests、隐藏目录和构建输出目录默认跳过。\n'
                    '知识包包含原文，不含 API Key；此工具不生成虚构个人经历，也不调用在线问答模型。\n')
        root.after(100, self.poll)

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        self.actions.append(button)
        return button

    def append(self, text):
        self.display.configure(state='normal')
        self.display.insert('end', text + '\n')
        self.display.see('end')
        self.display.configure(state='disabled')

    def pick_file(self):
        value = filedialog.askopenfilename(filetypes=[('支持的资料', '*.txt *.md *.rst *.pdf *.docx')])
        if value:
            self.source.set(value)

    def pick_folder(self):
        value = filedialog.askdirectory()
        if value:
            self.source.set(value)

    def pick_output(self):
        value = filedialog.asksaveasfilename(defaultextension='.wlkb', filetypes=[('知识包', '*.wlkb')])
        if value:
            self.output.set(value)

    def pick_package(self):
        value = filedialog.askopenfilename(filetypes=[('知识包', '*.wlkb')])
        if value:
            self.output.set(value)

    def demo(self):
        self.source.set(str(ROOT / 'examples' / 'linzhou' / 'source'))
        self.output.set(str(ROOT / 'output' / 'linzhou-demo.wlkb'))
        self.customer.set('demo-linzhou')
        self.name.set('林舟 · 虚构测试资料')
        self.query.set('数据库已经更新成功，但是通知没有发出去，怎么解决？')

    def run(self, operation):
        if self.worker and self.worker.is_alive():
            return
        self.cancel = threading.Event()
        for control in self.actions + self.controls:
            control.configure(state='disabled')
        self.cancel_button.configure(state='normal')
        self.spinner.start()
        self.status.set('正在处理…')
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
            return '模型已就绪。'
        self.run(operation)

    def generate(self):
        source, output, customer, name = self.source.get().strip(), self.output.get().strip(), self.customer.get().strip(), self.name.get().strip()
        if not source or not output or not customer or not name:
            messagebox.showerror('信息不完整', '请填写客户 ID、名称、资料和输出位置。')
            return
        def operation():
            result = build(source, output, customer, self.get_encoder(), name=name,
                           cancel=self.cancel, progress=self.log)
            return f'知识包就绪：{result["chunk_count"]} 个片段，版本 {result["version"]}。\n输出：{output}'
        self.run(operation)

    def lookup(self):
        package, query = self.output.get().strip(), self.query.get().strip()
        if not package or not query:
            messagebox.showerror('信息不完整', '请选择知识包并输入检索问题。')
            return
        def operation():
            result = search(package, query, self.get_encoder(), cancel=self.cancel)
            sections = [f'检索：{query}\n客户：{result["customer_id"]}']
            for i, item in enumerate(result['matches'], 1):
                location = f'第 {item["page"]} 页，' if item['page'] else ''
                sections.append(f'\n[{i}] {item["source"]} · {location}行 {item["line_start"]}—{item["line_end"]}\n'
                                f'{item["section"]}\n{item["text"]}')
            return '\n'.join(sections) + '\n\n' + result['note']
        self.run(operation)

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'done':
                    self.spinner.stop()
                    self.cancel_button.configure(state='disabled')
                    for control in self.actions + self.controls:
                        control.configure(state='normal')
                elif event == 'log':
                    self.status.set(value)
                    self.append(value)
                elif event == 'error':
                    self.status.set('任务失败，详见下方原因。')
                    self.append('错误：' + value)
                else:
                    self.status.set('已取消。' if event == 'cancelled' else '任务完成。')
                    self.append(value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.status.set('正在取消，请任务结束后关闭窗口。')
            return
        self.root.destroy()


def launch(model_dir=DEFAULT_MODEL_DIR):
    root = tk.Tk()
    Application(root, model_dir)
    root.mainloop()
