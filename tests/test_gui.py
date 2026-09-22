"""Exercise GUI worker/event flow with real embeddings; not a visual layout test."""
from pathlib import Path
import gc
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from kbtool.gui import Application


class GuiFlowTest(unittest.TestCase):
    def tearDown(self):
        # Destroyed Tk cycles must be finalized on the UI thread, never during worker allocation.
        gc.collect()

    def test_one_click_import_button_and_missing_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                app = Application(root)
                app.output.set(str(Path(directory) / 'customer.wlkb'))
                with patch('kbtool.gui.messagebox.showerror') as error:
                    app.import_button.invoke()
                    error.assert_called_once()
                Path(app.output.get()).write_bytes(b'package')
                with patch('kbtool.wenlu_import.import_package', return_value={'name': '客户知识库', 'path': 'managed.wlkb'}) as send:
                    app.import_button.invoke()
                    self.assertEqual(str(app.cancel_button['state']), 'disabled')
                    deadline = time.monotonic() + 5
                    while app.busy:
                        self.assertLess(time.monotonic(), deadline)
                        root.update()
                        time.sleep(.01)
                    send.assert_called_once()
                    self.assertIn('已导入闻录', app.status.get())
                    self.assertEqual(app.spinner.state, 'success')
            finally:
                root.destroy()

    def test_web_mode_build_and_source_display(self):
        from test_web_sources import HTML, WebSourceTests
        import httpx
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                app = Application(root)
                app.select_source_mode('web')
                root.geometry('860x740')
                root.update_idletasks()
                self.assertEqual(app.source_mode, 'web')
                self.assertEqual(app.source_entry.winfo_manager(), '')
                app.url_text.insert('1.0', 'https://example.com/article\nhttps://example.com/article#same')
                app.output.set(str(Path(directory) / 'web.wlkb'))
                with WebSourceTests().transport(lambda _: httpx.Response(200, text=HTML, headers={'content-type': 'text/html'})):
                    app.generate()
                    self.assertEqual(str(app.url_text['state']), 'disabled')
                    deadline = time.monotonic() + 30
                    while app.busy:
                        self.assertLess(time.monotonic(), deadline)
                        root.update()
                        time.sleep(.01)
                self.assertTrue(Path(app.output.get()).exists(), app.display.get('1.0', 'end'))
                self.assertIn('抓取完成 1/1', app.display.get('1.0', 'end'))
                app.query.set('消息失败如何补发')
                app.lookup()
                while app.busy:
                    self.assertLess(time.monotonic(), deadline)
                    root.update()
                    time.sleep(.01)
                self.assertIn('https://example.com/article', app.results.get('1.0', 'end'))
                self.assertIn('抓取时间', app.results.get('1.0', 'end'))
                app.url_text.delete('1.0', 'end')
                app.url_text.insert('1.0', 'file:///wrong')
                with patch('kbtool.gui.messagebox.showerror') as error:
                    app.generate()
                    error.assert_called_once()
                    self.assertFalse(app.busy)
                app.select_source_mode('local')
                self.assertEqual(app.source_entry.winfo_manager(), 'grid')
            finally:
                root.destroy()

    def test_compact_layout_and_flat_tab_navigation(self):
        from tkinter import font as tkfont
        for scaling in (1.333, 1.667):
            with self.subTest(scaling=scaling):
                root = tk.Tk()
                root.withdraw()
                root.tk.call('tk', 'scaling', scaling)
                try:
                    app = Application(root)
                    root.geometry('860x680')
                    root.update_idletasks()
                    entries = (app.customer_entry, app.source_entry, app.output_entry)
                    self.assertEqual(len({entry.winfo_rootx() for entry in entries}), 1)
                    line_height = tkfont.Font(font=app.results['font']).metrics('linespace')
                    self.assertGreaterEqual(app.results.winfo_height(), line_height * 4)
                    self.assertEqual(app.tabs.select(), str(app.result_page))
                    app.select_source_mode('web')
                    root.geometry('860x740')
                    root.deiconify()
                    root.update_idletasks()
                    self.assertGreaterEqual(app.results.winfo_height(), line_height * 4)
                    self.assertEqual(app.url_text.winfo_rootx(), app.output_entry.winfo_rootx())
                    app.tabs.next_tab(1)
                    self.assertEqual(app.tabs.select(), str(app.log_page))
                    app.tabs.next_tab(-1)
                    self.assertEqual(app.tabs.select(), str(app.result_page))
                finally:
                    root.destroy()

    def test_build_then_search_and_correct_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                app = Application(root)
                source = Path(directory) / 'project.md'
                source.write_text('# 通知故障\n\n数据库提交后消息没有发出，采用 outbox 同事务记录，再由后台补发。', encoding='utf-8')
                app.source.set(str(source))
                app.output.set(str(Path(directory) / 'customer.wlkb'))
                app.query.set('保存成功但是通知没发出怎么办？')
                self.assertFalse(hasattr(app, 'demo'))
                def finish():
                    deadline = time.monotonic() + 30
                    while app.worker.is_alive() or str(app.cancel_button['state']) != 'disabled':
                        if time.monotonic() > deadline:
                            app.cancel.set()
                            self.fail('GUI background task timed out')
                        root.update()
                        time.sleep(.01)
                    self.assertNotIn('错误：', app.display.get('1.0', 'end'))
                app.generate()
                finish()
                self.assertTrue(Path(app.output.get()).is_file())
                self.assertEqual(app.spinner.state, 'success')
                self.assertEqual(app.spinner.value, 1)
                app.lookup()
                finish()
                displayed = app.results.get('1.0', 'end')
                self.assertIn('outbox', displayed)
                self.assertIn('通知故障', displayed)
                self.assertEqual(app.tabs.select(), str(app.result_page))
                package = Path(app.output.get())
                app.output.set(str(package.with_suffix('.wldb')))
                app.lookup()
                finish()
                self.assertEqual(Path(app.output.get()), package)
                self.assertIn('后缀已纠正为 .wlkb', app.display.get('1.0', 'end'))
                self.assertIn('outbox', app.results.get('1.0', 'end'))
                self.assertFalse(package.with_suffix('.wldb').exists())
                with patch.object(app, 'get_encoder', side_effect=AssertionError('Should validate first')), \
                        patch('kbtool.gui.messagebox.showerror') as error:
                    for filename in ('missing.wldb', 'missing.wlkb', 'wrong.sqlite'):
                        app.output.set(str(Path(directory) / filename))
                        app.lookup()
                        self.assertIn('.wlkb', error.call_args.args[1])
                    self.assertEqual(error.call_count, 3)
            finally:
                root.destroy()

    def test_phase_progress_and_terminal_states(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = Application(root)
            app.update_progress('解析完成 2/8：项目.md')
            self.assertEqual(app.phase.get(), '解析资料')
            self.assertEqual(app.spinner.value, .25)
            app.update_progress('向量化：3/6 个片段')
            self.assertEqual(app.phase.get(), '生成向量')
            self.assertEqual(app.spinner.value, .5)
            app.update_progress('onnx/model_quantized.onnx：12.0 MB / 24.0 MB')
            self.assertEqual(app.phase.get(), '下载模型')
            self.assertEqual(app.spinner.value, .5)
            app.update_progress('写入知识包…')
            self.assertIsNone(app.spinner.value)
            self.assertIsNotNone(app.spinner.timer)
            app.update_progress('正在渲染网页：https://example.com')
            self.assertEqual(app.phase.get(), '加载动态网页')
            self.assertIsNone(app.spinner.value)
            app.set_progress('失败', .3, state='error')
            self.assertEqual(app.spinner.value, .3)
            self.assertIsNone(app.spinner.timer)
        finally:
            root.destroy()

    def test_worker_failure_cancel_and_recovery(self):
        from kbtool.embedding import Cancelled
        root = tk.Tk()
        root.withdraw()
        try:
            app = Application(root)
            def finish():
                deadline = time.monotonic() + 5
                while app.busy:
                    self.assertLess(time.monotonic(), deadline)
                    root.update()
                    time.sleep(.01)
            def fail():
                raise RuntimeError('timed out')
            app.run(fail, '准备模型', 'model')
            app.run(lambda: self.fail('Duplicate operation accepted'))
            finish()
            self.assertEqual(app.spinner.state, 'error')
            self.assertNotEqual(app.spinner.value, 1)
            self.assertIn('离线模型包', app.status.get())
            self.assertTrue(all(str(button['state']) == 'normal' for button in app.actions))
            def cancel():
                app.cancel.wait(2)
                raise Cancelled('已取消')
            app.run(cancel)
            app.cancel_task()
            finish()
            self.assertEqual(app.spinner.state, 'cancelled')
            self.assertIsNone(app.spinner.timer)
            app.run(lambda: '已完成')
            finish()
            self.assertEqual(app.spinner.state, 'success')
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
