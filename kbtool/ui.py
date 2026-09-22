"""Wenlu's light palette and typography, kept independent of the desktop app."""
import math
import tkinter as tk
from tkinter import font as tkfont, ttk

BG = '#f7f8fa'
SURFACE = '#ffffff'
TEXT = '#263044'
SECONDARY = '#4f586b'
MUTED = '#858b98'
BORDER = '#e2e5ed'
ACCENT = '#007ACC'
HOVER = '#E6F2FB'
TRACK = '#edf0f7'
FONT = 'Microsoft YaHei UI'


def setup(root):
    global FONT
    families = {name.lower(): name for name in tkfont.families(root)}
    FONT = next((families[name.lower()] for name in
                 ('Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI') if name.lower() in families),
                tkfont.nametofont('TkDefaultFont', root=root).actual('family'))
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
        tkfont.nametofont(name, root=root).configure(family=FONT, size=10)
    root.option_add('*Font', (FONT, 10))
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('WL.TEntry', fieldbackground=SURFACE, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                    padding=(9, 6), insertcolor=TEXT)
    style.map('WL.TEntry', bordercolor=[('focus', ACCENT)],
              fieldbackground=[('disabled', BG)], foreground=[('disabled', MUTED)])


def label(parent, text=None, variable=None, size=10, color=SECONDARY, bold=False, **kwargs):
    return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'), fg=color,
                    font=(FONT, size, 'bold' if bold else 'normal'), bd=0, **kwargs)


def button(parent, text, command, primary=False):
    normal = ACCENT if primary else BG
    hover = '#006BB3' if primary else HOVER
    widget = tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                       bg=normal, fg='white' if primary else '#737b8c',
                       activebackground=hover, activeforeground='white' if primary else ACCENT,
                       disabledforeground='#a6acba', font=(FONT, 9), padx=12, pady=6,
                       cursor='hand2', takefocus=True, highlightthickness=1,
                       highlightbackground=normal, highlightcolor=ACCENT)
    widget.bind('<Enter>', lambda _: widget.configure(bg=hover) if str(widget['state']) != 'disabled' else None)
    widget.bind('<Leave>', lambda _: widget.configure(bg=normal))
    return widget


class FlatTabs(tk.Frame):
    """Wenlu-style flat tabs with no platform notebook border or raised tab chrome."""
    def __init__(self, parent):
        super().__init__(parent, bg=SURFACE, bd=0, highlightthickness=0)
        self.navigation = tk.Frame(self, bg=SURFACE)
        self.navigation.pack(fill='x', pady=(0, 4))
        tk.Frame(self, bg=BORDER, height=1).pack(fill='x')
        self.body = tk.Frame(self, bg=SURFACE)
        self.body.pack(fill='both', expand=True)
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.pages = {}
        self.current = None

    def add(self, page, text):
        tab = tk.Button(self.navigation, text=text, command=lambda: self.select(page),
                        bg=SURFACE, fg=MUTED, activebackground=HOVER, activeforeground=ACCENT,
                        font=(FONT, 9), relief='flat', bd=0, highlightthickness=0,
                        padx=12, pady=7, cursor='hand2', takefocus=True)
        tab.pack(side='left', padx=(0, 6))
        tab.bind('<Right>', lambda _: self.next_tab(1))
        tab.bind('<Left>', lambda _: self.next_tab(-1))
        self.pages[str(page)] = (page, tab)
        page.grid(row=0, column=0, sticky='nsew')
        if self.current is None:
            self.select(page)
        else:
            self.pages[self.current][0].tkraise()

    def select(self, page=None):
        if page is None:
            return self.current
        self.current = str(page)
        self.pages[self.current][0].tkraise()
        for key, (_, tab) in self.pages.items():
            tab.configure(bg=HOVER if key == self.current else SURFACE,
                          fg=ACCENT if key == self.current else MUTED)

    def next_tab(self, direction):
        keys = list(self.pages)
        key = keys[(keys.index(self.current) + direction) % len(keys)]
        self.select(key)
        self.pages[key][1].focus_set()
        return 'break'


class StatusLine(tk.Label):
    """Keep long file paths and failures from growing the fixed task footer."""
    def __init__(self, parent, variable):
        super().__init__(parent, bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor='w', width=1, bd=0)
        self.variable = variable
        self.trace = variable.trace_add('write', self.refresh)
        self.bind('<Configure>', self.refresh)
        self.bind('<Destroy>', self.cleanup)

    def refresh(self, *_):
        text = ' '.join(self.variable.get().split())
        font = tkfont.Font(font=self.cget('font'))
        available = max(0, self.winfo_width() - 4)
        if font.measure(text) > available:
            while text and font.measure(text + '…') > available:
                text = text[:-1]
            text = text + '…' if text else ''
        self.configure(text=text)

    def cleanup(self, event):
        if event.widget is self:
            self.variable.trace_remove('write', self.trace)


class ProgressStrip(tk.Canvas):
    """A thin rounded track: actual phase ratios, or an unquantified busy indicator."""
    def __init__(self, parent):
        super().__init__(parent, height=10, bg=parent.cget('bg'), bd=0, highlightthickness=0)
        self.state = 'idle'
        self.value = None
        self.timer = None
        self.tick = 0
        self.bind('<Configure>', lambda _: self.draw())
        self.bind('<Destroy>', self.cleanup)

    def set(self, state, value=None):
        self.stop()
        self.state = state
        self.value = max(0, min(1, value)) if value is not None else None
        self.draw()
        if state == 'running' and value is None:
            self.animate()

    def stop(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None

    def animate(self):
        self.tick += 1
        self.draw()
        self.timer = self.after(35, self.animate)

    def draw(self):
        self.delete('all')
        width = max(10, self.winfo_width())
        start, end = 4, width - 4
        self.create_line(start, 5, end, 5, fill=TRACK, width=6, capstyle='round')
        color = {'success': '#15803d', 'error': '#b91c1c', 'cancelled': '#b45309'}.get(self.state, ACCENT)
        if self.value is not None and self.value > 0:
            self.create_line(start, 5, start + (end - start) * self.value, 5,
                             fill=color, width=6, capstyle='round')
        elif self.state == 'running':
            length = (end - start) * .2
            x = start + (end - start - length) * (1 - math.cos(self.tick / 16)) / 2
            self.create_line(x, 5, x + length, 5, fill=color, width=6, capstyle='round')

    def cleanup(self, event):
        if event.widget is self:
            self.stop()
