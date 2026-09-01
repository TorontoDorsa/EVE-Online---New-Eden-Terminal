#!/usr/bin/env python3
"""
New Eden Terminal — desktop app
------------------------------
Launches the same live HTML dashboard eve_web.py serves in the user's own
default browser (a normal webpage, not a wrapped window) and keeps a small
native launcher window open in the background — that launcher, via
pywebview, is what gives the app a lifecycle (something to quit) and a way
to open the command-console page as a real native app window on request,
separate from the browser-tab dashboard. The one thing this still needs
Tkinter for is a first-run screen asking for the user's own EVE developer
app credentials, since that has to happen before anything web-based can
even start.

Requires: pip install -r requirements.txt (adds pywebview + pythonnet on
top of flask/requests — pythonnet lets pywebview use the Chromium-based
WebView2 renderer on Windows instead of falling back to a legacy one that
can't render this project's CSS).

Usage:
    python eve_gui.py

Put this file in the same folder as eve_web.py, eve_esi_terminal.py, and
eve_sso_auth.py.
"""

import sys
import socket
import subprocess
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk

import webview

import eve_sso_auth as auth
import eve_web

BG = "#05070c"
TEXT = "#d7e1f2"
TEXT_DIM = "#6f7f9e"


def _free_port():
    """A free localhost port, picked fresh each run so this doesn't collide
    with a `python eve_web.py` the user might also have running."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_console_window(port):
    """Opens the command-console page as a genuinely separate native
    window/process — re-invokes this same script/exe in a self-contained
    `--console-window` mode (see __main__ below) rather than trying to
    create a second pywebview window inside whatever event loop is already
    running here, which isn't something to assume behaves a particular way
    without testing. Works the same whether called from the launcher
    window's "Open Console" button or from eve_web.py's /api/open-console
    route (the dashboard's own "Command terminal" link)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--console-window", "--port", str(port)]
    else:
        cmd = [sys.executable, __file__, "--console-window", "--port", str(port)]
    subprocess.Popen(cmd)


class SetupWindow:
    """First-run screen: collect the user's own EVE developer app
    credentials before anything else can start. Once saved, the dashboard
    window takes over and this Tk window is gone."""

    def __init__(self, root):
        self.root = root
        root.title("New Eden Terminal — Setup")
        root.geometry("820x480")
        root.configure(bg=BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT_DIM, font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=6)
        style.configure("TEntry", fieldbackground=BG, foreground=TEXT)

        frame = ttk.Frame(root, padding=28)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="NEW EDEN DATA TERMINAL", bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(frame, text="First-time setup — connect your own EVE developer app",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 18))

        body = tk.Label(
            frame, justify="left", bg=BG, fg=TEXT, wraplength=760, font=("Segoe UI", 9),
            text=(
                "This tool talks to EVE Online's ESI API on your behalf, which means it "
                "needs its own free developer application registered under your account — "
                "your login never touches anyone else's app or server.\n\n"
                "1. Click \"Open EVE Developer Portal\" below and sign in with your EVE account.\n"
                "2. Create a new application with callback URL:  http://localhost:8765/callback\n"
                "3. Copy its Client ID and Secret Key into the fields below.\n\n"
                "These are saved only on this PC and are never sent anywhere except EVE's own "
                "login servers."
            ),
        )
        body.pack(anchor="w", pady=(0, 16))

        ttk.Button(frame, text="Open EVE Developer Portal",
                   command=lambda: webbrowser.open("https://developers.eveonline.com/applications")
                   ).pack(anchor="w", pady=(0, 18))

        form = ttk.Frame(frame)
        form.pack(anchor="w")

        ttk.Label(form, text="Client ID:").grid(row=0, column=0, sticky="w", pady=6)
        self.cred_id_entry = ttk.Entry(form, width=52)
        self.cred_id_entry.grid(row=0, column=1, sticky="w", padx=(10, 0))

        ttk.Label(form, text="Secret Key:").grid(row=1, column=0, sticky="w", pady=6)
        self.cred_secret_entry = ttk.Entry(form, width=52, show="*")
        self.cred_secret_entry.grid(row=1, column=1, sticky="w", padx=(10, 0))

        self.cred_error = tk.Label(frame, text="", bg=BG, fg="#e5484d", font=("Segoe UI", 9))
        self.cred_error.pack(anchor="w", pady=(10, 0))

        ttk.Button(frame, text="Save & Continue", command=self._save).pack(anchor="w", pady=(16, 0))

    def _save(self):
        client_id = self.cred_id_entry.get().strip()
        client_secret = self.cred_secret_entry.get().strip()
        if not client_id or not client_secret:
            self.cred_error.configure(text="Both fields are required.")
            return
        auth.save_credentials(client_id, client_secret)
        self.root.destroy()


