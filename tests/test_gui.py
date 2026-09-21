"""Exercise GUI worker/event flow with real embeddings; not a visual layout test."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest

from kbtool.gui import Application


class GuiFlowTest(unittest.TestCase):
    def test_demo_build_then_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = tk.Tk()
            root.withdraw()
            try:
                app = Application(root)
                app.demo()
                app.output.set(str(Path(directory) / 'gui-demo.wlkb'))
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
                app.lookup()
                finish()
                displayed = app.display.get('1.0', 'end')
                self.assertIn('outbox', displayed)
                self.assertIn('S08', displayed)
            finally:
                root.destroy()


if __name__ == '__main__':
    unittest.main()
