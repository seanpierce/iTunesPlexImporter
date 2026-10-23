#!/usr/bin/env python3
"""Tkinter desktop front end for itunes_to_plex.py.

Place this next to itunes_to_plex.py and run: py itunes_plex_gui.py
Dependency: py -m pip install plexapi
"""
import csv
import os
import queue
import contextlib
import io
import sys
import uuid
import webbrowser
import threading
import ctypes
import platform
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ASSETS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def windows_dark_mode():
    """Read the current Windows application theme, not the wallpaper or system theme."""
    if sys.platform != 'win32':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            return value == 0
    except OSError:
        return False

# Import this module explicitly so PyInstaller bundles it in a standalone EXE.
import itunes_to_plex


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        if sys.platform == 'win32':
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('iTunesPlex.PlaylistImporter')
            except Exception:
                pass
        self.title('iTunes → Plex Playlist Importer')
        self._set_icons()
        self.style = ttk.Style(self)
        # Native Windows ttk themes often ignore custom button background colors.
        self.style.theme_use('clam')
        self._dark = None
        self._set_theme(windows_dark_mode())
        self.geometry('960x700')
        self.minsize(760, 540)
        self.xml = tk.StringVar()
        self.url = tk.StringVar(value='http://127.0.0.1:32400')
        self.library = tk.StringVar(value='Music')
        self.token = tk.StringVar(value=os.environ.get('PLEX_TOKEN', ''))
        self.report_path = tk.StringVar(value='No report generated yet')
        self.events = queue.Queue()
        self.running = False
        self.authenticating = False
        self.latest_report = None
        self._build()
        self.after(100, self._drain_events)
        self.after(2000, self._watch_theme)

    def _set_icons(self):
        ico = ASSETS / 'iTunesPlexImporter.ico'
        png = ASSETS / 'iTunesPlexImporter.png'
        if sys.platform == 'win32' and ico.is_file():
            try:
                self.iconbitmap(default=str(ico))
            except tk.TclError:
                pass
        if png.is_file():
            try:
                self._app_icon = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._app_icon)
            except tk.TclError:
                pass

    def _set_theme(self, dark):
        self._dark = dark
        bg = '#191d24' if dark else '#f5f7fa'
        panel = '#252a34' if dark else '#ffffff'
        input_bg = '#2e3541' if dark else '#ffffff'
        fg = '#edf1f7' if dark else '#19202b'
        muted = '#a9b3c3' if dark else '#586474'
        edge = '#414958' if dark else '#cbd4e0'
        selected = '#314969' if dark else '#dceafa'
        self.configure(background=bg)
        try:
            # DWMWA_USE_IMMERSIVE_DARK_MODE, Windows 10/11 (build dependent).
            if sys.platform == 'win32':
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                val = ctypes.c_int(1 if dark else 0)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass
        st = self.style
        st.configure('.', background=bg, foreground=fg, font=('Segoe UI', 10))
        st.configure('TFrame', background=bg)
        st.configure('TLabel', background=bg, foreground=fg)
        st.configure('Muted.TLabel', background=bg, foreground=muted)
        st.configure('TEntry', fieldbackground=input_bg, foreground=fg,
                     background=panel, insertcolor=fg, bordercolor=edge, lightcolor=edge, darkcolor=edge,
                     padding=5)
        st.map('TEntry', fieldbackground=[('readonly', input_bg)], foreground=[('disabled', muted)])
        st.configure('TButton', background=panel, foreground=fg, bordercolor=edge,
                     lightcolor=edge, darkcolor=edge, padding=(12, 8))
        st.map('TButton', background=[('active', selected), ('disabled', panel)],
               foreground=[('disabled', muted)])
        for name, color, hover in (
            ('Dry.TButton', '#2563eb', '#1d4ed8'),
            ('Apply.TButton', '#168451', '#106c40')):
            st.configure(name, background=color, foreground='#ffffff',
                         bordercolor=color, lightcolor=color, darkcolor=color,
                         padding=(18, 10), font=('Segoe UI', 10, 'bold'))
            st.map(name, background=[('disabled', '#677080' if dark else '#a4aab4'),
                                     ('pressed', hover), ('active', hover)],
                   foreground=[('disabled', '#e4e7ed'), ('active', '#ffffff')])
        st.configure('Treeview', background=panel, fieldbackground=panel, foreground=fg,
                     bordercolor=edge, rowheight=25)
        st.map('Treeview', background=[('selected', selected)], foreground=[('selected', fg)])
        st.configure('Treeview.Heading', background=input_bg, foreground=fg, relief='flat')
        st.map('Treeview.Heading', background=[('active', selected)])
        st.configure('TScrollbar', background=input_bg, troughcolor=bg)
        st.configure('TSeparator', background=edge)
        if hasattr(self, 'output'):
            self.output.configure(bg=panel, fg=fg, insertbackground=fg,
                                  selectbackground=selected, highlightbackground=edge,
                                  highlightcolor=edge, relief='flat')
        if hasattr(self, 'table'):
            self.table.tag_configure('bad', foreground='#ff8989' if dark else '#b42318')
            self.table.tag_configure('good', foreground='#65dba1' if dark else '#16703c')

    def _watch_theme(self):
        mode = windows_dark_mode()
        if mode != self._dark:
            self._set_theme(mode)
        self.after(2000, self._watch_theme)

    def _build(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill='both', expand=True)
        root.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(root, text='iTunes → Plex Playlist Importer', font=('Segoe UI', 17, 'bold')).grid(row=row, column=0, columnspan=3, sticky='w', pady=(0, 12))
        row += 1
        ttk.Label(root, text='iTunes library XML').grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(root, textvariable=self.xml).grid(row=row, column=1, sticky='ew', padx=8)
        ttk.Button(root, text='Browse…', command=self.browse).grid(row=row, column=2)
        row += 1
        ttk.Label(root, text='Plex server URL').grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(root, textvariable=self.url).grid(row=row, column=1, columnspan=2, sticky='ew', padx=(8, 0))
        row += 1
        ttk.Label(root, text='Music library name').grid(row=row, column=0, sticky='w', pady=5)
        ttk.Entry(root, textvariable=self.library).grid(row=row, column=1, columnspan=2, sticky='ew', padx=(8, 0))
        row += 1
        ttk.Label(root, text='Plex authentication').grid(row=row, column=0, sticky='w', pady=5)
        auth = ttk.Frame(root)
        auth.grid(row=row, column=1, columnspan=2, sticky='ew', padx=(8, 0))
        self.signin_btn = ttk.Button(auth, text='Sign in with Plex', command=self.sign_in)
        self.signin_btn.pack(side='left')
        self.auth_status = ttk.Label(auth, text=('Token supplied by environment' if self.token.get() else 'Not signed in'))
        self.auth_status.pack(side='left', padx=10)
        row += 1
        ttk.Label(root, text='Sign-in opens your browser. Access token stays in memory; no Plex password is collected.', style='Muted.TLabel').grid(row=row, column=0, columnspan=3, sticky='w', pady=(0, 10))
        row += 1
        buttons = ttk.Frame(root)
        buttons.grid(row=row, column=0, columnspan=3, sticky='w', pady=(0, 12))
        self.dry = ttk.Button(buttons, text='Dry Run (no changes)', style='Dry.TButton', command=lambda: self.start(False))
        self.dry.pack(side='left', padx=(0, 8))
        self.apply = ttk.Button(buttons, text='Apply (create playlists)', style='Apply.TButton', command=lambda: self.start(True))
        self.apply.pack(side='left')
        self.status = ttk.Label(buttons, text='Ready')
        self.status.pack(side='left', padx=16)
        row += 1
        ttk.Separator(root).grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 8))
        row += 1
        ttk.Label(root, text='Run output', font=('Segoe UI', 11, 'bold')).grid(row=row, column=0, columnspan=3, sticky='w')
        row += 1
        output_container = ttk.Frame(root)
        output_container.grid(row=row, column=0, columnspan=3, sticky='nsew', pady=(4, 12))
        root.rowconfigure(row, weight=2)
        output_container.columnconfigure(0, weight=1)
        output_container.rowconfigure(0, weight=1)
        self.output = tk.Text(output_container, wrap='word', state='disabled', height=12, font=('Consolas', 10))
        self.output.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(output_container, orient='vertical', command=self.output.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.output.configure(yscrollcommand=scroll.set)
        row += 1
        report_head = ttk.Frame(root)
        report_head.grid(row=row, column=0, columnspan=3, sticky='ew', pady=(0, 5))
        ttk.Label(report_head, text='Track matching report', font=('Segoe UI', 11, 'bold')).pack(side='left')
        self.open_btn = ttk.Button(report_head, text='Open CSV', command=self.open_csv, state='disabled')
        self.open_btn.pack(side='right')
        row += 1
        ttk.Label(root, textvariable=self.report_path, style='Muted.TLabel').grid(row=row, column=0, columnspan=3, sticky='w')
        row += 1
        cols = ('playlist', 'position', 'title', 'artist', 'status')
        report_box = ttk.Frame(root)
        report_box.grid(row=row, column=0, columnspan=3, sticky='nsew', pady=(5, 0))
        root.rowconfigure(row, weight=3)
        report_box.rowconfigure(0, weight=1)
        report_box.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(report_box, columns=cols, show='headings', height=9)
        for col, width in zip(cols, (190, 65, 260, 210, 110)):
            self.table.heading(col, text=col.replace('_', ' ').title())
            self.table.column(col, width=width, stretch=col != 'position')
        self.table.grid(row=0, column=0, sticky='nsew')
        bar = ttk.Scrollbar(report_box, orient='vertical', command=self.table.yview)
        bar.grid(row=0, column=1, sticky='ns')
        self.table.configure(yscrollcommand=bar.set)
        self._set_theme(self._dark)

    def browse(self):
        name = filedialog.askopenfilename(title='Select iTunes Library XML', filetypes=[('iTunes XML', '*.xml'), ('All files', '*.*')])
        if name:
            self.xml.set(name)

    def _append(self, value):
        self.output.configure(state='normal')
        self.output.insert('end', value)
        self.output.see('end')
        self.output.configure(state='disabled')

    def start(self, apply):
        xml = Path(self.xml.get().strip())
        if not xml.is_file():
            messagebox.showerror('Missing XML file', 'Select a valid exported iTunes Library XML file.')
            return
        if not self.url.get().strip().startswith(('http://', 'https://')):
            messagebox.showerror('Invalid server URL', 'Enter a Plex URL beginning with http:// or https://.')
            return
        if not self.library.get().strip() or not self.token.get().strip():
            messagebox.showerror('Missing setting', 'Enter your Plex music library name and Plex token.')
            return
        if apply and not messagebox.askyesno('Apply playlists?', 'Create playlists in Plex now?\n\nExisting playlists will be skipped. Only playlists with at least 95% matches are eligible.'):
            return
        report_dir = BASE / 'reports'
        report_dir.mkdir(exist_ok=True)
        mode = 'apply' if apply else 'dry_run'
        report = report_dir / f'itunes_plex_{mode}_{datetime.now():%Y%m%d_%H%M%S_%f}.csv'
        cmd = ['--xml', str(xml), '--url', self.url.get().strip(),
               '--library', self.library.get().strip(), '--report', str(report)]
        if apply:
            cmd.append('--apply')
        cmd.extend(['--token', self.token.get().strip()])
        self.running = True
        self.dry.configure(state='disabled')
        self.apply.configure(state='disabled')
        self.status.configure(text='Running…')
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.configure(state='disabled')
        self._append(f"Mode: {'APPLY' if apply else 'DRY RUN'}\nReport: {report}\n\n")
        threading.Thread(target=self._worker, args=(cmd, report), daemon=True).start()

    def sign_in(self):
        if self.running or self.authenticating:
            return
        self.authenticating = True
        self.signin_btn.configure(state='disabled')
        self.auth_status.configure(text='Starting Plex sign-in…')
        threading.Thread(target=self._auth_worker, daemon=True).start()

    def _auth_worker(self):
        try:
            from plexapi.myplex import MyPlexPinLogin, MyPlexAccount
            login = MyPlexPinLogin(oauth=True, headers={
                'X-Plex-Product': 'iTunes Plex Playlist Importer',
                'X-Plex-Client-Identifier': str(uuid.uuid4()),
            })
            login.run(timeout=120)
            self.events.put(('auth_open', login.oauthUrl()))
            login.waitForLogin()
            if not login.token:
                self.events.put(('auth_error', 'Sign-in expired or was cancelled. Please try again.'))
                return
            # Verify credentials with Plex before allowing imports.
            account = MyPlexAccount(token=login.token)
            self.events.put(('auth_ok', login.token, getattr(account, 'username', 'Plex user')))
        except Exception as exc:
            self.events.put(('auth_error', str(exc)))

    def _worker(self, cmd, report):
        # The importer runs in-process so --onefile PyInstaller bundles work.
        # Redirect only while the worker is active; the UI log is updated on the Tk thread.
        class QueueWriter(io.TextIOBase):
            def __init__(self, events):
                self.events = events
            def write(self, value):
                if value:
                    self.events.put(('line', value))
                return len(value)
            def flush(self):
                pass
        try:
            with contextlib.redirect_stdout(QueueWriter(self.events)), contextlib.redirect_stderr(QueueWriter(self.events)):
                old_argv = sys.argv
                try:
                    sys.argv = ['itunes_to_plex.py', *cmd]
                    itunes_to_plex.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
                    if isinstance(exc.code, str):
                        print(exc.code)
                finally:
                    sys.argv = old_argv
            self.events.put(('done', code, str(report)))
        except Exception as exc:
            self.events.put(('error', str(exc)))

    def _drain_events(self):
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == 'auth_open':
                    self.auth_status.configure(text='Complete sign-in in your browser…')
                    webbrowser.open(rest[0])
                elif kind == 'auth_ok':
                    token, user = rest
                    self.token.set(token)
                    self.authenticating = False
                    self.signin_btn.configure(state='normal')
                    self.auth_status.configure(text=f'Signed in: {user}')
                elif kind == 'auth_error':
                    self.authenticating = False
                    self.signin_btn.configure(state='normal')
                    self.auth_status.configure(text='Not signed in')
                    messagebox.showerror('Plex sign-in', rest[0])
                elif kind == 'line':
                    self._append(rest[0])
                elif kind == 'done':
                    code, path = rest
                    self._finish(f'Completed (exit {code})' if code == 0 else f'Failed (exit {code})')
                    if Path(path).is_file():
                        self._read_report(Path(path))
                    if code != 0:
                        messagebox.showerror('Import failed', 'The importer encountered an error. Check the run output above.')
                elif kind == 'error':
                    self._append(f'\nError starting importer: {rest[0]}\n')
                    self._finish('Failed')
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, status):
        self.running = False
        self.dry.configure(state='normal')
        self.apply.configure(state='normal')
        self.status.configure(text=status)

    def _read_report(self, path):
        self.latest_report = path
        self.report_path.set(str(path))
        self.open_btn.configure(state='normal')
        for item in self.table.get_children():
            self.table.delete(item)
        counts = {}
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                status = row.get('status', '')
                counts[status] = counts.get(status, 0) + 1
                self.table.insert('', 'end', values=tuple(row.get(key, '') for key in
                                  ('playlist', 'position', 'title', 'artist', 'status')),
                                  tags=('good' if status == 'matched' else 'bad',))
        self._append('\nReport status totals: ' + ', '.join(f'{k}: {v}' for k, v in sorted(counts.items())) + '\n')

    def open_csv(self):
        if self.latest_report and self.latest_report.is_file():
            os.startfile(self.latest_report)  # Windows: open with default CSV application.


if __name__ == '__main__':
    App().mainloop()