_LAUNCHER_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<style>
  body {{ margin:0; background:#05070c; color:#d7e1f2; font-family:"Segoe UI",sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         height:100vh; text-align:center; }}
  h1 {{ font-size:15px; letter-spacing:0.04em; color:#ffb545; margin:0 0 4px; }}
  p {{ font-size:11.5px; color:#6f7f9e; margin:0 0 20px; }}
  button {{ display:block; width:220px; margin:6px 0; padding:9px 0; background:#0d1420;
           border:1px solid #21314f; border-radius:6px; color:#d7e1f2; font-size:12.5px;
           cursor:pointer; }}
  button:hover {{ border-color:#ffb545; color:#ffb545; }}
  #quit-btn {{ margin-top:14px; color:#8a94a8; border-color:#2a3346; }}
</style></head>
<body>
  <h1>NEW EDEN TERMINAL</h1>
  <p>Running in the background — port {port}</p>
  <button onclick="window.pywebview.api.open_dashboard()">Open Dashboard</button>
  <button onclick="window.pywebview.api.open_console()">Open Console</button>
  <button id="quit-btn" onclick="window.pywebview.api.quit_app()">Quit</button>
</body></html>
"""


class LauncherAPI:
    """Bridged to the launcher window's JS via pywebview's js_api — see
    https://pywebview.flowrl.com/guide/api.html for the expose/js_api
    pattern this relies on."""

    def __init__(self, port):
        self.port = port

    def open_dashboard(self):
        webbrowser.open(f"http://127.0.0.1:{self.port}/dashboard")

    def open_console(self):
        spawn_console_window(self.port)

    def quit_app(self):
        for window in webview.windows:
            window.destroy()


def launch_dashboard():
    """Run eve_web.py's existing Flask app in a background thread, open the
    dashboard as an ordinary page in the user's real default browser, and
    keep a small native launcher window open — that launcher is the app's
    lifecycle anchor (closing it, or its Quit button, ends the process and
    stops the Flask server) and the way to open the console as a real
    native app window on request."""
    port = _free_port()
    thread = threading.Thread(
        target=lambda: eve_web.app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False),
        daemon=True,
    )
    thread.start()
    webbrowser.open(f"http://127.0.0.1:{port}/dashboard")
    webview.create_window(
        "New Eden Terminal", html=_LAUNCHER_HTML.format(port=port),
        width=340, height=280, resizable=False, js_api=LauncherAPI(port),
    )
    webview.start()


def _console_window_mode():
    """Self-contained --console-window mode: open a native window at an
    already-running server's /console page and block until it's closed.
    Knows nothing about the process that spawned it beyond the port."""
    if "--port" not in sys.argv:
        print("--console-window requires --port <n>")
        sys.exit(1)
    port = int(sys.argv[sys.argv.index("--port") + 1])
    webview.create_window("New Eden Terminal — Console", f"http://127.0.0.1:{port}/console",
                           width=900, height=650, min_size=(700, 500))
    webview.start()


if __name__ == "__main__":
    if "--console-window" in sys.argv:
        _console_window_mode()
        sys.exit(0)

    if not auth.has_credentials():
        root = tk.Tk()
        SetupWindow(root)
        root.mainloop()

    if auth.has_credentials():
        launch_dashboard()
