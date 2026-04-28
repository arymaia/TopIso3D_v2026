#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TopIso3D v2026 - Workspace + TRHO/TLAP Runner (auto-validate, no Validate button)

Fluxo:
1) Choose folder…
   -> valida automaticamente (sem botão)
      - OK se existir fort.9 OU existir pelo menos um *.f9
      - e se tiver permissão de escrita
2) Compute habilita se workspace OK
3) Run TRHO:
   -> garante fort.9:
      - se já existe: usa
      - se não existe e há exatamente 1 *.f9: cria symlink fort.9 -> arquivo.f9 (fallback: copia)
      - se há vários *.f9: pede confirmação (caso raro)
   -> roda TRHO (MOCK por padrão: progress + log)

Para usar com TRHO real:
- substituir build_trho_command()
- e descomentar o bloco REAL EXECUTION no worker
"""

from __future__ import annotations

import os
import platform
import time
import queue
import shutil
import json
import threading
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Callable, List, Tuple

import re
import pandas as pd
import numpy as np

# Plotting (PL2D Viewer)
import plotly.graph_objects as go
import plotly.express as px
import plotly.colors as pc


import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont


class _TolPromptDialog(simpledialog.Dialog):
    def __init__(self, parent, symbol: str):
        self.symbol = (symbol or '').strip()
        self.value = None
        super().__init__(parent, title='ATBP')

    def body(self, master):
        try:
            self.configure(bg=UI_BG_MAIN)
            self.resizable(False, False)
        except Exception:
            pass
        try:
            master.configure(bg=UI_BG_MAIN)
        except Exception:
            pass

        wrap = tk.Frame(master, bg=UI_BG_MAIN)
        wrap.grid(row=0, column=0, sticky='nsew', padx=12, pady=10)

        msg = (
            f"Element {self.symbol} is not present in the default TOPOND TOL table.\n"
            "Please enter a TOL value (bohr) for this element:"
        )
        tk.Label(
            wrap,
            text=msg,
            justify='left',
            anchor='w',
            bg=UI_BG_MAIN,
            fg=UI_FG_MAIN,
            font=('Arial', 12, 'bold'),
        ).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))

        tk.Label(
            wrap,
            text='TOL (bohr):',
            bg=UI_BG_MAIN,
            fg=UI_FG_MAIN,
            font=('Arial', 11, 'bold'),
        ).grid(row=1, column=0, sticky='w', padx=(0, 8))

        self.ent = tk.Entry(
            wrap,
            width=12,
            bg=UI_BG_FIELD,
            fg=UI_FG_MAIN,
            insertbackground=UI_FG_MAIN,
            relief='flat',
            highlightthickness=1,
            highlightbackground=UI_BG_DARK,
            highlightcolor=UI_ACCENT,
            font=('Arial', 11),
        )
        self.ent.grid(row=1, column=1, sticky='w')
        self.ent.insert(0, '1.0')
        return self.ent

    def buttonbox(self):
        box = tk.Frame(self, bg=UI_BG_MAIN)
        box.pack(fill='x', padx=12, pady=(0, 12))

        ok_btn = tk.Button(
            box,
            text='OK',
            width=10,
            command=self.ok,
            bg=UI_ACCENT,
            fg=UI_FG_MAIN,
            activebackground=UI_ACCENT,
            activeforeground=UI_FG_MAIN,
            relief='flat',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=4,
        )
        ok_btn.pack(side='left', padx=(0, 8))

        cancel_btn = tk.Button(
            box,
            text='Cancel',
            width=10,
            command=self.cancel,
            bg=UI_ACCENT,
            fg=UI_FG_MAIN,
            activebackground=UI_ACCENT,
            activeforeground=UI_FG_MAIN,
            relief='flat',
            font=('Arial', 11, 'bold'),
            padx=10,
            pady=4,
        )
        cancel_btn.pack(side='left')

        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def validate(self):
        raw = self.ent.get().strip().replace(',', '.')
        try:
            val = float(raw)
        except Exception:
            messagebox.showerror('ATBP', f'Invalid TOL value for {self.symbol}: {raw or "<empty>"}', parent=self)
            return False
        if val <= 0:
            messagebox.showerror('ATBP', f'TOL for {self.symbol} must be greater than zero.', parent=self)
            return False
        self.value = val
        return True

    def apply(self):
        pass
import traceback
import webbrowser
import tempfile
import sys
# ----------------------------
from datetime import datetime

# -----------------------------
# Window manager helpers
# -----------------------------
def _ensure_floating_window(win: tk.Misc) -> None:
    """Best-effort: ensure a Tk/Toplevel has normal decorations and can be moved.

    On some VM/window-manager combinations (and occasionally after PyInstaller
    builds), Tk windows may appear borderless/undecorated, which also makes
    dialogs feel "stuck". The implementation below keeps Linux/Windows behavior
    while avoiding X11-only hints on macOS.
    """
    try:
        # Allow WM decorations (title bar, borders, close button).
        try:
            win.wm_overrideredirect(False)
        except Exception:
            pass

        # Ensure we are not in a fullscreen state.
        try:
            win.attributes("-fullscreen", False)
        except Exception:
            pass

        # X11 window type hints are useful on Linux but can be noisy/ignored on macOS.
        if is_linux():
            try:
                win.wm_attributes("-type", "normal")
            except Exception:
                pass

        # Force WM to re-evaluate geometry/decorations.
        try:
            win.update_idletasks()
        except Exception:
            pass
    except Exception:
        pass


def _windows_subprocess_silent_kwargs() -> dict:
    """Return subprocess kwargs that hide console windows on Windows.

    Use this whenever we launch the console-based properties executable from the GUI.
    On Linux/macOS it returns an empty dict.
    """
    if not is_windows():
        return {}
    kw = {}
    try:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        pass
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kw["startupinfo"] = startupinfo
    except Exception:
        pass
    return kw


def _best_effort_make_executable(path_like: str | os.PathLike | None) -> Optional[Path]:
    """Resolve an executable path and, on Unix-like systems, ensure it is runnable.

    This is intentionally best-effort: it never raises just because chmod was not
    possible. On Windows we leave the file unchanged.
    """
    resolved = resolve_executable(path_like)
    if resolved is None:
        return None
    if is_windows():
        return resolved
    try:
        mode = resolved.stat().st_mode
        if not os.access(str(resolved), os.X_OK):
            resolved.chmod(mode | 0o111)
    except Exception:
        pass
    return resolved


def _open_file_uri_in_browser(target: Path) -> bool:
    """Open a local HTML file in the default browser across platforms."""
    target = Path(target).expanduser().resolve()
    uri = target.as_uri()
    try:
        if webbrowser.open_new_tab(uri):
            return True
    except Exception:
        pass

    try:
        if is_macos():
            subprocess.Popen(["open", str(target)])
            return True
        if is_windows():
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True
        # Prefer the filesystem path on Linux. Some browser sandboxes handle
        # direct local paths better than file:// URIs for user-generated files.
        subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception:
        return False


def _show_plotly_figure(fig, *, saved_html: Path | None = None) -> None:
    """Open a Plotly figure in a browser using a robust local file path.

    Temporary previews are stored under the user's TopIso3D config directory instead
    of /tmp. This avoids issues with browsers/sandboxes that may not resolve transient
    files created in /tmp fast enough or at all.
    """
    if saved_html is not None:
        saved_html = Path(saved_html).expanduser().resolve()
        fig.write_html(str(saved_html), include_plotlyjs=True, auto_open=False)
        if not _open_file_uri_in_browser(saved_html):
            raise RuntimeError(f"Could not open HTML viewer: {saved_html}")
        return

    tmp_dir = get_public_preview_dir(APP_NAME)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"topiso3d_plotly_{os.getpid()}_{int(time.time() * 1000)}.html"
    fig.write_html(str(tmp), include_plotlyjs=True, auto_open=False)
    if not tmp.exists():
        raise RuntimeError(f"Temporary Plotly HTML was not created: {tmp}")
    if not _open_file_uri_in_browser(tmp):
        raise RuntimeError(f"Could not open Plotly figure in the system browser: {tmp}")



def is_frozen_app() -> bool:
    """Return True when running from a frozen bundle/executable."""
    return bool(getattr(sys, "frozen", False))


def get_runtime_base_dir() -> Path:
    """Return the runtime base dir, including PyInstaller bundles."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            return Path(meipass).resolve()
        except Exception:
            return Path(meipass)
    return Path(__file__).resolve().parent


def collect_system_diagnostics(app: Optional["App"] = None) -> dict:
    """Collect lightweight diagnostics useful for first real Mac tests."""
    diag = {
        "platform_name": get_platform_name(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_executable": sys.executable,
        "python_version": sys.version.replace("\n", " "),
        "tk_patchlevel": "",
        "runtime_base_dir": str(get_runtime_base_dir()),
        "is_frozen": is_frozen_app(),
        "cwd": str(Path.cwd()),
        "config_dir": str(_config_dir()),
        "properties_configured": "",
        "properties_resolved": "",
        "workspace_dir": "",
        "workspace_exists": "",
        "workspace_writable": "",
    }
    try:
        diag["tk_patchlevel"] = str(tk.Tcl().eval("info patchlevel"))
    except Exception:
        pass
    if app is not None:
        try:
            pexe = getattr(app.state, "properties_exe", None)
            diag["properties_configured"] = str(pexe or "")
            resolved = resolve_executable(str(pexe or "").strip())
            diag["properties_resolved"] = str(resolved or "")
        except Exception:
            pass
        try:
            ws = getattr(app.state, "workspace_dir", None)
            diag["workspace_dir"] = str(ws or "")
            if ws:
                wsp = Path(ws)
                diag["workspace_exists"] = str(wsp.exists())
                diag["workspace_writable"] = str(os.access(str(wsp), os.W_OK))
        except Exception:
            pass
    return diag


def format_system_diagnostics(diag: dict) -> str:
    order = [
        "platform_name",
        "platform_system",
        "platform_release",
        "platform_version",
        "machine",
        "processor",
        "python_executable",
        "python_version",
        "tk_patchlevel",
        "runtime_base_dir",
        "is_frozen",
        "cwd",
        "config_dir",
        "properties_configured",
        "properties_resolved",
        "workspace_dir",
        "workspace_exists",
        "workspace_writable",
    ]
    lines = ["TopIso3D diagnostics", "====================", ""]
    for key in order:
        val = diag.get(key, "")
        lines.append(f"{key}: {val}")
    return "\n".join(lines)

# ----------------------------
# Settings (Phase 0 - minimal)
# ----------------------------
APP_NAME = "TopIso3D"
SETTINGS_FILENAME = "settings.json"

def get_platform_name() -> str:
    """Return a normalized platform name: linux, windows or macos."""
    sys_name = platform.system().lower()
    if sys_name.startswith("win"):
        return "windows"
    if sys_name == "darwin":
        return "macos"
    return "linux"

def is_windows() -> bool:
    return get_platform_name() == "windows"

def is_macos() -> bool:
    return get_platform_name() == "macos"

def is_linux() -> bool:
    return get_platform_name() == "linux"

def get_user_config_dir(app_name: str = APP_NAME) -> Path:
    """Return the per-user config directory following native OS conventions."""
    if is_windows():
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / app_name
        return Path.home() / "AppData" / "Roaming" / app_name

    if is_macos():
        return Path.home() / "Library" / "Application Support" / app_name

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / app_name

def _config_dir() -> Path:
    """Return per-user config dir with a resilient fallback for all platforms."""
    d = get_user_config_dir(APP_NAME)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path.home() / f".{APP_NAME.lower()}"
        d.mkdir(parents=True, exist_ok=True)
    return d

def settings_path() -> Path:
    return _config_dir() / SETTINGS_FILENAME


def get_public_preview_dir(app_name: str = APP_NAME) -> Path:
    """Return a browser-friendly preview folder for temporary HTML plots.

    On Linux, browsers distributed as Snap/Flatpak often cannot open files inside
    hidden directories such as ~/.config. We therefore prefer a visible location
    under ~/Documents when possible.
    """
    home = Path.home()
    candidates = []
    docs = home / "Documents"
    if docs.exists():
        candidates.append(docs / app_name / "plotly_tmp")
    candidates.append(home / f"{app_name}_plotly_tmp")
    if is_windows():
        dl = home / "Downloads"
        if dl.exists():
            candidates.insert(0, dl / app_name / "plotly_tmp")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
    # Last resort
    d = Path(tempfile.gettempdir()) / f"{app_name.lower()}_plotly_tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d

def load_settings() -> dict:
    sp = settings_path()
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_settings(data: dict) -> None:
    sp = settings_path()
    try:
        sp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # Best-effort only; do not crash the app.
        pass


DEFAULT_TRHO_OUTPUT_NAMES = ["trho.out", "trho.outp"]
DEFAULT_TLAP_OUTPUT_NAMES = ["tlap.out", "tlap.outp"]
DEFAULT_ATBP_OUTPUT_NAMES = ["atbp.out", "atbp.outp"]


def _sanitize_output_name_token(token: str) -> str:
    s = str(token or "").strip()
    s = s.replace("\\", "/").split("/")[-1]
    return s


def parse_output_name_list(raw, default_names: List[str]) -> List[str]:
    """Parse configurable output file names from settings/UI.

    Accepted input:
      - list/tuple of names
      - semicolon/comma/newline separated string
    """
    if isinstance(raw, (list, tuple)):
        seq = list(raw)
    else:
        txt = str(raw or "")
        seq = re.split(r"[;,\n]+", txt)

    out = []
    seen = set()
    for item in seq:
        name = _sanitize_output_name_token(item)
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(name)

    if out:
        return out

    defaults = []
    seen = set()
    for item in (default_names or []):
        name = _sanitize_output_name_token(item)
        if not name:
            continue
        low = name.lower()
        if low in seen:
            continue
        seen.add(low)
        defaults.append(name)
    return defaults


def format_output_name_list(names, default_names: List[str]) -> str:
    vals = parse_output_name_list(names, default_names)
    return "; ".join(vals)

def resolve_executable(exe: str | os.PathLike | None) -> Optional[Path]:
    """Resolve an executable that may be an absolute path or a command in PATH."""
    if exe is None:
        return None
    s = str(exe).strip()
    if not s:
        return None
    pth = Path(s).expanduser()
    if pth.is_file():
        return pth.resolve()
    w = shutil.which(s)
    return Path(w).resolve() if w else None

def get_default_properties_candidates() -> List[str | Path]:
    """Return OS-specific candidates for the CRYSTAL/TOPOND properties executable."""
    candidates: List[str | Path] = []

    if is_windows():
        env_keys = ("CRYSPROP_PROPERTIES", "TOPISO3D_PROPERTIES", "PROPERTIES_EXE")
        for key in env_keys:
            val = os.environ.get(key)
            if val:
                candidates.append(val)
        candidates.extend([
            "properties.exe",
            "properties",
        ])
        return candidates

    if is_macos():
        env_keys = ("CRYSPROP_PROPERTIES", "TOPISO3D_PROPERTIES", "PROPERTIES_EXE")
        for key in env_keys:
            val = os.environ.get(key)
            if val:
                candidates.append(val)
        candidates.extend([
            "properties",
            "/Applications/CRYSTAL/properties",
        ])
        return candidates

    env_keys = ("CRYSPROP_PROPERTIES", "TOPISO3D_PROPERTIES", "PROPERTIES_EXE")
    for key in env_keys:
        val = os.environ.get(key)
        if val:
            candidates.append(val)
    candidates.extend([
        Path("/usr/crysprop/CRYSTAL_f_orb/properties"),
        "properties",
    ])
    return candidates

def resolve_default_properties_executable() -> Path:
    """Resolve the best default properties executable for the current platform."""
    for cand in get_default_properties_candidates():
        resolved = resolve_executable(cand)
        if resolved is not None:
            return resolved
    return Path("properties.exe" if is_windows() else "properties")

def safe_symlink_or_copy(src: Path, dst: Path) -> Tuple[bool, str]:
    """Prepare dst from src, preferring a symlink and falling back to a copy."""
    src = Path(src).expanduser().resolve()
    dst = Path(dst).expanduser()

    try:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
    except Exception:
        pass

    try:
        target = src.name if src.parent.resolve() == dst.parent.resolve() else src
        dst.symlink_to(target)
        return True, f"created symlink {dst.name} -> {src.name}"
    except Exception:
        try:
            shutil.copy2(src, dst)
            return True, f"copied {src.name} -> {dst.name}"
        except Exception as e:
            return False, f"failed to prepare {dst.name} from {src.name}: {e}"


def run_external_program(
    exe: str | os.PathLike,
    *,
    cwd: str | os.PathLike,
    stdin_path: str | os.PathLike | None = None,
    stdout_path: str | os.PathLike | None = None,
    line_callback: Optional[Callable[[str], None]] = None,
    timeout: float | None = None,
    encoding: str = "utf-8",
) -> dict:
    """Run an external program in a platform-robust way.

    The program is launched without an intermediate shell, with stdout/stderr
    merged and decoded using a tolerant text mode. Output can be streamed to a
    callback and optionally mirrored to a file.
    """
    exe_path = resolve_executable(exe)
    if exe_path is None:
        raise FileNotFoundError(f"Executable not found: {exe!r}")

    cwd_path = Path(cwd).expanduser().resolve()
    stdin_file = None
    stdout_file = None
    process = None
    start = time.time()
    timed_out = False
    cmd = [str(exe_path)]

    try:
        if stdin_path is not None:
            stdin_file = open(Path(stdin_path).expanduser(), "r", encoding=encoding, errors="replace")
        if stdout_path is not None:
            stdout_file = open(Path(stdout_path).expanduser(), "w", encoding=encoding, errors="replace")

        process = subprocess.Popen(
            cmd,
            cwd=str(cwd_path),
            stdin=stdin_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=encoding,
            errors="replace",
            bufsize=1,
            **_windows_subprocess_silent_kwargs(),
        )

        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if line:
                if line_callback is not None:
                    line_callback(line.rstrip("\n"))
                if stdout_file is not None:
                    stdout_file.write(line)
            elif process.poll() is not None:
                break

            if timeout is not None and (time.time() - start) > timeout:
                timed_out = True
                process.kill()
                break

        if timed_out and process.stdout is not None:
            for line in process.stdout:
                if line_callback is not None:
                    line_callback(line.rstrip("\n"))
                if stdout_file is not None:
                    stdout_file.write(line)

        exit_code = process.wait() if process is not None else -1
    finally:
        if stdout_file is not None:
            stdout_file.flush()
            stdout_file.close()
        if stdin_file is not None:
            stdin_file.close()

    duration_s = float(time.time() - start)
    return {
        "command": cmd,
        "cwd": str(cwd_path),
        "exe_resolved": str(exe_path),
        "exit_code": int(exit_code),
        "duration_s": duration_s,
        "timed_out": bool(timed_out),
        "stdout_path": str(Path(stdout_path).expanduser()) if stdout_path is not None else "",
        "stdin_path": str(Path(stdin_path).expanduser()) if stdin_path is not None else "",
    }

# Sidebar button sizing (ttk uses Style for font; width is in text units)
SIDEBAR_BTN_WIDTH = 15

# Minimal infrastructure helpers (engine-stable)
# ----------------------------
def _ws_dir(ctx: "ProjectContext") -> Optional[Path]:
    return ctx.workspace_dir

def _trho_dir(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "trho") if ws else None

def _pl2d_runs_dir(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "pl2d_runs") if ws else None

def _log_path(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "topiso3d.log") if ws else None


def _workspace_state_path(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "topiso3d_state.json") if ws else None

def _trho_runs_dir(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "trho_runs") if ws else None

def _tlap_runs_dir(ctx: "ProjectContext") -> Optional[Path]:
    ws = _ws_dir(ctx)
    return (ws / "tlap_runs") if ws else None

def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write_json_file(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass

def log_event(ctx: "ProjectContext", message: str) -> None:
    """Append a timestamped line to workspace/topiso3d.log (best-effort).
    NOTE: Avoid calling this from high-frequency UI refreshes.
    """
    try:
        if not getattr(ctx, "enable_log", True):
            return
        lp = _log_path(ctx)
        if lp is None:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")

        # Trim if too large (best-effort)
        maxb = int(getattr(ctx, "max_log_bytes", 0) or 0)
        keepb = int(getattr(ctx, "keep_log_bytes", 0) or 0)
        if maxb > 0 and lp.exists():
            try:
                sz = lp.stat().st_size
                if sz > maxb and keepb > 0:
                    with lp.open("rb") as fb:
                        fb.seek(-min(keepb, sz), os.SEEK_END)
                        tail = fb.read()
                    with lp.open("wb") as fbw:
                        fbw.write(tail)
            except Exception:
                pass
    except Exception:
        # Logging must never break the GUI.
        pass



def _labeled_entry(parent, label, var: tk.StringVar, width: int = 10) -> ttk.Frame:
    """Small helper for (Label + Entry) packs.

    Returns a Frame with two attached attributes for later enable/disable:
      - frm._lbl : ttk.Label
      - frm._ent : ttk.Entry
    """
    frm = ttk.Frame(parent)
    if isinstance(label, tk.StringVar):
        lbl = ttk.Label(frm, textvariable=label)
    else:
        lbl = ttk.Label(frm, text=str(label))
    lbl.pack(side="left")
    ent = ttk.Entry(frm, textvariable=var, width=width)
    ent.pack(side="left", padx=(6, 0))
    # attach handles for UX toggling
    frm._lbl = lbl  # type: ignore[attr-defined]
    frm._ent = ent  # type: ignore[attr-defined]
    return frm


def _set_labeled_state(frm: ttk.Frame, enabled: bool) -> None:
    """Enable/disable a labeled entry frame and gray out label when disabled."""
    try:
        ent = getattr(frm, "_ent")
        lbl = getattr(frm, "_lbl")
    except Exception:
        return
    try:
        ent.configure(state=("normal" if enabled else "disabled"))
    except Exception:
        try:
            ent.state(["!disabled"] if enabled else ["disabled"])
        except Exception:
            pass
    try:
        # ttk labels support foreground; if a theme ignores it, it's still harmless.
        lbl.configure(foreground=("black" if enabled else "#666666"))
    except Exception:
        pass


def _bind_vertical_mousewheel(widget: tk.Misc, canvas: tk.Canvas) -> None:
    """Best-effort mousewheel binding for scrollable Tk canvases across platforms."""
    def _on_mousewheel(event):
        try:
            if getattr(event, 'num', None) == 4:
                canvas.yview_scroll(-1, 'units')
                return 'break'
            if getattr(event, 'num', None) == 5:
                canvas.yview_scroll(1, 'units')
                return 'break'
            delta = int(getattr(event, 'delta', 0) or 0)
            if delta == 0:
                return None
            if is_windows():
                steps = -int(delta / 120) if delta else 0
            else:
                steps = -1 if delta > 0 else 1
            if steps:
                canvas.yview_scroll(steps, 'units')
                return 'break'
        except Exception:
            return None
        return None

    for seq in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>', '<Button-5>'):
        try:
            widget.bind(seq, _on_mousewheel, add=True)
        except Exception:
            pass


def _make_scrollable_frame(parent: tk.Misc, *, canvas_bg: str | None = None) -> tuple[ttk.Frame, tk.Canvas, ttk.Scrollbar, ttk.Frame]:
    """Create a vertical scroll container and return (outer, canvas, scrollbar, inner)."""
    if canvas_bg is None:
        try:
            canvas_bg = str(ttk.Style().lookup("TFrame", "background") or "").strip()
        except Exception:
            canvas_bg = ""
        if not canvas_bg:
            try:
                canvas_bg = str(parent.winfo_toplevel().cget("bg") or "").strip()
            except Exception:
                canvas_bg = ""
        if not canvas_bg:
            canvas_bg = "#999999"

    outer = ttk.Frame(parent, style="Content.TFrame")
    canvas = tk.Canvas(outer, bg=canvas_bg, highlightthickness=0, bd=0)
    vbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=vbar.set)

    canvas.grid(row=0, column=0, sticky='nsew')
    vbar.grid(row=0, column=1, sticky='ns')
    outer.rowconfigure(0, weight=1)
    outer.columnconfigure(0, weight=1)

    inner = ttk.Frame(canvas, style="Content.TFrame")
    window_id = canvas.create_window((0, 0), window=inner, anchor='nw')

    def _sync_scrollregion(_event=None):
        try:
            canvas.configure(scrollregion=canvas.bbox('all'))
        except Exception:
            pass

    def _sync_inner_width(event=None):
        try:
            width = max(1, canvas.winfo_width())
            canvas.itemconfigure(window_id, width=width)
        except Exception:
            pass

    inner.bind('<Configure>', _sync_scrollregion, add=True)
    canvas.bind('<Configure>', _sync_inner_width, add=True)
    _bind_vertical_mousewheel(canvas, canvas)
    _bind_vertical_mousewheel(inner, canvas)

    try:
        canvas.configure(takefocus=0)
    except Exception:
        pass

    return outer, canvas, vbar, inner



# -----------------------------
# UI Theme (v2-like palette)
# -----------------------------
UI_BG_MAIN = "#999999"      # main window background
UI_BG_DARK = "#4F4F4F"      # dark header background
UI_BG_FIELD = "#cccccc"     # entry/list background
UI_BG_PANEL = "#D3D3D3"     # light panel background
UI_ACCENT = "#E6BA00"       # yellow accent
UI_FG_MAIN = "black"
UI_FG_MUTED = UI_BG_DARK

# -----------------------------
# Atomic-number normalization
# -----------------------------
# CRYSTAL/TOPOND may print pseudopotential-coded atomic numbers such as 241 for Nb.
# For visualization and labels we normalize them to the corresponding chemical Z.
_PSEUDO_Z_MAP = {
    241: 41,  # Nb
}

def normalize_atomic_number(z):
    try:
        zi = int(float(z))
    except Exception:
        return z
    return _PSEUDO_Z_MAP.get(zi, zi)


# -----------------------------
# State (ProjectContext)
# -----------------------------
@dataclass
class ProjectContext:
    workspace_dir: Optional[Path] = None

    # Auto-detected
    has_fort9: bool = False
    f9_candidates: List[Path] = field(default_factory=list)
    can_write: bool = False
    workspace_ok: bool = False
    workspace_msg: str = "—"
    properties_exe: Optional[Path] = None

    # Job state
    trho_done: bool = False
    status: str = "Ready."
    active_trho_run: Optional[Path] = None
    active_trho_label: str = "—"
    active_tlap_run: Optional[Path] = None
    active_tlap_label: str = "—"
    trho_mode: str = "relaxed"
    trho_ui_mode: str = "simple"
    trho_simple_preset: str = "relaxed"
    trho_adv_iauto: str = "-1"
    tlap_ui_mode: str = "simple"
    tlap_simple_preset: str = "relaxed"
    tlap_adv_iauto: str = "0"

    # Parsed TRHO/TLAP (filled after successful parse)
    trho_parsed: Optional["TrhoParsed"] = None
    trho_parse_attempted_out: str = ""
    tlap_parsed: Optional["TlapParsed"] = None
    tlap_done: bool = False
    tlap_parse_error: Optional[str] = None
    tlap_parse_attempted_out: str = ""
    report_method: str = "TRHO"
    df_bcp_props: Optional[pd.DataFrame] = None
    df_true_atoms: Optional[pd.DataFrame] = None
    pending_trho_run_name: str = ""
    pending_tlap_run_name: str = ""

    # PL2D state
    pl2d_cfg: Optional[dict] = None
    pl2d_signature: Optional[str] = None
    pl2d_run_dir: Optional[Path] = None


    pl2d_running: bool = False  # True while PL2D loop is running

    # Logging controls (topiso3d.log)
    enable_log: bool = True
    max_log_bytes: int = 512_000          # ~500 KB
    keep_log_bytes: int = 200_000         # keep last ~200 KB when trimming
    delete_log_on_trho_success: bool = False
    cleanup_policy: str = "minimal"  # one of: minimal, standard, none

    def __post_init__(self):
        if self.f9_candidates is None:
            self.f9_candidates = []


    # Backward-compatible alias: some code expects ctx.workspace_dir
    @property
    def workspace(self) -> Optional[Path]:
        return self.workspace_dir

    @workspace.setter
    def workspace(self, value: Optional[Path]):
        self.workspace_dir = value

# -----------------------------
# Parsed results (TRHO)
# -----------------------------

@dataclass
class TrhoParsed:
    """Container for parsed TRHO (TOPOND/Properties) results.

    We keep only what is needed for the next steps (reports + PL2D integration),
    and we avoid globals by storing everything inside this object / context.
    """

    str_type: str  # "Crystal" or "Molecule"
    df_primitive: pd.DataFrame
    df_true_atoms: pd.DataFrame
    df_cpviewer_atoms: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_cpviewer_pool_atoms: pd.DataFrame = field(default_factory=pd.DataFrame)
    cell_vectors_ang: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=float))

    # Minimal CP coordinate tables (Angstrom)
    df_bcp_coords: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_attr: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_ring: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_cage: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Verbose / property tables
    df_bcp_props: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_rcp_props: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_ccp_props: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Optional: non-nuclear attractors (can be empty)
    df_att_nao_nucl: pd.DataFrame = field(default_factory=pd.DataFrame)
    nna_count: int = 0
    nna_messages: List[str] = field(default_factory=list)
    nna_cutoff_ang: float = 0.350



def _parse_direct_lattice_vectors_ang(lines: List[str]) -> np.ndarray:
    """Parse the 3 direct lattice vectors in Angstrom from trho.out.

    Expected block::
        DIRECT LATTICE VECTOR COMPONENTS (ANGSTROM)
            ax ay az
            bx by bz
            cx cy cz
    Returns an empty (0,3) array when the block is absent or incomplete.
    """
    try:
        for i, ln in enumerate(lines):
            if "DIRECT LATTICE VECTOR COMPONENTS (ANGSTROM)" in ln.upper():
                rows = []
                j = i + 1
                while j < len(lines) and len(rows) < 3:
                    vals = re.findall(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?", lines[j])
                    if len(vals) >= 3:
                        rows.append([float(vals[0]), float(vals[1]), float(vals[2])])
                    j += 1
                if len(rows) == 3:
                    return np.asarray(rows, dtype=float)
                break
    except Exception:
        pass
    return np.zeros((0, 3), dtype=float)


def parse_trho_out(
    out_path: Path,
    *,
    bohr_to_ang: float = 0.5291772083,
    open_shell: bool = False,
    slab_2d: bool = False,
    # parâmetros geométricos para mapear coordenadas -> "pt_*" (só se você quiser já aqui)
    xmi: float | None = None,
    xma: float | None = None,
    ymi: float | None = None,
    yma: float | None = None,
    zmi: float | None = None,
    zma: float | None = None,
    x_inc: float | None = None,
    y_inc: float | None = None,
    n_planos: int | None = None,
    delta_z: float | None = None,
    nna_cutoff_ang: float = 0.350,
) -> TrhoParsed:

    out_path = Path(out_path).expanduser().resolve()
    if not out_path.exists():
        raise FileNotFoundError(out_path)

    txt = out_path.read_text(errors="ignore").splitlines()
    cell_vectors_ang = _parse_direct_lattice_vectors_ang(txt)

    # --- possible non-nuclear attractors (NNAs) explicitly flagged by TOPOND ---
    # Count *occurrences*, not unique message strings. Some TOPOND outputs repeat the
    # same warning text for multiple CPs, and de-duplicating would undercount NNAs.
    # We count the canonical warning phrase in the full text first; if absent, fall back
    # to a line-based scan for broader compatibility with minor output variations.
    raw_text = "\n".join(txt)
    canonical_pat = re.compile(
        r"THE\s*\(3,\s*-3\)\s*ATTRACTOR\s+IS\s+PROBABLY\s+A\s+NON-NUCLEAR\s+ATTRACTOR",
        re.IGNORECASE,
    )
    canonical_matches = list(canonical_pat.finditer(raw_text))
    nna_count = len(canonical_matches)

    nna_messages: List[str] = []
    if nna_count > 0:
        # Preserve one short message per detected occurrence for optional downstream use.
        nna_messages = [
            "THE (3,-3) ATTRACTOR IS PROBABLY A NON-NUCLEAR ATTRACTOR"
            for _ in canonical_matches
        ]
    else:
        for ln in txt:
            up = ln.upper()
            if "NON-NUCLEAR ATTRACTOR" in up:
                s = " ".join(str(ln).split())
                if s:
                    nna_messages.append(s)
        nna_count = len(nna_messages)
    # ---- robust token helpers (output format varies between PROPERTIES versions) ----
    _re_float = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")
    _re_int = re.compile(r"\b\d+\b")

    def _floats(line: str) -> list[float]:
        try:
            return [float(m) for m in _re_float.findall(line)]
        except Exception:
            return []

    def _ints(line: str) -> list[int]:
        try:
            return [int(m) for m in _re_int.findall(line)]
        except Exception:
            return []

    def _last_n(values: list[float], n: int) -> list[float]:
        if len(values) >= n:
            return values[-n:]
        return []


    # --- detect crystal vs molecule ---
    str_type = "Molecule"
    if any("DIRECT LATTICE" in ln for ln in txt):
        str_type = "Crystal"

    # --- number of atoms per cell ---
    num_atom = None
    for ln in txt:
        if "N. OF ATOMS PER CELL" in ln:
            parts = ln.split()
            # no seu código era parts[5]
            try:
                num_atom = int(parts[5])
            except Exception:
                pass
            break
    if num_atom is None:
        # Try a more tolerant detection (CRYSTAL output varies between versions).
        # We look for any line mentioning atoms per cell and grab the last integer in it.
        import re as _re
        for ln in txt:
            if ("ATOMS PER CELL" in ln) or ("N. OF ATOMS" in ln):
                ints = _re.findall(r"\\b\\d+\\b", ln)
                if ints:
                    try:
                        num_atom = int(ints[-1])
                        break
                    except Exception:
                        pass
        if num_atom is None:
            # Do not hard-fail: keep parsing what we can.
            num_atom = 0

    # --- TRUE atoms list ---
    atom_true = []
    for ln in txt:
        if "IN THE UNIT CELL" in ln:
            parts = ln.split()
            # seu if len(parts[3]) > 1 etc. (mantive a mesma heurística)
            if len(parts) >= 6:
                if len(parts[3]) > 1:
                    atom_true.append(int(parts[4]))
                else:
                    atom_true.append(int(parts[5]))

    atom_true_set = set(atom_true)

    # --- primitive atoms coordinates block ---
    # encontra linha "ATOM  SHEL    X(AU) ..."
    i0 = None
    for i, ln in enumerate(txt):
        if "ATOM  SHEL" in ln and "X(AU)" in ln and "Y(AU)" in ln and "Z(AU)" in ln:
            i0 = i
            break

    rows = []
    if i0 is not None and num_atom > 0:
        # no output do CRYSTAL/PROPERTIES, a tabela vem assim (exemplo):
        #  12 MG   3     0.000     0.000     0.000
        #   8 O    3     3.978     3.978     3.978
        start = i0 + 2
        for k in range(num_atom):
            if start + k >= len(txt):
                break
            parts = txt[start + k].split()
            if not parts:
                continue

            # formatos possíveis:
            # A) Z SYM SHEL X Y Z   (mais comum)
            # B) Z SHEL X Y Z       (mais raro)
            try:
                z = int(parts[0])
            except Exception:
                continue

            sym = ""
            if len(parts) >= 6 and parts[1].isalpha():
                # A)
                try:
                    sym = str(parts[1]).strip().capitalize()
                    shel = int(parts[2])
                    x = float(parts[3]); y = float(parts[4]); zc = float(parts[5])
                except Exception:
                    continue
            elif len(parts) >= 5:
                # B)
                try:
                    shel = int(parts[1])
                    x = float(parts[2]); y = float(parts[3]); zc = float(parts[4])
                except Exception:
                    continue
            else:
                continue

            z_norm = normalize_atomic_number(z)
            if not sym:
                try:
                    sym = atom_symbol(int(z_norm))
                except Exception:
                    sym = ""
            rows.append((z_norm, z, sym, x, y, zc))

    df_primitive = pd.DataFrame(rows, columns=["ELEMENT", "ELEMENT_RAW", "SYMBOL", "x_BOHR", "y_BOHR", "z_BOHR"])
    df_primitive.index = np.arange(1, len(df_primitive) + 1)

    # TRUE atoms: se o bloco "IN THE UNIT CELL" não existir, considera todos como TRUE
    if len(atom_true_set) == 0:
        df_primitive["TRUE"] = 1
    else:
        df_primitive["TRUE"] = [1 if i in atom_true_set else 0 for i in df_primitive.index]

    df_primitive["X_ANGSTROM"] = df_primitive["x_BOHR"] * bohr_to_ang
    df_primitive["Y_ANGSTROM"] = df_primitive["y_BOHR"] * bohr_to_ang
    df_primitive["Z_ANGSTROM"] = df_primitive["z_BOHR"] * bohr_to_ang

    df_true_atoms = df_primitive[df_primitive["TRUE"] == 1].copy()
    df_true_atoms = df_true_atoms.drop(columns=["x_BOHR", "y_BOHR", "z_BOHR", "TRUE"])

    # --- CP counts (robusto: tenta bloco verboso "CP TYPE" e também a tabela compacta)
    # Verboso (linhas "CP TYPE ...")
    # Match CP TYPE lines robustly (allow spaces like '(3, +1)')
    re_bcp = re.compile(r"\(3,\s*-1\)")
    re_attr = re.compile(r"\(3,\s*-3\)")
    re_rcp = re.compile(r"\(3,\s*\+1\)")
    re_ccp = re.compile(r"\(3,\s*\+3\)")

    cont_bcp_verbose = sum(1 for ln in txt if "CP TYPE" in ln and re_bcp.search(ln))
    cont_attr_verbose = sum(1 for ln in txt if "CP TYPE" in ln and re_attr.search(ln))
    cont_rcp_verbose = sum(1 for ln in txt if "CP TYPE" in ln and re_rcp.search(ln))
    cont_ccp_verbose = sum(1 for ln in txt if "CP TYPE" in ln and re_ccp.search(ln))

    # Compacto (tabela "CRITICAL POINTS FOUND").
    # Algumas versões do TOPOND inserem espaços no TYPE (ex.: "(3, -3)") e/ou usam
    # notação científica, então o parser abaixo é propositalmente tolerante.
    compact_rows = []
    in_cp_table = False
    header_seen = False
    cp_line_re = re.compile(
        r"^\s*(\d+)\)\s+([+\-0-9.Ee]+)\s+([+\-0-9.Ee]+)\s+([+\-0-9.Ee]+)\s+(\(\s*3\s*,\s*[+\-]?\d\s*\))\s*(.*)$"
    )
    for ln in txt:
        if "C R I T I C A L  P O I N T S  F O U N D" in ln:
            in_cp_table = True
            header_seen = False
            continue
        if in_cp_table and ("CP N." in ln and "TYPE" in ln):
            header_seen = True
            continue
        if in_cp_table and header_seen:
            m = cp_line_re.match(ln)
            if m:
                tail_floats = _floats(m.group(6))
                cp_type = re.sub(r"\s+", "", m.group(5))
                row = {
                    "CP_N": int(m.group(1)),
                    "X_ANG": float(m.group(2)),
                    "Y_ANG": float(m.group(3)),
                    "Z_ANG": float(m.group(4)),
                    "TYPE": cp_type,
                    "RHO": np.nan,
                    "LAPL": np.nan,
                    "L1": np.nan,
                    "L2": np.nan,
                    "L3": np.nan,
                    "ELLIP": np.nan,
                }
                if len(tail_floats) >= 1:
                    row["RHO"] = float(tail_floats[0])
                if len(tail_floats) >= 2:
                    row["LAPL"] = float(tail_floats[1])
                if len(tail_floats) >= 3:
                    row["L1"] = float(tail_floats[2])
                if len(tail_floats) >= 4:
                    row["L2"] = float(tail_floats[3])
                if len(tail_floats) >= 5:
                    row["L3"] = float(tail_floats[4])
                if len(tail_floats) >= 6:
                    row["ELLIP"] = float(tail_floats[5])
                compact_rows.append(row)
                continue
            # heurística de parada: uma linha longa de asteriscos depois da tabela ou fim do bloco
            if ln.strip().startswith("********") and compact_rows:
                break

    cont_bcp_compact = sum(1 for r in compact_rows if str(r.get("TYPE", "")).replace(" ", "") == "(3,-1)")
    cont_attr_compact = sum(1 for r in compact_rows if str(r.get("TYPE", "")).replace(" ", "") == "(3,-3)")
    cont_rcp_compact = sum(1 for r in compact_rows if str(r.get("TYPE", "")).replace(" ", "") == "(3,+1)")
    cont_ccp_compact = sum(1 for r in compact_rows if str(r.get("TYPE", "")).replace(" ", "") == "(3,+3)")

    # Usa o que existir (máximo entre verboso e compacto)
    cont_bcp = max(cont_bcp_verbose, cont_bcp_compact)
    cont_attr = max(cont_attr_verbose, cont_attr_compact)
    cont_rcp = max(cont_rcp_verbose, cont_rcp_compact)
    cont_ccp = max(cont_ccp_verbose, cont_ccp_compact)

    # pré-alocar arrays
    rho   = np.zeros(cont_bcp)
    grho  = np.zeros(cont_bcp)
    lap   = np.zeros(cont_bcp)
    gkin  = np.zeros(cont_bcp)
    kkin  = np.zeros(cont_bcp)
    vir   = np.zeros(cont_bcp)
    elf   = np.zeros(cont_bcp)
    elfb  = np.zeros(cont_bcp) if open_shell else None
    rhoa  = np.zeros(cont_bcp) if open_shell else None
    spin  = np.zeros(cont_bcp) if open_shell else None
    ellip = np.zeros(cont_bcp)
    eig   = np.zeros((cont_bcp, 3))
    neigh1 = np.full(cont_bcp, "", dtype="U24")
    neigh2 = np.full(cont_bcp, "", dtype="U24")
    dist1 = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    dist2 = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    xyz_bcp = np.zeros((cont_bcp, 3))
    attr1_atom_id = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr2_atom_id = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr1_x_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr1_y_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr1_z_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr2_x_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr2_y_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr2_z_ang = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr1_traj_len = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    attr2_traj_len = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    bpl_arr = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    rab_arr = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])
    bpl_over_rab_arr = np.full(cont_bcp, np.nan) if cont_bcp else np.array([])

    # --- RCP/CCP arrays ---
    # Coordenadas (AU)
    xyz_ring = np.zeros((cont_rcp, 3)) if cont_rcp else np.zeros((0,3))
    xyz_cage = np.zeros((cont_ccp, 3)) if cont_ccp else np.zeros((0,3))

    # Propriedades (tentamos bloco verboso; se não houver, caímos no fallback da tabela compacta)
    rho_ring  = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    grho_ring = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    lap_ring  = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    gkin_ring = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    kkin_ring = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    vir_ring  = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    elf_ring  = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    elfb_ring = np.full(cont_rcp, np.nan) if (cont_rcp and open_shell) else None
    rhoa_ring = np.full(cont_rcp, np.nan) if (cont_rcp and open_shell) else None
    spin_ring = np.full(cont_rcp, np.nan) if (cont_rcp and open_shell) else None
    ellip_ring = np.full(cont_rcp, np.nan) if cont_rcp else np.array([])
    eig_ring   = np.full((cont_rcp, 3), np.nan) if cont_rcp else np.zeros((0,3))

    rho_cage  = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    grho_cage = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    lap_cage  = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    gkin_cage = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    kkin_cage = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    vir_cage  = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    elf_cage  = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    elfb_cage = np.full(cont_ccp, np.nan) if (cont_ccp and open_shell) else None
    rhoa_cage = np.full(cont_ccp, np.nan) if (cont_ccp and open_shell) else None
    spin_cage = np.full(cont_ccp, np.nan) if (cont_ccp and open_shell) else None
    ellip_cage = np.full(cont_ccp, np.nan) if cont_ccp else np.array([])
    eig_cage   = np.full((cont_ccp, 3), np.nan) if cont_ccp else np.zeros((0,3))

    # ---- helpers ----
    def atom_symbol(z: int) -> str:
        # ajuste se você já tiver uma tabela melhor no seu projeto
        try:
            z = int(normalize_atomic_number(z))
        except Exception:
            pass
        periodic = {
            1:"H", 2:"He", 3:"Li", 4:"Be", 5:"B", 6:"C", 7:"N", 8:"O", 9:"F", 10:"Ne",
            11:"Na",12:"Mg",13:"Al",14:"Si",15:"P",16:"S",17:"Cl",18:"Ar",
            19:"K",20:"Ca",21:"Sc",22:"Ti",23:"V",24:"Cr",25:"Mn",26:"Fe",
            27:"Co",28:"Ni",29:"Cu",30:"Zn",31:"Ga",32:"Ge",33:"As",34:"Se",
            35:"Br",36:"Kr",37:"Rb",38:"Sr",39:"Y",40:"Zr",41:"Nb",42:"Mo",
            43:"Tc",44:"Ru",45:"Rh",46:"Pd",47:"Ag",48:"Cd",49:"In",50:"Sn",
            51:"Sb",52:"Te",53:"I",54:"Xe",55:"Cs",56:"Ba",57:"La",73:"Ta"
        }
        return periodic.get(z, f"Z{z}")

    # ---- parse BCP section by scanning CP TYPE blocks ----
    b = 0
    r = 0
    c = 0

    for i, ln in enumerate(txt):
        if "CP TYPE" not in ln:
            continue

        if "(3,-1)" in ln:
            # Robust parse: output format can vary; prefer extracting trailing floats/ints rather than fixed columns.
            try:
                coord_vals = _last_n(_floats(txt[i + 1]), 3)
                if len(coord_vals) != 3:
                    raise ValueError("Could not parse BCP coordinates")
                xyz_bcp[b, :] = coord_vals

                # If crystal, there is often a FRACT line after COORD; skip it.
                j = i + 2
                if str_type == "Crystal":
                    j += 1

                vals = _last_n(_floats(txt[j]), 3)
                if len(vals) != 3:
                    raise ValueError("Could not parse BCP rho/grad/lap")
                rho[b], grho[b], lap[b] = vals
                j += 1

                if open_shell:
                    vals = _last_n(_floats(txt[j]), 2)
                    if len(vals) == 2:
                        rhoa[b], spin[b] = vals
                    j += 1

                vals = _last_n(_floats(txt[j]), 2)
                if len(vals) == 2:
                    gkin[b], kkin[b] = vals
                j += 1

                vals = _last_n(_floats(txt[j]), 1)
                if len(vals) == 1:
                    vir[b] = vals[0]
                j += 1

                vals = _last_n(_floats(txt[j]), 1)
                if len(vals) == 1:
                    elf[b] = vals[0]
                j += 1

                if open_shell:
                    vals = _last_n(_floats(txt[j]), 1)
                    if len(vals) == 1:
                        elfb[b] = vals[0]
                    j += 1

                # eigenvalues line is further down; try to locate within a small window
                # rather than relying on fixed offsets.
                eig_set = False
                for jj in range(j, min(j + 20, len(txt))):
                    fl = _floats(txt[jj])
                    if len(fl) >= 3 and ("EIGEN" in txt[jj].upper() or "L1" in txt[jj] or "L2" in txt[jj]):
                        vals3 = _last_n(fl, 3)
                        if len(vals3) == 3:
                            eig[b, 0], eig[b, 1], eig[b, 2] = vals3
                            eig_set = True
                            j = jj + 1
                            break
                if not eig_set:
                    # fallback: look for any line with >=3 floats later in the block
                    for jj in range(j, min(j + 25, len(txt))):
                        vals3 = _last_n(_floats(txt[jj]), 3)
                        if len(vals3) == 3:
                            eig[b, 0], eig[b, 1], eig[b, 2] = vals3
                            j = jj + 1
                            break

                # ellipticity: look for a line containing "ELLIP" or at least one float
                for jj in range(j, min(j + 25, len(txt))):
                    if "ELLIP" in txt[jj].upper() or "ELLIPT" in txt[jj].upper():
                        vals1 = _last_n(_floats(txt[jj]), 1)
                        if len(vals1) == 1:
                            ellip[b] = vals1[0]
                            j = jj + 1
                            break
                else:
                    vals1 = _last_n(_floats(txt[j]), 1) if j < len(txt) else []
                    if len(vals1) == 1:
                        ellip[b] = vals1[0]

                # Primary identification of the atoms connected by the BCP:
                # use TOPOND's own "SEARCH OF BOND PATH ATTRACTORS" section.
                # The local "CLUSTER OF ATOMS AROUND THE CP" is kept only as a fallback,
                # because it lists atoms near the CP, not necessarily the two termini of
                # the bond path.
                try:
                    search_end = min(i + 240, len(txt))
                    kk = i + 1
                    while kk < search_end and "SEARCH OF BOND PATH ATTRACTORS" not in txt[kk]:
                        kk += 1

                    attr_info = []
                    if kk < search_end:
                        ll = kk + 1
                        while ll < search_end and len(attr_info) < 2:
                            line_up = txt[ll].upper().replace(" ", "")
                            if "ATTRACTORCPTYPE" in line_up and "(3,-3)" in line_up:
                                coords_au = _last_n(_floats(txt[ll + 1] if ll + 1 < len(txt) else ""), 3)
                                traj_len = np.nan
                                term_atom = None
                                term_atom_id = np.nan
                                term_dist = np.nan
                                if len(coords_au) == 3:
                                    # trajectory length for this terminus
                                    for mm in range(ll + 1, min(ll + 18, search_end)):
                                        if "TRAJECTORY LENGTH" in txt[mm].upper():
                                            vals1 = _last_n(_floats(txt[mm]), 1)
                                            if len(vals1) == 1:
                                                traj_len = vals1[0]
                                            break
                                    # nearest atom at the path terminus: first row in the terminus cluster
                                    term_hdr = ll
                                    while term_hdr < search_end and "CLUSTER OF ATOMS AROUND THE TERMINUS OF THE PATH" not in txt[term_hdr]:
                                        term_hdr += 1
                                    if term_hdr < search_end:
                                        nn = term_hdr + 1
                                        while nn < search_end:
                                            parts = txt[nn].split()
                                            if len(parts) >= 10 and parts[0].isdigit() and parts[1].isdigit() and parts[5].isdigit():
                                                try:
                                                    old_id = int(parts[1])
                                                    znum = int(parts[5])
                                                    term_atom = f"{atom_symbol(znum)}-{old_id}"
                                                    term_atom_id = old_id
                                                    term_dist = float(parts[-1])
                                                except Exception:
                                                    pass
                                                break
                                            if parts and ("ATTRACTOR" in txt[nn].upper() or "BPL (ANG)" in txt[nn].upper()):
                                                break
                                            nn += 1
                                    attr_info.append({
                                        "coord_ang": [c * bohr_to_ang for c in coords_au],
                                        "atom": term_atom or "",
                                        "atom_id": term_atom_id,
                                        # For the BCP report, DIST_ELEM*_ANG should be the
                                        # distance from the BCP to each bond-path attractor,
                                        # i.e. the trajectory length of that half-path.
                                        "dist": traj_len if np.isfinite(traj_len) else term_dist,
                                        "traj_len": traj_len,
                                    })
                                ll += 1
                            else:
                                ll += 1

                        if len(attr_info) >= 1:
                            neigh1[b] = attr_info[0]["atom"]
                            dist1[b] = attr_info[0]["dist"]
                            attr1_atom_id[b] = attr_info[0]["atom_id"]
                            attr1_x_ang[b], attr1_y_ang[b], attr1_z_ang[b] = attr_info[0]["coord_ang"]
                            attr1_traj_len[b] = attr_info[0]["traj_len"]
                        if len(attr_info) >= 2:
                            neigh2[b] = attr_info[1]["atom"]
                            dist2[b] = attr_info[1]["dist"]
                            attr2_atom_id[b] = attr_info[1]["atom_id"]
                            attr2_x_ang[b], attr2_y_ang[b], attr2_z_ang[b] = attr_info[1]["coord_ang"]
                            attr2_traj_len[b] = attr_info[1]["traj_len"]

                        # Bond-path summary line printed by TOPOND for this BCP.
                        for mm in range(kk, search_end):
                            if "BPL (ANG)" in txt[mm].upper() and "RAB" in txt[mm].upper():
                                vals3 = _last_n(_floats(txt[mm]), 3)
                                if len(vals3) == 3:
                                    bpl_arr[b], rab_arr[b], bpl_over_rab_arr[b] = vals3
                                break

                    # Fallback only if attractor parsing failed/incomplete.
                    if not neigh1[b] or not neigh2[b]:
                        found_neighbors = []
                        kk = i + 1
                        while kk < search_end and "CLUSTER OF ATOMS AROUND THE CP" not in txt[kk]:
                            kk += 1
                        if kk < search_end:
                            seen = set()
                            ll = kk + 1
                            while ll < search_end and len(found_neighbors) < 2:
                                line = txt[ll]
                                parts = line.split()
                                # Typical format:
                                # NEW OLD CELLx CELLY CELLZ AT.NU X(AU) Y(AU) Z(AU) DISTANCE(ANG)
                                if len(parts) >= 10 and parts[0].isdigit() and parts[1].isdigit() and parts[5].isdigit():
                                    try:
                                        old_id = int(parts[1])
                                        znum = int(parts[5])
                                        dist = float(parts[-1])
                                        key = (old_id, znum)
                                        if key not in seen:
                                            seen.add(key)
                                            found_neighbors.append((dist, atom_symbol(znum), old_id))
                                    except Exception:
                                        pass
                                elif found_neighbors and ("CP TYPE" in line or "ATTRACTOR CP TYPE" in line or not line.strip()):
                                    break
                                ll += 1

                        found_neighbors.sort(key=lambda t: t[0])
                        if len(found_neighbors) >= 1 and not neigh1[b]:
                            neigh1[b] = f"{found_neighbors[0][1]}-{found_neighbors[0][2]}"
                            dist1[b] = found_neighbors[0][0]
                        if len(found_neighbors) >= 2 and not neigh2[b]:
                            neigh2[b] = f"{found_neighbors[1][1]}-{found_neighbors[1][2]}"
                            dist2[b] = found_neighbors[1][0]
                except Exception:
                    pass

                b += 1
            except Exception:
                # Skip this CP block if parsing fails (do not abort whole TRHO parsing).
                continue

        elif re_rcp.search(ln) and cont_rcp:
            # COORD (AU)
            coord_vals = _last_n(_floats(txt[i + 1]), 3)
            if len(coord_vals) != 3:
                continue
            coord = coord_vals
            xyz_ring[r, :] = coord

            # tenta ler propriedades no mesmo padrão do BCP (quando o bloco verboso existir)
            try:
                j = i + 2
                if str_type == "Crystal":
                    j += 1  # pula FRACT

                props = txt[j].split()
                props = [float(x) for x in props[3:6]]
                rho_ring[r], grho_ring[r], lap_ring[r] = props
                j += 1

                if open_shell:
                    osln = txt[j].split()
                    osln = [float(x) for x in osln[3:5]]
                    rhoa_ring[r], spin_ring[r] = osln
                    j += 1

                kin = txt[j].split()
                kin = [float(x) for x in kin[5:7]]
                gkin_ring[r], kkin_ring[r] = kin
                j += 1

                vln = txt[j].split()
                vir_ring[r] = float(vln[3])
                j += 1

                eln = txt[j].split()
                elf_ring[r] = float(eln[2])
                j += 1

                if open_shell:
                    eln2 = txt[j].split()
                    elfb_ring[r] = float(eln2[2])
                    j += 1

                # eigenvalues
                j += 3
                e = txt[j].split()
                eig_ring[r, 0] = float(e[5])
                eig_ring[r, 1] = float(e[6])
                eig_ring[r, 2] = float(e[7])

                # ellipticity
                j += 5
                ell = txt[j].split()
                ellip_ring[r] = float(ell[2])
            except Exception:
                # se falhar, mantém NaN e será preenchido pelo fallback (tabela compacta), se disponível
                pass

            r += 1

        elif re_ccp.search(ln) and cont_ccp:
            # COORD (AU)
            coord_vals = _last_n(_floats(txt[i + 1]), 3)
            if len(coord_vals) != 3:
                continue
            coord = coord_vals
            xyz_cage[c, :] = coord

            try:
                j = i + 2
                if str_type == "Crystal":
                    j += 1  # pula FRACT

                props = txt[j].split()
                props = [float(x) for x in props[3:6]]
                rho_cage[c], grho_cage[c], lap_cage[c] = props
                j += 1

                if open_shell:
                    osln = txt[j].split()
                    osln = [float(x) for x in osln[3:5]]
                    rhoa_cage[c], spin_cage[c] = osln
                    j += 1

                kin = txt[j].split()
                kin = [float(x) for x in kin[5:7]]
                gkin_cage[c], kkin_cage[c] = kin
                j += 1

                vln = txt[j].split()
                vir_cage[c] = float(vln[3])
                j += 1

                eln = txt[j].split()
                elf_cage[c] = float(eln[2])
                j += 1

                if open_shell:
                    eln2 = txt[j].split()
                    elfb_cage[c] = float(eln2[2])
                    j += 1

                j += 3
                e = txt[j].split()
                eig_cage[c, 0] = float(e[5])
                eig_cage[c, 1] = float(e[6])
                eig_cage[c, 2] = float(e[7])

                j += 5
                ell = txt[j].split()
                ellip_cage[c] = float(ell[2])
            except Exception:
                pass

            c += 1

    # fallback: se não achou (3,+1)/(3,+3) no bloco verboso, mas existem na tabela compacta,
    # usa as coordenadas em Å e converte para AU.
    if cont_rcp and r == 0:
        ring_rows = [rr for rr in compact_rows if rr["TYPE"].replace(" ", "") == "(3,+1)"]
        if ring_rows:
            for k, rr in enumerate(ring_rows[:cont_rcp]):
                xyz_ring[k, 0] = rr["X_ANG"] / bohr_to_ang
                xyz_ring[k, 1] = rr["Y_ANG"] / bohr_to_ang
                xyz_ring[k, 2] = rr["Z_ANG"] / bohr_to_ang
            r = min(len(ring_rows), cont_rcp)

    if cont_ccp and c == 0:
        cage_rows = [rr for rr in compact_rows if rr["TYPE"].replace(" ", "") == "(3,+3)"]
        if cage_rows:
            for k, rr in enumerate(cage_rows[:cont_ccp]):
                xyz_cage[k, 0] = rr["X_ANG"] / bohr_to_ang
                xyz_cage[k, 1] = rr["Y_ANG"] / bohr_to_ang
                xyz_cage[k, 2] = rr["Z_ANG"] / bohr_to_ang
            c = min(len(cage_rows), cont_ccp)

    # --- fallback de PROPRIEDADES via tabela compacta (se não houve bloco verboso) ---
    # (3,+1)
    if cont_rcp:
        ring_rows = [rr for rr in compact_rows if rr["TYPE"].replace(" ", "") == "(3,+1)"]
        if ring_rows:
            for k, rr in enumerate(ring_rows[:cont_rcp]):
                # só preenche se ainda estiver NaN (isto preserva o verboso quando disponível)
                if k < len(rho_ring) and (np.isnan(rho_ring[k]) if isinstance(rho_ring[k], float) else False):
                    rho_ring[k] = rr["RHO"]
                if k < len(lap_ring) and (np.isnan(lap_ring[k]) if isinstance(lap_ring[k], float) else False):
                    lap_ring[k] = rr["LAPL"]
                if k < len(ellip_ring) and (np.isnan(ellip_ring[k]) if isinstance(ellip_ring[k], float) else False):
                    ellip_ring[k] = rr["ELLIP"]
                if k < eig_ring.shape[0]:
                    if np.isnan(eig_ring[k,0]): eig_ring[k,0] = rr["L1"]
                    if np.isnan(eig_ring[k,1]): eig_ring[k,1] = rr["L2"]
                    if np.isnan(eig_ring[k,2]): eig_ring[k,2] = rr["L3"]

    # (3,+3)
    if cont_ccp:
        cage_rows = [rr for rr in compact_rows if rr["TYPE"].replace(" ", "") == "(3,+3)"]
        if cage_rows:
            for k, rr in enumerate(cage_rows[:cont_ccp]):
                if k < len(rho_cage) and (np.isnan(rho_cage[k]) if isinstance(rho_cage[k], float) else False):
                    rho_cage[k] = rr["RHO"]
                if k < len(lap_cage) and (np.isnan(lap_cage[k]) if isinstance(lap_cage[k], float) else False):
                    lap_cage[k] = rr["LAPL"]
                if k < len(ellip_cage) and (np.isnan(ellip_cage[k]) if isinstance(ellip_cage[k], float) else False):
                    ellip_cage[k] = rr["ELLIP"]
                if k < eig_cage.shape[0]:
                    if np.isnan(eig_cage[k,0]): eig_cage[k,0] = rr["L1"]
                    if np.isnan(eig_cage[k,1]): eig_cage[k,1] = rr["L2"]
                    if np.isnan(eig_cage[k,2]): eig_cage[k,2] = rr["L3"]


    # montar df coords (ainda em AU; você usava ANGSTROM no nome mas na prática era AU nessa fase)
    df_bcp = pd.DataFrame(xyz_bcp, columns=["x_AU", "y_AU", "z_AU"])
    df_bcp["X_ANGSTROM"] = df_bcp["x_AU"] * bohr_to_ang
    df_bcp["Y_ANGSTROM"] = df_bcp["y_AU"] * bohr_to_ang
    df_bcp["Z_ANGSTROM"] = df_bcp["z_AU"] * bohr_to_ang
    df_bcp.index = np.arange(1, len(df_bcp) + 1)

    # propriedades derivadas
    with np.errstate(divide='ignore', invalid='ignore'):
        adim_ratio = np.where(np.abs(gkin) > 1e-12, np.abs(vir) / gkin, 0.0)
    bond_degree = (vir + gkin) / rho

    df_prop = pd.DataFrame({
        "ELEM1": neigh1,
        "DIST_ELEM1_ANG": dist1,
        "ELEM2": neigh2,
        "DIST_ELEM2_ANG": dist2,
        "ATTR1_ATOM_ID": attr1_atom_id,
        "ATTR2_ATOM_ID": attr2_atom_id,
        "ATTR1_X_ANGSTROM": attr1_x_ang,
        "ATTR1_Y_ANGSTROM": attr1_y_ang,
        "ATTR1_Z_ANGSTROM": attr1_z_ang,
        "ATTR2_X_ANGSTROM": attr2_x_ang,
        "ATTR2_Y_ANGSTROM": attr2_y_ang,
        "ATTR2_Z_ANGSTROM": attr2_z_ang,
        "ATTR1_TRAJ_LEN_ANG": attr1_traj_len,
        "ATTR2_TRAJ_LEN_ANG": attr2_traj_len,
        "BPL_ANG": bpl_arr,
        "RAB_ANG": rab_arr,
        "BPL_OVER_RAB": bpl_over_rab_arr,
        "RHO": rho,
        "GRHO": grho,
        "GKIN": gkin,
        "KKIN": kkin,
        "VIRIAL": vir,
        "ELF": elf,
        "ELLIP": ellip,
        "LAP": lap,
        "ADIM_RATIO": adim_ratio,
        "BOND_DEGREE": bond_degree,
        "LAMBDA1": eig[:, 0],
        "LAMBDA2": eig[:, 1],
        "LAMBDA3": eig[:, 2],
    })

    if open_shell:
        df_prop["RHOA"] = rhoa
        df_prop["SPIN_DENS"] = spin
        df_prop["ELFb"] = elfb

    df_prop.index = np.arange(1, len(df_prop) + 1)

    # add BCP_ELEM from the bond-path attractors (symbols only, without atom ids)
    def _bcp_elem_symbol(label: str) -> str:
        s = str(label or "").strip()
        m = re.match(r"([A-Za-z]{1,3})", s)
        return (m.group(1) if m else s).strip()

    b1 = [_bcp_elem_symbol(x) for x in df_prop["ELEM1"]]
    b2 = [_bcp_elem_symbol(x) for x in df_prop["ELEM2"]]
    df_prop["BCP_ELEM"] = [f"{x}-{y}" if (x and y) else (x or y or "BCP") for x, y in zip(b1, b2)]

    # junta coords
    df_bcp_props = pd.concat([df_prop, df_bcp[["X_ANGSTROM","Y_ANGSTROM","Z_ANGSTROM"]]], axis=1)

    # rings/cages (coords + props)
    df_ring = pd.DataFrame(xyz_ring, columns=["x_AU","y_AU","z_AU"]) if cont_rcp else pd.DataFrame()
    df_cage = pd.DataFrame(xyz_cage, columns=["x_AU","y_AU","z_AU"]) if cont_ccp else pd.DataFrame()

    # RCP props
    if cont_rcp:
        df_ring["X_ANGSTROM"] = df_ring["x_AU"] * bohr_to_ang
        df_ring["Y_ANGSTROM"] = df_ring["y_AU"] * bohr_to_ang
        df_ring["Z_ANGSTROM"] = df_ring["z_AU"] * bohr_to_ang
        df_ring.index = np.arange(1, len(df_ring) + 1)

        with np.errstate(divide='ignore', invalid='ignore'):
            adim_ratio_ring = np.where(np.abs(gkin_ring) > 1e-12, np.abs(vir_ring) / gkin_ring, 0.0)
        bond_degree_ring = np.where(np.isfinite(rho_ring) & (rho_ring != 0), (vir_ring + gkin_ring) / rho_ring, np.nan)

        df_rcp_props = pd.DataFrame({
            "RHO": rho_ring,
            "GRHO": grho_ring,
            "GKIN": gkin_ring,
            "KKIN": kkin_ring,
            "VIRIAL": vir_ring,
            "ELF": elf_ring,
            "ELLIP": ellip_ring,
            "LAP": lap_ring,
            "ADIM_RATIO": adim_ratio_ring,
            "BOND_DEGREE": bond_degree_ring,
            "LAMBDA1": eig_ring[:, 0] if eig_ring.size else np.array([]),
            "LAMBDA2": eig_ring[:, 1] if eig_ring.size else np.array([]),
            "LAMBDA3": eig_ring[:, 2] if eig_ring.size else np.array([]),
        })
        if open_shell:
            df_rcp_props["RHOA"] = rhoa_ring
            df_rcp_props["SPIN_DENS"] = spin_ring
            df_rcp_props["ELFb"] = elfb_ring

        df_rcp_props.index = np.arange(1, len(df_rcp_props) + 1)
        df_rcp_props = pd.concat([df_rcp_props, df_ring[["X_ANGSTROM","Y_ANGSTROM","Z_ANGSTROM"]]], axis=1)
    else:
        df_rcp_props = pd.DataFrame()

    # CCP props
    if cont_ccp:
        df_cage["X_ANGSTROM"] = df_cage["x_AU"] * bohr_to_ang
        df_cage["Y_ANGSTROM"] = df_cage["y_AU"] * bohr_to_ang
        df_cage["Z_ANGSTROM"] = df_cage["z_AU"] * bohr_to_ang
        df_cage.index = np.arange(1, len(df_cage) + 1)

        with np.errstate(divide='ignore', invalid='ignore'):
            adim_ratio_cage = np.where(np.abs(gkin_cage) > 1e-12, np.abs(vir_cage) / gkin_cage, 0.0)
        bond_degree_cage = np.where(np.isfinite(rho_cage) & (rho_cage != 0), (vir_cage + gkin_cage) / rho_cage, np.nan)

        df_ccp_props = pd.DataFrame({
            "RHO": rho_cage,
            "GRHO": grho_cage,
            "GKIN": gkin_cage,
            "KKIN": kkin_cage,
            "VIRIAL": vir_cage,
            "ELF": elf_cage,
            "ELLIP": ellip_cage,
            "LAP": lap_cage,
            "ADIM_RATIO": adim_ratio_cage,
            "BOND_DEGREE": bond_degree_cage,
            "LAMBDA1": eig_cage[:, 0] if eig_cage.size else np.array([]),
            "LAMBDA2": eig_cage[:, 1] if eig_cage.size else np.array([]),
            "LAMBDA3": eig_cage[:, 2] if eig_cage.size else np.array([]),
        })
        if open_shell:
            df_ccp_props["RHOA"] = rhoa_cage
            df_ccp_props["SPIN_DENS"] = spin_cage
            df_ccp_props["ELFb"] = elfb_cage

        df_ccp_props.index = np.arange(1, len(df_ccp_props) + 1)
        df_ccp_props = pd.concat([df_ccp_props, df_cage[["X_ANGSTROM","Y_ANGSTROM","Z_ANGSTROM"]]], axis=1)
    else:
        df_ccp_props = pd.DataFrame()

    # ---- attractors (3,-3) and non-nuclear attractors (NNAs) ----
    # Mirror analyze_trho_nna_v7.py as closely as possible:
    #   1) parse the CRYSTAL structural atom table (ATOM N.AT. ...) in Å;
    #   2) detect TRUE atom indices from "IN THE UNIT CELL" (fallback: all atoms);
    #   3) scan verbose ATTRACTOR CP TYPE (3,-3) blocks in order;
    #   4) keep only those whose local block contains the TOPOND warning token;
    #   5) classify by direct distance to the nearest reference atom.
    warning_token = "NON-NUCLEAR ATTRACTOR"
    nna_cols = [
        "CP_ID", "ATTRACTOR_ID", "X_ANGSTROM", "Y_ANGSTROM", "Z_ANGSTROM",
        "ATOM", "Sym", "ATOM_X_ANGSTROM", "ATOM_Y_ANGSTROM", "ATOM_Z_ANGSTROM",
        "d_min", "classification",
    ]

    full_text = "\n".join(txt)
    struct_atoms = _parse_crystal_atom_table(full_text)

    true_idx_list = []
    for _ln in txt:
        if "IN THE UNIT CELL" in _ln:
            parts = _ln.split()
            if len(parts) >= 6:
                try:
                    if len(parts[3]) > 1:
                        true_idx_list.append(int(parts[4]))
                    else:
                        true_idx_list.append(int(parts[5]))
                except Exception:
                    pass
    _seen_true = set()
    true_idx_list = [x for x in true_idx_list if not (x in _seen_true or _seen_true.add(x))]

    # IMPORTANT: for NNA discrimination we must mirror the external helper behavior
    # seen in the terminal comparison. In practice, the nearest-atom search must use
    # the full structural atom table, not the TRHO TRUE-atoms subset, otherwise CPs
    # that are actually closest to oxygen atoms (e.g. O22) get forced onto Nb TRUE
    # atoms (e.g. Nb1/Nb5).
    #
    # We still parse true_idx_list above for possible future UI/debug use, but the
    # actual distance classification below uses *all* atoms from the structural block.
    if struct_atoms:
        ref_atoms = list(struct_atoms)
    else:
        ref_atoms = []
        _src = df_true_atoms if (df_true_atoms is not None and not df_true_atoms.empty) else df_primitive
        if _src is not None and not _src.empty:
            for idx, row in _src.iterrows():
                try:
                    ref_atoms.append({
                        "atom_index": int(idx),
                        "symbol": str(row.get("SYMBOL", "") or ""),
                        "xA": float(row.get("X_ANGSTROM", np.nan)),
                        "yA": float(row.get("Y_ANGSTROM", np.nan)),
                        "zA": float(row.get("Z_ANGSTROM", np.nan)),
                    })
                except Exception:
                    pass

    flagged_rows = []
    cp_seq = 0
    for i, ln in enumerate(txt):
        compact_ln = str(ln).replace(" ", "").upper()
        if "ATTRACTORCPTYPE" not in compact_ln or "(3,-3)" not in compact_ln:
            continue
        cp_seq += 1
        coord_vals = _last_n(_floats(txt[i + 1] if i + 1 < len(txt) else ""), 3)
        if len(coord_vals) != 3:
            continue
        block = "\n".join(txt[i:min(i + 45, len(txt))]).upper()
        if warning_token not in block:
            continue

        cp_x_ang = float(coord_vals[0]) * bohr_to_ang
        cp_y_ang = float(coord_vals[1]) * bohr_to_ang
        cp_z_ang = float(coord_vals[2]) * bohr_to_ang

        nearest_atom = None
        dmin = np.nan
        if ref_atoms:
            try:
                best_d2 = float("inf")
                for a in ref_atoms:
                    dx = float(a.get("xA", np.nan)) - cp_x_ang
                    dy = float(a.get("yA", np.nan)) - cp_y_ang
                    dz = float(a.get("zA", np.nan)) - cp_z_ang
                    d2 = dx * dx + dy * dy + dz * dz
                    if d2 < best_d2:
                        best_d2 = d2
                        nearest_atom = a
                if nearest_atom is not None:
                    dmin = float(np.sqrt(best_d2))
            except Exception:
                nearest_atom = None
                dmin = np.nan

        classification = "unclassified"
        if nearest_atom is not None:
            classification = "likely NNA"
            if dmin <= nna_cutoff_ang:
                classification = "likely pseudopotential artifact"

        atom_id = np.nan
        atom_sym = ""
        atom_x = np.nan
        atom_y = np.nan
        atom_z = np.nan
        if nearest_atom is not None:
            try:
                atom_id = int(nearest_atom.get("atom_index"))
            except Exception:
                atom_id = np.nan
            try:
                atom_sym = str(nearest_atom.get("symbol", "") or "")
            except Exception:
                atom_sym = ""
            try:
                atom_x = float(nearest_atom.get("xA", np.nan))
                atom_y = float(nearest_atom.get("yA", np.nan))
                atom_z = float(nearest_atom.get("zA", np.nan))
            except Exception:
                pass

        flagged_rows.append({
            "CP_ID": cp_seq,
            "ATTRACTOR_ID": cp_seq,
            "X_ANGSTROM": cp_x_ang,
            "Y_ANGSTROM": cp_y_ang,
            "Z_ANGSTROM": cp_z_ang,
            "ATOM": atom_id,
            "Sym": atom_sym,
            "ATOM_X_ANGSTROM": atom_x,
            "ATOM_Y_ANGSTROM": atom_y,
            "ATOM_Z_ANGSTROM": atom_z,
            "d_min": dmin,
            "classification": classification,
        })

    df_attr = pd.DataFrame(flagged_rows, columns=nna_cols)
    df_att_nao_nucl = df_attr.copy()
    nna_count = int(len(df_att_nao_nucl))

    # ---- atoms for CP Viewer: expanded NEA clusters filtered by CP bounding box ----
    df_cpviewer_pool_atoms = _parse_non_equiv_atom_clusters(txt, bohr_to_ang=bohr_to_ang)
    df_cpviewer_atoms = _select_cpviewer_atoms_bbox(
        df_cpviewer_pool_atoms,
        df_bcp_props=df_bcp_props,
        df_ring=df_ring,
        df_cage=df_cage,
        df_attr=df_att_nao_nucl,
        margin_ang=2.5,
    )
    if df_cpviewer_atoms is None or df_cpviewer_atoms.empty:
        # Conservative fallback: the old compact topological context may still be useful
        # when the cluster block is absent or the CP bounding box becomes too restrictive.
        df_cpviewer_atoms = _parse_unique_atom_pairs_considered(txt, bohr_to_ang=bohr_to_ang)

    # ---- mapear para pt_* (opcional, mas você usa na visualização)
    # só calcula se todos os parâmetros vierem
    df_bcp_coords = df_bcp[["X_ANGSTROM","Y_ANGSTROM","Z_ANGSTROM"]].copy()
    if all(v is not None for v in [xmi,xma,ymi,yma,zmi,zma,x_inc,y_inc,n_planos,delta_z]):
        dx = (xma - xmi); dy = (yma - ymi); dz = (zma - zmi)
        xce = xmi + dx/2; yce = ymi + dy/2; zce = zmi + dz/2
        xce_plt = (dx / x_inc)
        yce_plt = (dy / y_inc)
        zce_plt = (n_planos / 2)

        pt_bcp_x = xce_plt/2 + ((df_bcp_coords["X_ANGSTROM"] - xce) / x_inc)
        pt_bcp_y = yce_plt/2 + ((df_bcp_coords["Y_ANGSTROM"] - yce) / y_inc)
        pt_bcp_z = zce_plt     + ((df_bcp_coords["Z_ANGSTROM"] - zce) / (delta_z / n_planos))
        df_bcp_coords["pt_bcp_x"] = pt_bcp_x
        df_bcp_coords["pt_bcp_y"] = pt_bcp_y
        df_bcp_coords["pt_bcp_z"] = pt_bcp_z

    return TrhoParsed(
        str_type=str_type,
        df_primitive=df_primitive,
        df_true_atoms=df_true_atoms,
        df_cpviewer_atoms=df_cpviewer_atoms,
        df_cpviewer_pool_atoms=df_cpviewer_pool_atoms,
        cell_vectors_ang=cell_vectors_ang,
        df_bcp_coords=df_bcp_coords,
        df_attr=df_attr,
        df_bcp_props=df_bcp_props,
        df_ring=df_ring,
        df_cage=df_cage,
        df_att_nao_nucl=df_att_nao_nucl,
        df_rcp_props=df_rcp_props,
        df_ccp_props=df_ccp_props,
        nna_count=nna_count,
        nna_messages=nna_messages,
        nna_cutoff_ang=float(nna_cutoff_ang),
    )


@dataclass
class TlapParsed:
    str_type: str = "Crystal"
    df_primitive: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_true_atoms: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_cp_props: pd.DataFrame = field(default_factory=pd.DataFrame)
    df_by_nea: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: dict = field(default_factory=dict)
    source_trho_run: str = ""


def parse_tlap_out(
    out_path: Path,
    *,
    trho_parsed: Optional[TrhoParsed] = None,
    bohr_to_ang: float = 0.5291772083,
    source_trho_run: str = "",
) -> TlapParsed:
    out_path = Path(out_path).expanduser().resolve()
    if not out_path.exists():
        raise FileNotFoundError(out_path)

    lines = out_path.read_text(errors="ignore").splitlines()
    str_type = "Crystal" if any("DIRECT LATTICE" in ln for ln in lines) else "Molecule"

    df_primitive = pd.DataFrame()
    df_true_atoms = pd.DataFrame()
    if trho_parsed is not None:
        try:
            df_primitive = trho_parsed.df_primitive.copy()
        except Exception:
            pass
        try:
            df_true_atoms = trho_parsed.df_true_atoms.copy()
        except Exception:
            pass

    _re_float = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")
    def _floats(s: str):
        try:
            return [float(x) for x in _re_float.findall(s)]
        except Exception:
            return []

    summary = {
        "iauto": None,
        "algorithm": "",
        "itype": "",
        "nt": None,
        "np": None,
        "ibpat": None,
        "nstep": None,
        "nnb": None,
    }
    for ln in lines:
        if "IAUTO" in ln and ":" in ln and summary["iauto"] is None:
            vals = _floats(ln)
            if vals:
                summary["iauto"] = int(vals[-1])
        elif "ALGORITHM FOR CP SEARCH" in ln and ":" in ln and not summary["algorithm"]:
            summary["algorithm"] = ln.split(":", 1)[1].strip()
        elif "EIG. FOL.: TYPE OF CPS SEARCHED FOR" in ln and ":" in ln and not summary["itype"]:
            summary["itype"] = ln.split(":", 1)[1].strip()
        elif "NUMBER OF ANGLE THETA INTERVALS" in ln and ":" in ln and summary["nt"] is None:
            vals = _floats(ln)
            if vals:
                summary["nt"] = int(vals[-1])
        elif "NUMBER OF ANGLE PHI INTERVALS" in ln and ":" in ln and summary["np"] is None:
            vals = _floats(ln)
            if vals:
                summary["np"] = int(vals[-1])
        elif "ATOMIC GRAPH EVALUATION" in ln and ":" in ln and summary["ibpat"] is None:
            summary["ibpat"] = 1 if ln.split(":",1)[1].strip().upper().startswith("T") else 0
        elif "MAX.NUM. OF N.R. OR EIG. FOL. STEPS" in ln and ":" in ln and summary["nstep"] is None:
            vals = _floats(ln)
            if vals:
                summary["nstep"] = int(vals[-1])
        elif "NEIGHBORS OF EACH NON-EQUIVALENT ATOM" in ln and ":" in ln and summary["nnb"] is None:
            vals = _floats(ln)
            if vals:
                summary["nnb"] = int(vals[-1])

    rea_start = re.compile(r"CP SEARCH FOR NON-EQUIVALENT ATOM\s+(\d+)\s+([A-Za-z]{1,3})", re.I)
    rea_end = re.compile(r"NON-EQUIV\. ATOM\s+([A-Za-z]{1,3})\s+(\d+)\s+NUMBER OF CPS FOUND:\s+(\d+)", re.I)
    rea_cp = re.compile(r"CP N\.\s+(\d+)", re.I)
    rea_type = re.compile(r"CP TYPE\s*:\s*(\([^\)]+\))", re.I)
    rea_coord = re.compile(r"COORD\(AU\).*:\s*([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)")
    rea_props = re.compile(r"PROPERTIES \(-LAP,GLAP,RHO\)\s*:\s*([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)")
    rea_eig = re.compile(r"EIGENVALUES \(L1 L2 L3\)\s*:\s*([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)")
    rea_table_cp = re.compile(r"^\s*([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+(\([^\)]+\))\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s*$")
    rea_near = re.compile(r"^\s*\(\s*([A-Za-z]{1,3})\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+([-+0-9.Ee]+)\s+AU\s*\)")

    cp_rows = []
    nea_rows = []

    # split output into NEA blocks first
    blocks = []
    current = None
    for ln in lines:
        m = rea_start.search(ln)
        if m:
            current = {
                "NEA_INDEX": int(m.group(1)),
                "NEA_SYMBOL": m.group(2).capitalize(),
                "lines": [ln],
            }
            continue
        if current is not None:
            current["lines"].append(ln)
            mend = rea_end.search(ln)
            if mend:
                current["NFOUND"] = int(mend.group(3))
                blocks.append(current)
                current = None

    for blk in blocks:
        nea_index = blk["NEA_INDEX"]
        nea_symbol = blk["NEA_SYMBOL"]
        blines = blk["lines"]
        rstar_ang = np.nan
        for ln in blines:
            if "SPHERE RADIUS (ANG)" in ln and ":" in ln:
                vals = _floats(ln)
                if vals:
                    rstar_ang = float(vals[-1])
                    break
        nea_rows.append({
            "NEA_INDEX": nea_index,
            "NEA_SYMBOL": nea_symbol,
            "RSTAR_ANG": rstar_ang,
            "NFOUND": int(blk.get("NFOUND", 0) or 0),
        })

        # detailed CP blocks inside this NEA block
        detailed = []
        current_cp = None
        for ln in blines:
            m = rea_cp.search(ln)
            if m:
                if isinstance(current_cp, dict) and current_cp.get("CP_N") is not None:
                    detailed.append(current_cp)
                current_cp = {
                    "NEA_INDEX": nea_index,
                    "NEA_SYMBOL": nea_symbol,
                    "CP_N": int(m.group(1)),
                    "TYPE": "",
                    "X_AU": None, "Y_AU": None, "Z_AU": None,
                    "NEG_LAP": None, "GLAP": None, "RHO": None,
                    "LAMBDA1": None, "LAMBDA2": None, "LAMBDA3": None,
                    "NEAREST_ATOM_SYMBOL": "",
                    "NEAREST_ATOM_INDEX": None,
                    "NEAREST_CELL_X": None,
                    "NEAREST_CELL_Y": None,
                    "NEAREST_CELL_Z": None,
                    "NEAREST_DIST_AU": None,
                }
                continue
            if current_cp is None:
                continue
            m = rea_type.search(ln)
            if m:
                current_cp["TYPE"] = m.group(1).replace(" ", "")
                continue
            m = rea_coord.search(ln)
            if m:
                current_cp["X_AU"] = float(m.group(1)); current_cp["Y_AU"] = float(m.group(2)); current_cp["Z_AU"] = float(m.group(3))
                continue
            m = rea_props.search(ln)
            if m:
                current_cp["NEG_LAP"] = float(m.group(1)); current_cp["GLAP"] = float(m.group(2)); current_cp["RHO"] = float(m.group(3))
                continue
            m = rea_eig.search(ln)
            if m:
                current_cp["LAMBDA1"] = float(m.group(1)); current_cp["LAMBDA2"] = float(m.group(2)); current_cp["LAMBDA3"] = float(m.group(3))
                continue
        if isinstance(current_cp, dict) and current_cp.get("CP_N") is not None:
            detailed.append(current_cp)

        # compact final table: nearest atom line comes after each compact CP row
        compact_rows = []
        pending = None
        for ln in blines:
            m = rea_table_cp.match(ln)
            if m:
                pending = {
                    "X_AU": float(m.group(1)),
                    "Y_AU": float(m.group(2)),
                    "Z_AU": float(m.group(3)),
                    "TYPE": m.group(4).replace(" ", ""),
                    "NEG_LAP": float(m.group(5)),
                    "RHO": float(m.group(6)),
                    "LAMBDA1": float(m.group(7)),
                    "LAMBDA2": float(m.group(8)),
                    "LAMBDA3": float(m.group(9)),
                }
                continue
            if pending is not None:
                m = rea_near.match(ln)
                if m:
                    pending["NEAREST_ATOM_SYMBOL"] = m.group(1).capitalize()
                    pending["NEAREST_ATOM_INDEX"] = int(m.group(2))
                    pending["NEAREST_CELL_X"] = int(m.group(3))
                    pending["NEAREST_CELL_Y"] = int(m.group(4))
                    pending["NEAREST_CELL_Z"] = int(m.group(5))
                    pending["NEAREST_DIST_AU"] = float(m.group(6))
                    compact_rows.append(pending)
                    pending = None
                    continue

        # merge compact nearest-atom information back into detailed rows by order
        for idx_cp, cp in enumerate(detailed):
            if idx_cp < len(compact_rows):
                comp = compact_rows[idx_cp]
                for key in (
                    "NEAREST_ATOM_SYMBOL", "NEAREST_ATOM_INDEX",
                    "NEAREST_CELL_X", "NEAREST_CELL_Y", "NEAREST_CELL_Z",
                    "NEAREST_DIST_AU",
                ):
                    cp[key] = comp.get(key)
            if cp.get("X_AU") is not None:
                cp["X_ANGSTROM"] = cp["X_AU"] * bohr_to_ang
                cp["Y_ANGSTROM"] = cp["Y_AU"] * bohr_to_ang
                cp["Z_ANGSTROM"] = cp["Z_AU"] * bohr_to_ang
            if cp.get("NEAREST_DIST_AU") is not None:
                cp["NEAREST_DIST_ANG"] = cp["NEAREST_DIST_AU"] * bohr_to_ang
            cp_rows.append(cp)

    df_cp_props = pd.DataFrame(cp_rows)
    if not df_cp_props.empty:
        cols = [
            "NEA_INDEX","NEA_SYMBOL","CP_N","TYPE",
            "X_AU","Y_AU","Z_AU","X_ANGSTROM","Y_ANGSTROM","Z_ANGSTROM",
            "NEG_LAP","GLAP","RHO","LAMBDA1","LAMBDA2","LAMBDA3",
            "NEAREST_ATOM_SYMBOL","NEAREST_ATOM_INDEX","NEAREST_CELL_X","NEAREST_CELL_Y","NEAREST_CELL_Z",
            "NEAREST_DIST_AU","NEAREST_DIST_ANG",
        ]
        df_cp_props = df_cp_props.reindex(columns=cols)
        df_cp_props.index = np.arange(1, len(df_cp_props) + 1)
    df_by_nea = pd.DataFrame(nea_rows)
    if not df_by_nea.empty:
        df_by_nea = df_by_nea.reindex(columns=["NEA_INDEX","NEA_SYMBOL","RSTAR_ANG","NFOUND"])
        df_by_nea.index = np.arange(1, len(df_by_nea) + 1)

    summary["n_neas"] = int(len(df_by_nea))
    summary["n_neas_with_cps"] = int((df_by_nea["NFOUND"] > 0).sum()) if not df_by_nea.empty else 0
    summary["total_cps"] = int(len(df_cp_props))

    return TlapParsed(
        str_type=str_type,
        df_primitive=df_primitive,
        df_true_atoms=df_true_atoms,
        df_cp_props=df_cp_props,
        df_by_nea=df_by_nea,
        summary=summary,
        source_trho_run=source_trho_run,
    )


# -----------------------------
# Main App
# -----------------------------

class SettingsDialog(tk.Toplevel):
    """Minimal Settings dialog (Phase 0): only 'properties' executable path."""
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        _ensure_floating_window(self)
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Executables", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky="w", columnspan=3, pady=(0, 8))

        ttk.Label(body, text="properties executable (CRYSTAL/TOPOND):").grid(row=1, column=0, sticky="w")

        current = ""
        try:
            current = str(getattr(app.state, "properties_exe", "") or "")
        except Exception:
            current = ""

        self.var_prop = tk.StringVar(value=current)
        ent = ttk.Entry(body, textvariable=self.var_prop, width=56)
        ent.grid(row=2, column=0, sticky="we", columnspan=2, pady=(4, 0))

        def browse():
            f = filedialog.askopenfilename(title="Select properties executable")
            if f:
                self.var_prop.set(f)

        ttk.Button(body, text="Browse…", command=browse).grid(row=2, column=2, padx=(8, 0), pady=(4, 0))

        self.lbl_test = ttk.Label(body, text=" ")
        self.lbl_test.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- Visualization defaults ---
        ttk.Label(body, text="Visualization", font=("TkDefaultFont", 11, "bold")).grid(row=4, column=0, sticky="w", columnspan=3, pady=(14, 6))
        ttk.Label(body, text="Laplacian isosurface colors:").grid(row=5, column=0, sticky="w")
        scheme_key = (self.app._settings.get("laplacian_scheme") or "blue_red").strip() or "blue_red"
        scheme_map = {
            "Blue (∇²ρ<0) / Red (∇²ρ>0)": "blue_red",
            "Red (∇²ρ<0) / Blue (∇²ρ>0)": "red_blue",
            "Viridis (single colorscale)": "viridis",
        }
        inv_scheme_map = {v: k for k, v in scheme_map.items()}
        self.var_lap_scheme = tk.StringVar(value=inv_scheme_map.get(scheme_key, "Blue (∇²ρ<0) / Red (∇²ρ>0)"))
        cmb = ttk.Combobox(body, textvariable=self.var_lap_scheme, values=list(scheme_map.keys()), state="readonly", width=40)
        cmb.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(body, text="NNA classification cutoff (Å):").grid(row=7, column=0, sticky="w", pady=(12, 0))
        self.var_nna_cutoff = tk.StringVar(value=str(self.app._settings.get("nna_cutoff_ang", 0.35)))
        ttk.Entry(body, textvariable=self.var_nna_cutoff, width=12).grid(row=8, column=0, sticky="w", pady=(4, 0))
        ttk.Label(body, text="Flagged (3,-3) attractors with d_min ≤ cutoff are classified as likely pseudopotential artifact; otherwise likely NNA.", wraplength=520, justify="left").grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(body, text="Cleanup after calculation", font=("TkDefaultFont", 11, "bold")).grid(row=10, column=0, sticky="w", columnspan=3, pady=(14, 6))
        cleanup_key = (self.app._settings.get("cleanup_policy") or "standard").strip().lower() or "standard"
        if cleanup_key not in ("standard", "minimal", "none"):
            cleanup_key = "standard"
        self.var_cleanup_policy = tk.StringVar(value=cleanup_key)
        ttk.Radiobutton(body, text="Standard (remove fort.9, fort.3, fort.11, fort.13)",
                        variable=self.var_cleanup_policy, value="standard").grid(row=11, column=0, columnspan=3,
                                                                                 sticky="w")
        ttk.Radiobutton(body, text="Minimal (remove fort.9 only)", variable=self.var_cleanup_policy,
                        value="minimal").grid(row=12, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Radiobutton(body, text="None (keep all files)", variable=self.var_cleanup_policy,
                        value="none").grid(row=13, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Label(body, text="External result file names", font=("TkDefaultFont", 11, "bold")).grid(row=14, column=0, sticky="w", columnspan=3, pady=(14, 6))
        ttk.Label(body, text="TRHO output names (semicolon separated):").grid(row=15, column=0, sticky="w", columnspan=3)
        self.var_trho_outputs = tk.StringVar(value=format_output_name_list(self.app._settings.get("trho_output_names"), DEFAULT_TRHO_OUTPUT_NAMES))
        ttk.Entry(body, textvariable=self.var_trho_outputs, width=56).grid(row=16, column=0, sticky="we", columnspan=3, pady=(4, 0))

        ttk.Label(body, text="TLAP output names (semicolon separated):").grid(row=17, column=0, sticky="w", columnspan=3, pady=(10, 0))
        self.var_tlap_outputs = tk.StringVar(value=format_output_name_list(self.app._settings.get("tlap_output_names"), DEFAULT_TLAP_OUTPUT_NAMES))
        ttk.Entry(body, textvariable=self.var_tlap_outputs, width=56).grid(row=18, column=0, sticky="we", columnspan=3, pady=(4, 0))

        ttk.Label(body, text="ATBP output names (semicolon separated):").grid(row=19, column=0, sticky="w", columnspan=3, pady=(10, 0))
        self.var_atbp_outputs = tk.StringVar(value=format_output_name_list(self.app._settings.get("atbp_output_names"), DEFAULT_ATBP_OUTPUT_NAMES))
        ttk.Entry(body, textvariable=self.var_atbp_outputs, width=56).grid(row=20, column=0, sticky="we", columnspan=3, pady=(4, 0))

        ttk.Label(body, text="These names are used only when reading external results. Internal TopIso3D runs still write trho.out, tlap.out and atbp.out.", wraplength=520, justify="left").grid(row=21, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def do_test():
            exe = self.var_prop.get().strip()
            rp = resolve_executable(exe)
            if rp and rp.exists():
                self.lbl_test.configure(text=f"✔ OK: {rp}")
            else:
                self.lbl_test.configure(text="✖ Not found. Use an absolute path or ensure 'properties' is in PATH.")

        btnrow = ttk.Frame(body)
        btnrow.grid(row=22, column=0, columnspan=3, sticky="e", pady=(12, 0))

        ttk.Button(btnrow, text="Test", command=do_test).pack(side="left")
        ttk.Button(btnrow, text="Cancel", command=self.destroy).pack(side="right", padx=(8, 0))

        def save_and_close():
            exe = self.var_prop.get().strip()
            if not exe:
                messagebox.showerror("Settings", "Please set the 'properties' executable path (or command in PATH).")
                return

            try:
                nna_cutoff = float(str(self.var_nna_cutoff.get()).strip().replace(",", "."))
            except Exception:
                messagebox.showerror("Settings", "Invalid NNA classification cutoff. Please enter a numeric value in Å.")
                return
            if nna_cutoff <= 0:
                messagebox.showerror("Settings", "NNA classification cutoff must be greater than zero.")
                return

            # Save to disk
            data = load_settings()
            data["properties_exe"] = exe
            data["nna_cutoff_ang"] = float(nna_cutoff)
            cleanup_policy = str(self.var_cleanup_policy.get() or "minimal").strip().lower()
            if cleanup_policy not in ("minimal", "standard", "none"):
                cleanup_policy = "minimal"
            data["cleanup_policy"] = cleanup_policy
            data["trho_output_names"] = parse_output_name_list(self.var_trho_outputs.get(), DEFAULT_TRHO_OUTPUT_NAMES)
            data["tlap_output_names"] = parse_output_name_list(self.var_tlap_outputs.get(), DEFAULT_TLAP_OUTPUT_NAMES)
            data["atbp_output_names"] = parse_output_name_list(self.var_atbp_outputs.get(), DEFAULT_ATBP_OUTPUT_NAMES)
            # Visualization defaults
            try:
                scheme_label = self.var_lap_scheme.get().strip()
                scheme_map = {
                    "Blue (∇²ρ<0) / Red (∇²ρ>0)": "blue_red",
                    "Red (∇²ρ<0) / Blue (∇²ρ>0)": "red_blue",
                    "Viridis (single colorscale)": "viridis",
                }
                data["laplacian_scheme"] = scheme_map.get(scheme_label, "blue_red")
            except Exception:
                data["laplacian_scheme"] = "blue_red"
            save_settings(data)

            # Apply live
            self.app._settings = data
            self.app.state.properties_exe = Path(exe)
            self.app.state.laplacian_scheme = (data.get("laplacian_scheme") or "blue_red").strip() or "blue_red"
            self.app.state.nna_cutoff_ang = float(data.get("nna_cutoff_ang", 0.35) or 0.35)
            self.app.state.cleanup_policy = str(data.get("cleanup_policy") or "minimal").strip().lower() or "minimal"
            self.app._settings["trho_output_names"] = parse_output_name_list(data.get("trho_output_names"), DEFAULT_TRHO_OUTPUT_NAMES)
            self.app._settings["tlap_output_names"] = parse_output_name_list(data.get("tlap_output_names"), DEFAULT_TLAP_OUTPUT_NAMES)
            self.app._settings["atbp_output_names"] = parse_output_name_list(data.get("atbp_output_names"), DEFAULT_ATBP_OUTPUT_NAMES)

            # Refresh UI hints/status and re-scan external results using the new accepted names.
            try:
                self.app.set_status("Settings saved ✓")
                self.app.re_scan_external_results_after_settings_change()
            except Exception:
                try:
                    self.app.refresh_all_pages()
                except Exception:
                    pass

            self.destroy()

        ttk.Button(btnrow, text="Save", command=save_and_close).pack(side="right")

        body.columnconfigure(0, weight=1)

        # Auto-test on open (non-blocking UX)
        self.after(50, do_test)


class CreateToolTip:
    """Minimal tooltip for Tk/ttk widgets."""

    def __init__(self, widget, text: str = ""):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        self._id = None
        self.widget.bind("<Enter>", self._enter, add=True)
        self.widget.bind("<Leave>", self._leave, add=True)
        self.widget.bind("<ButtonPress>", self._leave, add=True)

    def update_text(self, text: str):
        self.text = text or ""

    def _enter(self, _event=None):
        self._schedule()

    def _leave(self, _event=None):
        self._unschedule()
        self._hide()

    def _schedule(self):
        self._unschedule()
        self._id = self.widget.after(600, self._show)

    def _unschedule(self):
        if self._id is not None:
            try:
                self.widget.after_cancel(self._id)
            except Exception:
                pass
            self._id = None

    def _show(self):
        if self.tipwindow or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return
        tw = tk.Toplevel(self.widget)
        self.tipwindow = tw
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            font=("TkDefaultFont", 9),
        )
        lbl.pack(ipadx=6, ipady=4)

    def _hide(self):
        tw = self.tipwindow
        self.tipwindow = None
        if tw is not None:
            try:
                tw.destroy()
            except Exception:
                pass



class _RunNameDialog(tk.Toplevel):
    """Small themed dialog to ask the user for a run name."""
    def __init__(self, parent, title: str, prompt: str, initialvalue: str = ""):
        super().__init__(parent)
        _ensure_floating_window(self)
        self.parent = parent
        self.result = None
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        try:
            self.configure(bg=UI_BG_MAIN)
        except Exception:
            pass

        wrap = tk.Frame(self, bg=UI_BG_MAIN)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(
            wrap,
            text=prompt,
            justify="left",
            anchor="w",
            bg=UI_BG_MAIN,
            fg=UI_FG_MAIN,
            font=("Arial", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        self.var = tk.StringVar(value=initialvalue)
        self.ent = tk.Entry(
            wrap,
            textvariable=self.var,
            width=42,
            bg=UI_BG_FIELD,
            fg=UI_FG_MAIN,
            insertbackground=UI_FG_MAIN,
            relief="flat",
            highlightthickness=1,
            highlightbackground=UI_BG_DARK,
            highlightcolor=UI_ACCENT,
            font=("Arial", 11),
        )
        self.ent.pack(fill="x", pady=(0, 10))
        try:
            self.ent.selection_range(0, "end")
            self.ent.icursor("end")
            self.ent.focus_set()
        except Exception:
            pass

        btns = tk.Frame(wrap, bg=UI_BG_MAIN)
        btns.pack(fill="x")

        tk.Button(
            btns,
            text="OK",
            width=10,
            command=self._on_ok,
            bg=UI_ACCENT,
            fg=UI_FG_MAIN,
            activebackground=UI_ACCENT,
            activeforeground=UI_FG_MAIN,
            relief="flat",
            font=("Arial", 10, "bold"),
            padx=10,
            pady=4,
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btns,
            text="Cancel",
            width=10,
            command=self._on_cancel,
            bg=UI_ACCENT,
            fg=UI_FG_MAIN,
            activebackground=UI_ACCENT,
            activeforeground=UI_FG_MAIN,
            relief="flat",
            font=("Arial", 10, "bold"),
            padx=10,
            pady=4,
        ).pack(side="left")

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self._on_cancel())

        try:
            self.update_idletasks()
            px = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
            py = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 2)
            self.geometry(f"+{px}+{py}")
        except Exception:
            pass

    def _on_ok(self):
        self.result = self.var.get()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


def _ask_run_name(parent, title: str, prompt: str, initialvalue: str = ""):
    dlg = _RunNameDialog(parent, title=title, prompt=prompt, initialvalue=initialvalue)
    parent.wait_window(dlg)
    return dlg.result

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        # Ensure the main window is decorated and movable (important on some VMs).
        _ensure_floating_window(self)
        self.title("TopIso3D v2026")
        # Standard initial window size (avoid resize jumps across pages)
        self.geometry("1180x800")
        self.minsize(980, 640)
        self.resizable(True, True)

        # Make sure the window can always be closed from the window manager.
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # platform-tuned fonts + v2-like color palette for ttk widgets
        self._configure_platform_fonts()
        self._apply_theme()
        self.state = ProjectContext()

        # Backward-compatible alias (some pages expect app.ctx)
        self.ctx = self.state
        # CRYSTAL/TOPOND executable (used by TRHO/PL2D/ATBP).
        # Loaded from Settings; fallback to a sensible default.
        self._settings = load_settings()
        self._settings["trho_output_names"] = parse_output_name_list(self._settings.get("trho_output_names"), DEFAULT_TRHO_OUTPUT_NAMES)
        self._settings["tlap_output_names"] = parse_output_name_list(self._settings.get("tlap_output_names"), DEFAULT_TLAP_OUTPUT_NAMES)
        self._settings["atbp_output_names"] = parse_output_name_list(self._settings.get("atbp_output_names"), DEFAULT_ATBP_OUTPUT_NAMES)
        pexe = (self._settings.get("properties_exe") or "").strip()
        if pexe:
            self.state.properties_exe = Path(pexe)
        else:
            self.state.properties_exe = resolve_default_properties_executable()
        # User-configurable visualization defaults
        self.state.laplacian_scheme = (self._settings.get("laplacian_scheme") or "blue_red").strip() or "blue_red"
        try:
            self.state.nna_cutoff_ang = float(self._settings.get("nna_cutoff_ang", 0.35) or 0.35)
        except Exception:
            self.state.nna_cutoff_ang = 0.35
        self.state.cleanup_policy = str(self._settings.get("cleanup_policy") or "minimal").strip().lower() or "minimal"
        if self.state.cleanup_policy not in ("minimal", "standard", "none"):
            self.state.cleanup_policy = "minimal"

        try:
            log_event(self.state, "startup diagnostics: " + json.dumps(collect_system_diagnostics(self), ensure_ascii=False))
        except Exception:
            pass

        # UI refresh tuning (Windows redraws are noticeably slower under Tkinter)
        self._current_page_key = "Workspace"
        self._current_page = None
        self._ui_refresh_ms = 700 if is_windows() else 400
        self._last_ui_snapshot = None
        # Keep exactly one scheduled UI-refresh callback alive.
        # Calling refresh_ui_state() directly from other methods used to create
        # multiple self.after(...) loops, which is especially problematic on macOS
        # after opening/closing auxiliary windows such as Reports.
        self._refresh_ui_after_id = None

        # job plumbing
        self._job_thread: Optional[threading.Thread] = None
        self._job_queue: "queue.Queue[tuple]" = queue.Queue()
        self._job_running = False
        self._active_process = None
        self._active_job_kind = ""
        self._job_abort_requested = False

        self._build_layout()
        self._build_menubar()
        self._create_pages()
        self._build_sidebar()
        self._current_page = self.pages.get("Workspace")

        self.show_page("Workspace")

        # start queue polling
        self.after(100, self._poll_job_queue)
        self._schedule_refresh_ui_state()

        self.update_idletasks()
        self.after(10, self.deiconify)


    def _configure_platform_fonts(self):
        """Tune default Tk fonts per OS to improve visual quality, especially on Windows."""
        try:
            if is_windows():
                family = "Segoe UI"
                size = 10
                title_family = "Segoe UI Semibold"
            elif is_macos():
                family = "Helvetica"
                size = 12
                title_family = "Helvetica"
            else:
                family = "DejaVu Sans"
                size = 10
                title_family = "DejaVu Sans"
            self._ui_font_family = family
            self._ui_font_size = size
            self._ui_title_family = title_family

            for name in (
                "TkDefaultFont",
                "TkTextFont",
                "TkMenuFont",
                "TkHeadingFont",
                "TkCaptionFont",
                "TkSmallCaptionFont",
                "TkIconFont",
                "TkTooltipFont",
            ):
                try:
                    f = tkfont.nametofont(name)
                    f.configure(family=family, size=size)
                except Exception:
                    pass

            try:
                self.option_add("*Font", f"{{{family}}} {size}")
                self.option_add("*Menu.Font", f"{{{family}}} {size}")
                self.option_add("*TCombobox*Listbox.font", f"{{{family}}} {size}")
            except Exception:
                pass
        except Exception:
            self._ui_font_family = "TkDefaultFont"
            self._ui_font_size = 10
            self._ui_title_family = "TkDefaultFont"

    def _apply_theme(self):
        """Apply a v2-like color palette with platform-tuned fonts and spacing."""
        try:
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass

            family = getattr(self, "_ui_font_family", "TkDefaultFont")
            base_size = int(getattr(self, "_ui_font_size", 10) or 10)
            title_family = getattr(self, "_ui_title_family", family)

            self.configure(bg=UI_BG_MAIN)

            style.configure(".", background=UI_BG_MAIN, foreground=UI_FG_MAIN, font=(family, base_size))
            style.configure("TFrame", background=UI_BG_MAIN)
            style.configure("Sidebar.TFrame", background=UI_BG_MAIN)
            style.configure("Content.TFrame", background=UI_BG_MAIN)
            style.configure("Status.TFrame", background=UI_BG_MAIN)

            style.configure("TLabel", background=UI_BG_MAIN, foreground=UI_FG_MAIN, font=(family, base_size))
            style.configure("Muted.TLabel", background=UI_BG_MAIN, foreground=UI_FG_MUTED, font=(family, base_size))
            style.configure(
                "Title.TLabel",
                background=UI_BG_DARK,
                foreground=UI_ACCENT,
                font=(title_family, base_size + 3, "bold"),
                padding=(10, 8),
            )
            style.configure(
                "TitleCenter.TLabel",
                background=UI_BG_DARK,
                foreground=UI_ACCENT,
                font=(title_family, base_size + 3, "bold"),
                padding=(10, 8),
                anchor="center",
            )

            style.configure("TLabelframe", background=UI_BG_MAIN, foreground=UI_FG_MAIN)
            style.configure("TLabelframe.Label", background=UI_BG_MAIN, foreground=UI_FG_MAIN, font=(family, base_size, "bold"))

            style.configure("TButton", background=UI_ACCENT, foreground=UI_FG_MAIN, padding=(12, 8), font=(family, base_size))
            style.map(
                "TButton",
                background=[("active", UI_ACCENT), ("pressed", UI_ACCENT), ("disabled", UI_BG_PANEL)],
                foreground=[("disabled", UI_FG_MUTED)],
            )

            style.configure("SidebarNav.TButton", font=(family, base_size, "bold"), padding=(12, 8))
            style.configure("SidebarNavLeft.TButton", font=(family, base_size, "bold"), padding=(12, 8), anchor="w")
            style.configure("SidebarNavCenter.TButton", font=(family, base_size, "bold"), padding=(12, 8), anchor="center")

            style.configure("TCheckbutton", background=UI_BG_MAIN, foreground=UI_FG_MAIN, font=(family, base_size))

            style.configure("TEntry", fieldbackground=UI_BG_FIELD, foreground=UI_FG_MAIN, padding=5, insertwidth=1)
            style.configure("TCombobox", fieldbackground=UI_BG_FIELD, foreground=UI_FG_MAIN, padding=5, arrowsize=14)
            style.map("TCombobox", fieldbackground=[("readonly", UI_BG_FIELD)])

            style.configure("TSeparator", background=UI_BG_DARK)
            style.configure("TProgressbar", troughcolor=UI_BG_PANEL, background=UI_ACCENT)

            style.configure(
                "Treeview",
                background=UI_BG_FIELD,
                fieldbackground=UI_BG_FIELD,
                foreground=UI_FG_MAIN,
                font=(family, base_size),
                rowheight=24,
            )
            style.configure("Treeview.Heading", background=UI_BG_DARK, foreground=UI_ACCENT, font=(family, base_size, "bold"))
            style.map("Treeview.Heading", background=[("active", UI_BG_DARK)])

            try:
                self.option_add("*Listbox.font", f"{{{family}}} {base_size}")
                self.option_add("*Text.font", f"{{{family}}} {base_size}")
            except Exception:
                pass
        except Exception:
            pass

    # ---------- Layout ----------
    def _build_layout(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = ttk.Frame(self, padding=(12, 12), style="Sidebar.TFrame")
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(4, weight=1)

        
        # App title: stretch to the full sidebar width and center the text.
        ttk.Label(self.sidebar, text="TopIso3D v2026", style="TitleCenter.TLabel").grid(
            row=0, column=0, sticky="ew"
        )
        # Fixed-height workspace path area (max ~3 lines)
        self.ws_area = ttk.Frame(self.sidebar)
        self.ws_area.grid(row=1, column=0, sticky="ew", pady=(6, 10))
        self.ws_area.columnconfigure(0, weight=1)
        # Reserve a constant vertical space so the sidebar doesn't jump when the path wraps
        self.sidebar.grid_rowconfigure(1, minsize=54)
        self.lbl_workspace = ttk.Label(
            self.ws_area,
            text="No workspace selected",
            wraplength=220,
            justify="center",
            anchor="center",
            style="Muted.TLabel",
        )
        self.lbl_workspace.grid(row=0, column=0, sticky="nsew")
        self._ws_tooltip = CreateToolTip(self.lbl_workspace, text="")

        # Settings (separated from workflow navigation)
        # Keep the same left alignment as the workflow buttons by using the same
        # two-column row layout (badge + button).
        settings_row = ttk.Frame(self.sidebar)
        settings_row.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(settings_row, text=" ", width=2).grid(row=0, column=0, sticky="w")
        ttk.Button(
            settings_row,
            text="⚙ Settings",
            command=self.open_settings,
            style="SidebarNavCenter.TButton",
            width=SIDEBAR_BTN_WIDTH,
        ).grid(row=0, column=1, sticky="w")

        ttk.Separator(self.sidebar).grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.nav_frame = ttk.Frame(self.sidebar)
        self.nav_frame.grid(row=4, column=0, sticky="nsew")

        # Quick help/about (kept at bottom)
        ttk.Separator(self.sidebar).grid(row=5, column=0, sticky="ew", pady=(10, 10))
        qa = ttk.Frame(self.sidebar)
        qa.grid(row=6, column=0, sticky="ew")
        # Keep the same visual width as the workflow buttons (badge + button).
        ttk.Label(qa, text=" ", width=2).grid(row=0, column=0, rowspan=2, sticky="nw")
        ttk.Button(qa, text="Help", command=self._help, style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH).grid(row=0, column=1, sticky="w")
        ttk.Button(qa, text="About", command=self._about, style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH).grid(row=1, column=1, sticky="w", pady=(6, 0))

        # Content area
        self.content = ttk.Frame(self, padding=(16, 16), style="Content.TFrame")
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)

        # Status bar + task panel
        self.status_var = tk.StringVar(value="Ready.")
        self.task_text_var = tk.StringVar(value="")

        self.statusbar = ttk.Frame(self, padding=(10, 6), style="Status.TFrame")
        self.statusbar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.statusbar.columnconfigure(0, weight=1)

        ttk.Label(self.statusbar, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        self.task_bar = ttk.Progressbar(self.statusbar, mode="determinate", length=240, maximum=100, value=0)
        self.task_bar.grid(row=0, column=1, sticky="e", padx=(12, 8))
        # Hide the global progress bar (TRHO has its own progress bar in the Compute (TRHO) page)
        self.task_bar.grid_remove()
        ttk.Label(self.statusbar, textvariable=self.task_text_var).grid(row=0, column=2, sticky="e")
        ttk.Button(self.statusbar, text="Execution Details…", command=self.show_execution_details).grid(row=0, column=3, sticky="e", padx=(8, 0))

        self.lbl_bits = ttk.Label(self.statusbar, text="—")
        self.lbl_bits.grid(row=0, column=4, sticky="e", padx=(12, 0))

        # Details window (created lazily)
        self._task_win = None

    # ---------- Pages ----------
    def _create_pages(self):
        self.pages = {}
        for key, cls in [
            ("Workspace", WorkspacePage),
            ("Compute", ComputePage),
            ("TLAP", TLAPPage),
            ("CP Viewer", CPViewerPage),
            ("PL2D", PL2DPage),
            ("PL2D Viewer", PL2DViewerPage),
            ("ATBP", ATBPPage),
            ("BCP Evaluation", BCPEvalPage),
            ("Reports", ReportsPage),
        ]:
            p = cls(self.content, self)
            p.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = p

    def _build_sidebar(self):
        self.nav_items = [
            ("Workspace", "Workspace"),
            ("TRHO", "Compute"),
            ("TLAP", "TLAP"),
            ("CP Viewer", "CP Viewer"),
            ("PL2D", "PL2D"),
            ("PL2D Viewer", "PL2D Viewer"),
            ("ATBP", "ATBP"),
            ("BCP Evaluation", "BCP Evaluation"),
            ("Reports", "Reports"),
        ]
        self.nav_buttons = {}
        self.nav_badges = {}
        self.nav_tooltips = {}

        for r, (label, key) in enumerate(self.nav_items):
            row = ttk.Frame(self.nav_frame)
            row.grid(row=r, column=0, sticky="w", pady=3)

            badge = ttk.Label(row, text=" ", width=2)
            badge.grid(row=0, column=0, sticky="w")
            btn = ttk.Button(row, text=label, command=lambda k=key: self.show_page(k), style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH)
            btn.grid(row=0, column=1, sticky="w")

            self.nav_badges[key] = badge
            self.nav_buttons[key] = btn
            self.nav_tooltips[key] = CreateToolTip(btn, text="")

    # ---------- Navigation ----------
    def show_page(self, key: str):
        page = self.pages[key]
        self._current_page_key = key
        self._current_page = page
        page.tkraise()

        # Refresh only the page being shown. Refreshing every page on every
        # navigation makes Windows redraws visibly slow.
        try:
            refresh_state = getattr(page, "refresh_state", None)
            if callable(refresh_state):
                refresh_state()
        except Exception as e:
            self._job_queue.put(("log", f"[UI] refresh_state error in {key}: {e}"))

        try:
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                refresh()
        except Exception as e:
            self._job_queue.put(("log", f"[UI] refresh error in {key}: {e}"))

        # Some pages may not implement on_show(); keep navigation robust.
        on_show = getattr(page, "on_show", None)
        if callable(on_show):
            try:
                on_show()
            except Exception as e:
                # Avoid crashing the GUI due to a page refresh error.
                self._job_queue.put(("log", f"[UI] on_show error in {key}: {e}"))
        

    # ---------- Status / task panel ----------
    def set_status(self, msg: str):
        self.state.status = msg
        self.status_var.set(msg)

    def set_task(self, text: str = "", done: int = 0, total: int = 0, active: bool = False):
        if not active:
            self.task_text_var.set("")
            try:
                self.task_bar.stop()
            except Exception:
                pass
            self.task_bar.config(mode="determinate", maximum=100, value=0)
            self._task_details = {"text": "", "done": 0, "total": 0}
            self._update_task_details_window()
            return

        self.task_text_var.set(text)
        if total and total > 0:
            self.task_bar.config(mode="determinate", maximum=total, value=max(0, min(done, total)))
        else:
            self.task_bar.config(mode="indeterminate", maximum=100)
            self.task_bar.start(10)

        self._task_details = {"text": text, "done": done, "total": total}
        self._update_task_details_window()

    
    def show_execution_details(self):
        """Show execution details (task + last run metadata/output)."""
        # Reuse the existing Task Details window but present it as Execution Details.
        self.show_task_details()
        try:
            if getattr(self, "_task_win", None) is not None and self._task_win.winfo_exists():
                self._task_win.title("Execution Details")
        except Exception:
            pass
    def show_task_details(self):
        if getattr(self, "_task_win", None) is None or not self._task_win.winfo_exists():
            self._task_win = tk.Toplevel(self)
            _ensure_floating_window(self._task_win)
            self._task_win.title("Task Details")
            self._task_win.geometry("560x360")
            self._task_win.protocol("WM_DELETE_WINDOW", self._task_win.withdraw)

            frm = ttk.Frame(self._task_win, padding=12)
            frm.pack(fill="both", expand=True)

            ttk.Label(frm, text="Current task", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
            self._task_details_label = ttk.Label(frm, text="—", wraplength=520)
            self._task_details_label.pack(anchor="w", pady=(6, 10))

            self._task_details_progress = ttk.Progressbar(frm, mode="determinate", maximum=100, value=0)
            self._task_details_progress.pack(fill="x")

            self._task_details_counts = ttk.Label(frm, text="—")
            self._task_details_counts.pack(anchor="w", pady=(8, 6))

            # Last execution metadata (filled when available)
            self._exec_meta_label = ttk.Label(frm, text="—", wraplength=520, style="Muted.TLabel")
            self._exec_meta_label.pack(anchor="w", pady=(0, 10))

            ttk.Separator(frm).pack(fill="x", pady=10)

            ttk.Label(frm, text="Log", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
            self._task_log = tk.Text(frm, height=10, wrap="word")
            self._task_log.pack(fill="both", expand=True, pady=(6, 0))

            btns = ttk.Frame(frm)
            btns.pack(fill="x", pady=(10, 0))
            ttk.Button(btns, text="Hide", command=self._task_win.withdraw).pack(side="right")

        self._update_task_details_window()
        self._task_win.deiconify()
        self._task_win.lift()

    def task_log(self, line: str):
        if getattr(self, "_task_win", None) is not None and self._task_win.winfo_exists():
            self._task_log.insert("end", line.rstrip() + "\n")
            self._task_log.see("end")

    def _update_task_details_window(self):
        if getattr(self, "_task_win", None) is None or not self._task_win.winfo_exists():
            return
        # Update execution metadata (if any)
        try:
            le = getattr(self.state, "last_execution", None)
            if hasattr(self, "_exec_meta_label") and self._exec_meta_label.winfo_exists():
                if le:
                    meta = []
                    if le.get("command"):
                        meta.append("Command: " + " ".join(le["command"]) if isinstance(le["command"], (list, tuple)) else f"Command: {le['command']}")
                    if le.get("cwd"):
                        meta.append(f"CWD: {le['cwd']}")
                    if le.get("exit_code") is not None:
                        meta.append(f"Exit code: {le['exit_code']}")
                    if le.get("duration_s") is not None:
                        meta.append(f"Runtime: {le['duration_s']:.2f} s")
                    self._exec_meta_label.config(text=" | ".join(meta) if meta else "—")
                else:
                    self._exec_meta_label.config(text="(No execution recorded yet)")
        except Exception:
            pass
        d = getattr(self, "_task_details", {"text": "", "done": 0, "total": 0})
        self._task_details_label.config(text=d.get("text") or "—")
        done = int(d.get("done", 0))
        total = int(d.get("total", 0))
        if total > 0:
            self._task_details_progress.config(maximum=total, value=max(0, min(done, total)))
            self._task_details_counts.config(text=f"{done} / {total}")
        else:
            self._task_details_progress.config(maximum=100, value=0)
            self._task_details_counts.config(text="—")

    def _register_active_process(self, proc, job_kind: str) -> None:
        self._active_process = proc
        self._active_job_kind = str(job_kind or "").strip().upper()

    def _clear_active_process(self, proc=None) -> None:
        current = getattr(self, "_active_process", None)
        if proc is not None and current is not None and current is not proc:
            return
        self._active_process = None
        self._active_job_kind = ""

    def _job_was_aborted(self, job_kind: str = "") -> bool:
        if not bool(getattr(self, "_job_abort_requested", False)):
            return False
        active = str(getattr(self, "_active_job_kind", "") or "").strip().upper()
        asked = str(job_kind or "").strip().upper()
        return (not asked) or (not active) or active == asked

    def _reset_abort_state(self) -> None:
        self._job_abort_requested = False
        self._active_job_kind = ""

    def abort_current_job(self, job_kind: str = "") -> bool:
        if not self._job_running:
            messagebox.showinfo("Abort calculation", "There is no running calculation to abort.", parent=self)
            return False

        active_kind = str(getattr(self, "_active_job_kind", "") or "").strip().upper()
        asked_kind = str(job_kind or active_kind or "calculation").strip().upper()
        if active_kind and asked_kind and active_kind != asked_kind:
            messagebox.showinfo(
                "Abort calculation",
                f"A different calculation is currently running ({active_kind}).",
                parent=self,
            )
            return False

        proc = getattr(self, "_active_process", None)
        if proc is None:
            messagebox.showinfo("Abort calculation", "No external process is currently attached to this calculation.", parent=self)
            return False

        label = active_kind or asked_kind or "calculation"
        if not messagebox.askyesno(
            "Abort calculation",
            f"Abort the running {label} calculation?\n\nAny partial output generated so far will be kept in the run folder.",
            parent=self,
        ):
            return False

        self._job_abort_requested = True
        self.set_status(f"Aborting {label}…")
        try:
            self._job_queue.put(("log", f"[{label}] Abort requested by user."))
        except Exception:
            pass

        try:
            proc.terminate()
        except Exception as e:
            try:
                self._job_queue.put(("log", f"[{label}] terminate() failed: {e}"))
            except Exception:
                pass

        def _force_kill(expected_proc=proc, expected_label=label):
            current = getattr(self, "_active_process", None)
            if current is not expected_proc:
                return
            try:
                if expected_proc.poll() is None:
                    expected_proc.kill()
                    try:
                        self._job_queue.put(("log", f"[{expected_label}] Process still alive after terminate(); kill() sent."))
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.after(2500, _force_kill)
        except Exception:
            pass
        return True

    # ---------- Workspace auto-validation (NO side effects) ----------
    def auto_validate_workspace(self):
        """
        Automatic, non-invasive validation:
        - detects fort.9 and/or *.f9 candidates
        - detects write permission
        Does NOT create fort.9 here.
        """
        ctx = self.state
        p = ctx.workspace_dir

        ctx.has_fort9 = False
        ctx.f9_candidates = []
        ctx.can_write = False
        ctx.workspace_ok = False
        ctx.workspace_msg = "—"

        if not p or not p.exists():
            ctx.workspace_msg = "No folder selected"
            return

        fort9 = p / "fort.9"
        ctx.has_fort9 = fort9.exists()

        # 1) candidatos preferenciais: *.f9
        f9_candidates = sorted(p.glob("*.f9"))

        # 2) candidatos alternativos: *.9 (exclui fort.9 e também exclui *.f9)
        nine_candidates = sorted(
            q for q in p.glob("*.9")
            if q.name != "fort.9" and not q.name.endswith(".f9")
        )

        # guarda tudo em ctx (útil para debug/GUI)
        ctx.f9_candidates = f9_candidates + nine_candidates

        ctx.can_write = os.access(str(p), os.W_OK)

        wf_ok = ctx.has_fort9 or (len(ctx.f9_candidates) >= 1)

        if wf_ok and ctx.can_write:
            ctx.workspace_ok = True
            if ctx.has_fort9:
                ctx.workspace_msg = "OK ✓ (fort.9 found)"
            else:
                if len(ctx.f9_candidates) == 1:
                    ctx.workspace_msg = f"OK ✓ ({ctx.f9_candidates[0].name} found; fort.9 will be prepared on run)"
                else:
                    ctx.workspace_msg = f"OK ✓ ({len(ctx.f9_candidates)} *.f9/*.9 found; will ask which one on run)"
        else:
            problems = []
            if not wf_ok:
                problems.append("missing fort.9 and no *.f9/*.9")
            if not ctx.can_write:
                problems.append("no write permission")
            ctx.workspace_msg = "NOT OK (" + ", ".join(problems) + ")"

        # Sync active TRHO selection from workspace state (or fallback).
        self._sync_active_trho_state()

        # Auto-detect existing TRHO output and parse silently (enables Reports immediately),
        # even if the folder is not suitable for launching a new TRHO run.
        existing_trho = self._find_existing_trho_out()
        if existing_trho is not None:
            base_msg = ctx.workspace_msg
            if not ctx.workspace_ok:
                ctx.workspace_msg = base_msg + " | existing TRHO output found (reports available)"
            self.auto_parse_trho_if_exists()


    # ---------- fort.9 preparation (ONLY when TRHO starts) ----------
    def ensure_fort9_for_run(self) -> Tuple[bool, str]:
        """
        Ensures workdir/fort.9 exists.
        Called ONLY when TRHO is about to start, to avoid modifying the folder early.
        """
        ctx = self.state
        workdir = ctx.workspace_dir
        if not workdir:
            return False, "No workspace selected."

        fort9 = workdir / "fort.9"
        if fort9.exists():
            return True, "fort.9 found"

        # 1) candidatos preferenciais: *.f9
        f9s = sorted(workdir.glob("*.f9"))

        # 2) candidatos alternativos: *.9 (exclui fort.9 e também exclui *.f9)
        nines = sorted(
            q for q in workdir.glob("*.9")
            if q.name != "fort.9" and not q.name.endswith(".f9")
        )

        candidates = f9s + nines

        if len(candidates) == 0:
            return False, "missing fort.9 and no *.f9 / *.9 found"

        if len(candidates) > 1:
            choices = "\n".join([f"- {x.name}" for x in candidates])
            pick = messagebox.askquestion(
                "Multiple wavefunction candidates found",
                "Multiple wavefunction files were found:\n\n"
                f"{choices}\n\n"
                "Click 'Yes' to use the first one listed, or 'No' to cancel.",
                icon="warning"
            )
            if pick != "yes":
                return False, "User canceled selection (multiple candidates)."
            src = candidates[0]
        else:
            src = candidates[0]

        return safe_symlink_or_copy(src, fort9)

    # ---------- UI state rules ----------

    def _resolve_executable(self, exe: str) -> str:
        """Resolve an executable either as an absolute path or via PATH."""
        resolved = resolve_executable(exe)
        return str(resolved) if resolved is not None else ""

    def _format_workspace_path(self, full_path: str, *, max_lines: int = 3) -> str:
        """Format the workspace path for the sidebar (stable height, max lines).

        Strategy: prefer showing the *end* of the path, trimming the left part if needed.
        """
        full_path = (full_path or "").strip()
        if not full_path:
            return "No workspace selected"

        import textwrap

        # Approximate characters per line for wraplength~220px with default font.
        width_chars = 34

        wrapped_full = textwrap.wrap(full_path, width=width_chars)
        if len(wrapped_full) <= max_lines:
            return "\n".join(wrapped_full)

        # Otherwise, progressively shorten from the left (keep the tail).
        tail_len = min(len(full_path), 140)
        while tail_len > 30:
            candidate = "…" + full_path[-tail_len:]
            wrapped = textwrap.wrap(candidate, width=width_chars)
            if len(wrapped) <= max_lines:
                return "\n".join(wrapped)
            tail_len -= 5

        # Fallback: hard wrap a minimal tail
        candidate = "…" + full_path[-40:]
        wrapped = textwrap.wrap(candidate, width=width_chars)
        return "\n".join(wrapped[:max_lines])

    def _ensure_trho_parsed_for_followups(self) -> bool:
        """Best-effort: ensure an existing TRHO result is parsed and cached.

        This allows follow-up modules such as TLAP to be enabled even in a new
        session, as long as the current workspace already contains a parseable
        trho.out.
        """
        ctx = self.state
        try:
            parsed = getattr(ctx, "trho_parsed", None)
            df_true = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
            if parsed is not None and df_true is not None and not df_true.empty:
                return True
        except Exception:
            pass

        self._sync_active_trho_state()
        out_path = self._find_existing_trho_out()
        if out_path is None:
            return False

        try:
            self.auto_parse_trho_if_exists()
            parsed = getattr(ctx, "trho_parsed", None)
            df_true = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
            return parsed is not None and df_true is not None and not df_true.empty
        except Exception:
            return False

    def _tlap_ready(self) -> bool:
        """Return True when TLAP can rely on a valid TRHO result for this workspace."""
        ctx = self.state
        if not ctx.workspace_ok or self._job_running:
            return False
        return self._ensure_trho_parsed_for_followups()

    def _tlap_tooltip_text(self) -> str:
        ctx = self.state
        if self._job_running:
            return "TLAP is unavailable while another job is running."
        if not ctx.workspace_ok:
            return "TLAP requires a valid workspace first."
        if self._find_existing_trho_out() is None:
            return "TLAP requires a valid TRHO output in this workspace."
        if not self._ensure_trho_parsed_for_followups():
            return "TLAP requires a parseable TRHO output in this workspace."
        return ""

    def _cp_viewer_ready(self) -> bool:
        """Return True when the CP Viewer can rely on a valid parsed TRHO result."""
        ctx = self.state
        if self._job_running:
            return False
        if ctx.workspace_dir is None:
            return False
        if self._find_existing_trho_out() is None:
            return False
        return self._ensure_trho_parsed_for_followups()

    def _cp_viewer_tooltip_text(self) -> str:
        ctx = self.state
        if self._job_running:
            return "CP Viewer is unavailable while another job is running."
        if ctx.workspace_dir is None:
            return "CP Viewer requires a workspace first."
        if self._find_existing_trho_out() is None:
            return "CP Viewer requires a valid TRHO output in this workspace."
        if not self._ensure_trho_parsed_for_followups():
            return "CP Viewer requires a parseable TRHO output in this workspace."
        return ""

    def _schedule_refresh_ui_state(self):
        """Schedule one periodic UI refresh callback.

        Several actions can request an immediate refresh.  On macOS, if each
        request also leaves its own periodic after() callback behind, the app can
        look like it is stuck/busy after closing secondary windows.  This helper
        centralizes scheduling and prevents duplicate refresh loops.
        """
        try:
            old_id = getattr(self, "_refresh_ui_after_id", None)
            if old_id is not None:
                try:
                    self.after_cancel(old_id)
                except Exception:
                    pass
            self._refresh_ui_after_id = self.after(self._ui_refresh_ms, self.refresh_ui_state)
        except Exception:
            self._refresh_ui_after_id = None

    def refresh_ui_state(self):
            # This invocation is now running; allow a fresh single schedule at the end.
            self._refresh_ui_after_id = None
            ctx = self.state

            full_ws = str(ctx.workspace_dir) if ctx.workspace_dir else ""
            ws_label = self._format_workspace_path(full_ws) if full_ws else "No workspace selected"

            bits = []
            if ctx.workspace_dir:
                bits.append("dir ✓")
            if ctx.has_fort9:
                bits.append("fort.9 ✓")
            elif ctx.f9_candidates:
                has_f9 = any(p.suffix == ".f9" for p in ctx.f9_candidates)
                has_9 = any(p.suffix == ".9" for p in ctx.f9_candidates)
                if has_f9 and has_9:
                    bits.append("*.f9/*.9 ✓")
                elif has_9:
                    bits.append("*.9 ✓")
                else:
                    bits.append("*.f9 ✓")
            if ctx.can_write:
                bits.append("write ✓")
            if ctx.trho_done:
                bits.append("TRHO ✓")
            bits_text = " | ".join(bits) if bits else "—"

            tlap_ready = self._tlap_ready()
            cpv_ready = self._cp_viewer_ready()
            has_pl2d_runs = self._has_any_pl2d_runs()
            bcp_ready = (
                ctx.trho_done
                and (getattr(ctx, "trho_parsed", None) is not None)
                and (getattr(getattr(ctx, "trho_parsed", None), "df_bcp_props", None) is not None)
                and (not getattr(ctx.trho_parsed, "df_bcp_props").empty)
            )

            rules_enabled = {
                "Workspace": True,
                "Compute": ctx.workspace_ok,
                "TLAP": tlap_ready,
                "CP Viewer": cpv_ready,
                "PL2D": ctx.workspace_ok and ctx.trho_done and (not self._job_running),
                "PL2D Viewer": (ctx.workspace_dir is not None) and has_pl2d_runs and (not self._job_running),
                "ATBP": ctx.workspace_ok and ctx.trho_done and (not self._job_running),
                "BCP Evaluation": bcp_ready and (not self._job_running),
                "Reports": ctx.trho_done,
            }
            badges = {
                "Workspace": "✓" if ctx.workspace_ok else ("!" if ctx.workspace_dir else "!"),
                "Compute": "✓" if ctx.trho_done else ("!" if ctx.workspace_ok else "🔒"),
                "TLAP": "!" if tlap_ready else ("!" if ctx.workspace_ok else "🔒"),
                "CP Viewer": "✓" if cpv_ready else ("!" if ctx.workspace_dir else "🔒"),
                "PL2D": "✓" if getattr(ctx, "pl2d_run_dir", None) else ("!" if ctx.trho_done else "🔒"),
                "PL2D Viewer": "✓" if has_pl2d_runs else ("!" if ctx.workspace_dir else "🔒"),
                "ATBP": "✓" if getattr(ctx, "atbp_out_path", None) else ("!" if (ctx.workspace_ok and ctx.trho_done) else "🔒"),
                "BCP Evaluation": "✓" if bcp_ready else ("!" if ctx.workspace_ok else "🔒"),
                "Reports": "✓" if ctx.trho_done else "🔒",
            }

            snapshot = (
                ws_label,
                full_ws,
                ctx.status,
                bits_text,
                tuple((k, rules_enabled.get(k, True), badges.get(k, " ")) for _, k in self.nav_items),
            )
            force = snapshot != self._last_ui_snapshot

            if force:
                self.lbl_workspace.config(text=ws_label)
                if hasattr(self, "_ws_tooltip"):
                    self._ws_tooltip.update_text(full_ws if full_ws else "")
                self.status_var.set(ctx.status)
                self.lbl_bits.config(text=bits_text)

                for _, key in self.nav_items:
                    btn = self.nav_buttons[key]
                    enabled = rules_enabled.get(key, True)
                    btn.state(["!disabled"] if enabled else ["disabled"])
                    self.nav_badges[key].config(text=badges.get(key, " "))
                    if key == "TLAP":
                        tip = "" if enabled else self._tlap_tooltip_text()
                        try:
                            self.nav_tooltips[key].update_text(tip)
                        except Exception:
                            pass
                    elif key == "CP Viewer":
                        tip = "" if enabled else self._cp_viewer_tooltip_text()
                        try:
                            self.nav_tooltips[key].update_text(tip)
                        except Exception:
                            pass
                self._last_ui_snapshot = snapshot

            # Refresh only the currently visible page. This avoids expensive redraws
            # of every page on every polling cycle, which is especially slow on Windows.
            current_page = getattr(self, "_current_page", None)
            if current_page is not None:
                try:
                    refresh_state = getattr(current_page, "refresh_state", None)
                    if callable(refresh_state):
                        refresh_state()
                except Exception:
                    pass

            self._schedule_refresh_ui_state()

    def _has_any_pl2d_runs(self) -> bool:
        """Return True if we can find at least one PL2D run.

        Used only for UI badges/state. We intentionally do NOT require fort.9/TRHO
        to be present because PL2D runs can be inspected even if the TRHO runner
        prerequisites are missing.
        """
        ctx = self.state
        ws = ctx.workspace_dir
        if not ws:
            return False

        # If user selected a single run directory directly
        if (ws / "slice000").exists():
            return True

        roots = []
        if (ws / "pl2d_runs").exists():
            roots.append(ws / "pl2d_runs")
        if ws.name == "pl2d_runs":
            roots.append(ws)

        for root in roots:
            try:
                for rd in root.iterdir():
                    if rd.is_dir() and (rd / "slice000").exists():
                        return True
            except Exception:
                continue
        return False

    # ---------- Job runner ----------

    def refresh_all_pages(self):
        """Refresh navigation badges and the currently visible page after state changes."""
        self._sync_active_trho_state()
        self._sync_active_tlap_state()
        # Force a fresh nav/status rebuild on the next tick.
        self._last_ui_snapshot = None
        self.refresh_ui_state()
        current_page = getattr(self, "_current_page", None)
        if current_page is not None and hasattr(current_page, "refresh"):
            try:
                current_page.refresh()
            except Exception:
                pass

    def re_scan_external_results_after_settings_change(self) -> None:
        """Re-scan the current workspace after output-name settings change.

        This is needed when the user adds a new accepted external result name
        (for example *.outp) while the workspace is already open.
        """
        ctx = self.state
        if not getattr(ctx, "workspace_dir", None):
            self.refresh_all_pages()
            return

        # Clear cached parse/detection state so the current workspace is inspected again
        # using the newly saved accepted file names.
        ctx.trho_done = False
        ctx.trho_parsed = None
        ctx.trho_parse_error = None
        ctx.trho_parse_attempted_out = ""
        try:
            ctx.df_bcp_props = None
            ctx.df_true_atoms = None
        except Exception:
            pass

        ctx.tlap_done = False
        ctx.tlap_parsed = None
        ctx.tlap_parse_error = None
        ctx.tlap_parse_attempted_out = ""

        try:
            ctx.df_atbp = None
        except Exception:
            pass
        ctx.atbp_out_path = None
        ctx.atbp_run_dir = None

        # Rebuild active selections and try to detect/parse external results immediately.
        self._sync_active_trho_state()
        self._sync_active_tlap_state()
        try:
            self.auto_validate_workspace()
        except Exception:
            pass

        # TLAP: if an active run now resolves to a configured output name, parse it.
        try:
            self._sync_active_tlap_state()
            if self._find_active_tlap_out() is not None:
                self.ensure_active_tlap_parsed()
        except Exception:
            pass

        # ATBP: refresh active output path according to the newly accepted names.
        try:
            atbp_run = self._get_active_atbp_dir()
            ctx.atbp_run_dir = atbp_run
            ctx.atbp_out_path = self._find_active_atbp_out()
        except Exception:
            pass

        self.refresh_all_pages()

    def _load_workspace_state(self) -> dict:
        path = _workspace_state_path(self.state)
        if path is None or not path.exists():
            return {}
        return _read_json_file(path)

    def _save_workspace_state(self, data: dict) -> None:
        path = _workspace_state_path(self.state)
        if path is None:
            return
        _write_json_file(path, data)

    def _list_trho_run_dirs(self) -> List[Path]:
        root = _trho_runs_dir(self.state)
        if root is None or not root.exists():
            return []
        runs = []
        try:
            for p in sorted(root.iterdir()):
                if p.is_dir() and self._find_matching_output_in_dirs([p], self._configured_output_names("trho")) is not None:
                    runs.append(p)
        except Exception:
            pass
        return runs

    def _next_trho_run_dir(self) -> Path:
        root = _trho_runs_dir(self.state)
        if root is None:
            raise RuntimeError("No workspace_dir")
        root.mkdir(parents=True, exist_ok=True)
        used = set()
        for p in root.iterdir() if root.exists() else []:
            if p.is_dir():
                m = re.match(r"trho_(\d+)$", p.name)
                if m:
                    used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return root / f"trho_{n:03d}"

    def _default_trho_run_name(self, cfg: Optional[dict] = None) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        if isinstance(cfg, dict):
            preset = str(cfg.get("preset") or "").strip().lower()
            ui_mode = str(cfg.get("ui_mode") or "").strip().lower()
            iauto = str(cfg.get("IAUTO") or "").strip()
            if preset:
                tag = preset
            elif ui_mode == "advanced":
                tag = f"adv_i{iauto}" if iauto else "advanced"
            else:
                tag = "trho"
        else:
            tag = "trho"
        tag = re.sub(r"[^A-Za-z0-9_-]+", "_", tag).strip("_") or "trho"
        return f"{ts}_{tag}"

    def _sanitize_trho_run_name(self, name: str) -> str:
        s = str(name or "").strip()
        s = re.sub(r"[^A-Za-z0-9._ -]+", "_", s)
        s = re.sub(r"\s+", "_", s).strip("._-")
        return s or "trho_run"

    def _unique_trho_run_dir_for_name(self, name: str) -> Path:
        root = _trho_runs_dir(self.state)
        if root is None:
            raise RuntimeError("No workspace_dir")
        root.mkdir(parents=True, exist_ok=True)
        base = self._sanitize_trho_run_name(name)
        cand = root / base
        n = 2
        while cand.exists():
            cand = root / f"{base}_{n}"
            n += 1
        return cand

    def _prompt_trho_run_name(self, cfg: Optional[dict] = None) -> Optional[str]:
        default_name = self._default_trho_run_name(cfg)
        name = _ask_run_name(
            self,
            title="TRHO run name",
            prompt="Choose a name for this TRHO run:",
            initialvalue=default_name,
        )
        if name is None:
            return None
        return self._sanitize_trho_run_name(name)

    def _read_trho_run_meta(self, run_dir: Path) -> dict:
        meta = _read_json_file(run_dir / "run.json")
        if not meta:
            meta = {"kind": "TRHO", "run_name": run_dir.name, "status": "unknown"}
        return meta

    def _friendly_trho_run_label(self, run_dir: Path) -> str:
        meta = self._read_trho_run_meta(run_dir)
        run_name = str(meta.get("run_name") or "").strip()
        created = str(meta.get("created_at") or "").strip()
        short_time = created[11:16] if len(created) >= 16 else created
        if run_name:
            return f"{run_name} | {short_time}" if short_time else run_name
        preset = str(meta.get("preset") or "").strip()
        ui_mode = str(meta.get("ui_mode") or "").strip().lower()
        iauto = str(meta.get("iauto") or meta.get("IAUTO") or "").strip()
        parts = []
        if preset:
            parts.append(preset.capitalize())
        elif ui_mode:
            parts.append(ui_mode.capitalize())
        else:
            parts.append(run_dir.name)
        if iauto:
            parts.append(f"IAUTO {iauto}")
        if short_time:
            parts.append(short_time)
        return " | ".join(parts)

    def _get_active_trho_dir(self) -> Optional[Path]:
        state = self._load_workspace_state()
        rel = str(state.get("active_trho") or "").strip()
        ws = getattr(self.state, "workspace_dir", None)
        if ws and rel:
            p = (ws / rel).resolve()
            try:
                if p.exists() and self._find_matching_output_in_dirs([p], self._configured_output_names("trho")) is not None:
                    return p
            except Exception:
                pass
        runs = self._list_trho_run_dirs()
        if runs:
            return runs[0]
        # legacy fallback
        if ws is not None:
            legacy = ws / "trho"
            if legacy.exists() and self._find_matching_output_in_dirs([legacy], self._configured_output_names("trho")) is not None:
                return legacy
        return None

    def _set_active_trho_run(self, run_dir: Path, *, refresh: bool = True) -> None:
        ws = getattr(self.state, "workspace_dir", None)
        if ws is None:
            return
        try:
            rel = str(run_dir.resolve().relative_to(ws.resolve()))
        except Exception:
            rel = str(run_dir)
        state = self._load_workspace_state()
        state["active_trho"] = rel
        self._save_workspace_state(state)
        self.state.active_trho_run = run_dir
        self.state.active_trho_label = self._friendly_trho_run_label(run_dir)
        if refresh:
            self.auto_parse_trho_if_exists()

    def _sync_active_trho_state(self) -> None:
        run_dir = self._get_active_trho_dir()
        self.state.active_trho_run = run_dir
        self.state.active_trho_label = self._friendly_trho_run_label(run_dir) if run_dir else "—"

    def _get_trho_run_selector_data(self) -> Tuple[List[str], Dict[str, Path], str]:
        """Return selector labels, mapping, and active label for TRHO runs."""
        self._sync_active_trho_state()
        runs = list(self._list_trho_run_dirs())
        active = getattr(self.state, "active_trho_run", None)
        try:
            if active is not None:
                active_p = Path(active)
                if active_p.exists() and all(active_p.resolve() != rd.resolve() for rd in runs):
                    runs.insert(0, active_p)
        except Exception:
            pass

        raw_labels = []
        for rd in runs:
            try:
                raw_labels.append((rd, self._friendly_trho_run_label(rd) or rd.name))
            except Exception:
                raw_labels.append((rd, rd.name))
        counts = {}
        for _, lbl in raw_labels:
            counts[lbl] = counts.get(lbl, 0) + 1

        values: List[str] = []
        options: Dict[str, Path] = {}
        active_label = "—"
        for rd, lbl in raw_labels:
            display = f"{lbl} [{rd.name}]" if counts.get(lbl, 0) > 1 else lbl
            values.append(display)
            options[display] = rd
            try:
                if active is not None and Path(active).resolve() == rd.resolve():
                    active_label = display
            except Exception:
                pass

        if values and active_label == "—":
            active_label = values[0]
        return values, options, active_label

    def _build_trho_run_metadata(self, run_dir: Path, cfg: Optional[dict]) -> dict:
        meta = {
            "kind": "TRHO",
            "run_name": str(getattr(self.state, "pending_trho_run_name", "") or run_dir.name),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "finished",
        }
        if isinstance(cfg, dict):
            meta["ui_mode"] = cfg.get("ui_mode")
            meta["preset"] = cfg.get("preset")
            meta["iauto"] = cfg.get("IAUTO")
        return meta

    def _list_tlap_run_dirs(self) -> List[Path]:
        root = _tlap_runs_dir(self.state)
        if root is None or not root.exists():
            return []
        runs = []
        try:
            for p in sorted(root.iterdir()):
                if p.is_dir() and (p / "tlap.inp").exists():
                    runs.append(p)
        except Exception:
            pass
        return runs

    def _default_tlap_run_name(self, cfg: Optional[dict] = None) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        if isinstance(cfg, dict):
            preset = str(cfg.get("preset") or "").strip().lower()
            ui_mode = str(cfg.get("ui_mode") or "").strip().lower()
            iauto = str(cfg.get("IAUTO") or "").strip()
            if preset:
                tag = f"tlap_{preset}"
            elif ui_mode == "advanced":
                tag = f"tlap_adv_i{iauto}" if iauto else "tlap_advanced"
            else:
                tag = "tlap"
        else:
            tag = "tlap"
        tag = re.sub(r"[^A-Za-z0-9_-]+", "_", tag).strip("_") or "tlap"
        return f"{ts}_{tag}"

    def _sanitize_tlap_run_name(self, name: str) -> str:
        s = str(name or "").strip()
        s = re.sub(r"[^A-Za-z0-9._ -]+", "_", s)
        s = re.sub(r"\s+", "_", s).strip("._-")
        return s or "tlap_run"

    def _unique_tlap_run_dir_for_name(self, name: str) -> Path:
        root = _tlap_runs_dir(self.state)
        if root is None:
            raise RuntimeError("No workspace_dir")
        root.mkdir(parents=True, exist_ok=True)
        base = self._sanitize_tlap_run_name(name)
        cand = root / base
        n = 2
        while cand.exists():
            cand = root / f"{base}_{n}"
            n += 1
        return cand

    def _prompt_tlap_run_name(self, cfg: Optional[dict] = None) -> Optional[str]:
        default_name = self._default_tlap_run_name(cfg)
        name = _ask_run_name(
            self,
            title="TLAP run name",
            prompt="Choose a name for this TLAP run:",
            initialvalue=default_name,
        )
        if name is None:
            return None
        return self._sanitize_tlap_run_name(name)

    def _read_tlap_run_meta(self, run_dir: Path) -> dict:
        meta = _read_json_file(run_dir / "run.json")
        if not meta:
            meta = {"kind": "TLAP", "run_name": run_dir.name, "status": "unknown"}
        return meta

    def _friendly_tlap_run_label(self, run_dir: Path) -> str:
        meta = self._read_tlap_run_meta(run_dir)
        run_name = str(meta.get("run_name") or "").strip()
        created = str(meta.get("created_at") or "").strip()
        short_time = created[11:16] if len(created) >= 16 else created
        parts = []
        if run_name:
            parts.append(run_name)
        preset = str(meta.get("preset") or "").strip()
        if (not run_name) and preset:
            parts.append(preset)
        iauto = str(meta.get("iauto") or "").strip()
        if iauto and (f"iauto{iauto}" not in (run_name or "").lower()):
            parts.append(f"IAUTO {iauto}")
        source_trho = str(meta.get("source_trho") or "").strip()
        if source_trho:
            parts.append(f"from {source_trho}")
        if short_time:
            parts.append(short_time)
        return " | ".join(parts) if parts else run_dir.name

    def _get_active_tlap_dir(self) -> Optional[Path]:
        state = self._load_workspace_state()
        rel = str(state.get("active_tlap") or "").strip()
        ws = getattr(self.state, "workspace_dir", None)
        if ws and rel:
            p = (ws / rel).resolve()
            try:
                if p.exists() and (p / "tlap.inp").exists():
                    return p
            except Exception:
                pass
        runs = self._list_tlap_run_dirs()
        if runs:
            return runs[0]
        if ws is not None:
            legacy = ws / "tlap"
            if legacy.exists() and (legacy / "tlap.inp").exists():
                return legacy
        return None

    def _set_active_tlap_run(self, run_dir: Path, *, refresh: bool = True) -> None:
        ws = getattr(self.state, "workspace_dir", None)
        if ws is None:
            return
        try:
            rel = str(run_dir.resolve().relative_to(ws.resolve()))
        except Exception:
            rel = str(run_dir)
        state = self._load_workspace_state()
        state["active_tlap"] = rel
        self._save_workspace_state(state)
        self.state.active_tlap_run = run_dir
        self.state.active_tlap_label = self._friendly_tlap_run_label(run_dir)
        if refresh:
            self.ensure_active_tlap_parsed()
            self.refresh_all_pages()

    def _sync_active_tlap_state(self) -> None:
        run_dir = self._get_active_tlap_dir()
        self.state.active_tlap_run = run_dir
        self.state.active_tlap_label = self._friendly_tlap_run_label(run_dir) if run_dir else "—"

    def _get_tlap_run_selector_data(self) -> Tuple[List[str], Dict[str, Path], str]:
        self._sync_active_tlap_state()
        runs = list(self._list_tlap_run_dirs())
        active = getattr(self.state, "active_tlap_run", None)
        try:
            if active is not None:
                active_p = Path(active)
                if active_p.exists() and all(active_p.resolve() != rd.resolve() for rd in runs):
                    runs.insert(0, active_p)
        except Exception:
            pass
        raw_labels = []
        for rd in runs:
            try:
                raw_labels.append((rd, self._friendly_tlap_run_label(rd) or rd.name))
            except Exception:
                raw_labels.append((rd, rd.name))
        counts = {}
        for _, lbl in raw_labels:
            counts[lbl] = counts.get(lbl, 0) + 1
        values: List[str] = []
        options: Dict[str, Path] = {}
        active_label = "—"
        for rd, lbl in raw_labels:
            display = f"{lbl} [{rd.name}]" if counts.get(lbl, 0) > 1 else lbl
            values.append(display)
            options[display] = rd
            try:
                if active is not None and Path(active).resolve() == rd.resolve():
                    active_label = display
            except Exception:
                pass
        if values and active_label == "—":
            active_label = values[0]
        return values, options, active_label

    def _build_tlap_run_metadata(self, run_dir: Path, cfg: Optional[dict]) -> dict:
        source_trho = None
        active_trho = getattr(self.state, "active_trho_run", None)
        try:
            if active_trho is not None:
                source_trho = Path(active_trho).name
        except Exception:
            source_trho = None
        meta = {
            "kind": "TLAP",
            "run_name": str(getattr(self.state, "pending_tlap_run_name", "") or run_dir.name),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "finished",
            "source_trho": source_trho,
        }
        if isinstance(cfg, dict):
            meta["ui_mode"] = cfg.get("ui_mode")
            meta["preset"] = cfg.get("preset")
            meta["iauto"] = cfg.get("IAUTO")
        return meta

    def _configured_output_names(self, kind: str) -> List[str]:
        key = f"{str(kind or '').strip().lower()}_output_names"
        defaults = {
            "trho_output_names": DEFAULT_TRHO_OUTPUT_NAMES,
            "tlap_output_names": DEFAULT_TLAP_OUTPUT_NAMES,
            "atbp_output_names": DEFAULT_ATBP_OUTPUT_NAMES,
        }
        return parse_output_name_list(self._settings.get(key), defaults.get(key, []))

    def _find_matching_output_in_dirs(self, base_dirs, accepted_names: List[str]) -> Optional[Path]:
        names = parse_output_name_list(accepted_names, [])
        if not names:
            return None
        if isinstance(base_dirs, (str, os.PathLike, Path)):
            base_dirs = [base_dirs]
        for base in (base_dirs or []):
            if base is None:
                continue
            try:
                b = Path(base)
            except Exception:
                continue
            try:
                if not b.exists():
                    continue
            except Exception:
                continue
            for name in names:
                cand = b / name
                try:
                    if cand.exists() and cand.is_file():
                        return cand
                except Exception:
                    pass
        return None

    def _preferred_output_name(self, kind: str, fallback: str) -> str:
        names = self._configured_output_names(kind)
        return names[0] if names else fallback

    def _find_existing_trho_out(self) -> Optional[Path]:
        """Return the best existing TRHO output candidate for the current workspace."""
        ctx = self.state
        ws = getattr(ctx, "workspace_dir", None)
        if not ws:
            return None

        names = self._configured_output_names("trho")

        active_dir = self._get_active_trho_dir()
        if active_dir is not None:
            found = self._find_matching_output_in_dirs([active_dir], names)
            if found is not None:
                return found

        candidates = [
            ws,
            ws / "trho",
        ]
        return self._find_matching_output_in_dirs(candidates, names)

    def build_trho_command(self) -> List[str]:
        """
        Replace this with your real TRHO invocation.
        For now returns a MOCK marker.
        """
        return ["__MOCK_TRHO__"]


    def _find_active_tlap_out(self) -> Optional[Path]:
        run_dir = self._get_active_tlap_dir()
        if run_dir is None:
            return None
        return self._find_matching_output_in_dirs([Path(run_dir)], self._configured_output_names("tlap"))

    def ensure_active_tlap_parsed(self) -> bool:
        out_path = self._find_active_tlap_out()
        if out_path is None:
            self.state.tlap_parsed = None
            self.state.tlap_done = False
            return False
        try:
            source_trho = ""
            try:
                meta = self._read_tlap_run_meta(out_path.parent)
                source_trho = str(meta.get("source_trho") or "")
            except Exception:
                pass
            parsed = parse_tlap_out(
                out_path,
                trho_parsed=getattr(self.state, "trho_parsed", None),
                source_trho_run=source_trho,
            )
            self.state.tlap_parsed = parsed
            self.state.tlap_done = True
            self.state.tlap_parse_error = None
            return True
        except Exception as e:
            self.state.tlap_parsed = None
            self.state.tlap_done = True
            self.state.tlap_parse_error = str(e)
            return False

    def auto_parse_trho_if_exists(self):
        """If an existing trho.out is found, parse it silently and enable Reports.

        Accepted locations follow the configured TRHO output names in Settings.
        """
        ctx = self.state
        if not ctx.workspace_dir:
            return

        self._sync_active_trho_state()
        out_path = self._find_existing_trho_out()
        if out_path is None or not out_path.exists():
            return

        out_key = str(Path(out_path).resolve())
        if getattr(ctx, "trho_parse_attempted_out", "") == out_key:
            parsed = getattr(ctx, "trho_parsed", None)
            if parsed is not None or getattr(ctx, "trho_parse_error", None):
                return

        try:
            ctx.trho_parse_attempted_out = out_key
            parsed = parse_trho_out(
                out_path,
                open_shell=getattr(self.state, "open_shell", False),
                slab_2d=getattr(self.state, "slab_2d", False),
                nna_cutoff_ang=float(getattr(self.state, "nna_cutoff_ang", 0.35) or 0.35),
            )
            ctx.trho_parsed = parsed
            ctx.trho_done = True
            ctx.trho_parse_error = None
            log_event(ctx, f'TRHO existing auto-parsed: {out_path}')
            try:
                ctx.df_bcp_props = parsed.df_bcp_props.copy()
            except Exception:
                ctx.df_bcp_props = pd.DataFrame()
            try:
                ctx.df_true_atoms = parsed.df_true_atoms.copy()
            except Exception:
                ctx.df_true_atoms = pd.DataFrame()
            try:
                rel = out_path.relative_to(Path(ctx.workspace_dir))
            except Exception:
                rel = out_path
            self._sync_active_trho_state()
            self.set_status(f"✔ TRHO existing (auto-parsed: {rel})")
        except Exception as e:
            ctx.trho_parsed = None
            ctx.trho_done = True
            ctx.trho_parse_error = str(e)
            self.set_status(f"TRHO found (parse failed): {e}")

        self.refresh_all_pages()



    def parse_existing_trho(self):
        """Teste parcial A: parsear um TRHO já existente (trho/trho.out) sem rodar o properties."""
        ctx = self.state
        if not ctx.workspace_dir:
            messagebox.showwarning("TRHO", "Select a workspace first.")
            return

        out_path = self._find_existing_trho_out()
        if out_path is None or not out_path.exists():
            messagebox.showwarning("TRHO", "No configured TRHO output file was found in the workspace root or workspace/trho/.")
            return

        try:
            parsed = parse_trho_out(
                out_path,
                open_shell=getattr(self.state, "open_shell", False),
                slab_2d=getattr(self.state, "slab_2d", False),
                nna_cutoff_ang=float(getattr(self.state, "nna_cutoff_ang", 0.35) or 0.35),
            )
            ctx.trho_parsed = parsed
            ctx.trho_done = True
            log_event(ctx, 'TRHO finished OK')
            ctx.df_bcp_props = parsed.df_bcp_props
            ctx.df_true_atoms = parsed.df_true_atoms
            self.refresh_ui_state()

            summary = f"Parsed OK. TRUE atoms: {len(parsed.df_true_atoms)} | BCPs: {len(parsed.df_bcp_props)} | RCPs: {len(parsed.df_ring)} | CCPs: {len(parsed.df_cage)}"
            self._job_queue.put(("log", "[TRHO] " + summary))
            self._sync_active_trho_state()
            self.set_status("✔ TRHO parsed (existing trho.out)")
            # Non-blocking: details are available in the log panel.
            

        except Exception as e:
            # TRHO output exists, but parsing failed (keep app usable and explain in Reports).
            ctx.trho_parsed = None
            ctx.trho_done = True
            ctx.trho_parse_error = str(e)
            self._job_queue.put(("log", f"[TRHO] parsing failed: {e}"))
            self._job_queue.put(("parse_error", str(e)))
            self.set_status("TRHO finished (parsing failed)")

    def run_trho(self):
        if self._job_running:
            messagebox.showinfo("TRHO", "A job is already running.")
            return

        ctx = self.state
        log_event(ctx, 'TRHO started')
        if not ctx.workspace_ok or not ctx.workspace_dir:
            messagebox.showwarning("TRHO", "Workspace is not valid. Choose a folder with fort.9 or *.f9 and write permission.")
            return

        if not messagebox.askyesno(
            "Run TRHO",
            "TRHO calculations can be computationally demanding and may take some time to finish.\n\nDo you want to continue?",
        ):
            return

        # Create/link fort.9 ONLY now (right before running).
        ok, msg = self.ensure_fort9_for_run()
        if not ok:
            messagebox.showerror("TRHO", msg)
            return
        self._job_queue.put(("log", f"[Workspace] {msg}"))
        self.auto_validate_workspace()  # refresh detection; fort.9 should exist now

        self._job_running = True
        ctx.trho_done = False

        try:
            p = self.pages.get("Compute")
            if p is not None and hasattr(p, "_set_running"):
                p._set_running(True, "Running… (TRHO may take a long time)")
        except Exception:
            pass

        # Keep the bottom status bar free of redundant TRHO task text; the Compute page has its own progress UI.
        self.set_task(active=False)

        self.set_status("Running TRHO…")

        trho_cfg = None
        try:
            p_compute = self.pages.get("Compute")
            if p_compute is not None and hasattr(p_compute, "collect_trho_config"):
                trho_cfg = p_compute.collect_trho_config()
        except Exception:
            trho_cfg = None

        run_name = self._prompt_trho_run_name(trho_cfg)
        if run_name is None:
            self._job_running = False
            try:
                p = self.pages.get("Compute")
                if p is not None and hasattr(p, "_set_running"):
                    p._set_running(False)
            except Exception:
                pass
            self.set_status("TRHO run canceled")
            return
        self.state.pending_trho_run_name = run_name

        cmd = self.build_trho_command()

        def worker():
            try:
                import subprocess

                trho_dir = self.prepare_trho_folder()
                inp_path = self.write_trho_input(trho_dir)
                out_path = trho_dir / "trho.out"
                try:
                    meta = self._build_trho_run_metadata(trho_dir, trho_cfg)
                    meta["status"] = "running"
                    _write_json_file(trho_dir / "run.json", meta)
                except Exception:
                    pass

                exe = self.state.properties_exe
                exe_str = str(exe) if exe is not None else ""
                exe_path = _best_effort_make_executable(exe_str)
                exe_resolved = str(exe_path) if exe_path is not None else ""

                if not exe_resolved:
                    self._job_queue.put(("error", f"properties executable not found (configure it in Settings): {exe_str!r}"))
                    return

                # Record execution metadata (for Execution Details)
                import time as _time
                _t0 = _time.time()
                self.state.last_execution = {"command": [exe_resolved], "cwd": str(trho_dir), "exit_code": None, "duration_s": None}

                # Run properties by feeding TRHO input via stdin and capturing combined stdout/stderr.
                self._job_queue.put(("log", f"[TRHO] cwd: {trho_dir}"))
                self._job_queue.put(("log", f"[TRHO] exe: {exe_resolved}"))
                self._job_queue.put(("log", f"[TRHO] inp: {inp_path.name}"))
                self._job_queue.put(("log", f"[TRHO] out: {out_path.name}"))

                with open(inp_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
                    p = subprocess.Popen(
                        [str(exe_resolved)],
                        cwd=str(trho_dir),
                        stdin=fin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                        **_windows_subprocess_silent_kwargs(),
                    )
                    self._register_active_process(p, "TRHO")

                    # progresso indeterminado (até você ter métrica real)
                    self._job_queue.put(("progress", 0, 0))

                    # stream de saída pro log
                    for line in p.stdout:
                        self._job_queue.put(("log", line.rstrip("\n")))
                        fout.write(line)

                    p.wait()

                    rc = p.returncode
                    self._clear_active_process(p)
                    self._cleanup_run_temp_files(trho_dir, "TRHO")
                    try:
                        _t1 = _time.time()
                        if isinstance(getattr(self.state, "last_execution", None), dict):
                            self.state.last_execution["exit_code"] = rc
                            self.state.last_execution["duration_s"] = float(_t1 - _t0)
                    except Exception:
                        pass
                    if rc == 0 and out_path.exists():
                        # TRHO finished successfully (exit code 0). Parsing may still fail depending on output format.
                        self.state.trho_done = True
                        self.state.trho_parse_error = None
                        try:
                            # >>> AQUI ENTRA O PARSER <<<
                            parsed = parse_trho_out(
                                out_path,
                                open_shell=getattr(self.state, "open_shell", False),
                                slab_2d=getattr(self.state, "slab_2d", False),
                                nna_cutoff_ang=float(getattr(self.state, "nna_cutoff_ang", 0.35) or 0.35),
                            )

                            # guardar no contexto (sem globals)
                            self.state.trho_parsed = parsed
                            self.state.trho_done = True

                            # opcional: guardar atalhos
                            self.state.df_bcp_props = parsed.df_bcp_props
                            self.state.df_true_atoms = parsed.df_true_atoms

                            summary = f"Parsed OK. TRUE atoms: {len(parsed.df_true_atoms)} | BCPs: {len(parsed.df_bcp_props)} | RCPs: {len(parsed.df_ring)} | CCPs: {len(parsed.df_cage)}"
                            self._job_queue.put(("log", "[TRHO] " + summary))
                            meta = self._build_trho_run_metadata(trho_dir, trho_cfg)
                            _write_json_file(trho_dir / "run.json", meta)
                            self._job_queue.put(("parsed", summary))
                            self._job_queue.put(("trho_run_ready", str(trho_dir), self._friendly_trho_run_label(trho_dir)))

                        except Exception as e:
                            # TRHO ran (rc=0), but parsing failed. Keep TRHO as done and expose error in Reports.
                            self.state.trho_parsed = None
                            self.state.trho_done = True
                            self.state.trho_parse_error = str(e)
                            self._job_queue.put(("log", f"[TRHO] parsing failed: {e}"))
                            self._job_queue.put(("parse_error", str(e)))

                    self._job_queue.put(("done", rc))


            except Exception as e:
                self._clear_active_process()
                self._job_queue.put(("error", str(e)))

        self._job_thread = threading.Thread(target=worker, daemon=True)
        self._job_thread.start()

    def run_tlap(self, cfg: dict) -> None:
        """Run TLAP via properties for the currently prepared simple-mode input."""
        if self._job_running:
            messagebox.showinfo("TLAP", "A job is already running.")
            return

        ctx = self.state
        if not ctx.workspace_ok or not ctx.workspace_dir:
            messagebox.showwarning("TLAP", "Workspace is not valid. Choose a folder with fort.9 or *.f9 and write permission.")
            return

        ok, msg = self.ensure_fort9_for_run()
        if not ok:
            messagebox.showerror("TLAP", msg)
            return
        self._job_queue.put(("log", f"[Workspace] {msg}"))
        self.auto_validate_workspace()

        self._job_running = True
        self.set_task(active=False)
        self.set_status("Running TLAP…")

        try:
            p = self.pages.get("TLAP")
            if p is not None and hasattr(p, "_set_running"):
                p._set_running(True, "Running… (TLAP may take a long time)")
        except Exception:
            pass

        tlap_dir = self.prepare_tlap_folder()
        self._tlap_last_cfg = dict(cfg or {})
        inp_path = self.write_tlap_input(tlap_dir)
        try:
            meta = self._build_tlap_run_metadata(tlap_dir, cfg)
            meta["status"] = "running"
            _write_json_file(tlap_dir / "run.json", meta)
            summary = getattr(self.state, "tlap_last_summary", {}) or {}
            iauto = str(cfg.get("IAUTO", "0"))
            nea_count = summary.get("nea_count", "?")
            active = summary.get("active_shells", "?")
            vscc = summary.get("vscc", cfg.get("VSCC", False))
            with open(tlap_dir / "tlap_generation_summary.txt", "w", encoding="utf-8") as fh:
                fh.write(f"IAUTO={iauto}\n")
                fh.write(f"NEAs={nea_count}\n")
                fh.write(f"VSCC={vscc}\n")
                fh.write(f"active_shells={active}\n")
                fh.write(f"elements={','.join(summary.get('elements', []))}\n")
        except Exception:
            pass

        def worker():
            try:
                import subprocess
                out_path = tlap_dir / "tlap.out"
                exe = self.state.properties_exe
                exe_path = str(_best_effort_make_executable(exe) or "")
                if not exe_path:
                    self._job_queue.put(("tlap_fail", f"properties executable not found: {exe}", str(tlap_dir)))
                    return

                self._job_queue.put(("log", f"[TLAP] cwd: {tlap_dir}"))
                self._job_queue.put(("log", f"[TLAP] exe: {exe_path}"))
                self._job_queue.put(("log", f"[TLAP] inp: {inp_path.name}"))
                self._job_queue.put(("log", f"[TLAP] out: {out_path.name}"))

                with open(inp_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
                    p = subprocess.Popen(
                        [str(exe_path)],
                        cwd=str(tlap_dir),
                        stdin=fin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                        **_windows_subprocess_silent_kwargs(),
                    )
                    self._register_active_process(p, "TLAP")

                    for line in p.stdout:
                        self._job_queue.put(("log", line.rstrip("\n")))
                        fout.write(line)

                    p.wait()
                    rc = p.returncode
                    self._clear_active_process(p)
                    self._cleanup_run_temp_files(tlap_dir, "TLAP")
                    label = self._friendly_tlap_run_label(tlap_dir)
                    if rc == 0 and out_path.exists():
                        self._job_queue.put(("tlap_done", str(out_path), str(tlap_dir), label))
                    else:
                        self._job_queue.put(("tlap_fail", f"TLAP failed (rc={rc})", str(tlap_dir)))
            except Exception as e:
                self._clear_active_process()
                self._job_queue.put(("tlap_fail", str(e), str(tlap_dir)))

        self._job_thread = threading.Thread(target=worker, daemon=True)
        self._job_thread.start()

    def _poll_job_queue(self):
        try:
            while True:
                item = self._job_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self.task_log(item[1])

                elif kind == "progress":
                    done, total = item[1], item[2]
                    # TRHO uses the page-local progress bar; avoid duplicating task text in the bottom status bar.
                    self.set_task(active=False)

                elif kind == "pl2d_progress":
                    done, total, msg = item[1], item[2], item[3]
                    # Update page-local progress bar if page exists
                    try:
                        p = self.pages.get("PL2D")
                        if p is not None and hasattr(p, "pb"):
                            p.pb.configure(maximum=max(1, int(total)))
                            p.pb["value"] = int(done)
                            if hasattr(p, "lbl_pb"):
                                p.lbl_pb.configure(text=msg)
                            p.update_idletasks()
                    except Exception:
                        pass

                elif kind == "pl2d_done":
                    run_dir = item[1]
                    self.state.pl2d_run_dir = Path(run_dir) if run_dir else None
                    self._clear_active_process()
                    self._active_job_kind = ""
                    try:
                        p = self.pages.get("PL2D")
                        if p is not None:
                            p.lbl_status.configure(text="✔ PL2D finished")
                            p.btn_run.configure(state="normal")
                            if hasattr(p, "btn_export_campaign"):
                                p.btn_export_campaign.configure(state="normal")
                            if hasattr(p, "lbl_pb"):
                                p.lbl_pb.configure(text="Done.")
                            if hasattr(p, "pb"):
                                try:
                                    p.pb["value"] = p.pb.cget("maximum")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    self._job_running = False
                    self.state.pl2d_running = False
                    self.set_task(active=False)
                    self.set_status("PL2D finished ✓")
                    self.task_log("[PL2D] finished OK")
                    self.refresh_all_pages()

                elif kind == "pl2d_fail":
                    msg, run_dir = item[1], item[2]
                    self._clear_active_process()
                    self._active_job_kind = ""
                    self._job_running = False
                    self.state.pl2d_running = False
                    self.set_task(active=False)
                    self.set_status("PL2D failed")
                    self.task_log("[PL2D] failed: " + str(msg))
                    try:
                        p = self.pages.get("PL2D")
                        if p is not None:
                            p.btn_run.configure(state="normal")
                            if hasattr(p, "btn_export_campaign"):
                                p.btn_export_campaign.configure(state="normal")
                            p.lbl_status.configure(text="▶ PL2D not run")
                            if hasattr(p, "lbl_pb"):
                                p.lbl_pb.configure(text="Failed.")
                    except Exception:
                        pass
                    self.refresh_all_pages()
                    messagebox.showerror("PL2D", str(msg))

                elif kind == "tlap_done":
                    out_path, run_dir, label = item[1], item[2], item[3]
                    self._job_running = False
                    self.state.tlap_done = True
                    self.state.tlap_parse_error = None
                    self.set_task(active=False)
                    if self._job_was_aborted("TLAP"):
                        self.set_status("TLAP aborted")
                        self.task_log("[TLAP] aborted by user")
                    else:
                        self.set_status("TLAP finished ✓")
                        self.task_log("[TLAP] finished OK")

                    run_dir_p = Path(run_dir)
                    try:
                        meta = self._read_tlap_run_meta(run_dir_p)
                        meta["status"] = "aborted" if self._job_was_aborted("TLAP") else "finished"
                        _write_json_file(run_dir_p / "run.json", meta)
                    except Exception:
                        pass

                    existing_active = self._get_active_tlap_dir()
                    is_first = existing_active is None or not Path(existing_active).exists()
                    if is_first:
                        self._set_active_tlap_run(run_dir_p, refresh=False)
                        self.state.active_tlap_run = run_dir_p
                        self.state.active_tlap_label = label
                        self.task_log(f"[TLAP] active run set automatically: {label}")
                    else:
                        try:
                            if existing_active.resolve() != run_dir_p.resolve():
                                answer = messagebox.askyesno(
                                    "Active TLAP result",
                                    f"TLAP run saved as {run_dir_p.name}.\n\nDo you want to set it as the active TLAP result?"
                                )
                                if answer:
                                    self._set_active_tlap_run(run_dir_p, refresh=False)
                                    self.state.active_tlap_run = run_dir_p
                                    self.state.active_tlap_label = label
                                    self.task_log(f"[TLAP] active run changed to: {label}")
                            else:
                                self._set_active_tlap_run(run_dir_p, refresh=False)
                                self.state.active_tlap_run = run_dir_p
                                self.state.active_tlap_label = label
                        except Exception:
                            pass

                    try:
                        p = self.pages.get("TLAP")
                        if p is not None:
                            if hasattr(p, "_set_running"):
                                p._set_running(False)
                            if hasattr(p, "set_completion_text"):
                                p.set_completion_text("TLAP aborted" if self._job_was_aborted("TLAP") else "TLAP completed ✓")
                    except Exception:
                        pass
                    self.state.pending_tlap_run_name = ""
                    self._reset_abort_state()
                    self.refresh_all_pages()

                elif kind == "tlap_fail":
                    msg, run_dir = item[1], item[2]
                    self._job_running = False
                    self.state.tlap_done = False
                    self.set_task(active=False)
                    aborted = self._job_was_aborted("TLAP")
                    self.set_status("TLAP aborted" if aborted else "TLAP failed")
                    self.task_log(("[TLAP] aborted by user" if aborted else "[TLAP] failed: " + str(msg)))
                    try:
                        run_dir_p = Path(run_dir)
                        meta = self._read_tlap_run_meta(run_dir_p)
                        meta["status"] = "aborted" if aborted else "failed"
                        _write_json_file(run_dir_p / "run.json", meta)
                    except Exception:
                        pass
                    try:
                        p = self.pages.get("TLAP")
                        if p is not None:
                            if hasattr(p, "_set_running"):
                                p._set_running(False)
                            if hasattr(p, "set_completion_text"):
                                p.set_completion_text("TLAP aborted" if aborted else "TLAP failed ✖")
                    except Exception:
                        pass
                    self.state.pending_tlap_run_name = ""
                    self._reset_abort_state()
                    self.refresh_all_pages()
                    if not aborted:
                        messagebox.showerror("TLAP", str(msg))

                # -----------------------------
                # ATBP job events
                # -----------------------------
                elif kind == "atbp_done":
                    out_path, run_dir = item[1], item[2]
                    self._job_running = False
                    self.set_task(active=False)
                    aborted = self._job_was_aborted("ATBP")
                    run_dir_p = Path(run_dir)
                    try:
                        meta = self._read_atbp_run_meta(run_dir_p)
                        meta["status"] = "aborted" if aborted else "finished"
                        meta.pop("failure_reason", None)
                        meta.pop("failure_kind", None)
                        _write_json_file(run_dir_p / "run.json", meta)
                    except Exception:
                        pass
                    if aborted:
                        self.set_status("ATBP aborted")
                        self.task_log("[ATBP] aborted by user")
                    else:
                        self.set_status("ATBP finished ✓")
                        self.task_log("[ATBP] finished OK")
                        try:
                            self._set_active_atbp_run(run_dir_p)
                        except Exception:
                            pass

                    # Update ATBP page widgets (if present)
                    try:
                        p = self.pages.get("ATBP")
                        if p is not None:
                            if hasattr(p, "on_atbp_done"):
                                try:
                                    p.on_atbp_done(Path(out_path) if out_path else None)
                                except Exception:
                                    pass

                            if hasattr(p, "var_out"):
                                p.var_out.set(str(out_path))
                            if hasattr(p, "lbl_status"):
                                p.lbl_status.configure(text=(f"ATBP aborted: {run_dir_p.name}" if aborted else f"✔ Prepared/ran: {run_dir_p}"))
                            if hasattr(p, "btn_parse"):
                                p.btn_parse.configure(state="normal")
                            if hasattr(p, "btn_export_json"):
                                p.btn_export_json.configure(state="normal")
                            if hasattr(p, "btn_export_csv"):
                                p.btn_export_csv.configure(state="normal")
                    except Exception:
                        pass

                    self._reset_abort_state()
                    self.refresh_all_pages()

                elif kind == "atbp_issue":
                    issue, out_path, run_dir = item[1], item[2], item[3]
                    self._job_running = False
                    self.set_task(active=False)
                    aborted = self._job_was_aborted("ATBP")
                    run_dir_p = Path(run_dir)
                    try:
                        meta = self._read_atbp_run_meta(run_dir_p)
                        meta["status"] = "aborted" if aborted else "failed"
                        if (not aborted) and isinstance(issue, dict):
                            meta["failure_reason"] = str(issue.get("failure_reason") or "ATBP output issue")
                            meta["failure_kind"] = str(issue.get("kind") or "output_issue")
                        _write_json_file(run_dir_p / "run.json", meta)
                    except Exception:
                        pass

                    try:
                        p = self.pages.get("ATBP")
                        if p is not None:
                            if hasattr(p, "var_out"):
                                p.var_out.set(str(out_path))
                            if hasattr(p, "lbl_status"):
                                p.lbl_status.configure(text=(f"ATBP aborted: {run_dir_p.name}" if aborted else f"ATBP failed: {run_dir_p.name}"))
                            if hasattr(p, "_set_running"):
                                p._set_running(False)
                    except Exception:
                        pass

                    self.set_status("ATBP aborted" if aborted else "ATBP failed")
                    if aborted:
                        self.task_log("[ATBP] aborted by user")
                    else:
                        self.task_log("[ATBP] output issue: " + str(issue.get("failure_reason") if isinstance(issue, dict) else issue))
                    self._reset_abort_state()
                    self.refresh_all_pages()
                    if not aborted and isinstance(issue, dict):
                        box = messagebox.showwarning if issue.get("kind") in ("gauss_quadrature", "no_charge_values") else messagebox.showerror
                        box(str(issue.get("title") or "ATBP"), str(issue.get("message") or "ATBP output issue"))

                elif kind == "atbp_fail":
                    msg, run_dir = item[1], item[2]
                    self._job_running = False
                    self.set_task(active=False)
                    aborted = self._job_was_aborted("ATBP")
                    self.set_status("ATBP aborted" if aborted else "ATBP failed")
                    self.task_log(("[ATBP] aborted by user" if aborted else "[ATBP] failed: " + str(msg)))
                    try:
                        if run_dir:
                            run_dir_p = Path(run_dir)
                            meta = self._read_atbp_run_meta(run_dir_p)
                            meta["status"] = "aborted" if aborted else "failed"
                            if aborted:
                                meta.pop("failure_reason", None)
                                meta.pop("failure_kind", None)
                            _write_json_file(run_dir_p / "run.json", meta)
                    except Exception:
                        pass

                    try:
                        p = self.pages.get("ATBP")
                        if p is not None:
                            if (not aborted) and hasattr(p, "on_atbp_fail"):
                                try:
                                    p.on_atbp_fail(str(msg))
                                except Exception:
                                    pass
                            elif hasattr(p, "_set_running"):
                                p._set_running(False)
                            if hasattr(p, "lbl_status"):
                                p.lbl_status.configure(text=(f"ATBP aborted: {run_dir}" if aborted and run_dir else (f"✖ Failed: {run_dir}" if run_dir else "✖ Failed")))
                    except Exception:
                        pass

                    self._reset_abort_state()
                    self.refresh_all_pages()
                    if not aborted:
                        messagebox.showerror("ATBP", str(msg))

                elif kind == "trho_run_ready":
                    run_dir = Path(item[1])
                    label = str(item[2])
                    existing_active = self._get_active_trho_dir()
                    is_first = existing_active is None or not Path(existing_active).exists()
                    if is_first:
                        self._set_active_trho_run(run_dir, refresh=False)
                        self.state.active_trho_run = run_dir
                        self.state.active_trho_label = label
                        self.task_log(f"[TRHO] active run set automatically: {label}")
                    else:
                        try:
                            if existing_active.resolve() != run_dir.resolve():
                                answer = messagebox.askyesno(
                                    "Active TRHO result",
                                    f"TRHO run saved as {run_dir.name}.\n\nDo you want to set it as the active TRHO result?"
                                )
                                if answer:
                                    self._set_active_trho_run(run_dir, refresh=False)
                                    self.state.active_trho_run = run_dir
                                    self.state.active_trho_label = label
                                    self.task_log(f"[TRHO] active run changed to: {label}")
                            else:
                                self._set_active_trho_run(run_dir, refresh=False)
                                self.state.active_trho_run = run_dir
                                self.state.active_trho_label = label
                        except Exception:
                            pass
                    self.refresh_all_pages()

                elif kind == "parsed":
                    # Non-blocking: store summary and update status (do not open modal dialogs here)
                    summary = item[1]
                    self.task_log("[TRHO] " + summary)
                    self.set_status("✔ TRHO parsed")

                
                elif kind == "parse_error":
                    err = item[1]
                    # Parsing failed (but TRHO may have finished). Keep GUI responsive and show a clear status.
                    self.set_status("TRHO finished (parsing failed)")
                    # Keep a short note in the log; details already in the log above.
                    self.task_log("[TRHO] parsing failed (see log / Execution Details).")
                    self.refresh_all_pages()
                elif kind == "done":
                    rc = int(item[1])
                    aborted = self._job_was_aborted("TRHO")
                    try:
                        run_dir = Path(str((getattr(self.state, "last_execution", {}) or {}).get("cwd") or ""))
                        if run_dir and run_dir.exists():
                            meta = self._read_trho_run_meta(run_dir)
                            meta["status"] = ("finished" if (rc == 0 and not aborted) else ("aborted" if aborted else "failed"))
                            _write_json_file(run_dir / "run.json", meta)
                    except Exception:
                        pass
                    self._job_running = False
                    self.set_task(active=False)
                    try:
                        p = self.pages.get("Compute")
                        if p is not None and hasattr(p, "_set_running"):
                            p._set_running(False)
                    except Exception:
                        pass

                    if rc == 0 and not aborted:
                        self.state.trho_done = True
                        self.set_status("TRHO finished ✓")
                        self.task_log("[TRHO] finished OK (rc=0)")
                        try:
                            p = self.pages.get("Compute")
                            if p is not None:
                                dur = None
                                if isinstance(getattr(self.state, "last_execution", None), dict):
                                    dur = self.state.last_execution.get("duration_s")
                                if isinstance(dur, (int, float)):
                                    if dur >= 60:
                                        mins = int(dur // 60)
                                        secs = dur - 60 * mins
                                        dur_txt = f"{mins} min {secs:.2f} s"
                                    else:
                                        dur_txt = f"{dur:.2f} s"
                                    if hasattr(p, "set_runtime_text"):
                                        p.set_runtime_text(f"Total TRHO calculation time: {dur_txt}")
                                    if hasattr(p, "set_completion_text"):
                                        p.set_completion_text("TRHO completed ✓")
                                else:
                                    if hasattr(p, "set_runtime_text"):
                                        p.set_runtime_text("Total TRHO calculation time: unavailable")
                                    if hasattr(p, "set_completion_text"):
                                        p.set_completion_text("TRHO completed ✓")
                        except Exception:
                            pass

                        # Optional: delete log after a clean TRHO run (user preference)
                        if getattr(self.state, "delete_log_on_trho_success", False):
                            try:
                                lp = _log_path(self.state)
                                if lp and lp.exists():
                                    lp.unlink()
                            except Exception:
                                pass
                    else:
                        self.set_status("TRHO aborted" if aborted else f"TRHO failed (rc={rc})")
                        self.task_log("[TRHO] aborted by user" if aborted else f"[TRHO] failed (rc={rc})")
                        try:
                            p = self.pages.get("Compute")
                            if p is not None:
                                dur = None
                                if isinstance(getattr(self.state, "last_execution", None), dict):
                                    dur = self.state.last_execution.get("duration_s")
                                if isinstance(dur, (int, float)):
                                    if dur >= 60:
                                        mins = int(dur // 60)
                                        secs = dur - 60 * mins
                                        dur_txt = f"{mins} min {secs:.2f} s"
                                    else:
                                        dur_txt = f"{dur:.2f} s"
                                    if hasattr(p, "set_runtime_text"):
                                        p.set_runtime_text(f"Total TRHO calculation time: {dur_txt}")
                                else:
                                    if hasattr(p, "set_runtime_text"):
                                        p.set_runtime_text("Total TRHO calculation time: unavailable")
                                if hasattr(p, "set_completion_text"):
                                    p.set_completion_text("TRHO aborted" if aborted else f"TRHO failed ✖ (rc={rc})")
                        except Exception:
                            pass

                    # Ensure pages update enable/disable rules immediately
                    self._reset_abort_state()
                    self.refresh_all_pages()

                elif kind == "error":
                    aborted = self._job_was_aborted("TRHO")
                    self._job_running = False
                    self.set_task(active=False)
                    try:
                        p = self.pages.get("Compute")
                        if p is not None and hasattr(p, "_set_running"):
                            p._set_running(False)
                    except Exception:
                        pass
                    self.set_status("TRHO aborted" if aborted else "TRHO error")
                    self.task_log("[TRHO] aborted by user" if aborted else f"[ERROR] {item[1]}")
                    try:
                        p = self.pages.get("Compute")
                        if p is not None:
                            dur = None
                            if isinstance(getattr(self.state, "last_execution", None), dict):
                                dur = self.state.last_execution.get("duration_s")
                            if isinstance(dur, (int, float)):
                                if dur >= 60:
                                    mins = int(dur // 60)
                                    secs = dur - 60 * mins
                                    dur_txt = f"{mins} min {secs:.2f} s"
                                else:
                                    dur_txt = f"{dur:.2f} s"
                                if hasattr(p, "set_runtime_text"):
                                    p.set_runtime_text(f"Total TRHO calculation time: {dur_txt}")
                            else:
                                if hasattr(p, "set_runtime_text"):
                                    p.set_runtime_text("Total TRHO calculation time: unavailable")
                            if hasattr(p, "set_completion_text"):
                                p.set_completion_text("TRHO aborted" if aborted else "TRHO failed ✖")
                    except Exception:
                        pass
                    self._reset_abort_state()
                    self.refresh_all_pages()

        except queue.Empty:
            pass

        self.after(100, self._poll_job_queue)

    def _help(self):
        messagebox.showinfo(
            "Help",
            "Workflow:\n"
            "1) Choose Workspace folder\n"
            "2) App auto-validates (fort.9 or *.f9 + write)\n"
            "3) Go to Compute and run TRHO\n\n"
            "fort.9 creation is automatic ONLY when TRHO starts (if needed).\n\n"
            "For first real macOS tests, use Help > System diagnostics to collect the runtime environment."
        )

    def _show_system_diagnostics(self):
        diag_txt = format_system_diagnostics(collect_system_diagnostics(self))
        win = tk.Toplevel(self)
        _ensure_floating_window(win)
        win.title("System diagnostics")
        win.geometry("760x520")
        win.minsize(620, 380)

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        txt = tk.Text(frm, wrap="none")
        txt.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(frm, orient="horizontal", command=txt.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        txt.insert("1.0", diag_txt)
        txt.configure(state="disabled")

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def _copy():
            try:
                self.clipboard_clear()
                self.clipboard_append(diag_txt)
                self.set_status("System diagnostics copied to clipboard ✓")
            except Exception:
                pass

        def _save():
            fp = filedialog.asksaveasfilename(
                parent=win,
                title="Save diagnostics",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialfile="topiso3d_diagnostics.txt",
            )
            if fp:
                Path(fp).write_text(diag_txt, encoding="utf-8")

        ttk.Button(btns, text="Copy", command=_copy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Save…", command=_save).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="left")

    
    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=m_file)

        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="Settings…", command=self.open_settings)
        menubar.add_cascade(label="Tools", menu=m_tools)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="System diagnostics…", command=self._show_system_diagnostics)
        m_help.add_separator()
        m_help.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=m_help)

        self.config(menu=menubar)

    def open_settings(self) -> None:
        SettingsDialog(self)

    def _about(self):
        messagebox.showinfo("About", "TopIso3D v2026 revision Naiara v6: configurable external TRHO/TLAP/ATBP output names.")

    def _cleanup_run_temp_files(self, run_dir: Path, tag: str = "RUN") -> None:
        """Best-effort cleanup of temporary CRYSTAL/Properties files copied into a run folder.

        Policy is user-configurable in Settings:
          - minimal: remove fort.9 only
          - standard: remove fort.9, fort.3, fort.11, fort.13
          - none: keep all files
        """
        try:
            run_dir = Path(run_dir)
        except Exception:
            return

        policy = str(getattr(self.state, "cleanup_policy", "minimal") or "minimal").strip().lower()
        if policy == "none":
            try:
                self._job_queue.put(("log", f"[{tag}] Cleanup policy = none; keeping all temporary files."))
            except Exception:
                pass
            return

        to_remove = ["fort.9"]
        if policy == "standard":
            to_remove.extend(["fort.3", "fort.11", "fort.13"])

        removed = []
        failed = []
        for name in to_remove:
            p = run_dir / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
                    removed.append(name)
            except Exception as e:
                failed.append(f"{name}: {e}")

        try:
            if removed:
                self._job_queue.put(("log", f"[{tag}] Temporary files removed from run folder: {', '.join(removed)}."))
            else:
                self._job_queue.put(("log", f"[{tag}] No temporary files needed cleanup under policy '{policy}'."))
            for msg in failed:
                self._job_queue.put(("log", f"[{tag}] Could not remove temporary file {msg}"))
        except Exception:
            pass

    def prepare_trho_folder(self) -> Path:
        """Create a new TRHO run folder under trho_runs/ and ensure fort.9 is inside it."""
        workdir = self.state.workspace_dir
        if not workdir:
            raise RuntimeError("No workspace_dir")

        requested_name = str(getattr(self.state, "pending_trho_run_name", "") or "").strip()
        trho_dir = self._unique_trho_run_dir_for_name(requested_name) if requested_name else self._next_trho_run_dir()
        trho_dir.mkdir(parents=True, exist_ok=True)

        src_fort9 = workdir / "fort.9"
        if not src_fort9.exists():
            raise FileNotFoundError("fort.9 not found in workspace (should have been created).")

        dst_fort9 = trho_dir / "fort.9"
        if not dst_fort9.exists():
            shutil.copy2(src_fort9, dst_fort9)

        return trho_dir

    def build_trho_input_text(self) -> str:
        """Return the TRHO input generated from the current GUI state."""
        cfg = None
        try:
            page = self.pages.get("Compute")
            if page is not None and hasattr(page, "collect_trho_config"):
                cfg = page.collect_trho_config()
        except Exception:
            cfg = None

        if not isinstance(cfg, dict):
            mode = str(getattr(self.state, "trho_mode", "relaxed") or "relaxed").strip().lower()
            if mode == "sensitive":
                cfg = {
                    "ui_mode": "simple",
                    "preset": "sensitive",
                    "IAUTO": "-1",
                    "IEXT": "1",
                    "ICRIT": "0",
                    "IBPAT": "1",
                    "IPRINT": "0",
                    "NSTEP": "30",
                    "NNB": "15",
                    "RMAX": "12.0",
                    "TH": "6.0",
                }
            else:
                cfg = {
                    "ui_mode": "simple",
                    "preset": "relaxed",
                    "IAUTO": "-1",
                    "IEXT": "1",
                    "ICRIT": "0",
                    "IBPAT": "1",
                    "IPRINT": "0",
                    "NSTEP": "30",
                    "NNB": "10",
                    "RMAX": "10.",
                    "TH": "5.",
                }
        return self._build_trho_input_from_cfg(cfg)

    def _build_trho_input_from_cfg(self, cfg: dict) -> str:
        """Build a TRHO input block from a normalized configuration dictionary."""
        def _s(key: str, default: str = "") -> str:
            val = cfg.get(key, default)
            return str(val if val is not None else default).strip()

        iauto = _s("IAUTO", "-1")
        lines = ["TOPO", "TRHO", iauto]

        if iauto in ("-1", "-2"):
            lines.append(
                ",".join([
                    _s("IEXT", "1"),
                    _s("ICRIT", "0"),
                    _s("IBPAT", "1"),
                    _s("IPRINT", "0"),
                    _s("NSTEP", "30"),
                    _s("NNB", "10"),
                    _s("RMAX", "10.0"),
                    _s("TH", "5.0"),
                ])
            )
            if iauto == "-2":
                lines.append(_s("IFRA", "0"))
                lines.append(",".join([
                    _s("X", "0.0"),
                    _s("Y", "0.0"),
                    _s("Z", "0.0"),
                ]))
        elif iauto == "__removed_3__":
            imeth = _s("IMETH", "1")
            lines.append(
                ",".join([
                    imeth,
                    _s("IEXT", "0"),
                    _s("IBPAT", "1"),
                    _s("IPRINT", "0"),
                    _s("NSTEP", "5"),
                    _s("NNB", "7"),
                    _s("RMAX", "5.0"),
                ])
            )
            # For IAUTO = 3, TOPOND expects an extra TH line only when IMETH = 1.
            # With IMETH = 0, the grid definition follows immediately after the main line.
            if imeth == "1":
                lines.append(_s("TH", "0.0"))
            lines.append(",".join([_s("XMI", "0.0"), _s("XMA", "5.0"), _s("XINC", "0.5")]))
            lines.append(",".join([_s("YMI", "0.0"), _s("YMA", "5.0"), _s("YINC", "0.5")]))
            lines.append(",".join([_s("ZMI", "0.0"), _s("ZMA", "5.0"), _s("ZINC", "0.5")]))
            ncons = _s("NCONS", "0") or "0"
            lines.append(ncons)
            constraints = cfg.get("CONSTRAINTS", []) or []
            try:
                max_cons = max(0, int(float(ncons)))
            except Exception:
                max_cons = 0
            for item in constraints[:max_cons]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    lines.append(f"{item[0]},{item[1]}")
                else:
                    lines.append(str(item).strip())
        else:
            raise ValueError(f"Unsupported IAUTO in TopIso3D: {iauto}")

        lines.append("END")
        return "\n".join(lines) + "\n"

    def write_trho_input(self, trho_dir: Path) -> Path:
        """Write trho.inp according to the current TRHO GUI configuration."""
        inp = trho_dir / "trho.inp"
        template = self.build_trho_input_text()
        with open(inp, "w", encoding="utf-8") as f:
            f.write(template)
        return inp

    def prepare_tlap_folder(self) -> Path:
        """Create a new TLAP run folder under tlap_runs/ and ensure fort.9 is inside it."""
        workdir = self.state.workspace_dir
        if not workdir:
            raise RuntimeError("No workspace_dir")

        requested_name = str(getattr(self.state, "pending_tlap_run_name", "") or "").strip()
        if requested_name:
            tlap_dir = self._unique_tlap_run_dir_for_name(requested_name)
        else:
            root = _tlap_runs_dir(self.state)
            if root is None:
                raise RuntimeError("No workspace_dir")
            root.mkdir(parents=True, exist_ok=True)
            used = set()
            for p in root.iterdir() if root.exists() else []:
                if p.is_dir():
                    m = re.match(r"tlap_(\d+)$", p.name)
                    if m:
                        used.add(int(m.group(1)))
            n = 1
            while n in used:
                n += 1
            tlap_dir = root / f"tlap_{n:03d}"
        tlap_dir.mkdir(parents=True, exist_ok=True)

        src_fort9 = workdir / "fort.9"
        if not src_fort9.exists():
            raise FileNotFoundError("fort.9 not found in workspace (should have been created).")

        dst_fort9 = tlap_dir / "fort.9"
        if not dst_fort9.exists():
            shutil.copy2(src_fort9, dst_fort9)

        return tlap_dir

    def build_tlap_input_text(self) -> str:
        """Return the TLAP input generated from the current GUI state."""
        cfg = None
        try:
            page = self.pages.get("TLAP")
            if page is not None and hasattr(page, "collect_tlap_config"):
                cfg = page.collect_tlap_config()
        except Exception:
            cfg = None

        if not isinstance(cfg, dict):
            preset = str(getattr(self.state, "tlap_simple_preset", "relaxed") or "relaxed").strip().lower()
            cfg = {
                "ui_mode": "simple",
                "preset": preset,
                "IAUTO": "0",
                "IMETH": "1",
                "IEXT": "0",
                "IBPAT": "0",
                "IPRINT": "0",
                "NSTEP": "20" if preset != "sensitive" else "30",
                "NNB": "7" if preset != "sensitive" else "10",
                "RMAX": "5.0" if preset != "sensitive" else "7.0",
                "ITYPE": "0",
                "NT": "12",
                "NP": "18",
                "NNA": "0",
                "VSCC": True,
                "NMAX": "3" if preset != "sensitive" else "5",
                "RSTAR": "0.0",
                "RSTAR_OVERRIDES": {},
            }
        return self._build_tlap_input_from_cfg(cfg)

    def _build_tlap_input_from_cfg(self, cfg: dict) -> str:
        """Build a TLAP input block from a normalized configuration dictionary.

        Stage 1 implements the basic IAUTO = 0 workflow only. The search is expanded
        over the list of TRUE atoms (NEAs) recovered from TRHO. When VSCC is enabled,
        all recovered NEAs are activated using either a global GUI RSTAR override or
        element-specific defaults shared with the ATBP TOL table.
        """
        def _s(key: str, default: str = "") -> str:
            val = cfg.get(key, default)
            return str(val if val is not None else default).strip()

        def _as_bool(val) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return bool(val)
            s = str(val or "").strip().lower()
            return s in ("1", "true", "yes", "y", "on")

        iauto = _s("IAUTO", "0")
        if iauto != "0":
            raise ValueError(f"TLAP IAUTO = {iauto} is not implemented yet in TopIso3D.")

        parsed = getattr(self.state, "trho_parsed", None)
        df_true = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
        if df_true is None or getattr(df_true, "empty", True):
            raise ValueError("TLAP IAUTO = 0 requires TRUE atoms from a valid TRHO result.")

        nea_rows = [row for _, row in df_true.iterrows()]
        nea_count = int(len(nea_rows))
        use_vscc = _as_bool(cfg.get("VSCC", False))

        overrides = {str(k).strip().capitalize(): float(v) for k, v in (cfg.get("RSTAR_OVERRIDES", {}) or {}).items()}
        try:
            gui_rstar = float(str(cfg.get("RSTAR", "0.0") or "0.0").replace(",", "."))
        except Exception:
            gui_rstar = 0.0

        def _rstar_for_symbol(sym: str) -> float:
            s = str(sym or "").strip().capitalize()
            if gui_rstar > 0:
                return float(gui_rstar)
            if s in overrides:
                return float(overrides[s])
            val = _atbp_default_tol_bohr(s)
            if val is None:
                raise ValueError(
                    f"TLAP IAUTO = 0 requires an RSTAR value for element '{s}'. "
                    f"This element is not present in the default TOPOND TOL/RSTAR table."
                )
            return float(val)

        lines = [
            "TOPO",
            "TLAP",
            "0",
            ",".join([
                _s("IMETH", "1"),
                _s("IEXT", "0"),
                _s("IBPAT", "0"),
                _s("IPRINT", "0"),
                _s("NSTEP", "20"),
                _s("NNB", "7"),
                _s("RMAX", "5.0"),
            ]),
        ]

        if _s("IMETH", "1") == "1":
            lines.append(_s("ITYPE", "0"))

        lines.append(",".join([_s("NT", "12"), _s("NP", "18")]))
        lines.append(_s("NNA", "0"))

        nmax = _s("NMAX", "3")
        active_shells = 0
        element_sequence = []
        for row in nea_rows:
            sym = _atbp_symbol_from_row(row)
            sym_key = str(sym or "").strip().capitalize()
            element_sequence.append(sym_key)
            lines.append("1" if use_vscc else "0")
            if use_vscc:
                rstar_val = _rstar_for_symbol(sym_key)
                rstar_txt = f"{float(rstar_val):.3f}".rstrip("0").rstrip(".")
                if "." not in rstar_txt:
                    rstar_txt += ".0"
                lines.append(f"{nmax},{rstar_txt}")
                active_shells += 1

        lines.append("END")

        try:
            self.state.tlap_last_summary = {
                "iauto": iauto,
                "nea_count": nea_count,
                "vscc": bool(use_vscc),
                "active_shells": int(active_shells),
                "elements": element_sequence,
                "nmax": nmax,
                "gui_rstar": gui_rstar,
                "overrides": dict(overrides),
            }
        except Exception:
            pass

        return "\n".join(lines) + "\n"

    def write_tlap_input(self, tlap_dir: Path) -> Path:
        """Write tlap.inp according to the current TLAP GUI configuration."""
        inp = tlap_dir / "tlap.inp"
        cfg = getattr(self, "_tlap_last_cfg", None)
        if isinstance(cfg, dict):
            template = self._build_tlap_input_from_cfg(cfg)
        else:
            template = self.build_tlap_input_text()
        with open(inp, "w", encoding="utf-8") as f:
            f.write(template)
        return inp

    def prepare_atbp_folder(self) -> Path:
        """Create a new ATBP run folder under atbp_runs/ and ensure fort.9 is inside it."""
        workdir = self.state.workspace_dir
        if not workdir:
            raise RuntimeError("No workspace_dir")

        atbp_dir = self._next_atbp_run_dir()
        atbp_dir.mkdir(parents=True, exist_ok=True)

        src_fort9 = workdir / "fort.9"
        if not src_fort9.exists():
            raise FileNotFoundError("fort.9 not found in workspace (should have been created).")

        dst_fort9 = atbp_dir / "fort.9"
        if not dst_fort9.exists():
            shutil.copy2(src_fort9, dst_fort9)

        return atbp_dir

    def write_atbp_input(self, atbp_dir: Path, snippet: str) -> Path:
        """Write atbp.inp (overwrites existing, because it is generated by the GUI)."""
        inp = atbp_dir / "atbp.inp"
        with open(inp, "w", encoding="utf-8") as f:
            s = (snippet or "").strip()
            if s and not s.endswith("\n"):
                s += "\n"
            f.write(s)
        return inp

    def _atbp_runs_dir(self) -> Optional[Path]:
        ws = getattr(self.state, "workspace_dir", None)
        return (ws / "atbp_runs") if ws else None

    def _list_atbp_run_dirs(self) -> List[Path]:
        root = self._atbp_runs_dir()
        if root is None or not root.exists():
            return []
        runs = []
        try:
            for p in sorted(root.iterdir()):
                if p.is_dir() and self._find_matching_output_in_dirs([p], self._configured_output_names("atbp")) is not None:
                    runs.append(p)
        except Exception:
            pass
        return runs

    def _read_atbp_run_meta(self, run_dir: Path) -> dict:
        meta = _read_json_file(run_dir / "run.json")
        if not meta:
            meta = {"kind": "ATBP", "run_name": run_dir.name, "status": "unknown"}
        return meta

    def _friendly_atbp_run_label(self, run_dir: Path) -> str:
        meta = self._read_atbp_run_meta(run_dir)
        run_name = str(meta.get("run_name") or "").strip()
        created = str(meta.get("created_at") or "").strip()
        short_time = created[11:16] if len(created) >= 16 else created
        status = str(meta.get("status") or "").strip()
        parts = []
        parts.append(run_name or run_dir.name)
        if short_time:
            parts.append(short_time)
        if status and status not in ("unknown",):
            parts.append(status)
        return " | ".join(parts)

    def _get_atbp_run_selector_data(self) -> Tuple[List[str], Dict[str, Path], str]:
        runs = list(self._list_atbp_run_dirs())
        active = self._get_active_atbp_dir()
        try:
            if active is not None:
                active_p = Path(active)
                if active_p.exists() and all(active_p.resolve() != rd.resolve() for rd in runs):
                    runs.insert(0, active_p)
        except Exception:
            pass

        raw_labels = []
        for rd in runs:
            try:
                raw_labels.append((rd, self._friendly_atbp_run_label(rd) or rd.name))
            except Exception:
                raw_labels.append((rd, rd.name))

        counts = {}
        for _, lbl in raw_labels:
            counts[lbl] = counts.get(lbl, 0) + 1

        values: List[str] = []
        options: Dict[str, Path] = {}
        active_label = "—"
        for rd, lbl in raw_labels:
            display = f"{lbl} [{rd.name}]" if counts.get(lbl, 0) > 1 else lbl
            values.append(display)
            options[display] = rd
            try:
                if active is not None and Path(active).resolve() == rd.resolve():
                    active_label = display
            except Exception:
                pass

        if values and active_label == "—":
            active_label = values[-1]
        return values, options, active_label

    def _get_active_atbp_dir(self) -> Optional[Path]:
        state = self._load_workspace_state()
        rel = str(state.get("active_atbp") or "").strip()
        ws = getattr(self.state, "workspace_dir", None)
        if ws and rel:
            p = (ws / rel).resolve()
            try:
                if p.exists() and self._find_matching_output_in_dirs([p], self._configured_output_names("atbp")) is not None:
                    return p
            except Exception:
                pass
        runs = self._list_atbp_run_dirs()
        return runs[-1] if runs else None

    def _find_active_atbp_out(self) -> Optional[Path]:
        run_dir = self._get_active_atbp_dir()
        if run_dir is None:
            return None
        return self._find_matching_output_in_dirs([run_dir], self._configured_output_names("atbp"))

    def _set_active_atbp_run(self, run_dir: Path) -> None:
        ws = getattr(self.state, "workspace_dir", None)
        if ws is None:
            return
        try:
            rel = str(run_dir.resolve().relative_to(ws.resolve()))
        except Exception:
            rel = str(run_dir)
        state = self._load_workspace_state()
        state["active_atbp"] = rel
        self._save_workspace_state(state)
        self.state.atbp_run_dir = run_dir
        self.state.atbp_out_path = self._find_matching_output_in_dirs([run_dir], self._configured_output_names("atbp"))

    def _next_atbp_run_dir(self) -> Path:
        root = self._atbp_runs_dir()
        if root is None:
            raise RuntimeError("No workspace_dir")
        root.mkdir(parents=True, exist_ok=True)
        used = set()
        for p in root.iterdir() if root.exists() else []:
            if p.is_dir():
                m = re.match(r"atbp_(\d+)$", p.name)
                if m:
                    used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return root / f"atbp_{n:03d}"

    def _build_atbp_run_metadata(self, run_dir: Path) -> dict:
        return {
            "kind": "ATBP",
            "run_name": run_dir.name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "finished",
        }

    def _extract_atbp_error_line(self, out_path: Path, pattern: str) -> str:
        try:
            import re as _re
            rgx = _re.compile(pattern, _re.IGNORECASE)
            for ln in out_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if rgx.search(ln):
                    return " ".join(str(ln).split())
        except Exception:
            pass
        return ""

    def _inspect_atbp_output(self, out_path: Path) -> Optional[dict]:
        """Inspect ATBP output for known failure/incomplete-result patterns.

        Returns None when the run looks valid enough, otherwise a dict with:
          - kind: gauss_quadrature | generic_error | no_charge_values
          - title: popup title
          - message: user-facing popup text
          - failure_reason: concise reason for run.json
        """
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {
                "kind": "generic_error",
                "title": "ATBP failed",
                "message": f"The ATBP output could not be inspected.\n\n{e}",
                "failure_reason": f"ATBP output could not be inspected: {e}",
            }

        up = text.upper()
        if "GAUSS QUADRATURE NOT AVAILABLE" in up:
            detail = self._extract_atbp_error_line(out_path, r"GAUSS\s+QUADRATURE\s+NOT\s+AVAILABLE")
            return {
                "kind": "gauss_quadrature",
                "title": "ATBP warning",
                "message": (
                    "For this system, ATBP in STD mode is not advisable because the calculation stopped with a "
                    "Gauss quadrature error.\n\n"
                    "No charge values were written to the output table.\n"
                    "Please try the calculation with another mode."
                ),
                "failure_reason": detail or "Gauss quadrature not available",
            }

        error_markers = [
            "ERROR ****",
            " FATAL ",
            "ABORT",
            " SEVERE ",
            " YIELD ",
        ]
        if any(tok in up for tok in error_markers):
            detail = ""
            for pat in [r"ERROR\s*\*+.*", r"FATAL.*", r"ABORT.*", r"YIELD.*"]:
                detail = self._extract_atbp_error_line(out_path, pat)
                if detail:
                    break
            return {
                "kind": "generic_error",
                "title": "ATBP failed",
                "message": (
                    "The ATBP calculation did not finish successfully.\n\n"
                    "Please inspect the ATBP output file for details."
                ),
                "failure_reason": detail or "ATBP output contains an error marker",
            }

        try:
            df = parse_atbp_output(out_path)
        except Exception:
            df = pd.DataFrame()
        if df is None or df.empty:
            return {
                "kind": "no_charge_values",
                "title": "ATBP warning",
                "message": (
                    "The ATBP calculation finished without producing charge values in the output table.\n\n"
                    "Please inspect the ATBP output file and consider testing another mode."
                ),
                "failure_reason": "ATBP finished without charge values in the output table",
            }

        return None

    def run_atbp(self, snippet: str) -> None:
        """Run ATBP via properties, creating a new workspace/atbp_runs/atbp_xxx folder."""
        if self._job_running:
            messagebox.showinfo("ATBP", "A job is already running.")
            return

        ctx = self.state
        if not ctx.workspace_ok or not ctx.workspace_dir:
            messagebox.showwarning("ATBP", "Workspace is not valid. Choose a folder with fort.9 or *.f9 and write permission.")
            return

        ok, msg = self.ensure_fort9_for_run()
        if not ok:
            messagebox.showerror("ATBP", msg)
            return
        self._job_queue.put(("log", f"[Workspace] {msg}"))
        self.auto_validate_workspace()

        self._job_running = True
        self.set_task(text="ATBP: running…", done=0, total=0, active=True)
        self.set_status("Running ATBP…")

        def worker():
            try:
                import subprocess

                atbp_dir = self.prepare_atbp_folder()
                inp_path = self.write_atbp_input(atbp_dir, snippet)
                out_path = atbp_dir / "atbp.out"
                try:
                    meta = self._build_atbp_run_metadata(atbp_dir)
                    meta["status"] = "running"
                    _write_json_file(atbp_dir / "run.json", meta)
                except Exception:
                    pass

                exe = self.state.properties_exe
                exe_path = str(_best_effort_make_executable(exe) or "")
                if not exe_path:
                    self._job_queue.put(("atbp_fail", f"properties executable not found: {exe}", str(atbp_dir)))
                    return

                self._job_queue.put(("log", f"[ATBP] cwd: {atbp_dir}"))
                self._job_queue.put(("log", f"[ATBP] exe: {exe_path}"))
                self._job_queue.put(("log", f"[ATBP] inp: {inp_path.name}"))
                self._job_queue.put(("log", f"[ATBP] out: {out_path.name}"))
                try:
                    first_lines = [ln.strip() for ln in str(snippet).splitlines()[:8] if str(ln).strip()]
                    self._job_queue.put(("log", "[ATBP] input preview: " + " | ".join(first_lines)))
                except Exception:
                    pass

                with open(inp_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
                    p = subprocess.Popen(
                        [str(exe_path)],
                        cwd=str(atbp_dir),
                        stdin=fin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        universal_newlines=True,
                        **_windows_subprocess_silent_kwargs(),
                    )
                    self._register_active_process(p, "ATBP")

                    for line in p.stdout:
                        self._job_queue.put(("log", line.rstrip("\n")))
                        fout.write(line)

                    p.wait()

                    rc = p.returncode
                    self._clear_active_process(p)
                    self._cleanup_run_temp_files(atbp_dir, "ATBP")
                    if rc == 0 and out_path.exists():
                        issue = self._inspect_atbp_output(out_path)
                        if issue is None:
                            self._job_queue.put(("atbp_done", str(out_path), str(atbp_dir)))
                        else:
                            self._job_queue.put(("atbp_issue", issue, str(out_path), str(atbp_dir)))
                    else:
                        self._job_queue.put(("atbp_fail", f"ATBP failed (rc={rc})", str(atbp_dir)))

            except Exception as e:
                self._clear_active_process()
                self._job_queue.put(("atbp_fail", str(e), ""))

        self._job_thread = threading.Thread(target=worker, daemon=True)
        self._job_thread.start()





# -----------------------------
# ATBP (Bader/QTAIM charges) utilities
# -----------------------------

# Minimal periodic table (1..118). Used only if we need Z to compute q = Z - N(Ω).
_PERIODIC_TABLE = [
    None,
    "H","He",
    "Li","Be","B","C","N","O","F","Ne",
    "Na","Mg","Al","Si","P","S","Cl","Ar",
    "K","Ca","Sc","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn",
    "Ga","Ge","As","Se","Br","Kr",
    "Rb","Sr","Y","Zr","Nb","Mo","Tc","Ru","Rh","Pd","Ag","Cd",
    "In","Sn","Sb","Te","I","Xe",
    "Cs","Ba","La","Ce","Pr","Nd","Pm","Sm","Eu","Gd","Tb","Dy","Ho","Er","Tm","Yb","Lu",
    "Hf","Ta","W","Re","Os","Ir","Pt","Au","Hg",
    "Tl","Pb","Bi","Po","At","Rn",
    "Fr","Ra","Ac","Th","Pa","U","Np","Pu","Am","Cm","Bk","Cf","Es","Fm","Md","No","Lr",
    "Rf","Db","Sg","Bh","Hs","Mt","Ds","Rg","Cn",
    "Nh","Fl","Mc","Lv","Ts","Og"
]
_Z_BY_SYMBOL = {sym: i for i, sym in enumerate(_PERIODIC_TABLE) if sym}

# Lightweight atomic radii (Å) for CP Viewer marker sizing.
# Values are approximate covalent radii and are only used to improve visual
# distinction between elements in the first 3D viewer.
_COVALENT_RADII_ANG: Dict[str, float] = {
    "H": 0.31, "He": 0.28,
    "Li": 1.28, "Be": 0.96, "B": 0.84, "C": 0.76, "N": 0.71, "O": 0.66, "F": 0.57, "Ne": 0.58,
    "Na": 1.66, "Mg": 1.41, "Al": 1.21, "Si": 1.11, "P": 1.07, "S": 1.05, "Cl": 1.02, "Ar": 1.06,
    "K": 2.03, "Ca": 1.76, "Sc": 1.70, "Ti": 1.60, "V": 1.53, "Cr": 1.39, "Mn": 1.39, "Fe": 1.32,
    "Co": 1.26, "Ni": 1.24, "Cu": 1.32, "Zn": 1.22, "Ga": 1.22, "Ge": 1.20, "As": 1.19, "Se": 1.20,
    "Br": 1.20, "Kr": 1.16, "Rb": 2.20, "Sr": 1.95, "Y": 1.90, "Zr": 1.75, "Nb": 1.64, "Mo": 1.54,
    "Tc": 1.47, "Ru": 1.46, "Rh": 1.42, "Pd": 1.39, "Ag": 1.45, "Cd": 1.44, "In": 1.42, "Sn": 1.39,
    "Sb": 1.39, "Te": 1.38, "I": 1.39, "Xe": 1.40, "Cs": 2.44, "Ba": 2.15, "La": 2.07, "Ta": 1.70,
}

def _cpviewer_atom_radius_ang(symbol: str) -> float:
    sym = str(symbol or "").strip().capitalize()
    if sym in _COVALENT_RADII_ANG:
        return float(_COVALENT_RADII_ANG[sym])
    return 1.10

def _cpviewer_marker_size(symbol: str) -> float:
    """Map approximate atomic radius (Å) to a reasonable Plotly marker size."""
    r = _cpviewer_atom_radius_ang(symbol)
    size = 6.0 + 5.0 * (r - 0.60)
    return float(max(6.0, min(15.0, size)))

_ATBP_DEFAULT_TOL_BOHR: Dict[str, float] = {
    # TOPOND manual defaults (ATBP, Table 4.1)
    "H": 1.139, "He": 0.64,
    "Li": 2.494, "Be":1.594, "B": 1.188, "C": 0.942, "N": 0.776, "O": 0.658, "F": 0.569,
    "Ne": 0.500, "Na": 3.436, "Mg": 2.549, "Al": 2.081, "Si": 1.760, "P": 1.522, "S": 1.341, "Cl": 1.198, "Ar": 1.080,
    "K": 4.938, "Ca": 3.773, "Sc": 0.834, "Ti": 0.779, "V": 0.731, "Cr": 0.691, "Mn": 0.652, "Fe": 0.618, "Co": 0.587,
    "Ni": 0.559, "Cu": 0.535, "Zn": 0.510, "Ga": 0.487, "Ge": 0.466, "As": 2.175, "Se": 1.833, "Br": 1.652, "Kr": 1.503,
    "Rb": 5.516, "Sr": 4.369, "Y": 1.204, "Zr": 1.140, "Nb": 1.082, "Mo": 1.031, "Tc": 0.984, "Ru": 0.940, "Rh": 0.901,
    "Pd": 0.865, "Ag": 0.830, "Cd": 0.802, "In": 0.770, "Sn": 0.740, "Sb": 0.713, "Te": 0.688, "I": 2.228, "Xe": 2.000,
}

_ATBP_MODE_PRESETS: Dict[str, Dict[str, object]] = {
    "UNI Balanced": {"keyword": "UNI", "NVI": 6, "IPHI": 48, "ITH": 32, "IBETP": 96, "IMUL": 1, "IEXT": 0, "NOSE": 0, "ACC": "0.005"},
    "UNI Fast": {"keyword": "UNI", "NVI": 6, "IPHI": 32, "ITH": 24, "IBETP": 64, "IMUL": 0, "IEXT": 0, "NOSE": 0, "ACC": "0.01"},
    "STD": {"keyword": "STD"},
}


_ATBP_MODE_ALIASES: Dict[str, str] = {
    "uni balanced": "UNI Balanced",
    "balanced": "UNI Balanced",
    "uni_balanced": "UNI Balanced",
    "uni-balanced": "UNI Balanced",
    "unibalanced": "UNI Balanced",
    "uni fast": "UNI Fast",
    "fast": "UNI Fast",
    "uni_fast": "UNI Fast",
    "uni-fast": "UNI Fast",
    "unifast": "UNI Fast",
    "std": "STD",
}


def normalize_atbp_mode(mode: str) -> str:
    raw = (mode or "STD").strip()
    if raw in _ATBP_MODE_PRESETS:
        return raw
    key = re.sub(r"\s+", " ", raw).strip().lower()
    norm = _ATBP_MODE_ALIASES.get(key)
    if norm:
        return norm
    if "balanced" in key:
        return "UNI Balanced"
    if "fast" in key:
        return "UNI Fast"
    if "std" in key:
        return "STD"
    return "STD"


def _atbp_symbol_from_row(row: pd.Series) -> str:
    def _from_any(value) -> str:
        if value is None or pd.isna(value):
            return ""
        s = str(value).strip()
        if not s:
            return ""
        if s.isalpha() and 1 <= len(s) <= 3:
            return s.capitalize()
        try:
            z = int(float(s))
            if 0 < z < len(_PERIODIC_TABLE):
                sym = _PERIODIC_TABLE[z]
                if sym:
                    return str(sym)
        except Exception:
            pass
        return ""

    for key in ("SYMBOL", "symbol", "ELEM", "elem", "ELEMENT_SYMBOL", "AtomSymbol", "ATOM_SYMBOL", "EL", "el", "ELEMENT", "Z", "ATOMIC_NUMBER", "N.AT", "NAT", "atomic_number", "element", "ATOM", "atom"):
        if key in row:
            sym = _from_any(row.get(key))
            if sym:
                return sym
    try:
        for value in row.values.tolist():
            sym = _from_any(value)
            if sym:
                return sym
    except Exception:
        pass
    return ""


def _atbp_default_tol_bohr(symbol: str) -> Optional[float]:
    sym = (symbol or "").strip().capitalize()
    if not sym:
        return None
    val = _ATBP_DEFAULT_TOL_BOHR.get(sym)
    return float(val) if val is not None else None


def _atbp_missing_tol_symbols(true_atoms_df: Optional[pd.DataFrame]) -> List[str]:
    if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
        return []
    missing: List[str] = []
    seen = set()
    for _, row in true_atoms_df.iterrows():
        sym = _atbp_symbol_from_row(row)
        if not sym:
            continue
        key = sym.strip().capitalize()
        if key in seen:
            continue
        seen.add(key)
        if _atbp_default_tol_bohr(key) is None:
            missing.append(key)
    return missing


def build_atbp_input(mode: str = "STD", *, include_topo_wrapper: bool = True,
                     include_nna_section: bool = False, true_atoms_df: Optional[pd.DataFrame] = None,
                     tol_overrides: Optional[Dict[str, float]] = None) -> str:
    """Build an ATBP input snippet for STD / UNI Fast / UNI Balanced.

    For UNI modes, one block is generated per TRUE atom (NEA) extracted from TRHO.
    The β-sphere radius (TOL) is auto-filled from TOPOND manual defaults (Table 4.1).
    If an element is not present in the default table, caller must provide tol_overrides.
    """
    mode = normalize_atbp_mode(mode)
    preset = _ATBP_MODE_PRESETS.get(mode)
    if preset is None:
        raise ValueError(f"Unsupported ATBP mode: {mode}")

    lines: List[str] = []
    if include_topo_wrapper:
        lines.append("TOPO")
    lines.append("ATBP")

    keyword = str(preset.get("keyword", "STD"))
    if keyword == "STD":
        lines.append("STD")
        lines.append("0")
        lines.append("END")
        return "\n".join(lines) + "\n"

    if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
        raise ValueError("UNI modes require TRUE atoms / NEA. Run or parse TRHO first.")

    lines.append("UNI")
    nvi = int(preset["NVI"])
    iphi = int(preset["IPHI"])
    ith = int(preset["ITH"])
    ibetp = int(preset["IBETP"])
    imul = int(preset["IMUL"])
    iext = int(preset["IEXT"])
    nose = int(preset["NOSE"])
    acc = str(preset["ACC"])

    overrides = {str(k).strip().capitalize(): float(v) for k, v in (tol_overrides or {}).items()}

    for _, row in true_atoms_df.iterrows():
        sym = _atbp_symbol_from_row(row)
        sym_key = (sym or "").strip().capitalize()
        tol = overrides.get(sym_key)
        if tol is None:
            tol = _atbp_default_tol_bohr(sym_key)
        if tol is None:
            raise ValueError(
                f"ATBP UNI mode requires a TOL value for element '{sym_key}'. "
                f"This element is not present in the default TOPOND TOL table."
            )
        tol_txt = f"{tol:.3f}".rstrip("0").rstrip(".")
        lines.append(f"1,{tol_txt}")
        lines.append(f"{nvi},0,0,0")
        lines.append(f"{iphi},{ith},{ibetp},{imul},{iext},{nose},{acc}")

    lines.append("0")
    lines.append("END")
    return "\n".join(lines) + "\n"


def _try_parse_table_atom_charge(text: str) -> List[Dict]:
    """Heuristic: parse a compact table with ATOM ... CHARGE headers."""
    rows = []
    lines = text.splitlines()
    header_idx = None
    header_re = re.compile(r'^\s*ATOM\b.*\bCHARGE\b', re.IGNORECASE)
    for i, ln in enumerate(lines):
        if header_re.search(ln):
            header_idx = i
            break
    if header_idx is None:
        return rows

    # Consume until blank line or non-numeric stretch
    data_re = re.compile(
        r'^\s*(?P<idx>\d+)\s+(?P<sym>[A-Za-z]{1,3})\b(?P<rest>.*)$'
    )
    float_re = re.compile(r'[-+]?\d*\.\d+(?:[Ee][-+]?\d+)?|[-+]?\d+(?:[Ee][-+]?\d+)?')

    for ln in lines[header_idx+1:]:
        if not ln.strip():
            break
        m = data_re.match(ln)
        if not m:
            # stop if table ended
            if len(rows) > 0:
                break
            continue
        idx = int(m.group("idx"))
        sym = m.group("sym").capitalize()
        nums = [float(x) for x in float_re.findall(m.group("rest"))]
        if not nums:
            continue
        # Heuristic: last number is often the net charge
        charge = nums[-1]
        rows.append({
            "atom_index": idx,
            "symbol": sym,
            "n_omega": None,
            "q_topond": charge,
            "charge": (-charge if charge is not None else None),
            "volume": None,
            "source": "table"
        })
    return rows


def _parse_crystal_atom_table(text: str) -> List[Dict]:
    """Parse the CRYSTAL atom table (ATOM N.AT. ... ) and return list of atoms with coords in Å."""
    atoms: List[Dict] = []
    lines = text.splitlines()
    # Find header line containing "ATOM N.AT."
    start_idx = None
    for i, ln in enumerate(lines):
        if "ATOM N.AT." in ln:
            start_idx = i
            break
    if start_idx is None:
        return atoms
    # Data lines follow after a separator line of asterisks; stop at next separator/blank or non-matching
    data_re = re.compile(
        r'^\s*(\d+)\s+(\d+)\s+([A-Za-z]{1,3})\s+\d+\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s+'
    )
    for ln in lines[start_idx:start_idx+200]:
        m = data_re.match(ln)
        if not m:
            continue
        atom_index = int(m.group(1))
        z_raw = int(m.group(2))
        z = int(normalize_atomic_number(z_raw))
        sym = m.group(3).capitalize()
        if not sym or sym.upper().startswith("Z"):
            try:
                sym = _PERIODIC_TABLE[z]
            except Exception:
                pass
        x = float(m.group(4)); y = float(m.group(5)); zc = float(m.group(6))
        atoms.append({"atom_index": atom_index, "Z": z, "Z_RAW": z_raw, "symbol": sym, "xA": x, "yA": y, "zA": zc})
    return atoms


def _parse_unique_atom_pairs_considered(lines: List[str], bohr_to_ang: float = 0.5291772083) -> pd.DataFrame:
    """Parse the TRHO section 'UNIQUE ATOM PAIRS CONSIDERED' for CP Viewer atoms.

    This section is a compact topological environment around the non-equivalent atoms.
    It is richer than TRUE atoms alone, but much lighter than the full per-NEA clusters.
    We deduplicate atoms by the tuple (SYMBOL, OLD, CELL_X, CELL_Y, CELL_Z), keeping the
    first occurrence and recording the source NEA block where the atom first appeared.
    """
    if not lines:
        return pd.DataFrame()

    start = None
    for i, ln in enumerate(lines):
        if "TOPOLOGICAL ANALYSIS OF RHO : UNIQUE ATOM PAIRS CONSIDERED" in ln:
            start = i
            break
    if start is None:
        return pd.DataFrame()

    block_re = re.compile(r"NON\s+EQUIV\.\s+ATOM\s+(\d+)\s+([A-Za-z]{1,3})", re.IGNORECASE)
    row_re = re.compile(
        r"^\s*(\d+)\s+([A-Za-z]{1,3})\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+"
        r"([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s*$"
    )

    records: List[Dict[str, object]] = []
    src_nea_index = None
    src_nea_symbol = ""
    seen_header = False

    for ln in lines[start + 1:]:
        up = ln.upper()
        if "CRITICAL POINTS FOUND" in up:
            break

        m_blk = block_re.search(ln)
        if m_blk:
            src_nea_index = int(m_blk.group(1))
            src_nea_symbol = m_blk.group(2).capitalize()
            seen_header = False
            continue

        if "NEW" in up and "SYM" in up and "OLD" in up and "COORD." in up:
            seen_header = True
            continue

        if not seen_header or src_nea_index is None:
            continue

        m_row = row_re.match(ln)
        if not m_row:
            continue

        new_id = int(m_row.group(1))
        sym = m_row.group(2).capitalize()
        old_id = int(m_row.group(3))
        cell_x = int(m_row.group(4))
        cell_y = int(m_row.group(5))
        cell_z = int(m_row.group(6))
        x_bohr = float(m_row.group(7))
        y_bohr = float(m_row.group(8))
        z_bohr = float(m_row.group(9))
        dist_ang = float(m_row.group(10))

        records.append({
            "NEW": new_id,
            "SYMBOL": sym,
            "OLD": old_id,
            "CELL_X": cell_x,
            "CELL_Y": cell_y,
            "CELL_Z": cell_z,
            "x_BOHR": x_bohr,
            "y_BOHR": y_bohr,
            "z_BOHR": z_bohr,
            "X_ANGSTROM": x_bohr * bohr_to_ang,
            "Y_ANGSTROM": y_bohr * bohr_to_ang,
            "Z_ANGSTROM": z_bohr * bohr_to_ang,
            "DISTANCE_ANG": dist_ang,
            "SOURCE_NEA_INDEX": src_nea_index,
            "SOURCE_NEA_SYMBOL": src_nea_symbol,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["_dedup_key"] = list(zip(df["SYMBOL"], df["OLD"], df["CELL_X"], df["CELL_Y"], df["CELL_Z"]))
    df = df.drop_duplicates(subset=["_dedup_key"], keep="first").drop(columns=["_dedup_key"])
    df = df.sort_values(["SYMBOL", "OLD", "CELL_X", "CELL_Y", "CELL_Z", "NEW"]).reset_index(drop=True)
    df.index = np.arange(1, len(df) + 1)
    return df


def _parse_non_equiv_atom_clusters(lines: List[str], bohr_to_ang: float = 0.5291772083) -> pd.DataFrame:
    """Parse the expanded TRHO section 'CLUSTERS AROUND EACH OF THE NON-EQUIVALENT ATOMS'.

    This is a richer structural pool than TRUE atoms or the compact
    'UNIQUE ATOM PAIRS CONSIDERED' table. The raw pool can be large, so it is
    intended to be filtered later for CP Viewer purposes. Rows are deduplicated
    by (SYMBOL, OLD, CELL_X, CELL_Y, CELL_Z).
    """
    if not lines:
        return pd.DataFrame()

    start = None
    for i, ln in enumerate(lines):
        if 'CLUSTERS AROUND EACH OF THE NON-EQUIVALENT ATOMS' in ln:
            start = i
            break
    if start is None:
        return pd.DataFrame()

    block_re = re.compile(
        r"NON-EQUIV\.\s+ATOM\s+(\d+)\s+([A-Za-z]{1,3})\s*\(N\.\s*(\d+)\s+IN\s+THE\s+UNIT\s+CELL\)\,\s*CLUSTER\s+OF\s+(\d+)\s+ATOMS",
        re.IGNORECASE,
    )
    row_re = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+"
        r"([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
        r"([-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s*$"
    )

    periodic = {i: sym for i, sym in enumerate(_PERIODIC_TABLE) if i and sym}
    records = []
    src_nea_index = None
    src_nea_symbol = ''
    src_true_atom = None
    src_cluster_size = None
    seen_header = False

    for ln in lines[start + 1:]:
        up = ln.upper()
        if 'TOPOLOGICAL ANALYSIS OF RHO : UNIQUE ATOM PAIRS CONSIDERED' in up:
            break

        m_blk = block_re.search(ln)
        if m_blk:
            src_nea_index = int(m_blk.group(1))
            src_nea_symbol = m_blk.group(2).capitalize()
            src_true_atom = int(m_blk.group(3))
            src_cluster_size = int(m_blk.group(4))
            seen_header = False
            continue

        if 'NEW' in up and 'OLD' in up and 'AT. NU.' in up and 'COORD.(AU)' in up:
            seen_header = True
            continue

        if not seen_header or src_nea_index is None:
            continue

        m_row = row_re.match(ln)
        if not m_row:
            continue

        new_id = int(m_row.group(1))
        old_id = int(m_row.group(2))
        cell_x = int(m_row.group(3))
        cell_y = int(m_row.group(4))
        cell_z = int(m_row.group(5))
        z_raw = int(m_row.group(6))
        z_norm = int(normalize_atomic_number(z_raw))
        sym = str(periodic.get(z_norm, f'Z{z_norm}')).capitalize()
        x_bohr = float(m_row.group(7))
        y_bohr = float(m_row.group(8))
        z_bohr = float(m_row.group(9))
        dist_ang = float(m_row.group(10))

        records.append({
            'NEW': new_id,
            'SYMBOL': sym,
            'ELEMENT': z_norm,
            'ELEMENT_RAW': z_raw,
            'OLD': old_id,
            'CELL_X': cell_x,
            'CELL_Y': cell_y,
            'CELL_Z': cell_z,
            'x_BOHR': x_bohr,
            'y_BOHR': y_bohr,
            'z_BOHR': z_bohr,
            'X_ANGSTROM': x_bohr * bohr_to_ang,
            'Y_ANGSTROM': y_bohr * bohr_to_ang,
            'Z_ANGSTROM': z_bohr * bohr_to_ang,
            'DISTANCE_ANG': dist_ang,
            'SOURCE_NEA_INDEX': src_nea_index,
            'SOURCE_NEA_SYMBOL': src_nea_symbol,
            'SOURCE_TRUE_ATOM': src_true_atom,
            'SOURCE_CLUSTER_SIZE': src_cluster_size,
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df['_dedup_key'] = list(zip(df['SYMBOL'], df['OLD'], df['CELL_X'], df['CELL_Y'], df['CELL_Z']))
    df = df.drop_duplicates(subset=['_dedup_key'], keep='first').drop(columns=['_dedup_key'])
    df = df.sort_values(['DISTANCE_ANG', 'SYMBOL', 'OLD', 'CELL_X', 'CELL_Y', 'CELL_Z', 'NEW']).reset_index(drop=True)
    df.index = np.arange(1, len(df) + 1)
    return df


def _collect_cpviewer_cp_box(
    *,
    df_bcp_props: Optional[pd.DataFrame] = None,
    df_ring: Optional[pd.DataFrame] = None,
    df_cage: Optional[pd.DataFrame] = None,
    df_attr: Optional[pd.DataFrame] = None,
) -> Optional[tuple[float, float, float, float, float, float]]:
    """Return the CP bounding box in Å from all available TRHO critical points."""
    coords = []
    for df in (df_bcp_props, df_ring, df_cage, df_attr):
        if df is None or getattr(df, 'empty', True):
            continue
        cols = [c for c in ('X_ANGSTROM', 'Y_ANGSTROM', 'Z_ANGSTROM') if c in df.columns]
        if len(cols) != 3:
            continue
        sub = df[cols].apply(pd.to_numeric, errors='coerce').dropna()
        if not sub.empty:
            coords.append(sub)
    if not coords:
        return None
    allc = pd.concat(coords, ignore_index=True)
    return (
        float(allc['X_ANGSTROM'].min()), float(allc['X_ANGSTROM'].max()),
        float(allc['Y_ANGSTROM'].min()), float(allc['Y_ANGSTROM'].max()),
        float(allc['Z_ANGSTROM'].min()), float(allc['Z_ANGSTROM'].max()),
    )


def _select_cpviewer_atoms_bbox(
    df_atoms_pool: Optional[pd.DataFrame],
    *,
    df_bcp_props: Optional[pd.DataFrame] = None,
    df_ring: Optional[pd.DataFrame] = None,
    df_cage: Optional[pd.DataFrame] = None,
    df_attr: Optional[pd.DataFrame] = None,
    margin_ang: float = 2.5,
) -> pd.DataFrame:
    """Select CP Viewer atoms by filtering an expanded atom pool with CP bounding box + margin."""
    if df_atoms_pool is None or getattr(df_atoms_pool, 'empty', True):
        return pd.DataFrame()

    box = _collect_cpviewer_cp_box(
        df_bcp_props=df_bcp_props,
        df_ring=df_ring,
        df_cage=df_cage,
        df_attr=df_attr,
    )
    if box is None:
        return pd.DataFrame()

    xmin, xmax, ymin, ymax, zmin, zmax = box
    margin = float(margin_ang or 0.0)

    x = pd.to_numeric(df_atoms_pool.get('X_ANGSTROM', pd.Series(dtype=float)), errors='coerce')
    y = pd.to_numeric(df_atoms_pool.get('Y_ANGSTROM', pd.Series(dtype=float)), errors='coerce')
    z = pd.to_numeric(df_atoms_pool.get('Z_ANGSTROM', pd.Series(dtype=float)), errors='coerce')
    mask = (
        np.isfinite(x) & np.isfinite(y) & np.isfinite(z) &
        (x >= xmin - margin) & (x <= xmax + margin) &
        (y >= ymin - margin) & (y <= ymax + margin) &
        (z >= zmin - margin) & (z <= zmax + margin)
    )
    df = df_atoms_pool.loc[mask].copy()
    if df.empty:
        return df
    df['X_ANGSTROM'] = x.loc[mask].astype(float)
    df['Y_ANGSTROM'] = y.loc[mask].astype(float)
    df['Z_ANGSTROM'] = z.loc[mask].astype(float)
    df = df.sort_values(['DISTANCE_ANG', 'SYMBOL', 'OLD', 'CELL_X', 'CELL_Y', 'CELL_Z', 'NEW']).reset_index(drop=True)
    df.index = np.arange(1, len(df) + 1)
    return df



def _try_parse_atbp_populations_with_orig(text: str) -> List[Dict]:
    """Parse ATBP STD output blocks using ORIG.(AU) + ATOMIC POPULATIONS + VTOT.

    Robust line-based strategy (avoids greedy multi-line regex issues):
      - parse atom table to get (index, symbol, coords in Å)
      - for each ORIG.(AU) occurrence:
          * convert to Å and match nearest atom by position
          * find the next "ATOMIC POPULATIONS" header and read N/Q from the following lines
          * find "ATOMIC VOLUMES AND RELATED POPULATIONS" and read VTOT (or V001)
    """
    rows: List[Dict] = []
    atoms = _parse_crystal_atom_table(text)
    if not atoms:
        return rows

    bohr2ang = 0.52917721092

    def nearest_atom(xA: float, yA: float, zA: float):
        best = None
        best_d2 = 1e99
        for a in atoms:
            dx = a["xA"] - xA
            dy = a["yA"] - yA
            dz = a["zA"] - zA
            d2 = dx * dx + dy * dy + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best = a
        return best, best_d2**0.5

    orig_re = re.compile(
        r'ORIG\.\(AU\)\s*\(X\s+Y\s+Z\)\s*:\s*([-+0-9.Ee]+)\s+([-+0-9.Ee]+)\s+([-+0-9.Ee]+)'
    )
    num_re = re.compile(r'[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?')

    lines = text.splitlines()

    # Map character position to line index (cumulative)
    cum = []
    total = 0
    for ln in lines:
        total += len(ln) + 1
        cum.append(total)

    def charpos_to_lineidx(pos: int) -> int:
        # first index where cum[i] > pos
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] > pos:
                hi = mid
            else:
                lo = mid + 1
        return lo

    for m in orig_re.finditer(text):
        xB = float(m.group(1)); yB = float(m.group(2)); zB = float(m.group(3))
        xA = xB * bohr2ang; yA = yB * bohr2ang; zA = zB * bohr2ang
        a, dist = nearest_atom(xA, yA, zA)
        if a is None:
            continue

        li = charpos_to_lineidx(m.end())
        # Search forward for ATOMIC POPULATIONS
        n_omega = None
        charge = None
        volume = None

        j = li
        while j < len(lines) and j < li + 2500:
            if "ATOMIC POPULATIONS" in lines[j]:
                # next ~30 lines contain N and Q
                for k in range(j, min(j + 40, len(lines))):
                    s = lines[k].strip()
                    if s.startswith("N"):
                        mm = num_re.search(s)
                        if mm:
                            try:
                                n_omega = float(mm.group(0))
                            except Exception:
                                pass
                    if s.startswith("Q"):
                        mm = num_re.search(s)
                        if mm:
                            try:
                                charge = float(mm.group(0))
                            except Exception:
                                pass
                    if n_omega is not None and charge is not None:
                        break
            if "ATOMIC VOLUMES AND RELATED POPULATIONS" in lines[j]:
                for k in range(j, min(j + 80, len(lines))):
                    s = lines[k].strip()
                    if s.startswith("VTOT"):
                        mm = num_re.search(s)
                        if mm:
                            try:
                                volume = float(mm.group(0))
                            except Exception:
                                pass
                            break
                    if s.startswith("V001") and volume is None:
                        mm = num_re.search(s)
                        if mm:
                            try:
                                volume = float(mm.group(0))
                            except Exception:
                                pass
            # stop once we passed the basin integration footer for this block
            if "ATBP" in lines[j] and "TELAPSE" in lines[j] and n_omega is not None and charge is not None:
                # allow to continue a bit to catch VTOT, but we can stop early if already got it
                if volume is not None:
                    break
            j += 1

        if n_omega is None and charge is None and volume is None:
            continue

        rows.append({
            "atom_index": int(a["atom_index"]),
            "symbol": a["symbol"],
            "n_omega": n_omega,
            # TOPOND prints Q with its own sign convention; store it explicitly
            "q_topond": charge,
            # Chemical sign (cation positive)
            "charge": (-charge if charge is not None else None),
            "volume": volume,
            "source": "block",
        })

    return rows


def _try_parse_blocks_atomic_basin(text: str) -> List[Dict]:
    """Heuristic: parse per-atom 'ATOMIC BASIN' blocks and extract N(Ω)/charge/volume if present."""
    rows = []
    # Example patterns vary across TOPOND versions; keep broad.
    block_re = re.compile(r'ATOMIC\s+BASIN\s+OF\s+ATOM\s+(\d+)\s+([A-Za-z]{1,3})', re.IGNORECASE)
    # Capture candidate numeric values
    num_re = re.compile(r'[-+]?\d*\.\d+(?:[Ee][-+]?\d+)?|[-+]?\d+(?:[Ee][-+]?\d+)?')

    lines = text.splitlines()
    idxs = [(m.start(), int(m.group(1)), m.group(2).capitalize()) for m in block_re.finditer(text)]
    if not idxs:
        return rows

    # Convert char positions to line indices by scanning cumulative lengths
    # We'll instead walk line-by-line and detect starts.
    i = 0
    while i < len(lines):
        m = block_re.search(lines[i])
        if not m:
            i += 1
            continue
        atom_index = int(m.group(1))
        sym = m.group(2).capitalize()
        # scan next ~250 lines for properties
        scan = "\n".join(lines[i:i+250])
        n_omega = None
        charge = None
        volume = None

        # common keys
        # N(OMEGA) / ELECTRON POPULATION / POPULATION IN BASIN
        for pat in [
            r'N\s*\(\s*OMEGA\s*\)\s*[:=]\s*([\d\.Ee+-]+)',
            r'ELECTRON\s+POPULATION\s*[:=]\s*([\d\.Ee+-]+)',
            r'POPULATION\s+IN\s+BASIN\s*[:=]\s*([\d\.Ee+-]+)',
        ]:
            mm = re.search(pat, scan, re.IGNORECASE)
            if mm:
                try:
                    n_omega = float(mm.group(1))
                except Exception:
                    pass
                break

        # NET CHARGE / CHARGE
        for pat in [
            r'NET\s+CHARGE\s*[:=]\s*([\d\.Ee+-]+)',
            r'ATOMIC\s+CHARGE\s*[:=]\s*([\d\.Ee+-]+)',
            r'\bCHARGE\b\s*[:=]\s*([\d\.Ee+-]+)',
        ]:
            mm = re.search(pat, scan, re.IGNORECASE)
            if mm:
                try:
                    charge = float(mm.group(1))
                except Exception:
                    pass
                break

        # VOLUME
        for pat in [
            r'BASIN\s+VOLUME\s*[:=]\s*([\d\.Ee+-]+)',
            r'\bVOLUME\b\s*[:=]\s*([\d\.Ee+-]+)',
        ]:
            mm = re.search(pat, scan, re.IGNORECASE)
            if mm:
                try:
                    volume = float(mm.group(1))
                except Exception:
                    pass
                break

        # If charge missing but N(Ω) exists and we know Z: compute charge.
        if charge is None and n_omega is not None:
            Z = _Z_BY_SYMBOL.get(sym)
            if Z is not None:
                charge = float(Z) - float(n_omega)

        rows.append({
            "atom_index": atom_index,
            "symbol": sym,
            "n_omega": n_omega,
            # TOPOND prints Q with the electron-sign convention (often opposite of "chemical" charge).
            # We store both: q_topond (as printed) and charge = -q_topond (so cations are positive).
            "q_topond": charge,
            "charge": (-charge if charge is not None else None),
            "volume": volume,
            "source": "block"
        })
        i += 1
    return rows

def parse_atbp_output(out_path: Path) -> pd.DataFrame:
    """Parse ATBP output and return a DataFrame.

    This parser is intentionally heuristic (TOPOND output varies by version/settings).
    It tries: (1) compact atom/charge tables, (2) per-atom basin blocks.
    """
    text = out_path.read_text(encoding="utf-8", errors="replace")
    rows = _try_parse_table_atom_charge(text)
    if not rows:
        rows = _try_parse_atbp_populations_with_orig(text)
    if not rows:
        rows = _try_parse_blocks_atomic_basin(text)

    if not rows:
        return pd.DataFrame(columns=["atom_index", "symbol", "n_omega", "charge", "volume", "source"])

    df = pd.DataFrame(rows)
    # keep one row per atom (prefer block info if both exist)
    if df["atom_index"].duplicated().any():
        # rank sources: block > table
        src_rank = {"block": 0, "table": 1}
        df["_r"] = df["source"].map(lambda s: src_rank.get(s, 9))
        df = df.sort_values(["atom_index", "_r"]).drop_duplicates("atom_index", keep="first").drop(columns=["_r"])
    df = df.sort_values("atom_index").reset_index(drop=True)
    return df

def ensure_atbp_dir(workspace_dir: Path) -> Path:
    atbp_dir = workspace_dir / "atbp_runs"
    atbp_dir.mkdir(parents=True, exist_ok=True)
    return atbp_dir


def _compact_bcp_dist_headers(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a copy of a BCP-properties DataFrame with compact distance headers.

    Internal processing keeps the explicit names DIST_ELEM1_ANG and DIST_ELEM2_ANG,
    but for tables and exported reports we shorten both headers to DIST_(ANG),
    preserving their position right after ELEM1/ELEM2.
    """
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [
        "DIST_(ANG)" if c in ("DIST_ELEM1_ANG", "DIST_ELEM2_ANG") else c
        for c in out.columns
    ]
    return out

def _report_display_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a copy suitable for GUI/exports, keeping geometry only in Å.

    Internal parsers may keep AU/bohr columns for computations, but Reports should
    present a single unit system. This helper drops raw AU/bohr geometry columns
    while preserving the corresponding *_ANGSTROM fields.
    """
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    if out.empty:
        return out

    drop_cols = []
    for c in out.columns:
        up = str(c).upper()
        if up.endswith('_AU') or up.endswith('_BOHR'):
            drop_cols.append(c)
            continue
        if up in {'X_AU','Y_AU','Z_AU','X_BOHR','Y_BOHR','Z_BOHR','NEAREST_DIST_AU'}:
            drop_cols.append(c)
            continue
    if drop_cols:
        out = out.drop(columns=[c for c in drop_cols if c in out.columns], errors='ignore')
    return out


def _tlap_report_cp_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a TLAP CP table tailored for GUI/exports.

    The compact "nearest atom" fields are currently omitted from Reports because
    they are not robustly populated for all TLAP outputs yet. They can remain in
    the internal parser for future refinement without exposing empty columns.
    """
    out = _report_display_df(df)
    if out is None or out.empty:
        return pd.DataFrame()
    drop_cols = [
        "NEAREST_ATOM_SYMBOL",
        "NEAREST_ATOM_INDEX",
        "NEAREST_CELL_X",
        "NEAREST_CELL_Y",
        "NEAREST_CELL_Z",
        "NEAREST_DIST_ANG",
    ]
    out = out.drop(columns=[c for c in drop_cols if c in out.columns], errors="ignore")
    return out


def _tlap_run_info_df(parsed, ctx) -> pd.DataFrame:
    summary = getattr(parsed, "summary", {}) or {}
    row = {
        "TLAP_ACTIVE_RUN": getattr(ctx, "active_tlap_label", "—"),
        "SOURCE_TRHO_RUN": getattr(parsed, "source_trho_run", "") or getattr(ctx, "active_trho_label", "—"),
        "IAUTO": summary.get("iauto", "—"),
        "ALGORITHM": summary.get("algorithm", "—"),
        "CP_TYPE": summary.get("itype", "—"),
        "NT": summary.get("nt", "—"),
        "NP": summary.get("np", "—"),
        "NEAS_ANALYZED": summary.get("n_neas", 0),
        "NEAS_WITH_CPS": summary.get("n_neas_with_cps", 0),
        "TOTAL_CPS": summary.get("total_cps", 0),
    }
    return pd.DataFrame([row])


# -----------------------------
# Base Page
# -----------------------------
class BasePage(ttk.Frame):
    title = "Page"

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text=self.title, font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Separator(self).pack(fill="x", pady=(0, 12))

    def _make_scrollable_body(self) -> ttk.Frame:
        outer, canvas, vbar, inner = _make_scrollable_frame(self, canvas_bg=str(self.app.cget("bg") or "#999999"))
        outer.pack(fill="both", expand=True)
        self._scroll_outer = outer
        self._scroll_canvas = canvas
        self._scroll_vbar = vbar
        self._scroll_inner = inner
        return inner

    def on_show(self):
        pass

    def refresh_state(self):
        # Default: call self.refresh() if the page defines it.
        fn = getattr(self, 'refresh', None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


# -----------------------------
# CP Viewer Page
# -----------------------------
class CPViewerPage(BasePage):
    title = "CP Viewer"

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        self._wrap = 860

        ttk.Label(
            body,
            text="Interactive visualization of critical points and local atomic environment from the active TRHO run.",
            wraplength=self._wrap,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))

        frm_src = ttk.LabelFrame(body, text="Active data sources", padding=(12, 8))
        frm_src.grid(row=1, column=0, sticky="ew")
        frm_src.columnconfigure(1, weight=1)

        ttk.Label(frm_src, text="Active run:").grid(row=0, column=0, sticky="w")
        self.var_active_run = tk.StringVar(value="—")
        ttk.Label(frm_src, textvariable=self.var_active_run, wraplength=self._wrap - 120, justify="left").grid(row=0, column=1, sticky="ew")

        ttk.Label(frm_src, text="TRHO output:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_trho_out = tk.StringVar(value="—")
        ttk.Label(frm_src, textvariable=self.var_trho_out, wraplength=self._wrap - 120, justify="left").grid(row=1, column=1, sticky="ew", pady=(6, 0))

        ttk.Label(frm_src, text="Active TLAP run:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.var_active_tlap_run = tk.StringVar(value="—")
        ttk.Label(frm_src, textvariable=self.var_active_tlap_run, wraplength=self._wrap - 120, justify="left").grid(row=2, column=1, sticky="ew", pady=(6, 0))

        ttk.Label(frm_src, text="TLAP output:").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.var_tlap_out = tk.StringVar(value="—")
        ttk.Label(frm_src, textvariable=self.var_tlap_out, wraplength=self._wrap - 120, justify="left").grid(row=3, column=1, sticky="ew", pady=(6, 0))

        frm_sum = ttk.LabelFrame(body, text="Viewer summary", padding=(12, 8))
        frm_sum.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        frm_sum.columnconfigure(0, weight=1)

        self.var_counts = tk.StringVar(value="No parsed TRHO data available.")
        ttk.Label(frm_sum, textvariable=self.var_counts, wraplength=self._wrap, justify="left").grid(row=0, column=0, sticky="ew")

        frm_actions = ttk.LabelFrame(body, text="Actions", padding=(12, 6))
        frm_actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        frm_actions.columnconfigure(0, weight=1)
        frm_actions.columnconfigure(1, weight=0)

        self.lbl_status = ttk.Label(frm_actions, text="CP Viewer not ready", wraplength=self._wrap, justify="left")
        self.lbl_status.grid(row=0, column=0, sticky="ew")

        frm_left = ttk.Frame(frm_actions)
        frm_left.grid(row=1, column=0, sticky="nw", pady=(2, 0))
        frm_left.columnconfigure(0, weight=1)
        self.frm_actions_left = frm_left

        frm_right = ttk.Frame(frm_actions)
        frm_right.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(18, 0), pady=(0, 0))
        frm_right.columnconfigure(0, weight=1)
        self.frm_actions_right = frm_right

        self.btn_open_placeholder = ttk.Button(
            frm_right,
            text="Open CP Viewer",
            state="disabled",
            command=self._open_viewer,
        )
        self.btn_open_placeholder.grid(row=0, column=0, sticky="ew")

        self.var_cp_source = tk.StringVar(value="Rho")
        ttk.Label(frm_left, text="Critical points source:").grid(row=0, column=0, sticky="w")
        self.cmb_cp_source = ttk.Combobox(
            frm_left,
            textvariable=self.var_cp_source,
            state="readonly",
            width=16,
            values=["Rho", "Laplacian", "Both"],
        )
        self.cmb_cp_source.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cmb_cp_source.bind("<<ComboboxSelected>>", lambda _e: self._refresh_cp_source_ui())

        self.var_show_bondpaths = tk.BooleanVar(value=True)
        self.chk_show_bondpaths = ttk.Checkbutton(
            frm_left,
            text="Show bond paths associated with BCPs",
            variable=self.var_show_bondpaths,
        )
        self.chk_show_bondpaths.grid(row=2, column=0, sticky="w", pady=(6, 0))

        frm_tlap = ttk.LabelFrame(frm_left, text="Laplacian CP filters", padding=(10, 6))
        frm_tlap.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.frm_tlap_filters = frm_tlap
        self.var_show_tlap_attr = tk.BooleanVar(value=True)
        self.var_show_tlap_bcp = tk.BooleanVar(value=False)
        self.var_show_tlap_rcp = tk.BooleanVar(value=False)
        self.var_show_tlap_ccp = tk.BooleanVar(value=False)
        row_tlap1 = ttk.Frame(frm_tlap)
        row_tlap1.pack(fill="x")
        ttk.Checkbutton(row_tlap1, text="Show TLAP (3,-3)", variable=self.var_show_tlap_attr).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row_tlap1, text="Show TLAP (3,-1)", variable=self.var_show_tlap_bcp).pack(side="left", padx=(0, 12))
        row_tlap2 = ttk.Frame(frm_tlap)
        row_tlap2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row_tlap2, text="Show TLAP (3,+1)", variable=self.var_show_tlap_rcp).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row_tlap2, text="Show TLAP (3,+3)", variable=self.var_show_tlap_ccp).pack(side="left")

        self.var_save_html = tk.BooleanVar(value=False)
        self.var_save_note = tk.StringVar(value="")
        frm_save = ttk.Frame(frm_right)
        frm_save.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.frm_save_html = frm_save
        self.chk_save_html = ttk.Checkbutton(
            frm_save,
            text="Save Plotly project (HTML)",
            variable=self.var_save_html,
        )
        self.chk_save_html.grid(row=0, column=0, sticky="w")
        ttk.Label(
            frm_save,
            text="in active TRHO run folder",
            wraplength=210,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=(22, 0), pady=(0, 0))
        ttk.Label(
            frm_save,
            textvariable=self.var_save_note,
            justify="left",
            wraplength=210,
        ).grid(row=2, column=0, sticky="w", padx=(22, 0), pady=(0, 0))

        frm_render = ttk.LabelFrame(body, text="Rendering parameters", padding=(12, 8))
        frm_render.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        for col in (1, 3, 5):
            frm_render.columnconfigure(col, weight=1)

        self.var_marker_scale = tk.StringVar(value="1.00")
        ttk.Label(frm_render, text="Marker size scale:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm_render, textvariable=self.var_marker_scale, width=8).grid(row=0, column=1, sticky="w", padx=(6, 18))

        self.var_cell_color = tk.StringVar(value="Medium gray")
        ttk.Label(frm_render, text="Unit-cell color:").grid(row=0, column=2, sticky="w")
        self.cmb_cell_color = ttk.Combobox(
            frm_render,
            textvariable=self.var_cell_color,
            state="readonly",
            width=18,
            values=["Light gray", "Medium gray", "Dark gray"],
        )
        self.cmb_cell_color.grid(row=0, column=3, sticky="w", padx=(6, 18))

        self.var_cell_width = tk.StringVar(value="2.0")
        ttk.Label(frm_render, text="Unit-cell width:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(frm_render, textvariable=self.var_cell_width, width=8).grid(row=1, column=1, sticky="w", padx=(6, 18), pady=(10, 0))

        self.var_cell_opacity = tk.StringVar(value="0.80")
        ttk.Label(frm_render, text="Unit-cell opacity (0-1):").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(frm_render, textvariable=self.var_cell_opacity, width=8).grid(row=1, column=3, sticky="w", padx=(6, 18), pady=(10, 0))

        self._refresh_cp_source_ui()

    def _cp_source_requires_tlap(self) -> bool:
        src = str(getattr(self, "var_cp_source", tk.StringVar(value="Rho")).get() or "Rho").strip()
        return src in {"Laplacian", "Both"}

    def _refresh_cp_source_ui(self):
        src = str(getattr(self, "var_cp_source", tk.StringVar(value="Rho")).get() or "Rho").strip()
        need_tlap = src in {"Laplacian", "Both"}
        try:
            self.chk_show_bondpaths.configure(state=("normal" if src in {"Rho", "Both"} else "disabled"))
        except Exception:
            pass

        frm_tlap = getattr(self, "frm_tlap_filters", None)
        if frm_tlap is not None:
            try:
                if need_tlap:
                    frm_tlap.grid()
                else:
                    frm_tlap.grid_remove()
            except Exception:
                pass
            try:
                for child in frm_tlap.winfo_children():
                    for sub in ([child] + (list(child.winfo_children()) if hasattr(child, 'winfo_children') else [])):
                        try:
                            sub.configure(state=("normal" if need_tlap else "disabled"))
                        except Exception:
                            pass
            except Exception:
                pass

        if need_tlap:
            try:
                selected_map = self._selected_tlap_type_map()
                if not any(bool(v) for v in selected_map.values()):
                    self.var_show_tlap_attr.set(True)
            except Exception:
                pass

    def _selected_tlap_type_map(self):
        return {
            "(3,-3)": bool(getattr(self, "var_show_tlap_attr", tk.BooleanVar(value=True)).get()),
            "(3,-1)": bool(getattr(self, "var_show_tlap_bcp", tk.BooleanVar(value=False)).get()),
            "(3,+1)": bool(getattr(self, "var_show_tlap_rcp", tk.BooleanVar(value=False)).get()),
            "(3,+3)": bool(getattr(self, "var_show_tlap_ccp", tk.BooleanVar(value=False)).get()),
        }

    def _make_cpviewer_figure(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        if parsed is None:
            raise ValueError("No parsed TRHO data available.")

        df_atoms = getattr(parsed, "df_cpviewer_atoms", pd.DataFrame())
        if df_atoms is None or df_atoms.empty:
            df_atoms = getattr(parsed, "df_cpviewer_pool_atoms", pd.DataFrame())
        if df_atoms is None or df_atoms.empty:
            df_atoms = getattr(parsed, "df_true_atoms", pd.DataFrame())
        if df_atoms is None or df_atoms.empty:
            raise ValueError("No atom coordinates are available for CP Viewer.")

        fig = go.Figure()

        cp_source = str(getattr(self, "var_cp_source", tk.StringVar(value="Rho")).get() or "Rho").strip()
        show_rho_cps = cp_source in {"Rho", "Both"}
        show_lap_cps = cp_source in {"Laplacian", "Both"}
        tlap_parsed = None
        if show_lap_cps:
            ok_tlap = self.app.ensure_active_tlap_parsed()
            tlap_parsed = getattr(self.app.state, "tlap_parsed", None)
            if (not ok_tlap) or tlap_parsed is None or getattr(tlap_parsed, "df_cp_props", pd.DataFrame()).empty:
                raise ValueError("Laplacian CP mode requires a parsed active TLAP result.")


        def _safe_float(var_obj, default: float, *, vmin: float | None = None, vmax: float | None = None) -> float:
            try:
                val = float((var_obj.get() or "").strip().replace(",", "."))
            except Exception:
                val = float(default)
            if vmin is not None:
                val = max(float(vmin), val)
            if vmax is not None:
                val = min(float(vmax), val)
            return float(val)

        marker_scale = _safe_float(getattr(self, "var_marker_scale", tk.StringVar(value="1.0")), 1.0, vmin=0.4, vmax=4.0)
        cell_width = _safe_float(getattr(self, "var_cell_width", tk.StringVar(value="2.0")), 2.0, vmin=0.5, vmax=8.0)
        cell_opacity = _safe_float(getattr(self, "var_cell_opacity", tk.StringVar(value="0.8")), 0.8, vmin=0.05, vmax=1.0)
        cell_color_label = str(getattr(self, "var_cell_color", tk.StringVar(value="Medium gray")).get() or "Medium gray").strip()
        cell_rgb_map = {
            "Light gray": (190, 190, 190),
            "Medium gray": (130, 130, 130),
            "Dark gray": (80, 80, 80),
        }
        cell_rgb = cell_rgb_map.get(cell_color_label, (130, 130, 130))
        cell_rgba = f"rgba({cell_rgb[0]},{cell_rgb[1]},{cell_rgb[2]},{cell_opacity:.3f})"

        # Atoms: grouped by element and sized with lightweight approximate atomic radii.
        atom_df = df_atoms.copy()
        atom_df["SYMBOL"] = atom_df.get("SYMBOL", pd.Series(["Atom"] * len(atom_df))).astype(str)
        for sym, grp in atom_df.groupby("SYMBOL", sort=True):
            atom_marker_size = max(2.0, min(40.0, _cpviewer_marker_size(sym) * marker_scale))
            hover = []
            for idx, row in grp.iterrows():
                old_id = row.get("OLD", "—")
                cell = (row.get("CELL_X", 0), row.get("CELL_Y", 0), row.get("CELL_Z", 0))
                hover.append(
                    f"<b>Atom</b><br>"
                    f"Element: {sym}<br>"
                    f"OLD: {old_id}<br>"
                    f"cell=({cell[0]}, {cell[1]}, {cell[2]})<br>"
                    f"x={float(row.get('X_ANGSTROM', np.nan)):.3f} Å<br>"
                    f"y={float(row.get('Y_ANGSTROM', np.nan)):.3f} Å<br>"
                    f"z={float(row.get('Z_ANGSTROM', np.nan)):.3f} Å"
                )
            fig.add_trace(go.Scatter3d(
                x=grp["X_ANGSTROM"],
                y=grp["Y_ANGSTROM"],
                z=grp["Z_ANGSTROM"],
                mode="markers",
                name=f"Atoms: {sym}",
                legendgroup=f"atoms_{sym}",
                marker=dict(size=atom_marker_size, symbol="circle", opacity=0.65),
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            ))

        def _fmt_hover_value(val, decimals=6):
            if pd.isna(val):
                return "—"
            try:
                return f"{float(val):.{decimals}f}"
            except Exception:
                return str(val)

        def _add_cp_trace(df, name, symbol, size, cp_type, extra_cols=None):
            if df is None or df.empty:
                return
            extra_cols = extra_cols or []
            hover = []
            for idx, row in df.iterrows():
                title = name[:-1] if name.endswith('s') else name
                lines = [f"<b>{title} {cp_type}</b>"]
                lines.append(f"Index: {idx}")
                lines.append(f"x={float(row.get('X_ANGSTROM', np.nan)):.3f} Å")
                lines.append(f"y={float(row.get('Y_ANGSTROM', np.nan)):.3f} Å")
                lines.append(f"z={float(row.get('Z_ANGSTROM', np.nan)):.3f} Å")
                for col in extra_cols:
                    val = row.get(col, np.nan)
                    if pd.notna(val):
                        label = col
                        if col == 'LAP':
                            label = 'lap'
                        elif col == 'RHO':
                            label = 'rho'
                        elif col == 'ELLIP':
                            label = 'ellipticity'
                        elif col == 'LAMBDA1':
                            label = 'lambda1'
                        elif col == 'LAMBDA2':
                            label = 'lambda2'
                        elif col == 'LAMBDA3':
                            label = 'lambda3'
                        elif col == 'BCP_ELEM':
                            label = 'atoms'
                        elif col == 'classification':
                            label = 'classification'
                        elif col == 'Sym':
                            label = 'nearest_atom'
                        elif col == 'd_min':
                            label = 'd_min'
                        decimals = 4 if col in {'RHO', 'LAP', 'ELLIP', 'LAMBDA1', 'LAMBDA2', 'LAMBDA3', 'd_min'} else 6
                        suffix = ' Å' if col == 'd_min' else ''
                        lines.append(f"{label}={_fmt_hover_value(val, decimals)}{suffix}")
                hover.append("<br>".join(lines))
            fig.add_trace(go.Scatter3d(
                x=df["X_ANGSTROM"],
                y=df["Y_ANGSTROM"],
                z=df["Z_ANGSTROM"],
                mode="markers",
                name=name,
                marker=dict(size=max(3.0, min(36.0, float(size) * marker_scale)), symbol=symbol),
                text=hover,
                hovertemplate="%{text}<extra></extra>",
            ))

        if show_rho_cps:
            _add_cp_trace(
                getattr(parsed, "df_bcp_props", pd.DataFrame()),
                "BCPs",
                "diamond",
                6,
                "(3,-1)",
                ["RHO", "LAP", "ELLIP", "LAMBDA1", "LAMBDA2", "LAMBDA3", "BCP_ELEM"],
            )
            _add_cp_trace(
                getattr(parsed, "df_ring", pd.DataFrame()),
                "RCPs",
                "square",
                6,
                "(3,+1)",
                ["RHO", "LAP"],
            )
            _add_cp_trace(
                getattr(parsed, "df_cage", pd.DataFrame()),
                "CCPs",
                "x",
                7,
                "(3,+3)",
                ["RHO", "LAP"],
            )
            _add_cp_trace(
                getattr(parsed, "df_att_nao_nucl", pd.DataFrame()),
                "Flagged (3,-3)",
                "cross",
                7,
                "(3,-3)",
                ["classification", "Sym", "d_min"],
            )

        if show_lap_cps and tlap_parsed is not None:
            df_lap = getattr(tlap_parsed, "df_cp_props", pd.DataFrame())
            if df_lap is not None and not df_lap.empty and "TYPE" in df_lap.columns:
                df_lap = df_lap.copy()
                df_lap["TYPE_KEY"] = df_lap["TYPE"].astype(str).str.replace(" ", "", regex=False)
                selected_map = self._selected_tlap_type_map()
                style_map = {
                    "(3,-3)": dict(name="TLAP (3,-3)", symbol="diamond-open", color="#c2185b", line="#7a1239", size=7.0),
                    "(3,-1)": dict(name="TLAP (3,-1)", symbol="diamond", color="#7b1fa2", line="#4a1363", size=6.5),
                    "(3,+1)": dict(name="TLAP (3,+1)", symbol="square-open", color="#00897b", line="#00564d", size=6.5),
                    "(3,+3)": dict(name="TLAP (3,+3)", symbol="x", color="#ef6c00", line="#8a3f00", size=7.0),
                }
                for tkey, enabled in selected_map.items():
                    if not enabled:
                        continue
                    sub = df_lap.loc[df_lap["TYPE_KEY"] == tkey].copy()
                    if sub.empty:
                        continue
                    st = style_map.get(tkey, style_map["(3,-3)"])
                    hover = []
                    for idx, row in sub.iterrows():
                        lines = [f"<b>{st['name']}</b>"]
                        lines.append(f"Index: {idx}")
                        nea_sym = str(row.get("NEA_SYMBOL", "") or "")
                        nea_idx = row.get("NEA_INDEX", np.nan)
                        if nea_sym or pd.notna(nea_idx):
                            lines.append(f"NEA: {nea_sym}{int(nea_idx) if pd.notna(nea_idx) else ''}")
                        lines.append(f"x={float(row.get('X_ANGSTROM', np.nan)):.3f} Å")
                        lines.append(f"y={float(row.get('Y_ANGSTROM', np.nan)):.3f} Å")
                        lines.append(f"z={float(row.get('Z_ANGSTROM', np.nan)):.3f} Å")
                        for col, label in (("NEG_LAP", "-lap"), ("RHO", "rho"), ("LAMBDA1", "lambda1"), ("LAMBDA2", "lambda2"), ("LAMBDA3", "lambda3"), ("NEAREST_ATOM_SYMBOL", "nearest_atom"), ("NEAREST_ATOM_INDEX", "nearest_atom_index"), ("NEAREST_DIST_ANG", "nearest_dist")):
                            val = row.get(col, np.nan)
                            if pd.notna(val) and str(val) != "":
                                suffix = " Å" if col == "NEAREST_DIST_ANG" else ""
                                decimals = 4 if col not in {"NEAREST_ATOM_INDEX"} else 0
                                try:
                                    sval = f"{float(val):.{decimals}f}" if decimals > 0 else f"{int(float(val))}"
                                except Exception:
                                    sval = str(val)
                                lines.append(f"{label}={sval}{suffix}")
                        hover.append("<br>".join(lines))
                    fig.add_trace(go.Scatter3d(
                        x=sub["X_ANGSTROM"],
                        y=sub["Y_ANGSTROM"],
                        z=sub["Z_ANGSTROM"],
                        mode="markers",
                        name=st["name"],
                        marker=dict(size=max(3.0, min(36.0, float(st["size"]) * marker_scale)), symbol=st["symbol"], color=st["color"], line=dict(width=1, color=st["line"])),
                        text=hover,
                        hovertemplate="%{text}<extra></extra>",
                    ))

        def _add_bond_path_overlay(fig_obj, df_rows: pd.DataFrame, *, name: str = "Bond paths") -> None:
            if df_rows is None or df_rows.empty:
                return
            req_cols = [
                "ATTR1_X_ANGSTROM", "ATTR1_Y_ANGSTROM", "ATTR1_Z_ANGSTROM",
                "ATTR2_X_ANGSTROM", "ATTR2_Y_ANGSTROM", "ATTR2_Z_ANGSTROM",
            ]
            if any(c not in df_rows.columns for c in req_cols):
                return

            xs, ys, zs, hover = [], [], [], []
            for idx, row in df_rows.iterrows():
                try:
                    x1 = float(row["ATTR1_X_ANGSTROM"]); y1 = float(row["ATTR1_Y_ANGSTROM"]); z1 = float(row["ATTR1_Z_ANGSTROM"])
                    x2 = float(row["ATTR2_X_ANGSTROM"]); y2 = float(row["ATTR2_Y_ANGSTROM"]); z2 = float(row["ATTR2_Z_ANGSTROM"])
                except Exception:
                    continue
                vals = [x1, y1, z1, x2, y2, z2]
                if not all(np.isfinite(v) for v in vals):
                    continue
                xs.extend([x1, x2, None])
                ys.extend([y1, y2, None])
                zs.extend([z1, z2, None])
                pair = str(row.get("BCP_ELEM", "BCP") or "BCP")
                rho = _fmt_hover_value(row.get("RHO", np.nan), 4)
                lap = _fmt_hover_value(row.get("LAP", np.nan), 4)
                hover.extend([
                    f"<b>Bond path</b><br>BCP: {idx}<br>atoms={pair}<br>rho={rho}<br>lap={lap}",
                    f"<b>Bond path</b><br>BCP: {idx}<br>atoms={pair}<br>rho={rho}<br>lap={lap}",
                    None,
                ])
            if not xs:
                return
            fig_obj.add_trace(
                go.Scatter3d(
                    x=xs,
                    y=ys,
                    z=zs,
                    mode="lines",
                    name=name,
                    line=dict(width=4, color="rgba(25,25,25,0.95)"),
                    text=hover,
                    hovertemplate="%{text}<extra></extra>",
                    showlegend=True,
                )
            )

        if show_rho_cps and bool(getattr(self, "var_show_bondpaths", tk.BooleanVar(value=True)).get()):
            _add_bond_path_overlay(fig, getattr(parsed, "df_bcp_props", pd.DataFrame()))

        # Unit cell wireframe anchored on a *real lattice node* and chosen so the
        # complete cell stays as visible as possible within the atom/CP context.
        cell_vecs = np.asarray(getattr(parsed, "cell_vectors_ang", np.zeros((0, 3))), dtype=float)
        cell_vertices_xyz = None
        if cell_vecs.shape == (3, 3):
            a, b, c = cell_vecs[0], cell_vecs[1], cell_vecs[2]
            atom_xyz = atom_df[["X_ANGSTROM", "Y_ANGSTROM", "Z_ANGSTROM"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
            atom_xyz = atom_xyz[np.isfinite(atom_xyz).all(axis=1)] if atom_xyz.size else np.zeros((0, 3), dtype=float)

            origin = np.zeros(3, dtype=float)
            try:
                if atom_xyz.shape[0] > 0:
                    basis = np.column_stack((a, b, c))
                    frac = np.linalg.solve(basis, atom_xyz.T).T
                    # Candidate lattice nodes from nearby integer translations around
                    # the displayed atoms. We then choose the full primitive cell with
                    # the smallest overflow beyond the atom bounding box, which keeps
                    # the cell complete and crystallographically consistent.
                    frac_floor = np.floor(frac).astype(int)
                    candidates = set()
                    for base in frac_floor:
                        bi = tuple(int(x) for x in base.tolist())
                        for dx in (0, 1):
                            for dy in (0, 1):
                                for dz in (0, 1):
                                    candidates.add((bi[0] + dx, bi[1] + dy, bi[2] + dz))
                    if not candidates:
                        candidates.add((0, 0, 0))

                    atom_min = atom_xyz.min(axis=0)
                    atom_max = atom_xyz.max(axis=0)
                    atom_center = 0.5 * (atom_min + atom_max)
                    best_score = None
                    best_origin = None
                    best_vertices = None
                    for n0, n1, n2 in sorted(candidates):
                        cand_origin = n0 * a + n1 * b + n2 * c
                        verts_arr = np.vstack([
                            cand_origin,
                            cand_origin + a,
                            cand_origin + b,
                            cand_origin + c,
                            cand_origin + a + b,
                            cand_origin + a + c,
                            cand_origin + b + c,
                            cand_origin + a + b + c,
                        ])
                        vmin = verts_arr.min(axis=0)
                        vmax = verts_arr.max(axis=0)
                        overflow_low = np.maximum(atom_min - vmin, 0.0)
                        overflow_high = np.maximum(vmax - atom_max, 0.0)
                        overflow = float(np.sum(overflow_low + overflow_high))
                        center_dist = float(np.linalg.norm(0.5 * (vmin + vmax) - atom_center))
                        score = (overflow, center_dist)
                        if best_score is None or score < best_score:
                            best_score = score
                            best_origin = cand_origin
                            best_vertices = verts_arr
                    if best_origin is not None:
                        origin = best_origin
                        cell_vertices_xyz = best_vertices
            except Exception:
                origin = np.zeros(3, dtype=float)
                cell_vertices_xyz = None

            verts = {
                "000": origin,
                "100": origin + a,
                "010": origin + b,
                "001": origin + c,
                "110": origin + a + b,
                "101": origin + a + c,
                "011": origin + b + c,
                "111": origin + a + b + c,
            }
            if cell_vertices_xyz is None:
                cell_vertices_xyz = np.vstack(list(verts.values()))

            edges = [
                ("000", "100"), ("000", "010"), ("000", "001"),
                ("100", "110"), ("100", "101"),
                ("010", "110"), ("010", "011"),
                ("001", "101"), ("001", "011"),
                ("110", "111"), ("101", "111"), ("011", "111"),
            ]
            x_lines, y_lines, z_lines = [], [], []
            for u, v in edges:
                p1 = verts[u]
                p2 = verts[v]
                x_lines.extend([float(p1[0]), float(p2[0]), None])
                y_lines.extend([float(p1[1]), float(p2[1]), None])
                z_lines.extend([float(p1[2]), float(p2[2]), None])
            fig.add_trace(go.Scatter3d(
                x=x_lines,
                y=y_lines,
                z=z_lines,
                mode="lines",
                name="Unit cell",
                hoverinfo="skip",
                line=dict(width=cell_width, color=cell_rgba),
                showlegend=True,
            ))

        # Keep the visible field tied primarily to the atoms + CPs, but ensure the
        # selected unit cell is fully visible as well.
        plot_xyz = []
        try:
            atom_xyz = atom_df[["X_ANGSTROM", "Y_ANGSTROM", "Z_ANGSTROM"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
            atom_xyz = atom_xyz[np.isfinite(atom_xyz).all(axis=1)] if atom_xyz.size else np.zeros((0, 3), dtype=float)
            if atom_xyz.shape[0] > 0:
                plot_xyz.append(atom_xyz)
        except Exception:
            pass
        cp_range_frames = []
        if show_rho_cps:
            cp_range_frames.extend([
                getattr(parsed, "df_bcp_props", pd.DataFrame()),
                getattr(parsed, "df_ring", pd.DataFrame()),
                getattr(parsed, "df_cage", pd.DataFrame()),
                getattr(parsed, "df_att_nao_nucl", pd.DataFrame()),
            ])
        if show_lap_cps and tlap_parsed is not None:
            df_lap_range = getattr(tlap_parsed, "df_cp_props", pd.DataFrame())
            if df_lap_range is not None and not df_lap_range.empty and "TYPE" in df_lap_range.columns:
                cp_range_frames.append(df_lap_range.loc[df_lap_range["TYPE"].astype(str).str.replace(" ", "", regex=False) == "(3,-3)"])
        for _df in cp_range_frames:
            try:
                if _df is None or _df.empty:
                    continue
                arr = _df[["X_ANGSTROM", "Y_ANGSTROM", "Z_ANGSTROM"]].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
                arr = arr[np.isfinite(arr).all(axis=1)] if arr.size else np.zeros((0, 3), dtype=float)
                if arr.shape[0] > 0:
                    plot_xyz.append(arr)
            except Exception:
                pass
        scene_ranges = {}
        if plot_xyz:
            xyz = np.vstack(plot_xyz)
            xyz_min = xyz.min(axis=0)
            xyz_max = xyz.max(axis=0)
            if cell_vertices_xyz is not None and np.asarray(cell_vertices_xyz).shape == (8, 3):
                xyz_min = np.minimum(xyz_min, np.asarray(cell_vertices_xyz).min(axis=0))
                xyz_max = np.maximum(xyz_max, np.asarray(cell_vertices_xyz).max(axis=0))
            span = np.maximum(xyz_max - xyz_min, 1.0)
            pad = np.maximum(0.25, 0.06 * span)
            mins = xyz_min - pad
            maxs = xyz_max + pad
            scene_ranges = dict(
                xaxis=dict(range=[float(mins[0]), float(maxs[0])]),
                yaxis=dict(range=[float(mins[1]), float(maxs[1])]),
                zaxis=dict(range=[float(mins[2]), float(maxs[2])]),
            )

        fig.update_layout(
            title="TopIso3D CP Viewer",
            scene=dict(
                xaxis_title="X (Å)",
                yaxis_title="Y (Å)",
                zaxis_title="Z (Å)",
                aspectmode="data",
                camera=dict(projection=dict(type="orthographic")),
                **scene_ranges,
            ),
            margin=dict(l=0, r=0, t=40, b=0),
            legend=dict(itemsizing="constant"),
        )
        return fig

    def _next_saved_html_path(self, base_dir: Path) -> Path:
        base = Path(base_dir)
        n = 1
        while True:
            cand = base / f"cpviewer_{n:03d}.html"
            if not cand.exists():
                return cand
            n += 1

    def _open_viewer(self):
        try:
            fig = self._make_cpviewer_figure()
            run_dir = getattr(self.app.ctx, "active_trho_run", None)
            base_dir = None
            try:
                if run_dir is not None:
                    base_dir = Path(run_dir)
            except Exception:
                base_dir = None
            if base_dir is None:
                ws = getattr(self.app.ctx, "workspace_dir", None)
                base_dir = Path(ws) if ws is not None else Path.cwd()
            base_dir.mkdir(parents=True, exist_ok=True)

            if bool(self.var_save_html.get()):
                saved_path = self._next_saved_html_path(base_dir)
                _show_plotly_figure(fig, saved_html=saved_path)
                self.var_save_note.set(f"Last saved HTML: {saved_path.name}")
                self.app.set_status(f"CP Viewer opened and saved: {saved_path.name}")
            else:
                _show_plotly_figure(fig)
                self.var_save_note.set("Preview opened in browser (not saved).")
                self.app.set_status("CP Viewer opened")
        except Exception as e:
            messagebox.showerror("CP Viewer", f"Could not open CP Viewer:\n\n{e}")

    def refresh(self):
        ctx = self.app.ctx
        active_label = str(getattr(ctx, "active_trho_label", "—") or "—")
        self.var_active_run.set(active_label)
        active_tlap_label = str(getattr(ctx, "active_tlap_label", "—") or "—")
        self.var_active_tlap_run.set(active_tlap_label)

        out_path = self.app._find_existing_trho_out()
        if out_path is not None:
            try:
                rel = out_path.relative_to(Path(ctx.workspace_dir)) if ctx.workspace_dir else out_path
            except Exception:
                rel = out_path
            self.var_trho_out.set(str(rel))
        else:
            self.var_trho_out.set("—")

        tlap_out_path = self.app._find_active_tlap_out()
        if tlap_out_path is not None:
            try:
                rel_tlap = tlap_out_path.relative_to(Path(ctx.workspace_dir)) if ctx.workspace_dir else tlap_out_path
            except Exception:
                rel_tlap = tlap_out_path
            self.var_tlap_out.set(str(rel_tlap))
        else:
            self.var_tlap_out.set("—")

        parsed = getattr(ctx, "trho_parsed", None)
        if parsed is not None:
            n_cpv_atoms = len(getattr(parsed, "df_cpviewer_atoms", pd.DataFrame()))
            n_bcp = len(getattr(parsed, "df_bcp_props", pd.DataFrame()))
            n_rcp = len(getattr(parsed, "df_ring", pd.DataFrame()))
            n_ccp = len(getattr(parsed, "df_cage", pd.DataFrame()))
            n_nna = len(getattr(parsed, "df_att_nao_nucl", pd.DataFrame()))
            tlap_counts = {"(3,-3)": 0, "(3,-1)": 0, "(3,+1)": 0, "(3,+3)": 0}
            try:
                if self.app.ensure_active_tlap_parsed():
                    tlap_parsed = getattr(ctx, "tlap_parsed", None)
                    df_tlap = getattr(tlap_parsed, "df_cp_props", pd.DataFrame()) if tlap_parsed is not None else pd.DataFrame()
                    if df_tlap is not None and not df_tlap.empty and "TYPE" in df_tlap.columns:
                        s = df_tlap["TYPE"].astype(str).str.replace(" ", "", regex=False)
                        for key in tlap_counts:
                            tlap_counts[key] = int((s == key).sum())
            except Exception:
                pass
            self.var_counts.set(
                f"Atoms: {n_cpv_atoms} | Rho CPs: BCP {n_bcp}, RCP {n_rcp}, CCP {n_ccp}, flagged {n_nna} | Lap CPs: (3,-3) {tlap_counts['(3,-3)']}, (3,-1) {tlap_counts['(3,-1)']}, (3,+1) {tlap_counts['(3,+1)']}, (3,+3) {tlap_counts['(3,+3)']}"
            )
        else:
            err = str(getattr(ctx, "trho_parse_error", "") or "").strip()
            if err:
                self.var_counts.set(f"TRHO output detected, but parsing is not yet available here: {err}")
            else:
                self.var_counts.set("No parsed TRHO data available.")

        ready = self.app._cp_viewer_ready()
        need_tlap = self._cp_source_requires_tlap()
        tlap_ok = True
        if need_tlap:
            try:
                tlap_ok = bool(self.app.ensure_active_tlap_parsed() and getattr(ctx, "tlap_parsed", None) is not None and not getattr(getattr(ctx, "tlap_parsed", None), "df_cp_props", pd.DataFrame()).empty)
            except Exception:
                tlap_ok = False
        effective_ready = bool(ready and tlap_ok)

        if effective_ready:
            self.lbl_status.configure(text="CP Viewer ready")
        elif need_tlap and ready:
            self.lbl_status.configure(text="CP Viewer needs an active parsed TLAP result for the selected CP source")
        else:
            self.lbl_status.configure(text="CP Viewer not ready")

        try:
            self.btn_open_placeholder.configure(state=("normal" if effective_ready else "disabled"))
        except Exception:
            pass
        try:
            self.cmb_cp_source.configure(state=("readonly" if ready else "disabled"))
        except Exception:
            pass
        try:
            self.chk_save_html.configure(state=("normal" if effective_ready else "disabled"))
        except Exception:
            pass

        self._refresh_cp_source_ui()

        if effective_ready:
            run_dir = getattr(ctx, "active_trho_run", None)
            if run_dir is not None:
                try:
                    next_name = self._next_saved_html_path(Path(run_dir)).name
                    self.var_save_note.set(f"as {next_name}")
                except Exception:
                    self.var_save_note.set("as cpviewer_001.html")
            else:
                self.var_save_note.set("in the active TRHO run folder")
        else:
            self.var_save_note.set("")

    def on_show(self):
        self.refresh()

    def refresh_state(self):
        self.refresh()


# -----------------------------
# Workspace Page
# -----------------------------
class WorkspacePage(BasePage):
    title = "Workspace"

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Choose a folder that contains fort.9 OR a wavefunction file *.f9 (to run TRHO).").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        ttk.Button(body, text="Choose folder…", command=self.choose_folder).grid(row=1, column=0, sticky="w")
        self.path_var = tk.StringVar(value="")
        ttk.Entry(body, textvariable=self.path_var).grid(row=1, column=1, sticky="ew", padx=(10, 0))

        ttk.Separator(body).grid(row=2, column=0, columnspan=2, sticky="ew", pady=14)

        self.lbl = ttk.Label(body, text="Status: —")
        self.lbl.grid(row=3, column=0, columnspan=2, sticky="w")

        ttk.Separator(body).grid(row=4, column=0, columnspan=2, sticky="ew", pady=14)

        ttk.Label(
            body,
            text=("Automatic behavior:\n"
                  "- Validation happens immediately after choosing the folder.\n"
                  "- If only *.f9 exists, the app will create fort.9 automatically ONLY when TRHO starts.\n"
                  "  (so we don't modify your folder until you actually run something)"),
            justify="left"
        ).grid(row=5, column=0, columnspan=2, sticky="w")

    def choose_folder(self):
        d = filedialog.askdirectory(title="Select workspace folder (contains fort.9, *.f9, or trho.out)")
        if not d:
            return
        p = Path(d).resolve()
        self.app.ctx.workspace_dir = p
        self.path_var.set(str(p))
        self.app.ctx.trho_done = False
        self.app.ctx.trho_parsed = None
        self.app.ctx.trho_parse_error = None
        self.app.ctx.trho_parse_attempted_out = ""
        self.app.ctx.df_bcp_props = None
        self.app.ctx.df_true_atoms = None
        self.app.ctx.active_trho_run = None
        self.app.ctx.active_trho_label = "—"
        self.app.ctx.active_tlap_run = None
        self.app.ctx.active_tlap_label = "—"
        self.app.ctx.tlap_parse_attempted_out = ""
        self.app.ctx.tlap_parsed = None
        self.app.ctx.tlap_parse_error = None
        self.app.ctx.tlap_done = False
        self.app.ctx.atbp_run_dir = None
        self.app.ctx.atbp_out_path = None
        try:
            self.app.ctx.df_atbp = None
        except Exception:
            pass

        # Auto-validate immediately (no button)
        self.app.auto_validate_workspace()
        self.app.set_status(self.app.ctx.workspace_msg)

    def on_show(self):
        ctx = self.app.ctx
        self.path_var.set(str(ctx.workspace_dir) if ctx.workspace_dir else "")
        self.lbl.config(text=f"Status: {ctx.workspace_msg}")

    def refresh_state(self):
        self.lbl.config(text=f"Status: {self.app.ctx.workspace_msg}")


# -----------------------------
# Compute Page
# -----------------------------
class ComputePage(BasePage):
    title = "TRHO"

    _ADV_IAUTO_LABELS = {
        "IAUTO = -1  | Global automatic search around NEAs": "-1",
        "IAUTO = -2  | Global automatic search from seed point": "-2",
        
    }
    _ADV_IAUTO_LABELS_INV = {v: k for k, v in _ADV_IAUTO_LABELS.items()}

    def _build(self):
        super()._build()

        root = self._make_scrollable_body()
        root.columnconfigure(0, weight=1)

        self.frm_content = ttk.Frame(root)
        self.frm_content.grid(row=0, column=0, sticky="ew")
        self.frm_content.columnconfigure(0, weight=1)

        self._build_intro(self.frm_content)
        self._build_active_run_selector(self.frm_content)
        self._build_execution_mode(self.frm_content)
        self._build_simple_frame(self.frm_content)
        self._build_advanced_frame(self.frm_content)
        self._build_scope_note(self.frm_content)

        self.frm_run = ttk.LabelFrame(root, text="TRHO execution", padding=(12, 0))
        self.frm_run.grid(row=1, column=0, sticky="ew", pady=(0, 0))
        self.frm_run.columnconfigure(0, weight=1)
        self._build_run_area(self.frm_run)

        self._last_synced = None
        self._last_render_key = None
        self._trho_constraints = []
        self._refresh_trho_mode_ui(force=True)
        self._sync_from_state(force=True)
        self._sync_output_path()

    def _build_intro(self, parent):
        frm = ttk.Frame(parent)
        frm.pack(fill="x")
        ttk.Label(
            frm,
            text=(
                "Global topological TRHO search modes for critical-point recovery. "
                "Simple mode is the default TopIso3D workflow; Advanced mode is optional "
                "and is enabled only when explicitly selected by the user. "
                "TopIso3D currently supports IAUTO = -2 and -1."
            ),
            wraplength=980,
            justify="left",
        ).pack(anchor="w")

    def _build_active_run_selector(self, parent):
        frm = ttk.LabelFrame(parent, text="Stored TRHO runs", padding=(12, 6))
        frm.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Label(row, text="Active run:").pack(side="left", padx=(0, 8))
        self.var_active_trho_run = tk.StringVar(value="—")
        self.cmb_active_trho = ttk.Combobox(row, textvariable=self.var_active_trho_run, values=(), state="readonly", width=46)
        self.cmb_active_trho.pack(side="left", fill="x", expand=True)
        self.cmb_active_trho.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_trho_run())

        self.lbl_active_trho_hint = ttk.Label(frm, text="No stored TRHO run detected yet.", wraplength=920, justify="left")
        self.lbl_active_trho_hint.pack(anchor="w", pady=(4, 0))

        self._trho_run_options = {}

    def _refresh_active_trho_selector(self):
        if not hasattr(self, 'var_active_trho_run'):
            return
        app = self.app
        values, options, active_label = app._get_trho_run_selector_data()
        self._trho_run_options = options
        try:
            self.cmb_active_trho.configure(values=values)
        except Exception:
            pass
        if values:
            self.var_active_trho_run.set(active_label)
            self.lbl_active_trho_hint.configure(text="Select which stored TRHO result should be active for Reports and follow-up analyses.")
            try:
                self.cmb_active_trho.configure(state=("readonly" if (not app._job_running) else "disabled"))
            except Exception:
                pass
        else:
            self.var_active_trho_run.set("—")
            self.lbl_active_trho_hint.configure(text="No stored TRHO run detected yet.")
            try:
                self.cmb_active_trho.configure(state="disabled")
            except Exception:
                pass

    def _on_select_active_trho_run(self):
        choice = (self.var_active_trho_run.get() or "").strip()
        run_dir = self._trho_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_trho_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_trho_run(Path(run_dir), refresh=True)
        self.app.set_status(f"Active TRHO run: {self.app._friendly_trho_run_label(Path(run_dir))}")
        self._refresh_active_trho_selector()

    def _build_active_trho_frame(self, parent):
        frm = ttk.LabelFrame(parent, text="TRHO result used by TLAP", padding=(12, 6))
        frm.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Label(row, text="Using TRHO run:").pack(side="left", padx=(0, 8))
        self.var_active_trho_run = tk.StringVar(value="—")
        self.cmb_active_trho = ttk.Combobox(row, textvariable=self.var_active_trho_run, values=(), state="readonly", width=46)
        self.cmb_active_trho.pack(side="left", fill="x", expand=True)
        self.cmb_active_trho.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_trho_run())
        self.lbl_active_trho_hint = ttk.Label(frm, text="No stored TRHO run detected yet.", wraplength=920, justify="left")
        self.lbl_active_trho_hint.pack(anchor="w", pady=(4, 0))
        self._trho_run_options = {}

    def _refresh_active_trho_selector(self):
        if not hasattr(self, 'var_active_trho_run'):
            return
        app = self.app
        values, options, active_label = app._get_trho_run_selector_data()
        self._trho_run_options = options
        try:
            self.cmb_active_trho.configure(values=values)
        except Exception:
            pass
        if values:
            self.var_active_trho_run.set(active_label)
            self.lbl_active_trho_hint.configure(text="Select which stored TRHO result should be used as the source for TLAP.")
            try:
                self.cmb_active_trho.configure(state=("readonly" if (not app._job_running) else "disabled"))
            except Exception:
                pass
        else:
            self.var_active_trho_run.set("—")
            self.lbl_active_trho_hint.configure(text="No stored TRHO run detected yet.")
            try:
                self.cmb_active_trho.configure(state="disabled")
            except Exception:
                pass

    def _on_select_active_trho_run(self):
        choice = (self.var_active_trho_run.get() or "").strip()
        run_dir = self._trho_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_trho_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_trho_run(Path(run_dir), refresh=True)
        self._refresh_active_trho_selector()


    def _build_execution_mode(self, parent):
        self.var_ui_mode = tk.StringVar(value=str(getattr(self.app.state, "trho_ui_mode", "simple") or "simple"))
        frm = ttk.LabelFrame(parent, text="Execution mode (default: Simple)", padding=(12, 6))
        frm.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Simple", value="simple", variable=self.var_ui_mode, command=self._on_ui_mode_changed).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(row, text="Advanced", value="advanced", variable=self.var_ui_mode, command=self._on_ui_mode_changed).pack(side="left")
        ttk.Label(
            frm,
            text="Simple mode remains the default. Choose Advanced only when you want to override the preset workflow.",
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _build_simple_frame(self, parent):
        self.frm_simple = ttk.LabelFrame(parent, text="Simple presets", padding=(12, 6))
        self.frm_simple.pack(fill="x", pady=(8, 0))

        self.var_simple_preset = tk.StringVar(value=str(getattr(self.app.state, "trho_simple_preset", getattr(self.app.state, "trho_mode", "relaxed")) or "relaxed"))

        row = ttk.Frame(self.frm_simple)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Relaxed", value="relaxed", variable=self.var_simple_preset, command=self._on_simple_preset_changed).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(row, text="Sensitive", value="sensitive", variable=self.var_simple_preset, command=self._on_simple_preset_changed).pack(side="left")

        self.lbl_simple_summary = ttk.Label(self.frm_simple, text="", justify="left")
        self.lbl_simple_summary.pack(anchor="w", pady=(6, 0))

        self.lbl_simple_desc = ttk.Label(self.frm_simple, text="", wraplength=940, justify="left")
        self.lbl_simple_desc.pack(anchor="w", pady=(4, 0))

        self._refresh_simple_summary()

    def _build_advanced_frame(self, parent):
        self.frm_advanced = ttk.LabelFrame(parent, text="Advanced TRHO", padding=(12, 8))
        self.frm_advanced.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(self.frm_advanced)
        row.pack(fill="x")

        ttk.Label(row, text="TRHO strategy:").pack(side="left", padx=(0, 8))
        self.var_adv_iauto = tk.StringVar(value=str(getattr(self.app.state, "trho_adv_iauto", "-1") or "-1"))
        self.var_adv_iauto_label = tk.StringVar(value=self._ADV_IAUTO_LABELS_INV.get(self.var_adv_iauto.get(), next(iter(self._ADV_IAUTO_LABELS.keys()))))
        self.cmb_adv_iauto = ttk.Combobox(
            row,
            textvariable=self.var_adv_iauto_label,
            values=list(self._ADV_IAUTO_LABELS.keys()),
            state="readonly",
            width=51,
        )
        self.cmb_adv_iauto.pack(side="left", fill="x", expand=True)
        self.cmb_adv_iauto.bind("<<ComboboxSelected>>", lambda _e: self._on_adv_iauto_changed())

        self.lbl_adv_help = ttk.Label(
            self.frm_advanced,
            text=(
                "Advanced mode is optional and is activated only when the user selects it. "
                "It exposes the supported TRHO strategies without changing the default Simple workflow."
            ),
            wraplength=940,
            justify="left",
        )
        self.lbl_adv_help.pack(anchor="w", pady=(4, 0))

        self.frm_adv_dynamic = ttk.Frame(self.frm_advanced)
        self.frm_adv_dynamic.pack(fill="x", pady=(4, 0))

    def _build_scope_note(self, parent):
        self.lbl_scope = ttk.Label(
            parent,
            text=(
                "Default TRHO workflow in TopIso3D: Simple mode. Supported Advanced TRHO modes: IAUTO = -2 and -1. "
                "To use IAUTO = 0, 1, 2, 3 and 4, use TOPOND directly."
            ),
            wraplength=980,
            justify="left",
        )
        self.lbl_scope.pack(anchor="w", pady=(1, 0))

    def _build_run_area(self, parent):
        row_top = ttk.Frame(parent)
        row_top.pack(fill="x")
        row_top.columnconfigure(0, weight=1)

        self.lbl_exec_msg = ttk.Label(row_top, text="TRHO may take some time for certain systems.")
        self.lbl_exec_msg.grid(row=0, column=0, sticky="w", pady=(0, 0))

        self._pb_row = ttk.Frame(parent)
        self._pb_row.pack(fill="x", pady=(0, 0))
        self._pb_row.columnconfigure(0, weight=1)

        self.pb = ttk.Progressbar(self._pb_row, mode="indeterminate", length=260)
        self.pb.grid(row=0, column=0, sticky="w", pady=0)
        self.pb.stop()

        self.btn_run = ttk.Button(self._pb_row, text="Run TRHO", command=self.app.run_trho)
        self.btn_run.grid(row=0, column=1, sticky="e", padx=(12, 0), pady=0)

        self.btn_abort = ttk.Button(self._pb_row, text="Abort", command=lambda: self.app.abort_current_job("TRHO"))
        self.btn_abort.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=0)
        self.btn_abort.configure(state="disabled")

        self._runtime_row = ttk.Frame(parent)
        self._runtime_row.pack(fill="x", pady=(0, 0))
        self._runtime_row.columnconfigure(0, weight=1)

        self.lbl_runtime = ttk.Label(self._runtime_row, text="", anchor="w", justify="left")
        self.lbl_runtime.grid(row=0, column=0, sticky="w")

        self.lbl_runhint = self.lbl_exec_msg

    def _simple_preset_params(self, preset: str) -> dict:
        preset = str(preset or "relaxed").strip().lower()
        if preset == "sensitive":
            return {
                "IAUTO": "-1",
                "IEXT": "1",
                "ICRIT": "0",
                "IBPAT": "1",
                "IPRINT": "0",
                "NSTEP": "30",
                "NNB": "15",
                "RMAX": "12.0",
                "TH": "6.0",
                "description": "Sensitive: broader global search, useful when CP recovery is harder or Morse consistency is problematic.",
                "line": "1,0,1,0,30,15,12.0,6.0",
            }
        return {
            "IAUTO": "-1",
            "IEXT": "1",
            "ICRIT": "0",
            "IBPAT": "1",
            "IPRINT": "0",
            "NSTEP": "30",
            "NNB": "10",
            "RMAX": "10.0",
            "TH": "5.0",
            "description": "Relaxed: faster routine global search for the standard TopIso3D workflow.",
            "line": "1,0,1,0,30,10,10.,5.",
        }

    def _ensure_adv_vars(self):
        if hasattr(self, "_adv_vars"):
            return
        self._adv_vars = {
            "-1": {
                "IEXT": tk.StringVar(value="1"),
                "ICRIT": tk.StringVar(value="0"),
                "IBPAT": tk.StringVar(value="1"),
                "IPRINT": tk.StringVar(value="0"),
                "NSTEP": tk.StringVar(value="30"),
                "NNB": tk.StringVar(value="10"),
                "RMAX": tk.StringVar(value="10.0"),
                "TH": tk.StringVar(value="5.0"),
            },
            "-2": {
                "IEXT": tk.StringVar(value="1"),
                "ICRIT": tk.StringVar(value="0"),
                "IBPAT": tk.StringVar(value="1"),
                "IPRINT": tk.StringVar(value="0"),
                "NSTEP": tk.StringVar(value="30"),
                "NNB": tk.StringVar(value="10"),
                "RMAX": tk.StringVar(value="10.0"),
                "TH": tk.StringVar(value="5.0"),
                "IFRA": tk.StringVar(value="0"),
                "X": tk.StringVar(value="0.0"),
                "Y": tk.StringVar(value="0.0"),
                "Z": tk.StringVar(value="0.0"),
            },
            "3": {
                "IMETH": tk.StringVar(value="1"),
                "IEXT": tk.StringVar(value="0"),
                "IBPAT": tk.StringVar(value="1"),
                "IPRINT": tk.StringVar(value="0"),
                "NSTEP": tk.StringVar(value="5"),
                "NNB": tk.StringVar(value="7"),
                "RMAX": tk.StringVar(value="5.0"),
                "TH": tk.StringVar(value="0.0"),
                "ITYPE": tk.StringVar(value="1"),
                "XMI": tk.StringVar(value="0.0"),
                "XMA": tk.StringVar(value="5.0"),
                "XINC": tk.StringVar(value="0.5"),
                "YMI": tk.StringVar(value="0.0"),
                "YMA": tk.StringVar(value="5.0"),
                "YINC": tk.StringVar(value="0.5"),
                "ZMI": tk.StringVar(value="0.0"),
                "ZMA": tk.StringVar(value="5.0"),
                "ZINC": tk.StringVar(value="0.5"),
                "NCONS": tk.StringVar(value="0"),
            },
        }

    def _refresh_simple_summary(self):
        p = self._simple_preset_params(self.var_simple_preset.get())
        self.lbl_simple_summary.configure(
            text=(
                "Preset summary\n"
                f"IAUTO = {p['IAUTO']}\n"
                f"{p['line']}"
            )
        )
        self.lbl_simple_desc.configure(text=p["description"])

    def _on_ui_mode_changed(self):
        self.app.state.trho_ui_mode = self.var_ui_mode.get()
        self._refresh_trho_mode_ui(force=True)

    def _on_simple_preset_changed(self):
        preset = self.var_simple_preset.get().strip() or "relaxed"
        self.app.state.trho_simple_preset = preset
        self.app.state.trho_mode = preset
        self._refresh_simple_summary()

    def _on_adv_iauto_changed(self):
        label = (self.var_adv_iauto_label.get() or "").strip()
        iauto = self._ADV_IAUTO_LABELS.get(label, "-1")
        self.var_adv_iauto.set(iauto)
        self.app.state.trho_adv_iauto = iauto
        self._render_dynamic_trho_section(force=True)

    def _refresh_trho_mode_ui(self, force: bool = False):
        mode = (self.var_ui_mode.get() or "simple").strip().lower()
        current = getattr(self, '_last_ui_mode', None)
        if force or current != mode:
            if mode == "advanced":
                self.frm_simple.pack_forget()
                self.frm_advanced.pack(fill="x", pady=(4, 0), before=self.lbl_scope)
            else:
                self.frm_advanced.pack_forget()
                self.frm_simple.pack(fill="x", pady=(4, 0), before=self.lbl_scope)
            self._last_ui_mode = mode
        self._refresh_simple_summary()
        if mode == 'advanced':
            self._render_dynamic_trho_section(force=force)

    def _render_dynamic_trho_section(self, force: bool = False):
        self._ensure_adv_vars()
        iauto = str(self.var_adv_iauto.get() or "-1").strip()
        key = (iauto,)
        if (not force) and getattr(self, '_last_render_key', None) == key:
            return
        for child in self.frm_adv_dynamic.winfo_children():
            child.destroy()
        if iauto == "-2":
            self._render_adv_minus2(self.frm_adv_dynamic)
        elif iauto == "__removed_3__":
            self._render_adv_3(self.frm_adv_dynamic)
        else:
            self._render_adv_minus1(self.frm_adv_dynamic)
        self._last_render_key = key

    def _build_param_grid(self, parent, spec_rows, *, entry_width: int = 8, padx: int = 10, pady=(8, 8), label_width: int = 6, cell_padx: int = 12):
        grid = ttk.Frame(parent)
        grid.pack(fill="x", padx=padx, pady=pady)
        for c in range(4):
            grid.grid_columnconfigure(c, weight=0, minsize=130)
        for r, spec in enumerate(spec_rows):
            for c, (label, var, width) in enumerate(spec):
                frm = ttk.Frame(grid)
                frm.grid(row=r, column=c, sticky="w", padx=(0, cell_padx), pady=2)
                ttk.Label(frm, text=str(label), width=label_width, anchor="w").grid(row=0, column=0, sticky="w")
                ttk.Entry(frm, textvariable=var, width=(width or entry_width)).grid(row=0, column=1, sticky="w", padx=(4, 0))
        return grid

    def _build_axis_row(self, parent, axis: str, vars_: dict, *, entry_width: int = 8):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=2)
        ttk.Label(row, text=f"{axis}", width=6, anchor="w").grid(row=0, column=0, sticky="w")
        for i, suffix in enumerate(("MI", "MA", "INC"), start=1):
            cell = ttk.Frame(row)
            cell.grid(row=0, column=i, sticky="w", padx=(0, 12))
            ttk.Label(cell, text=f"{axis}{suffix}", width=6, anchor="w").grid(row=0, column=0, sticky="w")
            ttk.Entry(cell, textvariable=vars_[f"{axis}{suffix}"], width=entry_width).grid(row=0, column=1, sticky="w", padx=(4, 0))
        return row

    def _render_adv_general_block(self, parent, iauto: str):
        vars_ = self._adv_vars[iauto]
        frm = ttk.LabelFrame(parent, text="General parameters")
        frm.pack(fill="x")
        self._build_param_grid(
            frm,
            [
                [("IEXT", vars_["IEXT"], 8), ("ICRIT", vars_["ICRIT"], 8), ("IBPAT", vars_["IBPAT"], 8), ("IPRINT", vars_["IPRINT"], 8)],
                [("NSTEP", vars_["NSTEP"], 8), ("NNB", vars_["NNB"], 8), ("RMAX", vars_["RMAX"], 8), ("TH", vars_["TH"], 8)],
            ],
            pady=(8, 8),
        )

    def _render_adv_minus1(self, parent):
        self._render_adv_general_block(parent, "-1")
        ttk.Label(
            parent,
            text=(
                "IAUTO = -1 keeps the current TopIso3D philosophy and simply opens the preset "
                "parameters for manual editing in advanced mode."
            ),
            wraplength=930,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def _render_adv_minus2(self, parent):
        self._render_adv_general_block(parent, "-2")
        vars_ = self._adv_vars["-2"]
        frm = ttk.LabelFrame(parent, text="Seed point")
        frm.pack(fill="x", pady=(6, 0))
        self._build_param_grid(
            frm,
            [[("IFRA", vars_["IFRA"], 8), ("X", vars_["X"], 8), ("Y", vars_["Y"], 8), ("Z", vars_["Z"], 8)]],
            pady=(8, 8),
        )

        ttk.Label(
            parent,
            text="Define the seed point used to initiate the global automatic search.",
            wraplength=930,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

    def _render_adv_3(self, parent):
        ttk.Label(parent, text="IAUTO = 3 is not exposed in TopIso3D.").pack(anchor="w")

    def _edit_trho_constraints_placeholder(self):
        messagebox.showinfo(
            "TRHO constraints",
            "Constraint editing will be connected in the next implementation step."
        )

    def _stringify_param(self, value, *, default: str = "") -> str:
        if value is None:
            return str(default)
        if isinstance(value, tk.Variable):
            try:
                return str(value.get()).strip()
            except Exception:
                return str(default)
        return str(value).strip()

    def _collect_adv_params_for_iauto(self, iauto: str) -> dict:
        self._ensure_adv_vars()
        iauto = str(iauto or "-1").strip()
        vars_ = self._adv_vars.get(iauto, self._adv_vars["-1"])
        cfg = {"IAUTO": iauto}
        for key, var in vars_.items():
            cfg[key] = self._stringify_param(var)
        if iauto == "3":
            raise ValueError("IAUTO = 3 is not available in TopIso3D.")
            ncons_raw = cfg.get("NCONS", "0") or "0"
            try:
                ncons = max(0, int(float(ncons_raw)))
            except Exception:
                ncons = 0
            cfg["NCONS"] = str(ncons)
            constraints = getattr(self, "_trho_constraints", [])
            if not isinstance(constraints, list):
                constraints = []
            cfg["CONSTRAINTS"] = constraints[:ncons]
        return cfg

    def collect_trho_config(self) -> dict:
        mode = (self.var_ui_mode.get() or "simple").strip().lower()
        if mode != "advanced":
            preset = (self.var_simple_preset.get() or "relaxed").strip().lower()
            cfg = dict(self._simple_preset_params(preset))
            cfg["ui_mode"] = "simple"
            cfg["preset"] = preset
            cfg["IAUTO"] = "-1"
            return cfg

        iauto = str(self.var_adv_iauto.get() or "-1").strip()
        cfg = self._collect_adv_params_for_iauto(iauto)
        cfg["ui_mode"] = "advanced"
        cfg["preset"] = None
        cfg["RSTAR_OVERRIDES"] = dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})
        return cfg

    def _collect_adv_params_for_iauto(self, iauto: str) -> dict:
        self._ensure_adv_vars()
        iauto = str(iauto or "0").strip()
        vars_ = self._adv_vars.get(iauto, self._adv_vars["0"])
        cfg = {"IAUTO": iauto}
        for key, var in vars_.items():
            if isinstance(var, tk.BooleanVar):
                cfg[key] = bool(var.get())
            else:
                cfg[key] = str(var.get()).strip()
        cfg["NNA"] = "0"
        return cfg

    def _prompt_missing_tlap_rstars(self, cfg: Optional[dict] = None) -> Dict[str, float]:
        cfg = cfg or self.collect_tlap_config()
        if str(cfg.get("IAUTO", "0")).strip() != "0" or not bool(cfg.get("VSCC", False)):
            return dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})

        try:
            gui_rstar = float(str(cfg.get("RSTAR", "0.0") or "0.0").replace(",", "."))
        except Exception:
            gui_rstar = 0.0
        # Positive GUI RSTAR acts as a global override; no per-element prompting needed.
        if gui_rstar > 0:
            return {}

        true_atoms_df = getattr(self.app.ctx, "df_true_atoms", None)
        if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
            parsed = getattr(self.app.ctx, "trho_parsed", None)
            true_atoms_df = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
        if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
            return {}

        cache = getattr(self, "_tlap_manual_rstar_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._tlap_manual_rstar_cache = cache

        missing: List[str] = []
        seen = set()
        for _, row in true_atoms_df.iterrows():
            sym = _atbp_symbol_from_row(row)
            key = (sym or "").strip().capitalize()
            if not key or key in seen:
                continue
            seen.add(key)
            if _atbp_default_tol_bohr(key) is None and key not in cache:
                missing.append(key)

        vals: Dict[str, float] = dict(cache)
        for sym in missing:
            dlg = _TolPromptDialog(self, sym)
            if dlg.value is None:
                raise ValueError("TLAP input generation cancelled: missing RSTAR value(s) for IAUTO = 0.")
            vals[sym] = float(dlg.value)
            cache[sym] = float(dlg.value)
        return vals

    def collect_tlap_config(self) -> dict:
        mode = (self.var_ui_mode.get() or "simple").strip().lower()
        if mode != "advanced":
            preset = (self.var_simple_preset.get() or "relaxed").strip().lower()
            cfg = dict(self._simple_preset_params(preset))
            cfg["ui_mode"] = "simple"
            cfg["preset"] = preset
            cfg["NNA"] = "0"
            cfg["VSCC"] = bool(cfg.get("VSCC", False))
            cfg["RSTAR_OVERRIDES"] = dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})
            return cfg

        iauto = str(self.var_adv_iauto.get() or "0").strip()
        cfg = self._collect_adv_params_for_iauto(iauto)
        try:
            self._ensure_adv_vars()
            vars_ = self._adv_vars.get(iauto, self._adv_vars["0"])
            if "VSCC" in vars_ and isinstance(vars_["VSCC"], tk.BooleanVar):
                cfg["VSCC"] = bool(vars_["VSCC"].get())
        except Exception:
            cfg["VSCC"] = bool(cfg.get("VSCC", False))
        cfg["ui_mode"] = "advanced"
        cfg["preset"] = None
        cfg["NNA"] = "0"
        cfg["RSTAR_OVERRIDES"] = dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})
        return cfg

    def _sync_from_state(self, force: bool = False):
        state_key = (
            str(getattr(self.app.state, "trho_ui_mode", self.var_ui_mode.get()) or self.var_ui_mode.get()),
            str(getattr(self.app.state, "trho_simple_preset", getattr(self.app.state, "trho_mode", self.var_simple_preset.get())) or self.var_simple_preset.get()),
            str(getattr(self.app.state, "trho_adv_iauto", self.var_adv_iauto.get()) or self.var_adv_iauto.get()),
        )
        if (not force) and getattr(self, '_last_synced', None) == state_key:
            return
        ui_mode, preset, iauto = state_key
        self.var_ui_mode.set(ui_mode if ui_mode in ("simple", "advanced") else "simple")
        self.var_simple_preset.set(preset if preset in ("relaxed", "sensitive") else "relaxed")
        self.app.state.trho_mode = self.var_simple_preset.get()
        if iauto not in ("-1", "-2"):
            iauto = "-1"
        self.var_adv_iauto.set(iauto)
        self.var_adv_iauto_label.set(self._ADV_IAUTO_LABELS_INV.get(iauto, next(iter(self._ADV_IAUTO_LABELS.keys()))))
        self._refresh_simple_summary()
        self._refresh_trho_mode_ui(force=force)
        self._last_synced = (self.var_ui_mode.get(), self.var_simple_preset.get(), self.var_adv_iauto.get())

    def _sync_output_path(self) -> None:
        pass

    def _set_running(self, running: bool, hint: str = "") -> None:
        try:
            if running:
                self.lbl_runhint.configure(text=hint or "Running… (TRHO may take a long time)")
                if hasattr(self, "lbl_runtime"):
                    self.lbl_runtime.configure(text=" ")
                self.pb.start(12)
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state="disabled")
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="normal")
            else:
                self.pb.stop()
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state=("normal" if self.app.ctx.workspace_ok else "disabled"))
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="disabled")
            self.update_idletasks()
        except Exception:
            pass

    def set_completion_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runhint"):
                self.lbl_runhint.configure(text=text or "TRHO may take some time for certain systems.")
        except Exception:
            pass

    def set_runtime_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runtime"):
                self.lbl_runtime.configure(text=text or "", anchor="w", justify="left")
                try:
                    self.lbl_runtime.update_idletasks()
                except Exception:
                    pass
        except Exception:
            pass

    def refresh_state(self):
        self._sync_from_state(force=False)
        self._refresh_active_trho_selector()
        ready_run = self.app.ctx.workspace_ok and (not self.app._job_running)
        try:
            self.btn_run.configure(state=("normal" if ready_run else "disabled"))
        except Exception:
            self.btn_run.state(["!disabled"] if ready_run else ["disabled"])

        if self.app._job_running and str(getattr(self.app, "_active_job_kind", "") or "").upper() == "TRHO":
            self._set_running(True, "Running… (TRHO may take a long time)")
        else:
            self._set_running(False)

def main():
    app = App()
    app.mainloop()


class TLAPPage(BasePage):
    title = "TLAP"

    _ADV_IAUTO_LABELS = {
        "IAUTO = 0  | Automatic TLAP search": "0",
        "IAUTO = 1  | TLAP search from starting points": "1",
    }
    _ADV_IAUTO_LABELS_INV = {v: k for k, v in _ADV_IAUTO_LABELS.items()}

    def _build(self):
        super()._build()

        root = self._make_scrollable_body()
        root.columnconfigure(0, weight=1)

        self.frm_content = ttk.Frame(root)
        self.frm_content.grid(row=0, column=0, sticky="ew")
        self.frm_content.columnconfigure(0, weight=1)

        self._build_intro(self.frm_content)
        self._build_active_tlap_frame(self.frm_content)
        self._build_execution_mode(self.frm_content)
        self._build_simple_frame(self.frm_content)
        self._build_advanced_frame(self.frm_content)
        self._build_scope_note(self.frm_content)

        self.frm_run = ttk.LabelFrame(root, text="TLAP execution", padding=(8, 0))
        self.frm_run.grid(row=1, column=0, sticky="ew", pady=(0, 0))
        self.frm_run.columnconfigure(0, weight=1)
        self._build_run_area(self.frm_run)

        self._last_synced = None
        self._last_render_key = None
        self._refresh_tlap_mode_ui(force=True)
        self._sync_from_state(force=True)

    def _build_intro(self, parent):
        frm = ttk.Frame(parent)
        frm.pack(fill="x")
        ttk.Label(
            frm,
            text=(
                "Topological analysis of the Laplacian (TLAP). "
                "Simple mode is the default TopIso3D workflow; Advanced mode is optional "
                "and is enabled only when explicitly selected by the user. "
                "This page follows the same interaction model used in TRHO."
            ),
            wraplength=980,
            justify="left",
        ).pack(anchor="w")

    def _build_active_run_selector(self, parent):
        frm = ttk.LabelFrame(parent, text="Stored TRHO runs", padding=(12, 6))
        frm.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Label(row, text="Active run:").pack(side="left", padx=(0, 8))
        self.var_active_trho_run = tk.StringVar(value="—")
        self.cmb_active_trho = ttk.Combobox(row, textvariable=self.var_active_trho_run, values=(), state="readonly", width=46)
        self.cmb_active_trho.pack(side="left", fill="x", expand=True)
        self.cmb_active_trho.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_trho_run())

        self.lbl_active_trho_hint = ttk.Label(frm, text="No stored TRHO run detected yet.", wraplength=920, justify="left")
        self.lbl_active_trho_hint.pack(anchor="w", pady=(4, 0))

        self._trho_run_options = {}

    def _refresh_active_trho_selector(self):
        if not hasattr(self, 'var_active_trho_run'):
            return
        app = self.app
        values, options, active_label = app._get_trho_run_selector_data()
        self._trho_run_options = options
        try:
            self.cmb_active_trho.configure(values=values)
        except Exception:
            pass
        if values:
            self.var_active_trho_run.set(active_label)
            self.lbl_active_trho_hint.configure(text="Select which stored TRHO result should be active for Reports and follow-up analyses.")
            try:
                self.cmb_active_trho.configure(state=("readonly" if (not app._job_running) else "disabled"))
            except Exception:
                pass
        else:
            self.var_active_trho_run.set("—")
            self.lbl_active_trho_hint.configure(text="No stored TRHO run detected yet.")
            try:
                self.cmb_active_trho.configure(state="disabled")
            except Exception:
                pass

    def _on_select_active_trho_run(self):
        choice = (self.var_active_trho_run.get() or "").strip()
        run_dir = self._trho_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_trho_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_trho_run(Path(run_dir), refresh=True)
        self.app.set_status(f"Active TRHO run: {self.app._friendly_trho_run_label(Path(run_dir))}")

    def _build_active_tlap_frame(self, parent):
        frm = ttk.LabelFrame(parent, text="Stored TLAP runs", padding=(12, 6))
        frm.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Label(row, text="Active TLAP run:").pack(side="left", padx=(0, 8))
        self.var_active_tlap_run = tk.StringVar(value="—")
        self.cmb_active_tlap = ttk.Combobox(row, textvariable=self.var_active_tlap_run, values=(), state="readonly", width=46)
        self.cmb_active_tlap.pack(side="left", fill="x", expand=True)
        self.cmb_active_tlap.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_tlap_run())

        self.lbl_active_tlap_hint = ttk.Label(frm, text="No stored TLAP run detected yet.", wraplength=920, justify="left")
        self.lbl_active_tlap_hint.pack(anchor="w", pady=(4, 0))

        self._tlap_run_options = {}

    def _refresh_active_tlap_selector(self):
        app = self.app
        values, options, active_label = app._get_tlap_run_selector_data()
        self._tlap_run_options = options
        try:
            self.cmb_active_tlap.configure(values=values)
        except Exception:
            pass
        if values:
            self.var_active_tlap_run.set(active_label)
            self.lbl_active_tlap_hint.configure(text="Select which stored TLAP result should be active for follow-up analyses.")
            try:
                self.cmb_active_tlap.configure(state=("readonly" if (not app._job_running) else "disabled"))
            except Exception:
                pass
        else:
            self.var_active_tlap_run.set("—")
            self.lbl_active_tlap_hint.configure(text="No stored TLAP run detected yet.")
            try:
                self.cmb_active_tlap.configure(state="disabled")
            except Exception:
                pass

    def _on_select_active_tlap_run(self):
        choice = (self.var_active_tlap_run.get() or "").strip()
        run_dir = self._tlap_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_tlap_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_tlap_run(Path(run_dir), refresh=True)
        self.app.set_status(f"Active TLAP run: {self.app._friendly_tlap_run_label(Path(run_dir))}")
        self._refresh_active_tlap_selector()

    def _build_execution_mode(self, parent):
        self.var_ui_mode = tk.StringVar(value=str(getattr(self.app.state, "tlap_ui_mode", "simple") or "simple"))
        frm = ttk.LabelFrame(parent, text="Execution mode (default: Simple)", padding=(12, 6))
        frm.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Simple", value="simple", variable=self.var_ui_mode, command=self._on_ui_mode_changed).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(row, text="Advanced", value="advanced", variable=self.var_ui_mode, command=self._on_ui_mode_changed).pack(side="left")

    def _build_simple_frame(self, parent):
        self.frm_simple = ttk.LabelFrame(parent, text="Simple presets", padding=(12, 6))
        self.frm_simple.pack(fill="x", pady=(8, 0))

        self.var_simple_preset = tk.StringVar(value=str(getattr(self.app.state, "tlap_simple_preset", "relaxed") or "relaxed"))

        row = ttk.Frame(self.frm_simple)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Relaxed", value="relaxed", variable=self.var_simple_preset, command=self._on_simple_preset_changed).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(row, text="Sensitive", value="sensitive", variable=self.var_simple_preset, command=self._on_simple_preset_changed).pack(side="left")

        self.lbl_simple_summary = ttk.Label(self.frm_simple, text="", justify="left")
        self.lbl_simple_summary.pack(anchor="w", pady=(6, 0))

        self.lbl_simple_desc = ttk.Label(self.frm_simple, text="", wraplength=940, justify="left")
        self.lbl_simple_desc.pack(anchor="w", pady=(4, 0))

        self._refresh_simple_summary()

    def _build_advanced_frame(self, parent):
        self.frm_advanced = ttk.LabelFrame(parent, text="Advanced TLAP", padding=(12, 6))
        self.frm_advanced.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(self.frm_advanced)
        row.pack(fill="x")

        ttk.Label(row, text="TLAP strategy:").pack(side="left", padx=(0, 8))
        self.var_adv_iauto = tk.StringVar(value=str(getattr(self.app.state, "tlap_adv_iauto", "0") or "0"))
        self.var_adv_iauto_label = tk.StringVar(value=self._ADV_IAUTO_LABELS_INV.get(self.var_adv_iauto.get(), next(iter(self._ADV_IAUTO_LABELS.keys()))))
        self.cmb_adv_iauto = ttk.Combobox(
            row,
            textvariable=self.var_adv_iauto_label,
            values=list(self._ADV_IAUTO_LABELS.keys()),
            state="readonly",
            width=51,
        )
        self.cmb_adv_iauto.pack(side="left", fill="x", expand=True)
        self.cmb_adv_iauto.bind("<<ComboboxSelected>>", lambda _e: self._on_adv_iauto_changed())

        self.lbl_adv_help = ttk.Label(
            self.frm_advanced,
            text="Advanced mode is optional. TLAP execution is available for IAUTO = 0; IAUTO = 1 remains input-only.",
            wraplength=900,
            justify="left",
        )
        self.lbl_adv_help.pack(anchor="w", pady=(4, 0))

        self.frm_adv_dynamic = ttk.Frame(self.frm_advanced)
        self.frm_adv_dynamic.pack(fill="x", pady=(4, 0))

    def _build_scope_note(self, parent):
        self.lbl_scope = ttk.Label(
            parent,
            text="Supported TLAP modes in TopIso3D: IAUTO = 0 and 1.",
            wraplength=980,
            justify="left",
        )
        self.lbl_scope.pack(anchor="w", pady=(1, 0))

    def _build_run_area(self, parent):
        row_top = ttk.Frame(parent)
        row_top.pack(fill="x")
        row_top.columnconfigure(0, weight=1)

        self.lbl_exec_msg = ttk.Label(row_top, text="TLAP execution is available in Simple mode and in Advanced mode when IAUTO = 0.")
        self.lbl_exec_msg.grid(row=0, column=0, sticky="w", pady=(0, 0))

        self._pb_row = ttk.Frame(parent)
        self._pb_row.pack(fill="x", pady=(0, 0))
        self._pb_row.columnconfigure(0, weight=1)

        self.pb = ttk.Progressbar(self._pb_row, mode="indeterminate", length=260)
        self.pb.grid(row=0, column=0, sticky="w", pady=0)
        self.pb.stop()

        self.btn_run = ttk.Button(self._pb_row, text="Run TLAP", command=self._run_tlap_placeholder)
        self.btn_run.grid(row=0, column=1, sticky="e", padx=(12, 0), pady=0)

        self.btn_abort = ttk.Button(self._pb_row, text="Abort", command=lambda: self.app.abort_current_job("TLAP"))
        self.btn_abort.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=0)
        self.btn_abort.configure(state="disabled")

        self._runtime_row = ttk.Frame(parent)
        self._runtime_row.pack(fill="x", pady=(0, 0))
        self._runtime_row.columnconfigure(0, weight=1)

        self.lbl_runtime = ttk.Label(self._runtime_row, text="", anchor="w", justify="left")
        self.lbl_runtime.grid(row=0, column=0, sticky="w")

        self.lbl_runhint = self.lbl_exec_msg

    def _simple_preset_params(self, preset: str) -> dict:
        preset = str(preset or "relaxed").strip().lower()
        if preset == "sensitive":
            return {
                "IAUTO": "0",
                "IMETH": "1",
                "IEXT": "0",
                "IBPAT": "0",
                "IPRINT": "0",
                "NSTEP": "30",
                "NNB": "10",
                "RMAX": "7.0",
                "ITYPE": "0",
                "NT": "12",
                "NP": "18",
                "VSCC": True,
                "NMAX": "5",
                "RSTAR": "0.0",
                "description": "Sensitive: broader IAUTO = 0 TLAP search with VSCC enabled for all NEAs; default RSTAR values come from the shared TOPOND TOL/RSTAR table.",
                "line": "1,0,0,0,30,10,7.0 | NT,NP = 12,18",
            }
        return {
            "IAUTO": "0",
            "IMETH": "1",
            "IEXT": "0",
            "IBPAT": "0",
            "IPRINT": "0",
            "NSTEP": "20",
            "NNB": "7",
            "RMAX": "5.0",
            "ITYPE": "0",
            "NT": "12",
            "NP": "18",
            "VSCC": True,
            "NMAX": "3",
            "RSTAR": "0.0",
            "description": "Relaxed: default IAUTO = 0 TLAP search with VSCC enabled for all NEAs; if RSTAR stays at 0.0, element-specific defaults are used.",
            "line": "1,0,0,0,20,7,5.0 | NT,NP = 12,18",
        }

    def _ensure_adv_vars(self):
        if hasattr(self, "_adv_vars"):
            return
        self._adv_vars = {
            "0": {
                "IMETH": tk.StringVar(value="1"),
                "IEXT": tk.StringVar(value="0"),
                "IBPAT": tk.StringVar(value="0"),
                "IPRINT": tk.StringVar(value="0"),
                "NSTEP": tk.StringVar(value="20"),
                "NNB": tk.StringVar(value="7"),
                "RMAX": tk.StringVar(value="5.0"),
                "ITYPE": tk.StringVar(value="0"),
                "NT": tk.StringVar(value="12"),
                "NP": tk.StringVar(value="18"),
                "NMAX": tk.StringVar(value="3"),
                "RSTAR": tk.StringVar(value="0.0"),
                "VSCC": tk.BooleanVar(value=False),
            },
            "1": {
                "IMETH": tk.StringVar(value="1"),
                "IEXT": tk.StringVar(value="0"),
                "IBPAT": tk.StringVar(value="0"),
                "IPRINT": tk.StringVar(value="0"),
                "NSTEP": tk.StringVar(value="20"),
                "NNB": tk.StringVar(value="7"),
                "RMAX": tk.StringVar(value="5.0"),
                "ITYPE": tk.StringVar(value="0"),
                "NT": tk.StringVar(value="12"),
                "NP": tk.StringVar(value="18"),
                "X": tk.StringVar(value="0.0"),
                "Y": tk.StringVar(value="0.0"),
                "Z": tk.StringVar(value="0.0"),
                "NMAX": tk.StringVar(value="3"),
                "RSTAR": tk.StringVar(value="0.0"),
                "VSCC": tk.BooleanVar(value=False),
            },
        }

    def _refresh_simple_summary(self):
        p = self._simple_preset_params(self.var_simple_preset.get())
        self.lbl_simple_summary.configure(
            text=(
                "Preset summary\n"
                f"IAUTO = {p['IAUTO']}\n"
                f"{p['line']}"
            )
        )
        self.lbl_simple_desc.configure(text=p["description"])

    def _on_ui_mode_changed(self):
        self.app.state.tlap_ui_mode = self.var_ui_mode.get()
        self._refresh_tlap_mode_ui(force=True)

    def _on_simple_preset_changed(self):
        preset = self.var_simple_preset.get().strip() or "relaxed"
        self.app.state.tlap_simple_preset = preset
        self._refresh_simple_summary()

    def _on_adv_iauto_changed(self):
        label = (self.var_adv_iauto_label.get() or "").strip()
        iauto = self._ADV_IAUTO_LABELS.get(label, "0")
        self.var_adv_iauto.set(iauto)
        self.app.state.tlap_adv_iauto = iauto
        self._render_dynamic_tlap_section(force=True)

    def _refresh_tlap_mode_ui(self, force: bool = False):
        mode = (self.var_ui_mode.get() or "simple").strip().lower()
        current = getattr(self, '_last_ui_mode', None)
        if force or current != mode:
            if mode == "advanced":
                self.frm_simple.pack_forget()
                self.frm_advanced.pack(fill="x", pady=(4, 0), before=self.lbl_scope)
            else:
                self.frm_advanced.pack_forget()
                self.frm_simple.pack(fill="x", pady=(4, 0), before=self.lbl_scope)
            self._last_ui_mode = mode
        self._refresh_simple_summary()
        if mode == 'advanced':
            self._render_dynamic_tlap_section(force=force)

    def _render_dynamic_tlap_section(self, force: bool = False):
        self._ensure_adv_vars()
        iauto = str(self.var_adv_iauto.get() or "0").strip()
        key = (iauto,)
        if (not force) and getattr(self, '_last_render_key', None) == key:
            return
        for child in self.frm_adv_dynamic.winfo_children():
            child.destroy()
        if iauto == "1":
            self._render_adv_1(self.frm_adv_dynamic)
        else:
            self._render_adv_0(self.frm_adv_dynamic)
        self._last_render_key = key

    def _build_param_grid(self, parent, spec_rows, *, entry_width: int = 8, padx: int = 10, pady=(8, 8), label_width: int = 7, cell_padx: int = 12):
        grid = ttk.Frame(parent)
        grid.pack(fill="x", padx=padx, pady=pady)
        for c in range(4):
            grid.grid_columnconfigure(c, weight=0, minsize=130)
        for r, spec in enumerate(spec_rows):
            for c, (label, var, width) in enumerate(spec):
                frm = ttk.Frame(grid)
                frm.grid(row=r, column=c, sticky="w", padx=(0, cell_padx), pady=2)
                ttk.Label(frm, text=str(label), width=label_width, anchor="w").grid(row=0, column=0, sticky="w")
                ttk.Entry(frm, textvariable=var, width=(width or entry_width)).grid(row=0, column=1, sticky="w", padx=(4, 0))
        return grid

    def _render_common_tlap_blocks(self, parent, iauto: str):
        vars_ = self._adv_vars[iauto]

        frm_general = ttk.LabelFrame(parent, text="General parameters")
        frm_general.pack(fill="x")

        grid = ttk.Frame(frm_general)
        grid.pack(fill="x", padx=8, pady=(4, 4))

        def _cell_label(col, row, txt, padx=(0, 4)):
            ttk.Label(grid, text=txt).grid(row=row, column=col, sticky="w", padx=padx, pady=2)

        def _cell_entry(col, row, var, width=7, padx=(0, 12)):
            ttk.Entry(grid, textvariable=var, width=width).grid(row=row, column=col, sticky="w", padx=padx, pady=2)

        # Left block (same columns for both rows)
        _cell_label(0, 0, "IMETH")
        _cell_entry(1, 0, vars_["IMETH"])
        _cell_label(2, 0, "IEXT")
        _cell_entry(3, 0, vars_["IEXT"])
        _cell_label(4, 0, "IBPAT")
        _cell_entry(5, 0, vars_["IBPAT"])
        _cell_label(6, 0, "IPRINT")
        _cell_entry(7, 0, vars_["IPRINT"], padx=(0, 14))

        _cell_label(0, 1, "NSTEP")
        _cell_entry(1, 1, vars_["NSTEP"])
        _cell_label(2, 1, "NNB")
        _cell_entry(3, 1, vars_["NNB"])
        _cell_label(4, 1, "RMAX")
        _cell_entry(5, 1, vars_["RMAX"])
        _cell_label(6, 1, "ITYPE")
        _cell_entry(7, 1, vars_["ITYPE"], padx=(0, 14))

        # Vertical divider spanning both rows
        sep = ttk.Separator(grid, orient="vertical")
        sep.grid(row=0, column=8, rowspan=2, sticky="ns", padx=(6, 16), pady=0)

        # Right block (NT / NP) on first row only
        _cell_label(9, 0, "NT")
        _cell_entry(10, 0, vars_["NT"], padx=(0, 12))
        _cell_label(11, 0, "NP")
        _cell_entry(12, 0, vars_["NP"], padx=(0, 0))

        frm_tlap = ttk.LabelFrame(parent, text="TLAP-specific options")
        frm_tlap.pack(fill="x", pady=(4, 0))

        row_t1 = ttk.Frame(frm_tlap)
        row_t1.pack(fill="x", padx=8, pady=(4, 4))

        ttk.Checkbutton(
            row_t1,
            text="Search in VSCC shells",
            variable=vars_["VSCC"],
            command=lambda: self._render_dynamic_tlap_section(force=True),
        ).pack(side="left")

        if bool(vars_["VSCC"].get()):
            ttk.Label(row_t1, text="NMAX").pack(side="left", padx=(18, 4))
            ttk.Entry(row_t1, textvariable=vars_["NMAX"], width=8).pack(side="left")
            ttk.Label(row_t1, text="RSTAR").pack(side="left", padx=(18, 4))
            ttk.Entry(row_t1, textvariable=vars_["RSTAR"], width=8).pack(side="left")

        frm_type = ttk.LabelFrame(parent, text="Critical point type")
        frm_type.pack(fill="x", pady=(4, 0))

        cptype = vars_["ITYPE"]
        row_type = ttk.Frame(frm_type)
        row_type.pack(fill="x", padx=8, pady=4)
        ttk.Radiobutton(row_type, text="(3,-3)", value="0", variable=cptype).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(row_type, text="(3,-1)", value="1", variable=cptype).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(row_type, text="(3,+1)", value="2", variable=cptype).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(row_type, text="(3,+3)", value="3", variable=cptype).pack(side="left")

    def _render_adv_0(self, parent):
        self._render_common_tlap_blocks(parent, "0")
        ttk.Label(
            parent,
            text="IAUTO = 0 uses TRHO TRUE atoms. The same VSCC/NMAX/RSTAR choice is applied to every NEA.",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(3, 0))

    def _render_adv_1(self, parent):
        self._render_common_tlap_blocks(parent, "1")
        vars_ = self._adv_vars["1"]
        frm = ttk.LabelFrame(parent, text="Starting point")
        frm.pack(fill="x", pady=(10, 0))
        self._build_param_grid(
            frm,
            [[("X", vars_["X"], 8), ("Y", vars_["Y"], 8), ("Z", vars_["Z"], 8)]],
            label_width=3,
        )
        ttk.Label(
            parent,
            text="IAUTO = 1 starts the TLAP search from user coordinates. Input generation for IAUTO = 1 is not connected yet.",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(3, 0))

    def _collect_adv_params_for_iauto(self, iauto: str) -> dict:
        self._ensure_adv_vars()
        iauto = str(iauto or "0").strip()
        vars_ = self._adv_vars.get(iauto, self._adv_vars["0"])
        cfg = {"IAUTO": iauto}
        for key, var in vars_.items():
            if isinstance(var, tk.BooleanVar):
                cfg[key] = bool(var.get())
            else:
                cfg[key] = str(var.get()).strip()
        cfg["NNA"] = "0"
        return cfg

    def _prompt_missing_tlap_rstars(self, cfg: Optional[dict] = None) -> Dict[str, float]:
        cfg = cfg or self.collect_tlap_config()
        if str(cfg.get("IAUTO", "0")).strip() != "0" or not bool(cfg.get("VSCC", False)):
            return dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})

        try:
            gui_rstar = float(str(cfg.get("RSTAR", "0.0") or "0.0").replace(",", "."))
        except Exception:
            gui_rstar = 0.0
        # Positive GUI RSTAR acts as a global override; no per-element prompting needed.
        if gui_rstar > 0:
            return {}

        true_atoms_df = getattr(self.app.ctx, "df_true_atoms", None)
        if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
            parsed = getattr(self.app.ctx, "trho_parsed", None)
            true_atoms_df = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
        if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
            return {}

        cache = getattr(self, "_tlap_manual_rstar_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._tlap_manual_rstar_cache = cache

        missing: List[str] = []
        seen = set()
        for _, row in true_atoms_df.iterrows():
            sym = _atbp_symbol_from_row(row)
            key = (sym or "").strip().capitalize()
            if not key or key in seen:
                continue
            seen.add(key)
            if _atbp_default_tol_bohr(key) is None and key not in cache:
                missing.append(key)

        vals: Dict[str, float] = dict(cache)
        for sym in missing:
            dlg = _TolPromptDialog(self, sym)
            if dlg.value is None:
                raise ValueError("TLAP input generation cancelled: missing RSTAR value(s) for IAUTO = 0.")
            vals[sym] = float(dlg.value)
            cache[sym] = float(dlg.value)
        return vals

    def collect_tlap_config(self) -> dict:
        mode = (self.var_ui_mode.get() or "simple").strip().lower()
        if mode != "advanced":
            preset = (self.var_simple_preset.get() or "relaxed").strip().lower()
            cfg = dict(self._simple_preset_params(preset))
            cfg["ui_mode"] = "simple"
            cfg["preset"] = preset
            cfg["NNA"] = "0"
            cfg["RSTAR_OVERRIDES"] = dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})
            return cfg

        iauto = str(self.var_adv_iauto.get() or "0").strip()
        cfg = self._collect_adv_params_for_iauto(iauto)
        cfg["ui_mode"] = "advanced"
        cfg["preset"] = None
        cfg["RSTAR_OVERRIDES"] = dict(getattr(self, "_tlap_manual_rstar_cache", {}) or {})
        return cfg

    def _sync_from_state(self, force: bool = False):
        state_key = (
            str(getattr(self.app.state, "tlap_ui_mode", self.var_ui_mode.get()) or self.var_ui_mode.get()),
            str(getattr(self.app.state, "tlap_simple_preset", self.var_simple_preset.get()) or self.var_simple_preset.get()),
            str(getattr(self.app.state, "tlap_adv_iauto", self.var_adv_iauto.get()) or self.var_adv_iauto.get()),
        )
        if (not force) and getattr(self, '_last_synced', None) == state_key:
            return
        ui_mode, preset, iauto = state_key
        self.var_ui_mode.set(ui_mode if ui_mode in ("simple", "advanced") else "simple")
        self.var_simple_preset.set(preset if preset in ("relaxed", "sensitive") else "relaxed")
        if iauto not in ("0", "1"):
            iauto = "0"
        self.var_adv_iauto.set(iauto)
        self.var_adv_iauto_label.set(self._ADV_IAUTO_LABELS_INV.get(iauto, next(iter(self._ADV_IAUTO_LABELS.keys()))))
        self._refresh_simple_summary()
        self._refresh_tlap_mode_ui(force=force)
        self._last_synced = (self.var_ui_mode.get(), self.var_simple_preset.get(), self.var_adv_iauto.get())

    def _set_running(self, running: bool, hint: str = "") -> None:
        try:
            if running:
                self.lbl_runhint.configure(text=hint or "Running… (TLAP may take a long time)")
                if hasattr(self, "lbl_runtime"):
                    self.lbl_runtime.configure(text=" ")
                self.pb.start(12)
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state="disabled")
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="normal")
            else:
                self.pb.stop()
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state=("normal" if self.app._tlap_ready() else "disabled"))
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="disabled")
            self.update_idletasks()
        except Exception:
            pass

    def set_completion_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runhint"):
                self.lbl_runhint.configure(text=text or "TLAP Simple mode is the default; TLAP execution is available whenever IAUTO = 0 is selected.")
        except Exception:
            pass

    def set_runtime_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runtime"):
                self.lbl_runtime.configure(text=text or "", anchor="w", justify="left")
                try:
                    self.lbl_runtime.update_idletasks()
                except Exception:
                    pass
        except Exception:
            pass

    def _run_tlap_placeholder(self):
        try:
            cfg = self.collect_tlap_config()
            ui_mode = str(cfg.get("ui_mode", "simple") or "simple").strip().lower()
            iauto = str(cfg.get("IAUTO", "0") or "0").strip()
            if iauto != "0":
                messagebox.showinfo("TLAP", f"TLAP execution is currently enabled only for IAUTO = 0. Current IAUTO = {iauto}.")
                return
            if not messagebox.askyesno(
                "Run TLAP",
                "TLAP calculations can be computationally demanding and may take some time to finish.\n\nDo you want to continue?",
                parent=self,
            ):
                return
            run_name = self.app._prompt_tlap_run_name(cfg)
            if run_name is None:
                return
            self.app.state.pending_tlap_run_name = run_name
            cfg["RSTAR_OVERRIDES"] = self._prompt_missing_tlap_rstars(cfg)
            self.app._tlap_last_cfg = dict(cfg)
            summary = getattr(self.app.state, "tlap_last_summary", {}) or {}
            iauto = str(cfg.get("IAUTO", "0"))
            nea_count = summary.get("nea_count", "?")
            active = summary.get("active_shells", "?")
            vscc = summary.get("vscc", cfg.get("VSCC", False))
            self.app.task_log(f"[TLAP] mode={ui_mode} | IAUTO={iauto} | NEAs={nea_count} | VSCC={vscc} | active_shells={active}")
            self.app.run_tlap(cfg)
        except Exception as e:
            self.app.state.pending_tlap_run_name = ""
            messagebox.showerror("TLAP", str(e))

    def refresh_state(self):
        self._sync_from_state(force=False)
        self._refresh_active_trho_selector()
        self._refresh_active_tlap_selector()
        ready_run = self.app._tlap_ready()
        self.btn_run.state(["!disabled"] if ready_run else ["disabled"])

        if self.app._job_running and str(getattr(self.app, "_active_job_kind", "") or "").upper() == "TLAP":
            self._set_running(True, "Running… (TLAP may take a long time)")
        else:
            self._set_running(False)
            self.set_completion_text("▶ TLAP Simple mode ready")


class PL2DPage(BasePage):
    """PL2D configuration + runner (v2026)

    Design goals:
    - User defines XY square region by x_min, y_min, L (side length), and a single xy_inc.
    - The app snaps L to the nearest grid so Nx == Ny always.
    - Z region by z_min, z_max and n_slices (10..200).
    - Outputs are .DAT slices for selected isosurface types.
    - Existing runs are reused ONLY if the full parameter signature matches (manifest hash).
    """

    ISO_TYPES = [
        ("SURFRHOO", "ρ (electron density)"),
        ("SURFGRHO", "|∇ρ| (grad rho)"),
        ("SURFLAPM", "∇²ρ (Laplacian -)"),
        ("SURFLAPP", "∇²ρ (Laplacian +)"),
        ("SURFSPDE", "spin density"),
        ("SURFELFB", "ELF"),
        ("SURFGKIN", "G kinetic"),
        ("SURFKKIN", "K kinetic"),
        ("SURFVIRI", "Virial"),
    ]

    def _build(self):
        ttk.Label(self, text="PL2D (Slices) — region + isosurfaces", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=14, pady=(14, 8))
        ttk.Separator(self).pack(fill="x", padx=14, pady=(0, 8))

        outer, canvas, vbar, body = _make_scrollable_frame(self)
        outer.pack(fill="both", expand=True, padx=14, pady=8)
        self._scroll_outer = outer
        self._scroll_canvas = canvas
        self._scroll_vbar = vbar
        self._scroll_inner = body

        # --- XY square region ---
        frm_xy = ttk.LabelFrame(body, text="XY square region (Å)")
        frm_xy.pack(fill="x", pady=(0, 10))

        # Region modes (requested):
        # - Min+L: user provides x_min, y_min, L
        # - Min/Max: user provides x_min, x_max, y_min, y_max (we keep square)
        # - Center+L: user provides x_center, y_center, L
        # - Atom+L: center on an atom label + L
        # - BCP+L: center on a BCP id + L
        # - RCP+L: center on a RCP id + L
        # - CCP+L: center on a CCP id + L
        self.var_xy_mode = tk.StringVar(value="Min+L")

        self.var_xmin = tk.StringVar(value="0.0")
        self.var_ymin = tk.StringVar(value="0.0")
        self.var_xmax = tk.StringVar(value="5.0")
        self.var_ymax = tk.StringVar(value="5.0")
        self.var_L = tk.StringVar(value="2.0")
        self.var_inc = tk.StringVar(value="0.05")

        self.var_ref_kind = tk.StringVar(value="Atom")
        self.var_ref_id = tk.StringVar(value="1")

        # --- Row A: Mode + x/y (min or center) + L + xy_inc ---
        rowA = ttk.Frame(frm_xy)
        rowA.pack(fill="x", padx=10, pady=(8, 4))

        ttk.Label(rowA, text="Mode").pack(side="left", padx=(0, 6))
        self.cmb_xy_mode = ttk.Combobox(
            rowA,
            textvariable=self.var_xy_mode,
            values=("Min+L", "Min/Max", "Center+L", "Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L"),
            width=10,
            state="readonly",
        )
        self.cmb_xy_mode.pack(side="left", padx=(0, 14))
        self.cmb_xy_mode.bind("<<ComboboxSelected>>", lambda *_: self._on_xy_mode())
        # Also react to programmatic changes (e.g., restoring saved configs)
        self.var_xy_mode.trace_add("write", lambda *_: self._on_xy_mode())

        # These entries switch meaning between x_min/y_min and x_center/y_center.
        self.ent_x1 = _labeled_entry(rowA, "x_min", self.var_xmin, width=12)
        self.ent_x1.pack(side="left", padx=(0, 10))
        self.ent_y1 = _labeled_entry(rowA, "y_min", self.var_ymin, width=12)
        self.ent_y1.pack(side="left", padx=(0, 10))
        self.ent_L = _labeled_entry(rowA, "L (side)", self.var_L, width=12)
        self.ent_L.pack(side="left", padx=(0, 10))
        self.ent_inc = _labeled_entry(rowA, "xy_inc", self.var_inc, width=12)
        self.ent_inc.pack(side="left", padx=(0, 10))

        # --- Row B: x_max / y_max (only meaningful for Min/Max) ---
        rowB = ttk.Frame(frm_xy)
        rowB.pack(fill="x", padx=10, pady=(0, 6))
        self.ent_x2 = _labeled_entry(rowB, "x_max", self.var_xmax, width=12)
        self.ent_x2.pack(side="left", padx=(0, 10))
        self.ent_y2 = _labeled_entry(rowB, "y_max", self.var_ymax, width=12)
        self.ent_y2.pack(side="left", padx=(0, 10))

        # --- Z + slices ---
        frm_z = ttk.LabelFrame(body, text="Z range + slices")
        frm_z.pack(fill="x", pady=(0, 10))

        self.var_z_mode = tk.StringVar(value="Min/Max")

        self.var_zmin = tk.StringVar(value="0.0")
        self.var_zmax = tk.StringVar(value="5.0")
        self.var_zc = tk.StringVar(value="0.0")
        self.var_Lz = tk.StringVar(value="2.0")

        rowz = ttk.Frame(frm_z)
        rowz.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(rowz, text="Mode").pack(side="left", padx=(0, 6))
        self.cmb_z_mode = ttk.Combobox(
            rowz,
            textvariable=self.var_z_mode,
            values=("Min/Max", "Center+L", "Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L"),
            width=10,
            state="readonly",
        )
        self.cmb_z_mode.pack(side="left", padx=(0, 14))
        self.cmb_z_mode.bind("<<ComboboxSelected>>", lambda *_: self._on_z_mode())
        # Also react to programmatic changes (e.g., restoring saved configs)
        self.var_z_mode.trace_add("write", lambda *_: self._on_z_mode())

        self.ent_z1 = _labeled_entry(rowz, "z_min", self.var_zmin, width=12)
        self.ent_z1.pack(side="left", padx=(0, 10))
        self.ent_z2 = _labeled_entry(rowz, "z_max", self.var_zmax, width=12)
        self.ent_z2.pack(side="left", padx=(0, 10))
        self.ent_zc = _labeled_entry(rowz, "z_center", self.var_zc, width=12)
        self.ent_zc.pack(side="left", padx=(0, 10))
        self.ent_Lz = _labeled_entry(rowz, "Lz", self.var_Lz, width=12)
        self.ent_Lz.pack(side="left", padx=(0, 10))

        # n_slices presets + custom
        self.var_slice_mode = tk.StringVar(value="50")
        self.var_slice_custom = tk.StringVar(value="50")

        rows = ttk.Frame(frm_z)
        rows.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(rows, text="n_slices:").pack(side="left", padx=(0, 8))
        for v in ("10", "50", "100"):
            ttk.Radiobutton(rows, text=v, value=v, variable=self.var_slice_mode, command=self._on_slice_mode).pack(side="left")
        ttk.Radiobutton(rows, text="Custom", value="custom", variable=self.var_slice_mode, command=self._on_slice_mode).pack(side="left", padx=(10, 0))
        self.ent_custom = ttk.Entry(rows, textvariable=self.var_slice_custom, width=6)
        self.ent_custom.pack(side="left", padx=(6, 0))

        self.lbl_slice_hint = ttk.Label(frm_z, text="Limits: min 10, max 200.", foreground="#444")
        self.lbl_slice_hint.pack(anchor="w", padx=12, pady=(0, 6))

        # Shared center target for both XY and Z center-based modes
        frm_center = ttk.LabelFrame(body, text="Center target for XY and/or Z")
        frm_center.pack(fill="x", pady=(0, 10))

        row_ref = ttk.Frame(frm_center)
        row_ref.pack(fill="x", padx=10, pady=(8, 6))
        ttk.Label(row_ref, text="Target").pack(side="left", padx=(0, 8))
        self.cmb_ref_kind = ttk.Combobox(
            row_ref,
            textvariable=self.var_ref_kind,
            values=("Atom", "BCP", "RCP", "CCP", "NNA"),
            width=6,
            state="readonly",
        )
        self.cmb_ref_kind.pack(side="left", padx=(0, 8))
        ttk.Label(row_ref, text="id/label").pack(side="left", padx=(0, 6))
        self.ent_ref_id = ttk.Entry(row_ref, textvariable=self.var_ref_id, width=8)
        self.ent_ref_id.pack(side="left", padx=(0, 8))
        self.btn_use_center = ttk.Button(row_ref, text="Use center", command=self._use_center_from_target)
        self.btn_use_center.pack(side="left", padx=(0, 14))

        self.var_snap = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm_center, text="Snap L to grid (recommended)", variable=self.var_snap, command=self._on_params_changed).pack(anchor="w", padx=12, pady=(0, 6))

        self.lbl_xy_summary = ttk.Label(frm_center, text="", foreground="#444")
        self.lbl_xy_summary.pack(anchor="w", padx=12, pady=(0, 10))

        # --- Isosurface types ---
        frm_iso = ttk.LabelFrame(body, text="Isosurface types (.DAT)")
        frm_iso.pack(fill="x", pady=(0, 10))

        self.iso_vars = {}
        self._iso_bulk = False
        self.var_all_iso = tk.BooleanVar(value=False)

        grid = ttk.Frame(frm_iso)
        grid.pack(fill="x", padx=10, pady=8)
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="iso_cols")

        # All isosurfaces toggle (kept prominent on its own row)
        ttk.Checkbutton(
            grid,
            text="All Isosurfaces",
            variable=self.var_all_iso,
            command=self._toggle_all_isosurfaces,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=(0, 12), pady=(0, 8))

        for i, (key, label) in enumerate(self.ISO_TYPES):
            var = tk.BooleanVar(value=(key == "SURFRHOO"))
            self.iso_vars[key] = var
            cb = ttk.Checkbutton(grid, text=f"{key} — {label}", variable=var, command=self._on_iso_changed)
            row = 1 + (i // 3)
            col = i % 3
            cb.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=2)

        # If all are selected at startup, reflect it (normally false because only SURFRHOO starts checked)
        self._sync_all_isosurfaces_var()

# --- Actions ---
        frm_act = ttk.Frame(body)
        frm_act.pack(fill="x", pady=(2, 0))
        frm_act.columnconfigure(0, weight=3)
        frm_act.columnconfigure(1, weight=2, minsize=390)

        # Left side: optional project name chosen by the user.
        frm_project = ttk.LabelFrame(frm_act, text="PL2D project name")
        frm_project.grid(row=0, column=0, sticky="new", padx=(0, 12), pady=(0, 2))
        frm_project.columnconfigure(0, weight=1)

        self.var_project_name = tk.StringVar(value="")
        self.ent_project_name = ttk.Entry(frm_project, textvariable=self.var_project_name)
        self.ent_project_name.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        ttk.Label(
            frm_project,
            text="Optional. Leave blank to use the automatic TopIso3D name.",
            foreground="#444",
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 6))

        # Right side: execution controls.
        # Use a grid with reserved width so the progress text does not expand over the project-name box.
        frm_run = ttk.Frame(frm_act)
        frm_run.grid(row=0, column=1, sticky="new", pady=(2, 0))
        frm_run.columnconfigure(0, weight=0, minsize=320)
        frm_run.columnconfigure(1, weight=0, minsize=95)

        self.var_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm_run,
            text="Force run (ignore existing)",
            variable=self.var_force,
            command=self._on_params_changed,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        btns_run = ttk.Frame(frm_run)
        btns_run.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.btn_run = ttk.Button(btns_run, text="Run PL2D", command=self._run_pl2d)
        self.btn_run.pack(side="left")

        self.btn_export_campaign = ttk.Button(
            btns_run,
            text="Export campaign",
            command=self._export_pl2d_campaign,
        )
        self.btn_export_campaign.pack(side="left", padx=(8, 0))

        # Progress (per-slice)
        self.pb = ttk.Progressbar(frm_run, orient="horizontal", mode="determinate", length=320)
        self.pb.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        # Progress text (e.g., Done (12/100)) keeps a reserved column, preventing overlap with the left frame.
        self.lbl_pb = ttk.Label(frm_run, text="", width=12, anchor="w")
        self.lbl_pb.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        self.lbl_status = ttk.Label(frm_run, text="▶ PL2D not run", font=("TkDefaultFont", 10, "bold"))
        self.lbl_status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Bind changes
        for var in (
            self.var_xy_mode, self.var_xmin, self.var_ymin, self.var_xmax, self.var_ymax, self.var_L, self.var_inc,
            self.var_ref_kind, self.var_ref_id,
            self.var_z_mode, self.var_zmin, self.var_zmax, self.var_zc, self.var_Lz,
            self.var_slice_custom, self.var_project_name,
        ):
            var.trace_add("write", lambda *_: self._on_params_changed())

        self._on_slice_mode()
        self._on_xy_mode()
        self._on_z_mode()
        self._on_params_changed()
        self.refresh()

    def _on_close(self):
        """Close Reports Viewer cleanly without leaving stale references/callbacks."""
        try:
            if getattr(self.app, "_report_viewer_win", None) is self:
                self.app._report_viewer_win = None
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def refresh(self):
        # Enable only if workspace is ready and TRHO is available/parsed
        ready = self.app.state.workspace_ok and self.app.state.trho_parsed is not None
        # While running, keep a clear status message
        if getattr(self.app.state, 'pl2d_running', False):
            self.lbl_status.configure(text='⏳ PL2D running…')
            self.btn_run.configure(state='disabled')
            if hasattr(self, "btn_export_campaign"):
                self.btn_export_campaign.configure(state='disabled')
            return

        self.btn_run.configure(state=("normal" if ready else "disabled"))
        if hasattr(self, "btn_export_campaign"):
            self.btn_export_campaign.configure(state=("normal" if ready else "disabled"))

        if not self.app.state.workspace_ok:
            self.lbl_status.configure(text="▶ Choose a workspace first")
        elif self.app.state.trho_parsed is None:
            self.lbl_status.configure(text="▶ Run/parse TRHO first")
        else:
            # Status based on detection
            self._update_existing_detection()

    def _on_slice_mode(self):
        mode = self.var_slice_mode.get()
        self.ent_custom.configure(state=("normal" if mode == "custom" else "disabled"))
        self._on_params_changed()

    def _get_n_slices(self) -> int:
        mode = self.var_slice_mode.get()
        if mode == "custom":
            return int(float(self.var_slice_custom.get()))
        return int(mode)

    def _parse_float(self, s: str) -> float:
        return float(s.strip().replace(",", "."))

    def _compute_xy(self):
        mode = (self.var_xy_mode.get() or "Min+L").strip()
        L = self._parse_float(self.var_L.get())
        inc = self._parse_float(self.var_inc.get())
        if inc <= 0:
            raise ValueError("xy_inc must be > 0")
        if L <= 0:
            raise ValueError("L must be > 0")

        if mode == "Min+L":
            xmin = self._parse_float(self.var_xmin.get())
            ymin = self._parse_float(self.var_ymin.get())
        elif mode == "Min/Max":
            xmin = self._parse_float(self.var_xmin.get())
            ymin = self._parse_float(self.var_ymin.get())
            xmax_in = self._parse_float(self.var_xmax.get())
            ymax_in = self._parse_float(self.var_ymax.get())
            Lx = xmax_in - xmin
            Ly = ymax_in - ymin
            if Lx <= 0 or Ly <= 0:
                raise ValueError("x_max/y_max must be greater than x_min/y_min")
            L = min(Lx, Ly)
        else:
            xc = self._parse_float(self.var_xmin.get())
            yc = self._parse_float(self.var_ymin.get())
            xmin = xc - L / 2.0
            ymin = yc - L / 2.0

        # Snap L to grid so Nx == Ny always
        N = int(round(L / inc)) + 1
        if N < 2:
            N = 2
        L_eff = (N - 1) * inc
        if not self.var_snap.get():
            # Without snapping, we still enforce Nx==Ny but don't modify L in UI;
            # x_max/y_max will use L_eff anyway to keep the grid consistent.
            pass
        xmax = xmin + L_eff
        ymax = ymin + L_eff
        return xmin, xmax, ymin, ymax, inc, N, L, L_eff

    def _compute_z(self):
        mode = (self.var_z_mode.get() or "Min/Max").strip()
        if mode == "Min/Max":
            zmin = self._parse_float(self.var_zmin.get())
            zmax = self._parse_float(self.var_zmax.get())
        else:
            zc = self._parse_float(self.var_zc.get())
            Lz = self._parse_float(self.var_Lz.get())
            if Lz <= 0:
                raise ValueError("Lz must be > 0")
            zmin = zc - Lz / 2.0
            zmax = zc + Lz / 2.0
        if zmax <= zmin:
            raise ValueError("z_max must be > z_min")
        return zmin, zmax

    def _on_xy_mode(self):
        # Always trust what the user sees in the combobox (avoids state desync)
        try:
            mode = (self.cmb_xy_mode.get() or self.var_xy_mode.get() or "Min+L").strip()
        except Exception:
            mode = (self.var_xy_mode.get() or "Min+L").strip()

        # Update label text for the first two fields (min vs center).
        want_center = mode in ("Center+L", "Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L")
        try:
            getattr(self.ent_x1, "_lbl").configure(text=("x_center" if want_center else "x_min"))
            getattr(self.ent_y1, "_lbl").configure(text=("y_center" if want_center else "y_min"))
        except Exception:
            pass

        # Enable/disable fields based on mode (UX: guide the user).
        is_minmax = (mode == "Min/Max")
        is_center_manual = (mode == "Center+L")
        is_auto_center = mode in ("Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L")
        is_minL = (mode == "Min+L")

        # x_min/y_min or x_center/y_center:
        # - enabled in manual modes; disabled in auto-center modes
        _set_labeled_state(self.ent_x1, enabled=(not is_auto_center))
        _set_labeled_state(self.ent_y1, enabled=(not is_auto_center))

        # x_max/y_max only for Min/Max
        _set_labeled_state(self.ent_x2, enabled=is_minmax)
        _set_labeled_state(self.ent_y2, enabled=is_minmax)

        # L is used in Min+L, Center+L, Atom+L, BCP+L (not in Min/Max)
        _set_labeled_state(self.ent_L, enabled=(not is_minmax))

        # xy_inc always used
        _set_labeled_state(self.ent_inc, enabled=True)

        # Target center controls enabled only for Atom/BCP modes
        if is_auto_center:
            self.cmb_ref_kind.configure(state="readonly")
            self.ent_ref_id.configure(state="normal")
            self.btn_use_center.configure(state="normal")
            if mode == "Atom+L":
                self.var_ref_kind.set("Atom")
            elif mode == "BCP+L":
                self.var_ref_kind.set("BCP")
            elif mode == "RCP+L":
                self.var_ref_kind.set("RCP")
            elif mode == "CCP+L":
                self.var_ref_kind.set("CCP")
            elif mode == "NNA+L":
                self.var_ref_kind.set("NNA")
            elif mode == "NNA+L":
                self.var_ref_kind.set("NNA")
        else:
            self.cmb_ref_kind.configure(state="disabled")
            self.ent_ref_id.configure(state="disabled")
            self.btn_use_center.configure(state="disabled")

        self._on_params_changed()

    def _on_z_mode(self):
        mode = (self.var_z_mode.get() or "Min/Max").strip()
        is_minmax = (mode == "Min/Max")
        is_auto_center = mode in ("Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L")

        # In Min/Max: z_min/z_max enabled, z_center/Lz disabled
        _set_labeled_state(self.ent_z1, enabled=is_minmax)
        _set_labeled_state(self.ent_z2, enabled=is_minmax)
        _set_labeled_state(self.ent_zc, enabled=(not is_minmax and not is_auto_center))
        _set_labeled_state(self.ent_Lz, enabled=(not is_minmax))

        # Share the same target selector used for XY auto-centering.
        # If Z is in an auto-center mode, keep the selector aligned with that mode.
        if is_auto_center:
            self.cmb_ref_kind.configure(state="readonly")
            self.ent_ref_id.configure(state="normal")
            self.btn_use_center.configure(state="normal")
            if mode == "Atom+L":
                self.var_ref_kind.set("Atom")
            elif mode == "BCP+L":
                self.var_ref_kind.set("BCP")
            elif mode == "RCP+L":
                self.var_ref_kind.set("RCP")
            elif mode == "CCP+L":
                self.var_ref_kind.set("CCP")
            elif mode == "NNA+L":
                self.var_ref_kind.set("NNA")
            elif mode == "NNA+L":
                self.var_ref_kind.set("NNA")
        else:
            # Only disable the shared target controls when XY also does not use them.
            try:
                xy_mode = (self.var_xy_mode.get() or "Min+L").strip()
            except Exception:
                xy_mode = "Min+L"
            xy_auto = xy_mode in ("Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L")
            if not xy_auto:
                self.cmb_ref_kind.configure(state="disabled")
                self.ent_ref_id.configure(state="disabled")
                self.btn_use_center.configure(state="disabled")

        self._on_params_changed()
    def _use_center_from_target(self):
        """Fill x_center/y_center (and z_center if relevant) from Atom label, BCP/RCP/CCP id.

        Notes:
          - TRUE atoms live in self.app.state.df_true_atoms with columns *_ANGSTROM and index starting at 1.
          - BCP coordinates live in self.app.state.df_bcp_props (preferred) or df_bcp_coords, also indexed from 1.
            - RCP/CCP coordinates live in parsed TRHO tables (df_rcp_props / df_ccp_props) and may also be cached in app.state.
          - In center modes, the GUI stores x_center/y_center inside var_xmin/var_ymin (labels change dynamically).
        """
        # Need TRHO parsed/loaded
        if self.app.state.trho_parsed is None and getattr(self.app.state, "df_true_atoms", None) is None:
            return

        kind = (self.var_ref_kind.get() or "Atom").strip()
        rid = (self.var_ref_id.get() or "").strip()
        if not rid:
            return

        # parse integer id from user input
        m = re.search(r"(\d+)", rid)
        if not m:
            return
        idx = int(m.group(1))

        def _pick_xyz(row):
            """Return (x,y,z) in Å from a pandas row with flexible column names."""
            for cx, cy, cz in (
                ("X_ANGSTROM", "Y_ANGSTROM", "Z_ANGSTROM"),
                ("x", "y", "z"),
                ("X", "Y", "Z"),
            ):
                if cx in row and cy in row and cz in row:
                    return float(row[cx]), float(row[cy]), float(row[cz])
            raise KeyError("No XYZ columns found")

        def _set_center(xc, yc, zc):
            # Apply only to dimensions currently using center-based modes.
            xy_mode = (self.var_xy_mode.get() or "Min+L").strip()
            if xy_mode in ("Center+L", "Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L"):
                # In center modes, x_center/y_center are stored in var_xmin/var_ymin
                self.var_xmin.set(f"{xc:.6f}")
                self.var_ymin.set(f"{yc:.6f}")
            z_mode = (self.var_z_mode.get() or "Min/Max").strip()
            if z_mode in ("Center+L", "Atom+L", "BCP+L", "RCP+L", "CCP+L", "NNA+L"):
                self.var_zc.set(f"{zc:.6f}")

        try:
            if kind == "Atom":
                df = getattr(self.app.state, "df_true_atoms", None)
                if df is None or getattr(df, "empty", True):
                    return
                if idx not in df.index:
                    return
                r0 = df.loc[idx]
                xc, yc, zc = _pick_xyz(r0)
                _set_center(xc, yc, zc)
            elif kind == "BCP":
                df = getattr(self.app.state, "df_bcp_props", None)
                if df is None or getattr(df, "empty", True):
                    parsed = getattr(self.app.state, "trho_parsed", None)
                    df = (getattr(parsed, "df_bcp_props", None) if parsed is not None else None)
                if df is None or getattr(df, "empty", True):
                    df = getattr(self.app.state, "df_bcp_coords", None)
                if df is None or getattr(df, "empty", True):
                    return
                if idx in df.index:
                    r0 = df.loc[idx]
                elif 1 <= idx <= len(df):
                    r0 = df.iloc[idx - 1]
                else:
                    return
                xc, yc, zc = _pick_xyz(r0)
                _set_center(xc, yc, zc)

            elif kind == "RCP":
                df = getattr(self.app.state, "df_rcp_props", None)
                if df is None or getattr(df, "empty", True):
                    parsed = getattr(self.app.state, "trho_parsed", None)
                    df = (getattr(parsed, "df_rcp_props", None) if parsed is not None else None)
                if df is None or getattr(df, "empty", True):
                    return
                if idx in df.index:
                    r0 = df.loc[idx]
                elif 1 <= idx <= len(df):
                    r0 = df.iloc[idx - 1]
                else:
                    return
                xc, yc, zc = _pick_xyz(r0)
                _set_center(xc, yc, zc)

            elif kind == "CCP":
                df = getattr(self.app.state, "df_ccp_props", None)
                if df is None or getattr(df, "empty", True):
                    parsed = getattr(self.app.state, "trho_parsed", None)
                    df = (getattr(parsed, "df_ccp_props", None) if parsed is not None else None)
                if df is None or getattr(df, "empty", True):
                    return
                if idx in df.index:
                    r0 = df.loc[idx]
                elif 1 <= idx <= len(df):
                    r0 = df.iloc[idx - 1]
                else:
                    return
                xc, yc, zc = _pick_xyz(r0)
                _set_center(xc, yc, zc)
            elif kind == "NNA":
                parsed = getattr(self.app.state, "trho_parsed", None)
                df = (getattr(parsed, "df_att_nao_nucl", None) if parsed is not None else None)
                if df is None or getattr(df, "empty", True):
                    return
                if "CP_ID" in df.columns:
                    hit = df.loc[df["CP_ID"] == idx]
                    if not hit.empty:
                        r0 = hit.iloc[0]
                    elif 1 <= idx <= len(df):
                        r0 = df.iloc[idx - 1]
                    else:
                        return
                elif idx in df.index:
                    r0 = df.loc[idx]
                elif 1 <= idx <= len(df):
                    r0 = df.iloc[idx - 1]
                else:
                    return
                xc, yc, zc = _pick_xyz(r0)
                _set_center(xc, yc, zc)
        except Exception:
            return

    def _collect_iso(self):
        selected = [k for k, v in self.iso_vars.items() if v.get()]
        return sorted(selected)

    def _effective_project_name(self) -> str:
        raw = str(self.var_project_name.get() or "").strip()
        if raw:
            return " ".join(raw.split())
        ctx = self.app.state
        if getattr(ctx, "workspace_dir", None):
            return ctx.workspace_dir.name
        return "PL2D"

    def _project_name_is_custom(self) -> bool:
        return bool(str(self.var_project_name.get() or "").strip())

    def _safe_project_slug(self, name: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())
        slug = re.sub(r"_+", "_", slug).strip("._-")
        return slug[:64] or "PL2D"

    def _sync_all_isosurfaces_var(self):
        """Keep 'All isosurfaces' checkbox consistent with individual selections."""
        if not hasattr(self, "var_all_iso"):
            return
        try:
            all_on = all(v.get() for v in self.iso_vars.values()) if self.iso_vars else False
        except Exception:
            all_on = False
        self._iso_bulk = True
        try:
            self.var_all_iso.set(all_on)
        finally:
            self._iso_bulk = False

    def _toggle_all_isosurfaces(self):
        if self._iso_bulk:
            return
        val = bool(self.var_all_iso.get())
        self._iso_bulk = True
        try:
            for v in self.iso_vars.values():
                v.set(val)
        finally:
            self._iso_bulk = False
        self._on_params_changed()

    def _on_iso_changed(self):
        if not self._iso_bulk:
            self._sync_all_isosurfaces_var()
        self._on_params_changed()

    def _build_config(self):
        xmin, xmax, ymin, ymax, inc, N, L_in, L_eff = self._compute_xy()
        zmin, zmax = self._compute_z()

        ns = self._get_n_slices()
        if ns < 10 or ns > 200:
            raise ValueError("n_slices must be between 10 and 200")

        iso = self._collect_iso()
        if not iso:
            raise ValueError("Select at least one isosurface type")

        cfg = {
            "xmin": xmin, "xmax": xmax,
            "ymin": ymin, "ymax": ymax,
            "inc": inc,
            "N": N,
            "L_in": L_in, "L_eff": L_eff,
            "zmin": zmin, "zmax": zmax,
            "n_slices": ns,
            "iso": iso,
            "snap": bool(self.var_snap.get()),
            "project_name": self._effective_project_name(),
            "project_name_custom": self._project_name_is_custom(),
        }
        return cfg

    def _signature(self, cfg: dict) -> str:
        # Only what defines the grid and outputs
        import hashlib, json as _json
        payload = {
            "xmin": round(cfg["xmin"], 12),
            "xmax": round(cfg["xmax"], 12),
            "ymin": round(cfg["ymin"], 12),
            "ymax": round(cfg["ymax"], 12),
            "inc": round(cfg["inc"], 12),
            "zmin": round(cfg["zmin"], 12),
            "zmax": round(cfg["zmax"], 12),
            "n_slices": int(cfg["n_slices"]),
            "iso": list(cfg["iso"]),
        }
        if bool(cfg.get("project_name_custom", False)):
            payload["project_name"] = str(cfg.get("project_name", "") or "")
            payload["project_name_custom"] = True
        b = _json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha1(b).hexdigest()

    def _runs_root(self) -> Optional[Path]:
        wd = self.app.state.workspace_dir
        if wd is None:
            return None
        return wd / "pl2d_runs"

    def _find_existing(self, sig: str):
        root = self._runs_root()
        if root is None or not root.exists():
            return None
        for run_dir in sorted(root.glob("*")):
            mf = run_dir / "manifest.json"
            if not mf.exists():
                continue
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("signature") == sig and data.get("status") == "complete":
                return run_dir
        return None

    def _validate_run(self, run_dir: Path, cfg: dict) -> bool:
        # minimal sanity: for each expected slice, ensure at least one selected .DAT exists
        ns = int(cfg["n_slices"])
        iso = cfg["iso"]
        for i in range(ns):
            sdir = run_dir / f"slice{i:03d}"
            if not sdir.exists():
                return False
            for key in iso:
                if not (sdir / f"{key}.DAT").exists():
                    return False
        return True

    def _on_params_changed(self):
        # Update computed summary + detection
        try:
            cfg = self._build_config()
            xmin, xmax, ymin, ymax, inc, N, L_in, L_eff = self._compute_xy()
            zmin, zmax = self._compute_z()
            adj = " (snapped)" if abs(L_eff - L_in) > 1e-10 and self.var_snap.get() else ""
            mode = (self.var_xy_mode.get() or "Min+L").strip()
            self.lbl_xy_summary.configure(
                text=f"Mode={mode} | Nx=Ny={N} | Effective L={L_eff:.6f}{adj} | x:[{xmin:.6f},{xmax:.6f}] | y:[{ymin:.6f},{ymax:.6f}] | z:[{zmin:.6f},{zmax:.6f}]"
            )
            self.app.state.pl2d_cfg = cfg
            self.app.state.pl2d_signature = self._signature(cfg)
            self._update_existing_detection()
        except Exception as e:
            self.lbl_xy_summary.configure(text=f"Invalid parameters: {e}")
            self.lbl_status.configure(text="▶ PL2D not run")
            self.app.state.pl2d_cfg = None
            self.app.state.pl2d_signature = None

    def _update_existing_detection(self):
        if not (self.app.state.workspace_ok and self.app.state.trho_parsed is not None):
            return
        cfg = getattr(self.app.state, "pl2d_cfg", None)
        sig = getattr(self.app.state, "pl2d_signature", None)
        if not cfg or not sig:
            self.lbl_status.configure(text="▶ PL2D not run")
            return

        if getattr(self.app.state, 'pl2d_running', False):
            self.lbl_status.configure(text='⏳ PL2D running…')
            return


        if self.var_force.get():
            self.lbl_status.configure(text="▶ PL2D not run (force)")
            return

        run_dir = self._find_existing(sig)
        if run_dir and self._validate_run(run_dir, cfg):
            self.app.state.pl2d_run_dir = run_dir
            self.lbl_status.configure(text="✔ PL2D existing")
        else:
            self.app.state.pl2d_run_dir = None
            self.lbl_status.configure(text="▶ PL2D not run")


def _create_pl2d_run_dir(self, root: Path, cfg: dict, sig: str) -> tuple[str, Path]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    user_project_name = str(cfg.get("project_name") or "").strip() if bool(cfg.get("project_name_custom", False)) else ""
    if user_project_name:
        base_run_name = self._safe_project_slug(user_project_name) or "PL2D"
        run_name = base_run_name
        run_dir = root / run_name
        counter = 2
        while run_dir.exists():
            run_name = f"{base_run_name}_{counter}"
            run_dir = root / run_name
            counter += 1
    else:
        run_name = f"{sig[:10]}_{cfg['n_slices']:03d}_{ts}"
        run_dir = root / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    return ts, run_dir

def _compute_pl2d_zs(self, cfg: dict) -> list[float]:
    ns = int(cfg["n_slices"])
    zmin = float(cfg["zmin"])
    zmax = float(cfg["zmax"])
    if ns <= 0:
        return [zmin]
    dz = (zmax - zmin) / ns
    return [zmin + i * dz for i in range(ns + 1)]

def _pl2d_flags_line(self, cfg: dict) -> str:
    iso_set = set(cfg["iso"])
    flags = [
        1 if "SURFRHOO" in iso_set else 0,
        1 if "SURFSPDE" in iso_set else 0,
        1 if "SURFLAPP" in iso_set else 0,
        1 if "SURFLAPM" in iso_set else 0,
        1 if "SURFGRHO" in iso_set else 0,
        1 if "SURFKKIN" in iso_set else 0,
        1 if "SURFGKIN" in iso_set else 0,
        1 if "SURFVIRI" in iso_set else 0,
        1 if "SURFELFB" in iso_set else 0,
        0, 0, 0,
    ]
    return ",".join(str(x) for x in flags)

def _write_pl2d_input_for_slice(self, sdir: Path, z: float, cfg: dict, *, out_name: str) -> Path:
    bohr_to_ang = float(getattr(self.app.state, "bohr_to_ang", 0.5291772083))
    a_coord = (0.0, 0.0)
    b_coord = (1.0, 0.0)
    c_coord = (0.0, 1.0)
    xmin = float(cfg["xmin"])
    xmax = float(cfg["xmax"])
    ymin = float(cfg["ymin"])
    ymax = float(cfg["ymax"])
    inc = float(cfg["inc"])
    flags_line = self._pl2d_flags_line(cfg)
    inp = sdir / "pl2d.inp"
    with open(inp, "w", encoding="utf-8") as f:
        pl2d_text = (
            "TOPO\n"
            "PL2D\n"
            "0\n"
            f"{a_coord[0]/bohr_to_ang} {a_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
            "0\n"
            f"{b_coord[0]/bohr_to_ang} {b_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
            "0\n"
            f"{c_coord[0]/bohr_to_ang} {c_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
            "3\n"
            "0\n"
            "30,15.0\n"
            "1\n"
            f"{xmin} {xmax} {inc}\n"
            f"{ymin} {ymax} {inc}\n"
            f"{flags_line}\n"
            f"{out_name}\n"
            "1\n"
            "2.,0.0,0\n"
            "2.2,1,1,1\n"
            "36,0\n"
            "END\n"
        )
        f.write(pl2d_text)
    return inp



def _write_pl2d_unix_scripts(self, run_dir: Path, cfg: dict) -> None:
    slice_dirs = sorted([p.name for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("slice")])
    dat_names = []
    for key in cfg.get("iso", []):
        dat_name = f"{key}.DAT"
        if dat_name not in dat_names:
            dat_names.append(dat_name)
    dat_glob = " ".join(dat_names) if dat_names else "*.DAT"

    max_index = max(0, len(slice_dirs) - 1)
    jobs_default = min(10, max(1, len(slice_dirs))) if slice_dirs else 1
    local_platform = get_platform_name()

    run_all = textwrap.dedent(f"""        #!/usr/bin/env bash
        set -euo pipefail

        ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
        PROPERTIES_EXE="${{PROPERTIES_EXE:-properties}}"
        FORT9_SOURCE="${{FORT9_SOURCE:-$ROOT_DIR/fort.9}}"
        MODULE_LOAD_CMD="${{MODULE_LOAD_CMD:-}}"

        if [[ -n "$MODULE_LOAD_CMD" ]]; then
          eval "$MODULE_LOAD_CMD"
        fi

        if [[ ! -f "$FORT9_SOURCE" ]]; then
          echo "fort.9 not found. Place fort.9 in the campaign root or set FORT9_SOURCE." >&2
          exit 1
        fi

        for d in "$ROOT_DIR"/slice*; do
          [[ -d "$d" ]] || continue
          cp -f "$FORT9_SOURCE" "$d/fort.9"
          (
            cd "$d"
            "$PROPERTIES_EXE" < pl2d.inp > pl2d.out 2> pl2d.err
            rm -f fort.9 fort.3 fort.11 fort.13
          )
        done

        echo "PL2D campaign finished. Expected data files: {dat_glob}"
        """)

    run_parallel = textwrap.dedent(f"""        #!/usr/bin/env bash
        set -euo pipefail

        ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
        PROPERTIES_EXE="${{PROPERTIES_EXE:-properties}}"
        FORT9_SOURCE="${{FORT9_SOURCE:-$ROOT_DIR/fort.9}}"
        MODULE_LOAD_CMD="${{MODULE_LOAD_CMD:-}}"
        JOBS="${{JOBS:-{jobs_default}}}"

        if [[ -n "$MODULE_LOAD_CMD" ]]; then
          eval "$MODULE_LOAD_CMD"
        fi

        if [[ ! -f "$FORT9_SOURCE" ]]; then
          echo "fort.9 not found. Place fort.9 in the campaign root or set FORT9_SOURCE." >&2
          exit 1
        fi

        export ROOT_DIR PROPERTIES_EXE FORT9_SOURCE
        find "$ROOT_DIR" -maxdepth 1 -type d -name 'slice*' | sort | xargs -I{{}} -P "$JOBS" bash -c '
          d="$1"
          cp -f "$FORT9_SOURCE" "$d/fort.9"
          cd "$d"
          "$PROPERTIES_EXE" < pl2d.inp > pl2d.out 2> pl2d.err
          rm -f fort.9 fort.3 fort.11 fort.13
        ' _ {{}}

        echo "PL2D parallel campaign finished. Expected data files: {dat_glob}"
        """)

    submit_slurm = textwrap.dedent(f"""        #!/usr/bin/env bash
        #SBATCH --job-name=pl2d_campaign
        #SBATCH --output=slurm_%A_%a.out
        #SBATCH --error=slurm_%A_%a.err
        #SBATCH --array=0-{max_index}%{jobs_default}
        #SBATCH --ntasks=1
        #SBATCH --cpus-per-task=1

        set -euo pipefail
        ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
        PROPERTIES_EXE="${{PROPERTIES_EXE:-properties}}"
        FORT9_SOURCE="${{FORT9_SOURCE:-$ROOT_DIR/fort.9}}"
        MODULE_LOAD_CMD="${{MODULE_LOAD_CMD:-module load crystal}}"
        SLICE_DIR=$(printf "%s/slice%03d" "$ROOT_DIR" "$SLURM_ARRAY_TASK_ID")

        if [[ -n "$MODULE_LOAD_CMD" ]]; then
          eval "$MODULE_LOAD_CMD"
        fi

        if [[ ! -d "$SLICE_DIR" ]]; then
          echo "Missing slice directory: $SLICE_DIR" >&2
          exit 1
        fi
        if [[ ! -f "$FORT9_SOURCE" ]]; then
          echo "fort.9 not found. Place fort.9 in the campaign root or set FORT9_SOURCE." >&2
          exit 1
        fi

        cp -f "$FORT9_SOURCE" "$SLICE_DIR/fort.9"
        cd "$SLICE_DIR"
        "$PROPERTIES_EXE" < pl2d.inp > pl2d.out 2> pl2d.err
        rm -f fort.9 fort.3 fort.11 fort.13
        """)

    cleanup_for_return = textwrap.dedent("""        #!/usr/bin/env bash
        set -euo pipefail

        ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

        echo "Cleaning PL2D campaign for return/export from: $ROOT_DIR"

        # Remove per-slice temporary files created only for execution.
        for d in "$ROOT_DIR"/slice*; do
          [[ -d "$d" ]] || continue
          rm -f "$d"/fort.9 "$d"/fort.* "$d"/*.LOG "$d"/INPUT
        done

        # Remove root-level execution leftovers that are usually not needed back in TopIso3D.
        rm -f "$ROOT_DIR"/fort.9
        rm -f "$ROOT_DIR"/slurm-*.out "$ROOT_DIR"/slurm-*.err
        rm -f "$ROOT_DIR"/slurm_*.out "$ROOT_DIR"/slurm_*.err

        echo "Cleanup finished."
        echo "Kept files per slice: pl2d.inp, pl2d.out, pl2d.err, *.DAT"
        """)

    readme_run = textwrap.dedent(f"""        TopIso3D PL2D campaign
        ======================

        Local platform used to export this campaign: {local_platform}
        Run directory: {run_dir.name}
        Number of slices: {len(slice_dirs)}
        Slice folders: slice000 ... slice{max_index:03d}

        Files included
        --------------
        - manifest.json
        - fort.9
        - sliceXXX/pl2d.inp
        - run_all.sh
        - run_parallel.sh
        - submit_slurm.sh
        - cleanup_for_return.sh

        Recommended usage
        -----------------
        1) Windows:
           Generate the campaign locally, transfer the whole folder to a Linux/macOS
           machine or cluster, and execute the shell scripts there.

        2) Linux/macOS local execution:
           chmod +x run_all.sh run_parallel.sh cleanup_for_return.sh
           bash run_all.sh

        3) Linux/macOS parallel local execution:
           chmod +x run_parallel.sh
           JOBS={jobs_default} bash run_parallel.sh

        4) Slurm cluster execution:
           chmod +x submit_slurm.sh
           sbatch submit_slurm.sh

        Environment notes
        -----------------
        - The scripts expect a CRYSTAL/TOPOND properties executable.
        - By default they call: properties
        - You can override it with:
              PROPERTIES_EXE=/full/path/to/properties bash run_all.sh
        - For cluster environments that require module loading, use:
              MODULE_LOAD_CMD="module load crystal" sbatch submit_slurm.sh
          The exported Slurm script already defaults to that module-load pattern.

        fort.9 handling
        ---------------
        - fort.9 must be present in the campaign root.
        - Each slice receives a temporary copy of fort.9 before execution.
        - Temporary fort.* files created during execution are removed automatically.

        Return / back-export
        --------------------
        After the campaign finishes, you can reduce the amount of transferred files with:
            chmod +x cleanup_for_return.sh
            bash cleanup_for_return.sh

        This cleanup keeps, inside each slice:
        - pl2d.inp
        - pl2d.out
        - pl2d.err
        - {dat_glob}

        Important
        ---------
        This campaign was generated to be OS-agnostic at the preparation level.
        Shell execution itself must be performed on a Unix-like environment
        (Linux, macOS, or a Linux cluster).
        """)

    for name, content in {
        "run_all.sh": run_all,
        "run_parallel.sh": run_parallel,
        "submit_slurm.sh": submit_slurm,
        "cleanup_for_return.sh": cleanup_for_return,
        "README_RUN.txt": readme_run,
    }.items():
        path = run_dir / name
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o755)
        except Exception:
            pass


def _export_pl2d_campaign(self):
    """Phase 2: generate a full PL2D campaign without executing properties locally."""
    if not (self.app.state.workspace_ok and self.app.state.trho_parsed is not None):
        messagebox.showwarning("PL2D", "Run/parse TRHO first.")
        return

    try:
        cfg = self._build_config()
    except Exception as e:
        messagebox.showerror("PL2D", f"Invalid configuration: {e}")
        return

    ctx = self.app.state
    fort9_src = None
    if ctx.workspace_dir:
        cand = ctx.workspace_dir / "fort.9"
        if cand.exists():
            fort9_src = cand
    if fort9_src is None:
        messagebox.showerror("PL2D", "fort.9 not found in workspace. Make sure the workspace has fort.9 first.")
        return

    sig = self._signature(cfg)
    root = self._runs_root()
    root.mkdir(parents=True, exist_ok=True)

    try:
        ts, run_dir = self._create_pl2d_run_dir(root, cfg, sig)
        zs = self._compute_pl2d_zs(cfg)
        out_name = str(cfg.get("project_name") or (ctx.workspace_dir.name if ctx.workspace_dir else "PL2D"))

        try:
            f9_stat = fort9_src.stat()
            f9_fp = {"size": int(f9_stat.st_size), "mtime": int(f9_stat.st_mtime)}
        except Exception:
            f9_fp = {}

        mf = {
            "signature": sig,
            "created_at": ts,
            "config": cfg,
            "engine": "properties",
            "properties_exe": "properties",
            "project_name": str(cfg.get("project_name", "") or "") if bool(cfg.get("project_name_custom", False)) else "",
            "source": {"fort9": str(fort9_src), "fort9_fp": f9_fp},
            "execution_mode": "exported",
            "status": "ready_for_execution",
            "execution_mode": "exported_campaign",
            "slice_count": len(zs),
            "expected_outputs": [f"{key}.DAT" for key in cfg.get("iso", [])],
        }
        (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")

        shutil.copy2(fort9_src, run_dir / "fort.9")

        for i, z in enumerate(zs):
            sdir = run_dir / f"slice{i:03d}"
            sdir.mkdir()
            self._write_pl2d_input_for_slice(sdir, z, cfg, out_name=out_name)

        self._write_pl2d_unix_scripts(run_dir, cfg)

        self.app.state.pl2d_run_dir = run_dir
        self.lbl_status.configure(text="✔ PL2D campaign exported")
        self.app.set_status(f"PL2D campaign exported: {run_dir.name}")
        self.app.refresh_all_pages()

        messagebox.showinfo(
            "PL2D export campaign",
            "PL2D campaign exported successfully.\n\n"
            f"Run folder: {run_dir.name}\n"
            f"Slices prepared: {len(zs)}\n\n"
            "Files generated:\n"
            "- manifest.json\n"
            "- fort.9\n"
            "- sliceXXX/pl2d.inp\n"
            "- run_all.sh\n"
            "- run_parallel.sh\n"
            "- submit_slurm.sh\n"
            "- cleanup_for_return.sh\n"
            "- README_RUN.txt\n\n"
            "This run is ready for Linux/macOS/cluster execution.\n"
            "On Windows, transfer the campaign and execute the scripts in a Unix-like environment."
        )
    except Exception as e:
        messagebox.showerror("PL2D", f"Failed to export campaign: {e}")

def _cleanup_pl2d_slice_temp_files(self, slice_dir: Path) -> None:
    """Best-effort cleanup after a locally executed PL2D slice."""
    policy = str(getattr(self.app.state, "cleanup_policy", "minimal") or "minimal").strip().lower()
    if policy == "none":
        return
    to_remove = ["fort.9"]
    if policy == "standard":
        to_remove.extend(["fort.3", "fort.11", "fort.13"])
    for fn in to_remove:
        try:
            p = Path(slice_dir) / fn
            if p.exists() or p.is_symlink():
                p.unlink()
        except Exception:
            pass


def _run_pl2d(self):
    """Run PL2D in a background worker so the GUI stays responsive during slices."""
    log_event(self.app.ctx, 'PL2D started')

    if self.app._job_running:
        messagebox.showinfo("PL2D", "A job is already running.")
        return

    if not (self.app.state.workspace_ok and self.app.state.trho_parsed is not None):
        self.app.state.pl2d_running = False
        messagebox.showwarning("PL2D", "Run/parse TRHO first.")
        return

    try:
        cfg = self._build_config()
    except Exception as e:
        self.app.state.pl2d_running = False
        messagebox.showerror("PL2D", f"Invalid configuration: {e}")
        return

    ctx = self.app.state
    local_platform = get_platform_name()
    prop_exe = getattr(ctx, "properties_exe", None)
    exe_path = _best_effort_make_executable(str(prop_exe) if prop_exe is not None else None)
    if not exe_path:
        self.app.state.pl2d_running = False
        messagebox.showerror("PL2D", f"properties executable not found: {prop_exe}")
        return

    fort9_src = None
    if ctx.workspace_dir:
        cand = ctx.workspace_dir / "fort.9"
        if cand.exists():
            fort9_src = cand
    if fort9_src is None:
        self.app.state.pl2d_running = False
        messagebox.showerror("PL2D", "fort.9 not found in workspace. Make sure the workspace has fort.9 first.")
        return

    sig = self._signature(cfg)
    root = self._runs_root()
    root.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    user_project_name = str(cfg.get("project_name") or "").strip() if bool(cfg.get("project_name_custom", False)) else ""
    if user_project_name:
        base_run_name = self._safe_project_slug(user_project_name) or "PL2D"
        run_name = base_run_name
        run_dir = root / run_name
        counter = 2
        while run_dir.exists():
            run_name = f"{base_run_name}_{counter}"
            run_dir = root / run_name
            counter += 1
    else:
        run_name = f"{sig[:10]}_{cfg['n_slices']:03d}_{ts}"
        run_dir = root / run_name
    run_dir.mkdir(parents=True, exist_ok=False)

    try:
        f9_stat = fort9_src.stat()
        f9_fp = {"size": int(f9_stat.st_size), "mtime": int(f9_stat.st_mtime)}
    except Exception:
        f9_fp = {}

    mf = {
        "signature": sig,
        "created_at": ts,
        "config": cfg,
        "engine": "properties",
        "properties_exe": str(exe_path),
        "project_name": str(cfg.get("project_name", "") or "") if bool(cfg.get("project_name_custom", False)) else "",
        "source": {"fort9": str(fort9_src), "fort9_fp": f9_fp},
        "status": "running",
    }
    (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")

    ns = int(cfg["n_slices"])
    zmin = float(cfg["zmin"])
    zmax = float(cfg["zmax"])
    if ns <= 0:
        zs = [zmin]
    else:
        dz = (zmax - zmin) / ns
        zs = [zmin + i * dz for i in range(ns + 1)]

    iso_set = set(cfg["iso"])
    flags = [
        1 if "SURFRHOO" in iso_set else 0,
        1 if "SURFSPDE" in iso_set else 0,
        1 if "SURFLAPP" in iso_set else 0,
        1 if "SURFLAPM" in iso_set else 0,
        1 if "SURFGRHO" in iso_set else 0,
        1 if "SURFKKIN" in iso_set else 0,
        1 if "SURFGKIN" in iso_set else 0,
        1 if "SURFVIRI" in iso_set else 0,
        1 if "SURFELFB" in iso_set else 0,
        0, 0, 0,
    ]
    flags_line = ",".join(str(x) for x in flags)

    bohr_to_ang = float(getattr(ctx, "bohr_to_ang", 0.5291772083))
    a_coord = (0.0, 0.0)
    b_coord = (1.0, 0.0)
    c_coord = (0.0, 1.0)

    xmin = float(cfg["xmin"])
    xmax = float(cfg["xmax"])
    ymin = float(cfg["ymin"])
    ymax = float(cfg["ymax"])
    inc = float(cfg["inc"])

    out_name = str(cfg.get("project_name") or (ctx.workspace_dir.name if ctx.workspace_dir else "PL2D"))

    self.app._job_running = True
    self.app.state.pl2d_running = True
    self.app._active_job_kind = "PL2D"
    self.lbl_status.configure(text='⏳ PL2D running…')
    self.btn_run.configure(state='disabled')
    if hasattr(self, "btn_export_campaign"):
        self.btn_export_campaign.configure(state='disabled')
    try:
        self.pb.configure(maximum=len(zs), value=0)
        self.lbl_pb.configure(text=f"Slice 0/{len(zs)}")
    except Exception:
        pass
    try:
        self.app.set_task(active=False)
    except Exception:
        pass
    self.app.set_status("Running PL2D…")
    self.app._job_queue.put(("log", f"[PL2D] Local execution platform: {local_platform}"))
    self.app._job_queue.put(("log", f"[PL2D] Executable: {exe_path}"))

    def worker():
        ok_all = True
        failure_msg = ""
        try:
            for i, z in enumerate(zs):
                sdir = run_dir / f"slice{i:03d}"
                sdir.mkdir()

                try:
                    shutil.copy2(fort9_src, sdir / "fort.9")
                except Exception as e:
                    ok_all = False
                    failure_msg = f"Failed to copy fort.9 to {sdir}: {e}"
                    self.app._job_queue.put(("log", f"[PL2D] {failure_msg}"))
                    break

                inp = sdir / "pl2d.inp"
                try:
                    with open(inp, "w", encoding="utf-8") as f:
                        pl2d_text = (
                            "TOPO\n"
                            "PL2D\n"
                            "0\n"
                            f"{a_coord[0]/bohr_to_ang} {a_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
                            "0\n"
                            f"{b_coord[0]/bohr_to_ang} {b_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
                            "0\n"
                            f"{c_coord[0]/bohr_to_ang} {c_coord[1]/bohr_to_ang} {z/bohr_to_ang}\n"
                            "3\n"
                            "0\n"
                            "30,15.0\n"
                            "1\n"
                            f"{xmin} {xmax} {inc}\n"
                            f"{ymin} {ymax} {inc}\n"
                            f"{flags_line}\n"
                            f"{out_name}\n"
                            "1\n"
                            "2.,0.0,0\n"
                            "2.2,1,1,1\n"
                            "36,0\n"
                            "END\n"
                        )
                        f.write(pl2d_text)
                except Exception as e:
                    ok_all = False
                    failure_msg = f"Failed to write pl2d.inp in {sdir}: {e}"
                    self.app._job_queue.put(("log", f"[PL2D] {failure_msg}"))
                    break

                out = sdir / "pl2d.out"
                err = sdir / "pl2d.err"
                try:
                    with open(inp, "r", encoding="utf-8", errors="ignore") as fin,                          open(out, "w", encoding="utf-8") as fout,                          open(err, "w", encoding="utf-8") as ferr:
                        proc = subprocess.Popen(
                            [str(exe_path)],
                            stdin=fin,
                            stdout=fout,
                            stderr=ferr,
                            cwd=str(sdir),
                            **_windows_subprocess_silent_kwargs(),
                        )
                        self.app._register_active_process(proc, "PL2D")
                        rc = proc.wait()
                        self.app._clear_active_process(proc)
                    if rc != 0:
                        ok_all = False
                        failure_msg = f"properties returned {rc} on slice {i:03d}"
                        self.app._job_queue.put(("log", f"[PL2D] {failure_msg}"))
                        try:
                            if err.exists():
                                err_txt = err.read_text(errors="ignore")
                                self.app._job_queue.put(("log", "[PL2D] STDERR:\n" + err_txt[-1000:]))
                            if out.exists():
                                out_txt = out.read_text(errors="ignore")
                                self.app._job_queue.put(("log", "[PL2D] STDOUT tail:\n" + out_txt[-1000:]))
                        except Exception:
                            pass
                        break
                except Exception as e:
                    self.app._clear_active_process()
                    ok_all = False
                    failure_msg = f"Failed to run properties on slice {i:03d}: {e}"
                    self.app._job_queue.put(("log", f"[PL2D] {failure_msg}"))
                    try:
                        err.write_text("EXCEPTION\n" + str(e) + "\n\n" + traceback.format_exc(), encoding="utf-8")
                    except Exception:
                        pass
                    break

                self._cleanup_pl2d_slice_temp_files(sdir)
                self.app._job_queue.put(("pl2d_progress", i + 1, len(zs), f"Slice {i+1}/{len(zs)}"))

            mf["status"] = "complete" if ok_all else "failed"
            mf["execution_mode"] = "local_windows" if is_windows() else "local_unix"
            (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")

            if not ok_all:
                log_event(ctx, f"PL2D finished FAIL: {run_dir.name}")
                self.app._job_queue.put(("pl2d_fail", failure_msg or "PL2D failed. Check slice folders and pl2d.out for details.", str(run_dir)))
                return

            self.app._job_queue.put(("pl2d_progress", len(zs), len(zs), f"Done ({len(zs)}/{len(zs)})"))
            self.app.state.pl2d_run_dir = run_dir
            log_event(ctx, f"PL2D finished OK: {run_dir.name}")
            self.app._job_queue.put(("pl2d_done", str(run_dir)))
        except Exception as e:
            self.app._clear_active_process()
            try:
                mf["status"] = "failed"
                mf["execution_mode"] = "local_windows" if is_windows() else "local_unix"
                (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")
            except Exception:
                pass
            self.app._job_queue.put(("pl2d_fail", str(e), str(run_dir)))

    self.app._job_thread = threading.Thread(target=worker, daemon=True)
    self.app._job_thread.start()




# Bind Phase 2 PL2D helper functions to the PL2DPage class
PL2DPage._write_pl2d_input_for_slice = _write_pl2d_input_for_slice
PL2DPage._write_pl2d_unix_scripts = _write_pl2d_unix_scripts
PL2DPage._export_pl2d_campaign = _export_pl2d_campaign
PL2DPage._cleanup_pl2d_slice_temp_files = _cleanup_pl2d_slice_temp_files
PL2DPage._run_pl2d = _run_pl2d

class PL2DViewerPage(BasePage):
    """PL2D Project Viewer (v2-like logic, minimal refactor)

    Goals:
    - List existing PL2D runs inside workspace/pl2d_runs/
    - Auto-detect number of slices from the folder structure (no manual selection)
    - Default opacity = 0.2 (later to Settings)
    - Render an Isosurface from selected SURF*.DAT using Plotly
    """

    title = "PL2D Viewer"

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        # Make the left control panel wider (especially for Geometric/Gatti mode)
        # and keep the action column compact.
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(1, weight=1)
        body.grid_columnconfigure(1, minsize=220)

        # --- Run selection ---
        frm_run = ttk.LabelFrame(body, text="Existing PL2D runs (workspace/pl2d_runs)")
        frm_run.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        frm_run.columnconfigure(1, weight=1)

        ttk.Label(frm_run, text="Run:").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
        self.run_var = tk.StringVar(value="")
        self.cmb_runs = ttk.Combobox(frm_run, textvariable=self.run_var, state="readonly", width=55)
        self.cmb_runs.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 4))
        self.cmb_runs.bind("<<ComboboxSelected>>", lambda e: self._on_run_selected())

        ttk.Button(frm_run, text="Refresh list", command=self.refresh_runs).grid(row=0, column=2, sticky="e", padx=(0, 10), pady=(8, 4))

        self.lbl_run_info = ttk.Label(frm_run, text="—", foreground="#444")
        self.lbl_run_info.grid(row=1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        # --- Surface selection + plot controls ---
        # Use a sizeable scrollable area for the left control column so the PL2D Viewer
        # remains usable on shorter Windows screens, while also tightening some paddings.
        frm_ctl_outer, self._ctl_canvas, self._ctl_scrollbar, frm_ctl = _make_scrollable_frame(body)
        frm_ctl_outer.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        frm_ctl.columnconfigure(0, weight=1)

        frm_surf = ttk.LabelFrame(frm_ctl, text="Topological isosurface (.DAT)")
        frm_surf.grid(row=0, column=0, sticky="ew")
        frm_surf.columnconfigure(1, weight=1)

        self.surf_var = tk.StringVar(value="SURFRHOO")

        ttk.Label(frm_surf, text="Surface:").grid(row=0, column=0, sticky="w", padx=(10, 8), pady=8)
        self.cmb_surface = ttk.Combobox(
            frm_surf,
            textvariable=self.surf_var,
            state="readonly",
            width=42,
            values=(),
        )
        self.cmb_surface.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=8)
        self.cmb_surface.bind("<<ComboboxSelected>>", lambda e: self._on_surf_selected())

        # --- Plot params (minimal; v2-like) ---
        frm_params = ttk.LabelFrame(frm_ctl, text="Plot parameters")
        frm_params.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.var_opacity = tk.StringVar(value="0.2")  # default requested
        self.var_count = tk.StringVar(value="3")
        self.var_isomin = tk.StringVar(value="")
        self.var_isomax = tk.StringVar(value="")

        # Gatti mode (geometric iso levels): base * factor^k (positive only)
        self.var_mode = tk.StringVar(value="linear")
        self.var_base_iso = tk.StringVar(value="")
        self.var_factor = tk.StringVar(value="4")
        self.var_max_levels = tk.StringVar(value="8")
        self.var_use_data_max = tk.BooleanVar(value=True)
        self.var_descending = tk.BooleanVar(value=False)
        self.var_geo_limit = tk.StringVar(value="")

        # Shared controls (apply to both modes)
        row_shared = ttk.Frame(frm_params)
        row_shared.pack(fill="x", padx=10, pady=(8, 4))
        _labeled_entry(row_shared, "Opacity (0-1)", self.var_opacity, width=8).pack(side="left", padx=(0, 18))

        # Camera projection (requested): perspective vs orthographic
        self.var_projection = tk.StringVar(value="orthographic")
        ttk.Label(row_shared, text="Projection").pack(side="left", padx=(0, 6))
        self.cmb_projection = ttk.Combobox(
            row_shared,
            textvariable=self.var_projection,
            values=("perspective", "orthographic"),
            width=14,
            state="readonly",
        )
        self.cmb_projection.pack(side="left", padx=(0, 18))

        # --- Iso level mode ---
        frm_mode = ttk.Frame(frm_params)
        frm_mode.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(frm_mode, text="Mode:").pack(side="left")
        self.rb_linear = ttk.Radiobutton(frm_mode, text="Linear", variable=self.var_mode, value="linear", command=self._on_mode_change)
        self.rb_linear.pack(side="left", padx=(8, 0))
        self.rb_geo = ttk.Radiobutton(frm_mode, text="Geometric (Gatti)", variable=self.var_mode, value="geometric", command=self._on_mode_change)
        self.rb_geo.pack(side="left", padx=(10, 0))

        # Linear levels controls
        self.frm_linear_box = ttk.LabelFrame(frm_params, text="Linear levels")
        self.frm_linear_box.pack(fill="x", padx=10, pady=(0, 6))
        self.frm_linear = ttk.Frame(self.frm_linear_box)
        self.frm_linear.pack(fill="x", padx=10, pady=8)
        _labeled_entry(self.frm_linear, "#Isosurfaces", self.var_count, width=8).pack(side="left", padx=(0, 18))
        self.ent_isomin = _labeled_entry(self.frm_linear, "Min iso", self.var_isomin, width=12)
        self.ent_isomin.pack(side="left", padx=(0, 18))
        self.ent_isomax = _labeled_entry(self.frm_linear, "Max iso", self.var_isomax, width=12)
        self.ent_isomax.pack(side="left", padx=(0, 0))

        # Geometric (Gatti) controls
        self.frm_geo_box = ttk.LabelFrame(frm_params, text="Geometric levels (Gatti)")
        self.frm_geo_box.pack(fill="x", padx=10, pady=(0, 6))
        self.frm_geo = ttk.Frame(self.frm_geo_box)
        self.frm_geo.pack(fill="x", padx=10, pady=8)

        # Dynamic labels: Base/Limit correspond to Min/Max depending on Asc/Desc
        self.var_base_label = tk.StringVar(value="Base iso")
        self.var_limit_label = tk.StringVar(value="Limit")

        # Row 1: Factor / Max levels / toggles
        geo_row1 = ttk.Frame(self.frm_geo)
        geo_row1.pack(fill="x", pady=(0, 6))
        self.ent_factor = _labeled_entry(geo_row1, "Factor", self.var_factor, width=8)
        self.ent_factor.pack(side="left", padx=(0, 18))
        self.ent_maxlv = _labeled_entry(geo_row1, "Max levels", self.var_max_levels, width=8)
        self.ent_maxlv.pack(side="left", padx=(0, 18))

        self.chk_desc = ttk.Checkbutton(geo_row1, text="Descending", variable=self.var_descending, command=self._on_mode_change)
        self.chk_desc.pack(side="left", padx=(0, 18))
        self.chk_usemax = ttk.Checkbutton(geo_row1, text="Use dataset max", variable=self.var_use_data_max, command=self._on_mode_change)
        self.chk_usemax.pack(side="left")

        # Row 2: Base iso / Limit
        geo_row2 = ttk.Frame(self.frm_geo)
        geo_row2.pack(fill="x", pady=(0, 0))
        self.ent_base = _labeled_entry(geo_row2, self.var_base_label, self.var_base_iso, width=12)
        self.ent_base.pack(side="left", padx=(0, 18))
        self.ent_geolim = _labeled_entry(geo_row2, self.var_limit_label, self.var_geo_limit, width=12)
        self.ent_geolim.pack(side="left")

        # Defer mode refresh until widgets are created
        self.after(0, self._on_mode_change)

        self.hint = ttk.Label(frm_params, text="Tip: If Min/Max are empty, they are auto-set from data.", foreground="#555")
        self.hint.pack(anchor="w", padx=10, pady=(0, 6))

        # --- Overlays (TRUE atoms + BCPs) ---
        # Lightweight overlays using Scatter3d (no Mesh3d spheres).
        frm_ov = ttk.LabelFrame(frm_params, text="Overlays (3D)")
        frm_ov.pack(fill="x", padx=10, pady=(0, 6))
        frm_ov.columnconfigure(0, weight=0)
        frm_ov.columnconfigure(1, weight=1)

        # TRUE atoms controls (left)
        frm_atoms = ttk.LabelFrame(frm_ov, text="TRUE atoms")
        frm_atoms.grid(row=0, column=0, sticky="nw", padx=(0, 8), pady=(6, 6))

        self.var_show_atoms = tk.BooleanVar(value=True)
        self.var_atom_size = tk.StringVar(value="4")
        self.var_atom_labels = tk.BooleanVar(value=True)
        self.var_atom_group = tk.BooleanVar(value=True)

        row_a = ttk.Frame(frm_atoms)
        row_a.pack(fill="x", pady=(6, 4), padx=10)
        ttk.Checkbutton(row_a, text="Show", variable=self.var_show_atoms).pack(side="left", padx=(0, 14))
        _labeled_entry(row_a, "Size", self.var_atom_size, width=6).pack(side="left")

        row_b = ttk.Frame(frm_atoms)
        row_b.pack(fill="x", pady=(0, 8), padx=10)
        ttk.Checkbutton(row_b, text="Labels", variable=self.var_atom_labels).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_b, text="Group by element", variable=self.var_atom_group).pack(side="left")

        # BCP controls (right) — rendered with a different marker (diamond-open) to distinguish from atoms.
        frm_bcps = ttk.LabelFrame(frm_ov, text="BCPs")
        frm_bcps.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(6, 6))

        self.var_show_bcps = tk.BooleanVar(value=True)
        self.var_bcp_size = tk.StringVar(value="4")
        self.var_bcp_labels = tk.BooleanVar(value=True)
        self.var_bcp_group = tk.BooleanVar(value=True)
        self.var_show_bond_paths = tk.BooleanVar(value=True)

        row_c = ttk.Frame(frm_bcps)
        row_c.pack(fill="x", pady=(6, 4), padx=10)
        ttk.Checkbutton(row_c, text="Show", variable=self.var_show_bcps).pack(side="left", padx=(0, 14))
        _labeled_entry(row_c, "Size", self.var_bcp_size, width=6).pack(side="left")

        row_d = ttk.Frame(frm_bcps)
        row_d.pack(fill="x", pady=(0, 8), padx=10)
        ttk.Checkbutton(row_d, text="Labels", variable=self.var_bcp_labels).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_d, text="Group by pair", variable=self.var_bcp_group).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_d, text="Bond path", variable=self.var_show_bond_paths).pack(side="left")


        # RCP/CCP controls — optional markers for ring/cage CPs (from TRHO output).
        # Placed in the Viewer (not in the PL2D runner) because it only affects visualization overlays.
        frm_rcpccp = ttk.LabelFrame(frm_ov, text="RCP / CCP")
        frm_rcpccp.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 6))

        self.var_show_rcps = tk.BooleanVar(value=True)
        self.var_show_ccps = tk.BooleanVar(value=True)
        self.var_rcp_size = tk.StringVar(value="4")
        self.var_ccp_size = tk.StringVar(value="4")
        self.var_rcp_labels = tk.BooleanVar(value=True)
        self.var_ccp_labels = tk.BooleanVar(value=True)

        row_e = ttk.Frame(frm_rcpccp)
        row_e.pack(fill="x", pady=(6, 4), padx=10)
        ttk.Checkbutton(row_e, text="Show RCP", variable=self.var_show_rcps).pack(side="left", padx=(0, 10))
        _labeled_entry(row_e, "Size", self.var_rcp_size, width=6).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_e, text="Labels", variable=self.var_rcp_labels).pack(side="left", padx=(0, 24))

        ttk.Checkbutton(row_e, text="Show CCP", variable=self.var_show_ccps).pack(side="left", padx=(0, 10))
        _labeled_entry(row_e, "Size", self.var_ccp_size, width=6).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_e, text="Labels", variable=self.var_ccp_labels).pack(side="left")

        # NNA controls — optional markers for flagged (3,-3) attractors.
        frm_nna = ttk.LabelFrame(frm_ov, text="NNA")
        frm_nna.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=0, pady=(0, 6))

        self.var_show_nna = tk.BooleanVar(value=True)
        self.var_nna_size = tk.StringVar(value="4")
        self.var_nna_labels = tk.BooleanVar(value=True)

        row_f = ttk.Frame(frm_nna)
        row_f.pack(fill="x", pady=(6, 4), padx=10)
        ttk.Checkbutton(row_f, text="Show NNA", variable=self.var_show_nna).pack(side="left", padx=(0, 10))
        _labeled_entry(row_f, "Size", self.var_nna_size, width=6).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(row_f, text="Labels", variable=self.var_nna_labels).pack(side="left")

# --- Actions ---
        frm_act = ttk.Frame(body)
        frm_act.grid(row=1, column=1, sticky="ne")
        frm_act.columnconfigure(0, weight=1)

        self.btn_plot = ttk.Button(frm_act, text="Visualize (Plotly)", command=self._plot, width=18)
        try:
            self.btn_plot.configure(padding=(10, 6))
        except Exception:
            pass
        self.btn_plot.pack(anchor="nw")

        # --- Optional: save Plotly project to HTML (v2-like) ---
        # Run/project name is already selected above (Run combobox), so we do not repeat it here.
        # The HTML will be saved inside the selected run folder.
        self.var_save_html = tk.BooleanVar(value=False)

        self.chk_save_html = ttk.Checkbutton(
            frm_act,
            text="Save Plotly project (HTML) in run folder",
            variable=self.var_save_html,
        )
        self.chk_save_html.pack(anchor="nw", pady=(10, 0))

        self.lbl_status = ttk.Label(frm_act, text="—", foreground="#444", wraplength=360)
        self.lbl_status.pack(anchor="nw", pady=(10, 0))

    def _on_mode_change(self):
        """Enable/disable parameter fields according to iso-level mode."""
        mode = self.var_mode.get().strip() or "linear"
        if mode == "geometric":
            # Show/Hide mode-specific panels
            try:
                self.frm_linear_box.pack_forget()
            except Exception:
                pass
            try:
                self.frm_geo_box.pack(fill="x", padx=10, pady=(0, 6))
            except Exception:
                pass

            for w in [self.ent_base, self.ent_factor, self.ent_maxlv, self.chk_usemax, self.ent_geolim]:
                try:
                    w.configure(state="normal")
                except Exception:
                    pass
            # If using dataset max, disable limit entry
            try:
                self.ent_geolim.configure(state=("disabled" if self.var_use_data_max.get() else "normal"))
            except Exception:
                pass
            
            # Convenience: when switching to Gatti mode, auto-seed base/limit from current Linear Min/Max
            # - Ascending: base=Min, limit=Max
            # - Descending: base=Max, limit=Min
            try:
                descending = bool(self.var_descending.get())
                lin_min = self.var_isomin.get().strip()
                lin_max = self.var_isomax.get().strip()
                if not self.var_base_iso.get().strip():
                    if descending and lin_max:
                        self.var_base_iso.set(lin_max)
                    elif (not descending) and lin_min:
                        self.var_base_iso.set(lin_min)
                if not self.var_geo_limit.get().strip():
                    if descending and lin_min:
                        self.var_geo_limit.set(lin_min)
                    elif (not descending) and lin_max:
                        self.var_geo_limit.set(lin_max)

                # If we have an explicit limit, prefer it over dataset-based defaults.
                if self.var_geo_limit.get().strip():
                    self.var_use_data_max.set(False)
                    try:
                        self.ent_geolim.configure(state="normal")
                    except Exception:
                        pass
            except Exception:
                pass

            # Update dynamic labels (Base/Limit act like extrema)
            try:
                descending = bool(self.var_descending.get())
                if descending:
                    self.var_base_label.set("Base iso (Max)")
                    self.var_limit_label.set("Limit (Min)")
                else:
                    self.var_base_label.set("Base iso (Min)")
                    self.var_limit_label.set("Limit (Max)")
            except Exception:
                pass
        else:
            # Show/Hide mode-specific panels
            try:
                self.frm_geo_box.pack_forget()
            except Exception:
                pass
            try:
                self.frm_linear_box.pack(fill="x", padx=10, pady=(0, 6))
            except Exception:
                pass

            # Re-enable linear inputs
            for w in [self.ent_isomin, self.ent_isomax]:
                try:
                    w.configure(state="normal")
                except Exception:
                    pass

        self.refresh_runs()

    def refresh_runs(self):
        ws = self.app.ctx.workspace_dir
        runs: list[str] = []

        def _collect(root: Path):
            nonlocal runs
            if not root.exists():
                return
            for rd in sorted(root.iterdir()):
                if not rd.is_dir():
                    continue
                # Prefer complete runs with manifest, but allow any folder with slice000
                mf = rd / "manifest.json"
                ok = False
                if mf.exists():
                    try:
                        data = json.loads(mf.read_text(encoding="utf-8"))
                        ok = (data.get("status") == "complete")
                    except Exception:
                        ok = False
                if ok or (rd / "slice000").exists():
                    runs.append(rd.name)

        if ws:
            # Most common layout: <workspace>/pl2d_runs/<run>/slice000
            if (ws / "pl2d_runs").exists():
                _collect(ws / "pl2d_runs")
            # If user selected pl2d_runs itself as workspace
            elif ws.name == "pl2d_runs":
                _collect(ws)
            # If user selected a single run folder directly
            elif (ws / "slice000").exists():
                runs = [ws.name]

        self.cmb_runs["values"] = runs
        if runs:
            if self.run_var.get() not in runs:
                self.run_var.set(runs[0])
            self._on_run_selected()
        else:
            self.run_var.set("")
            self.lbl_run_info.config(text="No PL2D runs found. Tip: select the workspace folder that contains 'pl2d_runs', or select 'pl2d_runs' itself.")
            self.cmb_surface["values"] = ()
            self.surf_var.set("")
            if hasattr(self, "lbl_status"):
                self.lbl_status.config(text="—")

    def _current_run_dir(self) -> Optional[Path]:
        ws = self.app.ctx.workspace_dir
        if not ws:
            return None
        name = self.run_var.get().strip()
        if not name:
            return None
        # Three possible interpretations based on what the user picked as workspace:
        # 1) <workspace>/pl2d_runs/<run>
        # 2) <pl2d_runs>/<run>
        # 3) <run> (workspace is the run itself)
        if (ws / "slice000").exists():
            return ws
        if (ws / "pl2d_runs").exists():
            return ws / "pl2d_runs" / name
        if ws.name == "pl2d_runs":
            return ws / name
        # fallback
        return ws / "pl2d_runs" / name

    def _detect_slices(self, run_dir: Path) -> list[Path]:
        # slices are folders slice000..sliceXYZ
        slices = sorted([p for p in run_dir.glob("slice*") if p.is_dir() and p.name[5:].isdigit()])
        return slices

    def _available_surfaces(self, run_dir: Path) -> list[str]:
        # Check slice000 for *.DAT
        s0 = run_dir / "slice000"
        if not s0.exists():
            return []
        dats = sorted([p.name for p in s0.glob("*.DAT")])
        # keep only SURF*.DAT to match v2 GUI
        surfs = [d.replace(".DAT", "") for d in dats if d.upper().startswith("SURF")]
        return surfs

    def _on_run_selected(self):
        rd = self._current_run_dir()
        if rd is None or not rd.exists():
            self.lbl_run_info.config(text="Invalid run selection.")
            return

        slices = self._detect_slices(rd)
        n_slices = max(0, len(slices) - 1)  # v2 meaning (intervals); folders are planes
        self.lbl_run_info.config(text=f"Folder: {rd.name} | planes: {len(slices)} | n_slices={n_slices}")


        surfs = self._available_surfaces(rd)
        self.cmb_surface["values"] = surfs

        # Set a default surface (prefer SURFRHOO)
        if surfs:
            pick = "SURFRHOO" if "SURFRHOO" in surfs else surfs[0]
            if self.surf_var.get().strip() not in surfs:
                self.surf_var.set(pick)
            # auto-set min/max from this surface
            self._auto_set_isorange()
        else:
            self.surf_var.set("")
        self.refresh_state()

        self.lbl_status.config(text="Ready to visualize.")
        self.refresh_state()

    def _on_surf_selected(self):
        name = self.surf_var.get().strip()
        if not name:
            return
        self._auto_set_isorange()
        self.refresh_state()

    def _auto_set_isorange(self):
        """Auto-fill Linear min/max (and seed Gatti base/limit) from selected surface."""
        try:
            rd = self._get_selected_run_dir()
            if rd is None:
                return
            surf = self.surf_var.get().strip()
            if not surf:
                return
            vmin, vmax = self._load_volume(rd, surf, compute_only_minmax=True)
            # If fields are empty, fill them. (Keeps manual edits.)
            if not self.var_isomin.get().strip():
                self.var_isomin.set(f"{vmin:.6g}")
            if not self.var_isomax.get().strip():
                self.var_isomax.set(f"{vmax:.6g}")

            # Also seed Gatti fields if user is in geometric mode and fields are empty.
            mode = (self.var_mode.get().strip() or "linear").lower()
            if mode == "geometric":
                descending = bool(self.var_descending.get())
                if not self.var_base_iso.get().strip():
                    self.var_base_iso.set(f"{(vmax if descending else vmin):.6g}")
                if not self.var_geo_limit.get().strip():
                    self.var_geo_limit.set(f"{(vmin if descending else vmax):.6g}")
        except Exception:
            pass
    def _parse_float(self, s: str) -> float:
        return float(s.strip().replace(",", "."))

    def _load_volume(self, run_dir: Path, surf: str, *, compute_only_minmax: bool = False):
        """Load PL2D volume from slice folders using the v2 algorithm:
        - get nptx/npty from 2nd line of slice000/<surf>.DAT (after dropping first line)
        - for each slice file: drop first 5 lines, read numeric grid, reshape (nptx,npty)
        - stack planes into (n_planes, nptx, npty)
        """
        slices = self._detect_slices(run_dir)
        if not slices:
            raise RuntimeError("No slice folders found.")

        # Read nptx/npty from the FIRST valid slice (some runs may have incomplete slice000)
        f0 = None
        lines0 = None
        for sdir in slices:
            cand = sdir / f"{surf}.DAT"
            if not cand.exists():
                continue
            try:
                with cand.open("r", encoding="utf-8", errors="ignore") as fp:
                    lns = fp.readlines()
            except Exception:
                continue
            if len(lns) >= 2:
                f0 = cand
                lines0 = lns
                break

        if f0 is None or lines0 is None:
            raise FileNotFoundError(f"Missing/invalid {surf}.DAT in all slice folders (run: {run_dir.name})")

        nconj = lines0[1].strip().split()
        if len(nconj) < 2:
            raise RuntimeError(f"Could not read nptx/npty from: {f0}")
        nptx, npty = int(float(nconj[0])), int(float(nconj[1]))

# Read planes
        planes = []
        vmin = None
        vmax = None
        header_lines = 5

        for sdir in slices:
            fdat = sdir / f"{surf}.DAT"
            if not fdat.exists():
                raise FileNotFoundError(f"Missing {surf}.DAT in {sdir.name}")
            with fdat.open("r", encoding="utf-8", errors="ignore") as fp:
                lns = fp.readlines()

            if len(lns) <= header_lines:
                # Incomplete run (or a partially written slice). Skip this slice instead of aborting.
                continue

            data_txt = " ".join([x.strip() for x in lns[header_lines:]])
            arr = np.asarray(pd.to_numeric(data_txt.split()), dtype=float)
            if arr.size != nptx * npty:
                raise RuntimeError(f"Grid size mismatch in {fdat.name} ({sdir.name}): got {arr.size}, expected {nptx*npty}")
            # Orientation note:
            # PROPERTIES PL2D grids are written with the *second* index (Y) varying slowest in many cases.
            # To keep the visual axes consistent with the PL2D input points (P2 along +x, P3 along +y),
            # we reshape as (npty, nptx) and transpose back to (nptx, npty).
            grid = arr.reshape(npty, nptx).T

            if compute_only_minmax:
                mn = float(np.min(grid))
                mx = float(np.max(grid))
                vmin = mn if vmin is None else min(vmin, mn)
                vmax = mx if vmax is None else max(vmax, mx)
            else:
                planes.append(grid)

        if compute_only_minmax:
            if vmin is None or vmax is None:
                raise RuntimeError("Could not compute min/max.")
            return vmin, vmax

        if not planes:
            raise RuntimeError("No valid slices found (all DAT files were missing/too short).")

        volume = np.stack(planes, axis=0)  # (n_planes, nptx, npty)
        return volume

    def _plot(self):
        rd = self._current_run_dir()
        if rd is None or not rd.exists():
            messagebox.showwarning("PL2D Viewer", "Select a valid run folder.")
            return
        surf = self.surf_var.get().strip()
        if not surf:
            messagebox.showwarning("PL2D Viewer", "Select an isosurface type.")
            return

        # Colorscale overrides for Laplacian isosurfaces (SURFLAPM/SURFLAPP)
        lap_cs = None
        try:
            scheme = (getattr(self.app.state, "laplacian_scheme", None) or self.app._settings.get("laplacian_scheme") or "blue_red").strip() or "blue_red"
            if surf in ("SURFLAPM", "SURFLAPP"):
                if scheme == "viridis":
                    lap_cs = "Viridis"
                elif scheme == "red_blue":
                    lap_cs = "Reds" if surf == "SURFLAPM" else "Blues"
                else:  # blue_red (default)
                    lap_cs = "Blues" if surf == "SURFLAPM" else "Reds"
        except Exception:
            lap_cs = None

        try:
            opacity = self._parse_float(self.var_opacity.get())
            if not (0.0 <= opacity <= 1.0):
                raise ValueError("Opacity must be between 0 and 1.")
            count = int(float(self.var_count.get()))
            if count < 1:
                raise ValueError("#Isosurfaces must be >= 1")
        except Exception as e:
            messagebox.showerror("PL2D Viewer", f"Invalid plot parameters: {e}")
            return

        # Load full volume
        try:
            vol = self._load_volume(rd, surf, compute_only_minmax=False)
            n_planes, nptx, npty = vol.shape
            # Auto-set min/max if empty
            if not self.var_isomin.get().strip():
                self.var_isomin.set(f"{float(vol.min()):.6g}")
            if not self.var_isomax.get().strip():
                self.var_isomax.set(f"{float(vol.max()):.6g}")

            mode = (self.var_mode.get().strip() or "linear").lower()

            if mode == "geometric":
                # Geometric iso levels: base * factor^k (positive only), up to a limit
                base = self._parse_float(self.var_base_iso.get())
                if base <= 0:
                    raise ValueError("Base iso must be > 0.")
                factor = self._parse_float(self.var_factor.get())
                descending = bool(self.var_descending.get())
                max_levels = int(float(self.var_max_levels.get() or "0"))
                if max_levels < 1:
                    raise ValueError("Max levels must be >= 1.")
                if max_levels > 1 and factor <= 1.0:
                    raise ValueError("Factor must be > 1 when Max levels > 1.")
                if max_levels < 1:
                    raise ValueError("Max levels must be >= 1.")

                data_max = float(vol.max())
                data_min = float(vol.min())

                # Sanity checks vs dataset range (helps avoid confusing plots)
                if not (data_min <= base <= data_max):
                    raise ValueError(f"Base iso ({base}) is outside dataset range [{data_min:.6g}, {data_max:.6g}].")

                # Limit resolution:
                # - If the user provided an explicit Limit, ALWAYS use it (even if "Use dataset max" is checked).
                # - Otherwise, use dataset max only when enabled.
                limit_txt = (self.var_geo_limit.get() or "").strip()
                if limit_txt:
                    limit = self._parse_float(limit_txt)
                elif bool(self.var_use_data_max.get()):
                    limit = (data_min if descending else data_max)
                else:
                    raise ValueError("Set a numeric 'Limit' or enable 'Use dataset max'.")
                if limit <= 0:
                    raise ValueError("Limit must be > 0.")
                if not (data_min <= limit <= data_max):
                    raise ValueError(f"Limit ({limit}) is outside dataset range [{data_min:.6g}, {data_max:.6g}].")
                if descending:
                    if base < limit:
                        raise ValueError(f"Base iso ({base}) is less than limit ({limit}) in Descending mode.")
                else:
                    if base > limit:
                        raise ValueError(f"Base iso ({base}) is greater than limit ({limit}).")

                # Build levels
                levels = []
                v = base
                for _ in range(max_levels):
                    if descending:
                        if v < limit - 1e-15:
                            break
                    else:
                        if v > limit + 1e-15:
                            break
                    levels.append(float(v))
                    if max_levels == 1:
                        break
                    v = (v / factor) if descending else (v * factor)

                if not levels:
                    raise ValueError("No iso levels generated (check base/factor/limit).")
            else:
                isomin = self._parse_float(self.var_isomin.get())
                isomax = self._parse_float(self.var_isomax.get())
                if isomax <= isomin:
                    raise ValueError("Max iso must be > Min iso.")
        except Exception as e:
            messagebox.showerror("PL2D Viewer", f"Failed to plot: {e}")
            return

        # Build scaled grid coordinates (Å) using PL2D inputs when available.
        # Default (fallback): index coordinates.
        x_coords = np.arange(nptx, dtype=float)
        y_coords = np.arange(npty, dtype=float)
        z_coords = np.arange(n_planes, dtype=float)

        try:
            # 1) X/Y ranges from slice000/pl2d.inp (last two numeric triplets are x_range_inc and y_range_inc)
            inp0 = rd / "slice000" / "pl2d.inp"
            if inp0.exists():
                triples = []
                for ln in inp0.read_text(errors="ignore").splitlines():
                    ln = ln.strip()
                    if (not ln) or ("," in ln):
                        continue
                    parts = ln.split()
                    if len(parts) == 3:
                        try:
                            triples.append([float(p) for p in parts])
                        except Exception:
                            pass
                if len(triples) >= 2:
                    (xmin, xmax, _xinc) = triples[-2]
                    (ymin, ymax, _yinc) = triples[-1]
                    # Use linspace to match exactly nptx/npty grid points
                    x_coords = np.linspace(xmin, xmax, int(nptx))
                    y_coords = np.linspace(ymin, ymax, int(npty))

            # 2) Z planes from each slice/pl2d.inp (average z of the first 3 coordinate triplets)
            slice_dirs = sorted([p for p in rd.iterdir() if p.is_dir() and p.name.startswith("slice")])
            z_list = []
            for sd in slice_dirs:
                f = sd / "pl2d.inp"
                if not f.exists():
                    z_list = []
                    break
                triples = []
                for ln in f.read_text(errors="ignore").splitlines():
                    ln = ln.strip()
                    if (not ln) or ("," in ln):
                        continue
                    parts = ln.split()
                    if len(parts) == 3:
                        try:
                            triples.append([float(p) for p in parts])
                        except Exception:
                            pass
                if len(triples) >= 3:
                    zvals = (triples[0][2], triples[1][2], triples[2][2])
                    z_list.append(float(sum(zvals) / 3.0))

            if z_list:
                if len(z_list) == int(n_planes):
                    z_coords = np.asarray(z_list, dtype=float)
                else:
                    # Fallback: map first->last to n_planes
                    z_coords = np.linspace(float(z_list[0]), float(z_list[-1]), int(n_planes))

            # Unit handling: in our workflow X and Y are in Å, but Z from PL2D plane points is in Bohr.
            # Convert Z coordinates to Å to avoid the ~1.8897x apparent stretching and doubled z-axis range.
            try:
                bohr_to_ang = 0.529177210903
                z_coords = z_coords * bohr_to_ang
            except Exception:
                pass
        except Exception:
            # Keep index coordinates on any parsing error
            pass

        # Build coordinate arrays matching vol shape (n_planes, nptx, npty)
        Zc = z_coords[:, None, None] * np.ones((int(n_planes), int(nptx), int(npty)))
        Xc = x_coords[None, :, None] * np.ones((int(n_planes), int(nptx), int(npty)))
        Yc = y_coords[None, None, :] * np.ones((int(n_planes), int(nptx), int(npty)))

        fig = go.Figure()

        # Axis labels in Å (scaled from PL2D inputs when available)
        try:
            proj = (self.var_projection.get() if hasattr(self, "var_projection") else "orthographic")
            proj = (proj or "orthographic").strip().lower()
            if proj not in ("perspective", "orthographic"):
                proj = "orthographic"
        except Exception:
            proj = "orthographic"

        xmin, xmax = float(np.min(Xc)), float(np.max(Xc))
        ymin, ymax = float(np.min(Yc)), float(np.max(Yc))
        zmin, zmax = float(np.min(Zc)), float(np.max(Zc))

        Lx = xmax - xmin
        Ly = ymax - ymin
        Lz = zmax - zmin

        fig.update_layout(
            scene=dict(
                xaxis=dict(title='x (Å)', range=[xmin, xmax]),
                yaxis=dict(title='y (Å)', range=[ymin, ymax]),
                zaxis=dict(title='z (Å)', range=[zmin, zmax]),
                aspectmode='manual',
                aspectratio=dict(x=Lx, y=Ly, z=Lz),
            ),
            scene_camera=dict(projection=dict(type=proj)),
        )

        if (self.var_mode.get().strip() or "linear").lower() == "geometric":
            # In practice, multiple fixed-level isosurfaces look *much* fainter than a single
            # continuous isosurface. If user keeps the default opacity=0.2, the result becomes
            # barely visible. We apply a gentle visibility boost only in the geometric mode.
            try:
                op_user = float(opacity)
            except Exception:
                op_user = 0.2
            op_eff = op_user
            if len(levels) >= 2 and op_eff < 0.35:
                op_eff = min(0.85, op_eff * 3.0)

            # Discrete colors for Gatti levels (ordered palette)
            try:
                samples = np.linspace(0.10, 0.90, max(1, len(levels)))
                lvl_colors = pc.sample_colorscale((lap_cs or "Viridis"), samples)
            except Exception:
                # Fallback palette (Viridis-like)
                lvl_colors = [
                    "rgba(68,1,84,1)",
                    "rgba(71,44,122,1)",
                    "rgba(59,81,139,1)",
                    "rgba(44,113,142,1)",
                    "rgba(33,144,141,1)",
                    "rgba(39,173,129,1)",
                    "rgba(92,200,99,1)",
                    "rgba(170,220,50,1)",
                    "rgba(253,231,37,1)",
                ]
                if len(lvl_colors) < len(levels):
                    # repeat if needed
                    lvl_colors = (lvl_colors * (len(levels)//len(lvl_colors) + 1))[:len(levels)]
            # One Isosurface trace per level (Plotly does not accept arbitrary level lists in a single trace)
            for i, lvl in enumerate(levels):
                fig.add_trace(
                    go.Isosurface(
                        x=Xc.flatten(),
                        y=Yc.flatten(),
                        z=Zc.flatten(),
                        value=vol.flatten(),
                        opacity=float(op_eff),
                        isomin=float(lvl),
                        isomax=float(lvl),
                        name=f"iso={float(lvl):.6g} a.u.",
                        showlegend=True,
                        legendgroup="gatti_levels",
                        # Hide caps to avoid the "transparent box" look.
                        caps=dict(x_show=False, y_show=False, z_show=False),
                        surface_count=1,
                        colorscale=[[0, lvl_colors[i % len(lvl_colors)]], [1, lvl_colors[i % len(lvl_colors)]]],
                        hoverinfo="skip",
                        showscale=False,
                    )
                )
            # Show selected levels explicitly (shortened if too long)
            lev_strs = [f"{v:.6g}" for v in levels]
            if len(lev_strs) > 8:
                lev_disp = ", ".join(lev_strs[:4] + ["…"] + lev_strs[-3:])
            else:
                lev_disp = ", ".join(lev_strs)
            title_txt = f"{rd.name} | {n_planes-1} slices | {len(levels)} levels | {surf} | Mode=Geometric (Gatti) | base={levels[0]:.6g} a.u. | factor={self.var_factor.get().strip() or '4'} | limit={limit:.6g} a.u. | levels (a.u.): {lev_disp}"
        else:
            fig.add_trace(
                go.Isosurface(
                    x=Xc.flatten(),
                    y=Yc.flatten(),
                    z=Zc.flatten(),
                    value=vol.flatten(),
                    opacity=float(opacity),
                    isomin=float(isomin),
                    isomax=float(isomax),
                    caps=dict(x_show=False, y_show=False, z_show=False),
                    surface_count=int(count),
                    colorscale=(lap_cs if lap_cs is not None else "Viridis"),
                    hoverinfo="skip",
                colorbar=dict(title='a.u.'),
                )
            )
            title_txt = f"{rd.name} | {n_planes-1} slices | {count} isosurfaces | {surf} | Mode=Linear | Min={isomin} a.u. | Max={isomax} a.u."

        # --- Optional overlay: TRUE atoms (Scatter3d, lightweight) ---
        try:
            if getattr(self, "var_show_atoms", None) is not None and bool(self.var_show_atoms.get()):
                df_atoms = getattr(self.app.state, "df_true_atoms", None)
                if df_atoms is not None and hasattr(df_atoms, "empty") and (not df_atoms.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    eps_vis = 0.10  # Å

                    dfp = df_atoms[
                        (df_atoms["X_ANGSTROM"] >= xmin_b + eps_vis) & (df_atoms["X_ANGSTROM"] <= xmax_b - eps_vis) &
                        (df_atoms["Y_ANGSTROM"] >= ymin_b + eps_vis) & (df_atoms["Y_ANGSTROM"] <= ymax_b - eps_vis) &
                        (df_atoms["Z_ANGSTROM"] >= zmin_b + eps_vis) & (df_atoms["Z_ANGSTROM"] <= zmax_b - eps_vis)
                    ].copy()

                    if not dfp.empty:
                        periodic = {
                            1:"H", 2:"He", 3:"Li", 4:"Be", 5:"B", 6:"C", 7:"N", 8:"O", 9:"F", 10:"Ne",
                            11:"Na",12:"Mg",13:"Al",14:"Si",15:"P",16:"S",17:"Cl",18:"Ar",
                            19:"K",20:"Ca",21:"Sc",22:"Ti",23:"V",24:"Cr",25:"Mn",26:"Fe",
                            27:"Co",28:"Ni",29:"Cu",30:"Zn",31:"Ga",32:"Ge",33:"As",34:"Se",
                            35:"Br",36:"Kr",37:"Rb",38:"Sr",39:"Y",40:"Zr",41:"Nb",42:"Mo",
                            43:"Tc",44:"Ru",45:"Rh",46:"Pd",47:"Ag",48:"Cd",49:"In",50:"Sn",
                            51:"Sb",52:"Te",53:"I",54:"Xe",55:"Cs",56:"Ba",57:"La",58:"Ce",
                            59:"Pr",60:"Nd",61:"Pm",62:"Sm",63:"Eu",64:"Gd",65:"Tb",66:"Dy",
                            67:"Ho",68:"Er",69:"Tm",70:"Yb",71:"Lu",72:"Hf",73:"Ta",74:"W",
                            75:"Re",76:"Os",77:"Ir",78:"Pt",79:"Au",80:"Hg",81:"Tl",82:"Pb",
                            83:"Bi",84:"Po",85:"At",86:"Rn",87:"Fr",88:"Ra",89:"Ac",90:"Th",
                            91:"Pa",92:"U"
                        }
                        dfp["_sym"] = [periodic.get(int(normalize_atomic_number(z)), f"Z{int(normalize_atomic_number(z))}") for z in dfp["ELEMENT"].tolist()]

                        show_labels = bool(getattr(self, "var_atom_labels", tk.BooleanVar(value=True)).get())
                        group = bool(getattr(self, "var_atom_group", tk.BooleanVar(value=True)).get())

                        try:
                            atom_size = float((self.var_atom_size.get() or "4").strip().replace(",", "."))
                        except Exception:
                            atom_size = 4.0
                        atom_size = max(1.0, min(atom_size, 20.0))

                        # Colors per element (stable, cyclic palette)
                        try:
                            palette = pc.qualitative.Dark24
                        except Exception:
                            palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]

                        uniq = sorted(dfp["_sym"].unique().tolist())
                        colmap = {sym: palette[i % len(palette)] for i, sym in enumerate(uniq)}

                        def _labels(subdf):
                            idxs = subdf.index.tolist()  # TRUE atom labels (1-based)
                            syms = subdf["_sym"].tolist()
                            return [f"{s}{i}" for s, i in zip(syms, idxs)]

                        mode_atoms = "markers+text" if show_labels else "markers"

                        if group:
                            # One trace per element (better legend control)
                            for sym in uniq:
                                sub = dfp[dfp["_sym"] == sym]
                                if sub.empty:
                                    continue
                                fig.add_trace(
                                    go.Scatter3d(
                                        x=sub["X_ANGSTROM"],
                                        y=sub["Y_ANGSTROM"],
                                        z=sub["Z_ANGSTROM"],
                                        mode=mode_atoms,
                                        text=_labels(sub) if show_labels else None,
                                        textposition="top center",
                                        name=f"{sym} (TRUE)",
                                        marker=dict(
                                            size=atom_size,
                                            color=colmap.get(sym, "blue"),
                                            opacity=1.0,
                                            line=dict(width=1, color="DarkSlateGrey"),
                                        ),
                                        hovertemplate=(
                                            f"{sym} (TRUE)<br>"
                                            "x: %{x:.4f} Å<br>y: %{y:.4f} Å<br>z: %{z:.4f} Å<extra></extra>"
                                        ),
                                        showlegend=True,
                                    )
                                )
                        else:
                            # Single trace (v2 behavior)
                            fig.add_trace(
                                go.Scatter3d(
                                    x=dfp["X_ANGSTROM"],
                                    y=dfp["Y_ANGSTROM"],
                                    z=dfp["Z_ANGSTROM"],
                                    mode=mode_atoms,
                                    text=_labels(dfp) if show_labels else None,
                                    textposition="top center",
                                    name="TRUE atoms",
                                    marker=dict(
                                        size=atom_size,
                                        color=[colmap.get(sym, "blue") for sym in dfp["_sym"].tolist()],
                                        opacity=1.0,
                                        line=dict(width=1, color="DarkSlateGrey"),
                                    ),
                                    hovertemplate="TRUE atom<br>x: %{x:.4f} Å<br>y: %{y:.4f} Å<br>z: %{z:.4f} Å<extra></extra>",
                                    showlegend=True,
                                )
                            )
        except Exception:
            # Never fail plotting due to atoms overlay
            pass


        # --- Optional overlay: bond paths from TOPOND bond-path attractors ---
        # Rendered as subtle dotted line segments between the two attractor coordinates
        # stored for each BCP. This is intentionally lightweight and optional.
        def _add_bond_path_overlay(fig_obj, df_rows: pd.DataFrame, *, name: str = "Bond path") -> None:
            if df_rows is None or df_rows.empty:
                return
            req_cols = [
                "ATTR1_X_ANGSTROM", "ATTR1_Y_ANGSTROM", "ATTR1_Z_ANGSTROM",
                "ATTR2_X_ANGSTROM", "ATTR2_Y_ANGSTROM", "ATTR2_Z_ANGSTROM",
            ]
            if any(c not in df_rows.columns for c in req_cols):
                return

            xs, ys, zs = [], [], []
            n_dots = 22  # denser dotted appearance for a smoother, subtler bond path
            for _, row in df_rows.iterrows():
                try:
                    x1 = float(row["ATTR1_X_ANGSTROM"])
                    y1 = float(row["ATTR1_Y_ANGSTROM"])
                    z1 = float(row["ATTR1_Z_ANGSTROM"])
                    x2 = float(row["ATTR2_X_ANGSTROM"])
                    y2 = float(row["ATTR2_Y_ANGSTROM"])
                    z2 = float(row["ATTR2_Z_ANGSTROM"])
                except Exception:
                    continue
                vals = [x1, y1, z1, x2, y2, z2]
                if not all(np.isfinite(v) for v in vals):
                    continue
                for t in np.linspace(0.08, 0.92, n_dots):
                    xs.append(x1 + (x2 - x1) * t)
                    ys.append(y1 + (y2 - y1) * t)
                    zs.append(z1 + (z2 - z1) * t)
                xs.append(None)
                ys.append(None)
                zs.append(None)

            if not xs:
                return

            fig_obj.add_trace(
                go.Scatter3d(
                    x=xs,
                    y=ys,
                    z=zs,
                    mode="markers",
                    name=name,
                    marker=dict(color="rgba(60,60,60,0.55)", size=1.3, symbol="circle"),
                    hoverinfo="skip",
                    showlegend=True,
                )
            )

        # --- Optional overlay: BCPs (Scatter3d, lightweight) ---
        # Uses TRHO-parsed BCP table (df_bcp_props) with x/y/z in Å.
        cp_legend_flags = {"bcp": False, "rcp": False, "ccp": False}
        cp_legend_groups = {"bcp": "cp_bcp", "rcp": "cp_rcp", "ccp": "cp_ccp"}
        cp_legend_seen = {"bcp": False, "rcp": False, "ccp": False}
        cp_plot_counts = {"bcp": 0, "rcp": 0, "ccp": 0}
        nna_legend_flag = False
        nna_legend_group = "cp_nna"
        try:
            if getattr(self, "var_show_bcps", None) is not None and bool(self.var_show_bcps.get()):
                df_bcp = getattr(self.app.state, "df_bcp_props", None)
                if df_bcp is not None and hasattr(df_bcp, "empty") and (not df_bcp.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    eps_vis = 0.10  # Å

                    dfp = df_bcp[
                        (df_bcp["X_ANGSTROM"] >= xmin_b + eps_vis) & (df_bcp["X_ANGSTROM"] <= xmax_b - eps_vis) &
                        (df_bcp["Y_ANGSTROM"] >= ymin_b + eps_vis) & (df_bcp["Y_ANGSTROM"] <= ymax_b - eps_vis) &
                        (df_bcp["Z_ANGSTROM"] >= zmin_b + eps_vis) & (df_bcp["Z_ANGSTROM"] <= zmax_b - eps_vis)
                    ].copy()

                    if not dfp.empty:
                        cp_plot_counts["bcp"] = int(len(dfp))
                        cp_legend_flags["bcp"] = (cp_plot_counts["bcp"] > 0)
                        if getattr(self, "var_show_bond_paths", None) is not None and bool(self.var_show_bond_paths.get()):
                            _add_bond_path_overlay(fig, dfp)
                        show_labels = bool(getattr(self, "var_bcp_labels", tk.BooleanVar(value=False)).get())
                        group = bool(getattr(self, "var_bcp_group", tk.BooleanVar(value=True)).get())

                        try:
                            bcp_size = float((self.var_bcp_size.get() or "4").strip().replace(",", "."))
                        except Exception:
                            bcp_size = 4.0
                        bcp_size = max(1.0, min(bcp_size, 30.0))

                        # Elegant and clearly distinct from atoms: open diamond + dark outline.
                        symbol = "diamond-open"

                        # Pair label as in v2 (BCP_ELEM = concatenated element symbols)
                        if "BCP_ELEM" not in dfp.columns:
                            dfp["BCP_ELEM"] = "BCP"

                        # Colors per pair (stable palette)
                        try:
                            palette = pc.qualitative.Set3
                        except Exception:
                            palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]

                        uniq = sorted(dfp["BCP_ELEM"].astype(str).unique().tolist())
                        colmap = {k: palette[i % len(palette)] for i, k in enumerate(uniq)}

                        def _bcp_text(subdf):
                            # Index is already 1-based in our TRHO parser
                            idxs = subdf.index.tolist()
                            if show_labels:
                                return [f"BCP{int(i)}" for i in idxs]
                            return [""] * len(idxs)

                        mode_bcps = "markers+text" if show_labels else "markers"

                        # Build hover with key properties when available
                        hover_cols = []
                        hover_labels = {
                            "DIST_ELEM1_ANG": "DIST_(ANG)",
                            "DIST_ELEM2_ANG": "DIST_(ANG)",
                        }
                        for col in ("ELEM1", "DIST_ELEM1_ANG", "ELEM2", "DIST_ELEM2_ANG", "RHO", "LAP", "BOND_DEGREE", "ADIM_RATIO", "ELLIP"):
                            if col in dfp.columns:
                                hover_cols.append(col)

                        def _customdata(subdf):
                            if not hover_cols:
                                return None
                            return subdf[hover_cols].to_numpy()

                        hovertemplate = "<b>BCP%{text}</b><br>" if show_labels else "<b>BCP</b><br>"
                        if hover_cols:
                            # Add each col line
                            for i, col in enumerate(hover_cols):
                                hovertemplate += f"{hover_labels.get(col, col)}: %{{customdata[{i}]}}<br>"
                        hovertemplate += "x=%{x:.4f} Å<br>y=%{y:.4f} Å<br>z=%{z:.4f} Å<extra></extra>"

                        if group:
                            for pair in uniq:
                                subp = dfp[dfp["BCP_ELEM"].astype(str) == str(pair)]
                                if subp.empty:
                                    continue
                                fig.add_trace(
                                    go.Scatter3d(
                                        x=subp["X_ANGSTROM"],
                                        y=subp["Y_ANGSTROM"],
                                        z=subp["Z_ANGSTROM"],
                                        mode=mode_bcps,
                                        text=_bcp_text(subp),
                                        name=f"{pair} (BCP)",
                                        marker=dict(
                                            size=bcp_size,
                                            symbol=symbol,
                                            color=colmap[str(pair)],
                                            line=dict(width=2, color="black"),
                                        ),
                                        customdata=_customdata(subp),
                                        hovertemplate=hovertemplate,
                                        legendgroup=cp_legend_groups["bcp"],
                                        showlegend=False,
                                    )
                                )
                                cp_legend_seen["bcp"] = True
                        else:
                            fig.add_trace(
                                go.Scatter3d(
                                    x=dfp["X_ANGSTROM"],
                                    y=dfp["Y_ANGSTROM"],
                                    z=dfp["Z_ANGSTROM"],
                                    mode=mode_bcps,
                                    text=_bcp_text(dfp),
                                    name="BCPs",
                                    marker=dict(
                                        size=bcp_size,
                                        symbol=symbol,
                                        color="#000000",
                                        line=dict(width=2, color="black"),
                                    ),
                                    customdata=_customdata(dfp),
                                    hovertemplate=hovertemplate,
                                    legendgroup=cp_legend_groups["bcp"],
                                    showlegend=False,
                                )
                            )
                            cp_legend_seen["bcp"] = True
        except Exception:
            # Never fail plotting due to BCPs overlay
            pass

        # --- Optional overlay: RCPs (Ring Critical Points, (3,+1)) ---
        try:
            if getattr(self, "var_show_rcps", None) is not None and bool(self.var_show_rcps.get()):
                parsed_trho = getattr(self.app.state, "trho_parsed", None)
                df_rcp = (getattr(parsed_trho, "df_rcp_props", None) if parsed_trho is not None else None)
                if df_rcp is not None and hasattr(df_rcp, "empty") and (not df_rcp.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    eps_vis = 0.10  # Å

                    # Ensure numeric CP coordinates (some CRYSTAL outputs carry them as strings)
                    _x = pd.to_numeric(df_rcp.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_rcp.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_rcp.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    # Filter by current PL2D box using the same internal visibility margin applied to other markers.
                    mask = np.isfinite(_x) & np.isfinite(_y) & np.isfinite(_z) & (_x >= (xmin_b + eps_vis)) & (_x <= (xmax_b - eps_vis)) & (_y >= (ymin_b + eps_vis)) & (_y <= (ymax_b - eps_vis)) & (_z >= (zmin_b + eps_vis)) & (_z <= (zmax_b - eps_vis))
                    dfp = df_rcp.loc[mask].copy()
                    dfp["X_ANGSTROM"] = _x.loc[mask].astype(float)
                    dfp["Y_ANGSTROM"] = _y.loc[mask].astype(float)
                    dfp["Z_ANGSTROM"] = _z.loc[mask].astype(float)

                    if not dfp.empty:
                        cp_plot_counts["rcp"] = int(len(dfp))
                        cp_legend_flags["rcp"] = (cp_plot_counts["rcp"] > 0)
                        show_labels = bool(getattr(self, "var_rcp_labels", tk.BooleanVar(value=False)).get())
                        try:
                            rcp_size = float((self.var_rcp_size.get() or "4").strip().replace(",", "."))
                        except Exception:
                            rcp_size = 4.0
                        rcp_size = max(1.0, min(rcp_size, 30.0))

                        symbol = "square-open"
                        idxs = dfp.index.tolist()
                        texts = [f"RCP{int(i)}" for i in idxs] if show_labels else [""] * len(idxs)
                        mode_rcp = "markers+text" if show_labels else "markers"

                        hover_cols = []
                        for col in ("RHO", "LAP", "ADIM_RATIO", "BOND_DEGREE", "LAMBDA1", "LAMBDA2", "LAMBDA3", "ELLIP"):
                            if col in dfp.columns:
                                hover_cols.append(col)

                        customdata = dfp[hover_cols].to_numpy() if hover_cols else None

                        hovertemplate = "<b>RCP</b><br>"
                        if show_labels:
                            hovertemplate = "<b>%{text}</b><br>"
                        if hover_cols:
                            for i, col in enumerate(hover_cols):
                                hovertemplate += f"{hover_labels.get(col, col)}: %{{customdata[{i}]}}<br>"
                        hovertemplate += "x=%{x:.4f} Å<br>y=%{y:.4f} Å<br>z=%{z:.4f} Å<extra></extra>"

                        fig.add_trace(
                            go.Scatter3d(
                                x=dfp["X_ANGSTROM"],
                                y=dfp["Y_ANGSTROM"],
                                z=dfp["Z_ANGSTROM"],
                                mode=mode_rcp,
                                text=texts,
                                textposition="top center",
                                name="RCP (RING)",
                                marker=dict(
                                    size=rcp_size,
                                    symbol=symbol,
                                    color="#7f7f7f",
                                    opacity=1.0,
                                    line=dict(width=1, color="DarkSlateGrey"),
                                ),
                                customdata=customdata,
                                hovertemplate=hovertemplate,
                                legendgroup=cp_legend_groups["rcp"],
                                showlegend=False,
                            )
                        )
                        cp_legend_seen["rcp"] = True
        except Exception:
            # Never fail plotting due to RCPs overlay
            pass

        # --- Optional overlay: CCPs (Cage Critical Points, (3,+3)) ---
        try:
            if getattr(self, "var_show_ccps", None) is not None and bool(self.var_show_ccps.get()):
                parsed_trho = getattr(self.app.state, "trho_parsed", None)
                df_ccp_props = (getattr(parsed_trho, "df_ccp_props", None) if parsed_trho is not None else None)
                df_ccp_coords = (getattr(parsed_trho, "df_cage", None) if parsed_trho is not None else None)

                # Be conservative here: only expose a CCP legend/trace when there is at least one
                # real cage-coordinate parsed from TRHO. This avoids false positives from partially
                # populated property tables or stale/ghost CCP rows.
                if (
                    df_ccp_coords is not None and hasattr(df_ccp_coords, "empty") and (not df_ccp_coords.empty)
                    and df_ccp_props is not None and hasattr(df_ccp_props, "empty") and (not df_ccp_props.empty)
                ):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    # Internal visibility margin for CCPs (Å).
                    # Empirically tuned from the MgO tests:
                    #   L=2.0 and 2.1 -> CCP entered the old legend logic but was not visibly rendered;
                    #   L=2.2         -> CCP became visibly present;
                    #   L=1.9         -> CCP already disappeared from the legend.
                    # Because L(side) grows symmetrically around the center, a 0.2 Å change in L
                    # corresponds to ~0.1 Å per face. Using eps=0.10 Å therefore suppresses edge/
                    # vertex-near CCPs that are not visually reliable, while keeping clearly visible ones.
                    eps_ccp = 0.10

                    # Start from the coordinate table, which is the safest source for existence.
                    # Then join the property columns (same CCP indexing when parsing is correct).
                    cols_keep = [c for c in ("RHO", "LAP", "ADIM_RATIO", "BOND_DEGREE", "LAMBDA1", "LAMBDA2", "LAMBDA3", "ELLIP") if c in df_ccp_props.columns]
                    df_ccp = df_ccp_coords.copy()
                    for col in cols_keep:
                        df_ccp[col] = df_ccp_props[col]

                    # Ensure numeric CP coordinates (some CRYSTAL outputs carry them as strings)
                    _x = pd.to_numeric(df_ccp.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_ccp.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_ccp.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    # Filter by the current PL2D box using an *internal* margin eps_ccp.
                    # This avoids creating a CCP legend entry for points that mathematically touch the box
                    # but are so close to an edge/vertex that they do not render visibly in Plotly.
                    # Also drop rows that sit exactly on the origin and require at least one meaningful
                    # CCP property value. This removes ghost rows coming from partially initialized cage arrays.
                    finite_mask = np.isfinite(_x) & np.isfinite(_y) & np.isfinite(_z)
                    bounds_mask = (_x >= (xmin_b + eps_ccp)) & (_x <= (xmax_b - eps_ccp)) & (_y >= (ymin_b + eps_ccp)) & (_y <= (ymax_b - eps_ccp)) & (_z >= (zmin_b + eps_ccp)) & (_z <= (zmax_b - eps_ccp))
                    nonzero_coord_mask = (_x.abs() > 1.0e-10) | (_y.abs() > 1.0e-10) | (_z.abs() > 1.0e-10)
                    if cols_keep:
                        prop_frame = df_ccp[cols_keep].apply(pd.to_numeric, errors="coerce")
                        finite_prop_mask = prop_frame.notna().any(axis=1)
                        meaningful_prop_mask = prop_frame.abs().gt(1.0e-12).any(axis=1)
                        prop_mask = finite_prop_mask & meaningful_prop_mask
                    else:
                        prop_mask = pd.Series(True, index=df_ccp.index)
                    mask = finite_mask & bounds_mask & nonzero_coord_mask & prop_mask
                    dfp = df_ccp.loc[mask].copy()
                    dfp["X_ANGSTROM"] = _x.loc[mask].astype(float)
                    dfp["Y_ANGSTROM"] = _y.loc[mask].astype(float)
                    dfp["Z_ANGSTROM"] = _z.loc[mask].astype(float)

                    if not dfp.empty:
                        cp_plot_counts["ccp"] = int(len(dfp))
                        cp_legend_flags["ccp"] = (cp_plot_counts["ccp"] > 0)
                        show_labels = bool(getattr(self, "var_ccp_labels", tk.BooleanVar(value=False)).get())
                        try:
                            ccp_size = float((self.var_ccp_size.get() or "4").strip().replace(",", "."))
                        except Exception:
                            ccp_size = 4.0
                        ccp_size = max(1.0, min(ccp_size, 30.0))

                        symbol = "x"
                        idxs = dfp.index.tolist()
                        texts = [f"CCP{int(i)}" for i in idxs] if show_labels else [""] * len(idxs)
                        mode_ccp = "markers+text" if show_labels else "markers"

                        hover_cols = []
                        for col in ("RHO", "LAP", "ADIM_RATIO", "BOND_DEGREE", "LAMBDA1", "LAMBDA2", "LAMBDA3", "ELLIP"):
                            if col in dfp.columns:
                                hover_cols.append(col)

                        customdata = dfp[hover_cols].to_numpy() if hover_cols else None

                        hovertemplate = "<b>CCP</b><br>"
                        if show_labels:
                            hovertemplate = "<b>%{text}</b><br>"
                        if hover_cols:
                            for i, col in enumerate(hover_cols):
                                hovertemplate += f"{hover_labels.get(col, col)}: %{{customdata[{i}]}}<br>"
                        hovertemplate += "x=%{x:.4f} Å<br>y=%{y:.4f} Å<br>z=%{z:.4f} Å<extra></extra>"

                        fig.add_trace(
                            go.Scatter3d(
                                x=dfp["X_ANGSTROM"],
                                y=dfp["Y_ANGSTROM"],
                                z=dfp["Z_ANGSTROM"],
                                mode=mode_ccp,
                                text=texts,
                                textposition="top center",
                                name="CCP (CAGE)",
                                marker=dict(
                                    size=ccp_size,
                                    symbol=symbol,
                                    color="limegreen",
                                    opacity=1.0,
                                    line=dict(width=1, color="DarkSlateGrey"),
                                ),
                                customdata=customdata,
                                hovertemplate=hovertemplate,
                                legendgroup=cp_legend_groups["ccp"],
                                showlegend=False,
                            )
                        )
                        cp_legend_seen["ccp"] = True
        except Exception:
            # Never fail plotting due to CCPs overlay
            pass

        # --- Optional overlay: NNA (flagged non-nuclear attractors, (3,-3)) ---
        try:
            if getattr(self, "var_show_nna", None) is not None and bool(self.var_show_nna.get()):
                parsed_trho = getattr(self.app.state, "trho_parsed", None)
                df_nna = (getattr(parsed_trho, "df_att_nao_nucl", None) if parsed_trho is not None else None)
                if df_nna is not None and hasattr(df_nna, "empty") and (not df_nna.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    eps_vis = 0.10  # Å

                    _x = pd.to_numeric(df_nna.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_nna.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_nna.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    mask = (
                        (_x >= (xmin_b + eps_vis)) & (_x <= (xmax_b - eps_vis)) &
                        (_y >= (ymin_b + eps_vis)) & (_y <= (ymax_b - eps_vis)) &
                        (_z >= (zmin_b + eps_vis)) & (_z <= (zmax_b - eps_vis))
                    )
                    dfp = df_nna.loc[mask].copy()
                    dfp["X_ANGSTROM"] = _x.loc[mask].astype(float)
                    dfp["Y_ANGSTROM"] = _y.loc[mask].astype(float)
                    dfp["Z_ANGSTROM"] = _z.loc[mask].astype(float)

                    if not dfp.empty:
                        show_labels = bool(getattr(self, "var_nna_labels", tk.BooleanVar(value=True)).get())
                        try:
                            nna_size = float((self.var_nna_size.get() or "4").strip().replace(",", "."))
                        except Exception:
                            nna_size = 4.0
                        nna_size = max(1.0, min(nna_size, 30.0))

                        if "N" in dfp.columns:
                            n_ids = pd.to_numeric(dfp.get("N"), errors="coerce")
                            fallback_ids = pd.Series(dfp.index.to_numpy(dtype=int) + 1, index=dfp.index, dtype=float)
                        else:
                            fallback_ids = pd.Series(dfp.index.to_numpy(dtype=int) + 1, index=dfp.index, dtype=float)
                            n_ids = fallback_ids.copy()
                        n_ids = n_ids.fillna(fallback_ids).astype(int).tolist()

                        texts = [f"NNA{int(i)}" for i in n_ids] if show_labels else [""] * len(dfp)
                        mode_nna = "markers+text" if show_labels else "markers"

                        hover_cols = []
                        for col in ("classification", "d_min", "ATOM", "Sym"):
                            if col in dfp.columns:
                                hover_cols.append(col)
                        customdata = dfp[hover_cols].to_numpy() if hover_cols else None

                        hovertemplate = "<b>%{text}</b><br>" if show_labels else "<b>NNA</b><br>"
                        for i, col in enumerate(hover_cols):
                            if col == "d_min":
                                hovertemplate += f"{col}: %{{customdata[{i}]}} Å<br>"
                            else:
                                hovertemplate += f"{col}: %{{customdata[{i}]}}<br>"
                        hovertemplate += "x=%{x:.4f} Å<br>y=%{y:.4f} Å<br>z=%{z:.4f} Å<extra></extra>"

                        nna_legend_flag = True
                        fig.add_trace(
                            go.Scatter3d(
                                x=dfp["X_ANGSTROM"],
                                y=dfp["Y_ANGSTROM"],
                                z=dfp["Z_ANGSTROM"],
                                mode=mode_nna,
                                text=texts,
                                textposition="top center",
                                name="NNA",
                                marker=dict(
                                    size=nna_size,
                                    symbol="cross",
                                    color="black",
                                    opacity=1.0,
                                    line=dict(width=2, color="black"),
                                ),
                                customdata=customdata,
                                hovertemplate=hovertemplate,
                                legendgroup=nna_legend_group,
                                showlegend=False,
                            )
                        )
        except Exception:
            # Never fail plotting due to NNA overlay
            pass

        # --- CP legend proxies (2D Scatter) ---
        # Use lightweight 2D legend-only traces so the legend symbol/color matches exactly
        # while clicks still toggle the real 3D traces through legendgroup.
        # Re-check CCP directly from the figure traces to avoid any stale flag creating a false legend item.
        try:
            ccp_trace_present = False
            for tr in getattr(fig, "data", ()):
                try:
                    if getattr(tr, "type", "") != "scatter3d":
                        continue
                    if getattr(tr, "legendgroup", None) != cp_legend_groups["ccp"]:
                        continue
                    xs = list(getattr(tr, "x", []) or [])
                    ys = list(getattr(tr, "y", []) or [])
                    zs = list(getattr(tr, "z", []) or [])
                    has_xyz = False
                    for xv, yv, zv in zip(xs, ys, zs):
                        if xv is None or yv is None or zv is None:
                            continue
                        try:
                            if np.isfinite(float(xv)) and np.isfinite(float(yv)) and np.isfinite(float(zv)):
                                has_xyz = True
                                break
                        except Exception:
                            continue
                    if has_xyz:
                        ccp_trace_present = True
                        break
                except Exception:
                    continue
            cp_legend_seen["ccp"] = bool(ccp_trace_present)
            cp_legend_flags["ccp"] = bool(ccp_trace_present and cp_plot_counts.get("ccp", 0) > 0)
            if nna_legend_flag:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="NNA",
                    marker=dict(size=9, symbol="cross", color="black", line=dict(width=2, color="black")),
                    legendgroup=nna_legend_group,
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_flags.get("bcp", False) and cp_plot_counts.get("bcp", 0) > 0:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="BCP",
                    marker=dict(size=9, symbol="diamond-open", color="#000000", line=dict(width=2, color="black")),
                    legendgroup=cp_legend_groups["bcp"],
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_flags.get("rcp", False) and cp_plot_counts.get("rcp", 0) > 0:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="RCP",
                    marker=dict(size=9, symbol="square-open", color="#7f7f7f", line=dict(width=1, color="DarkSlateGrey")),
                    legendgroup=cp_legend_groups["rcp"],
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_seen.get("ccp", False) and cp_legend_flags.get("ccp", False) and cp_plot_counts.get("ccp", 0) > 0:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="CCP",
                    marker=dict(size=9, symbol="x", color="limegreen", line=dict(width=1, color="DarkSlateGrey")),
                    legendgroup=cp_legend_groups["ccp"],
                    showlegend=True, hoverinfo="skip"
                ))
        except Exception:
            pass

        legend_kwargs = dict(y=0.98, yanchor='top', x=0.02, xanchor='left', groupclick='togglegroup')
        if (self.var_mode.get().strip() or "linear").lower() == "geometric":
            legend_kwargs["title"] = 'Isosurface levels'

        fig.update_layout(
            legend=legend_kwargs,
            title={
                "text": title_txt,
                "x": 0.98,
                "xanchor": "right",
            },
            xaxis=dict(visible=False, showgrid=False, zeroline=False),
            yaxis=dict(visible=False, showgrid=False, zeroline=False),
        )
        self.lbl_status.config(text=f"Plotting {surf}… (this opens in your browser window)")

        # Optional: export HTML (v2-like behavior)
        if getattr(self, "var_save_html", None) is not None and self.var_save_html.get():
            try:
                out_dir = Path(rd)
                out_dir.mkdir(parents=True, exist_ok=True)

                def _sanitize(s: str) -> str:
                    return re.sub(r"[^A-Za-z0-9_\-]+", "_", s).strip("_")

                base = _sanitize(out_dir.name)
                surf_tag = _sanitize(surf)

                # Match v2 naming style: replace '.' so filenames stay clean
                try:
                    if (self.var_mode.get().strip() == "geometric") and ("levels" in locals()) and levels:
                        mi_val = min(levels)
                        mx_val = max(levels)
                    else:
                        mi_val = isomin
                        mx_val = isomax
                except Exception:
                    mi_val = isomin
                    mx_val = isomax

                mi = str(mi_val).replace(".", "")
                mx = str(mx_val).replace(".", "")
                html_name = f"{base}_{surf_tag}_{mi}_{mx}.html"
                html_path = out_dir / html_name

                fig.write_html(str(html_path), include_plotlyjs=True)
                self.lbl_status.config(text=f"Saved HTML: {html_path}")
            except Exception as e:
                self.lbl_status.config(text=f"HTML export failed: {e}")
        _show_plotly_figure(fig)
        self.lbl_status.config(text="Done.")

    def refresh_state(self):
        # PL2D Viewer must work even when TRHO prerequisites are missing (no fort.9 / *.f9).
        # We only require that a folder was selected and that a valid run folder is selected.
        ws = self.app.ctx.workspace_dir
        has_ws = bool(ws)

        # Allow selecting runs even if workspace_ok is False
        self.cmb_runs.configure(state=("readonly" if has_ws else "disabled"))

        # Enable plot only when a run is selected AND a SURF*.DAT is selected.
        run_selected = bool(self.run_var.get().strip())
        surf_selected = bool(self.surf_var.get().strip())
        self.btn_plot.configure(state=("normal" if has_ws and run_selected and surf_selected else "disabled"))
        self.app.ctx.pl2d_view_ready = bool(has_ws and run_selected and surf_selected)




class DataFrameTable(ttk.Frame):
    """Simple DataFrame viewer (Treeview) with optional quick filter and header sorting.
    Designed to be lightweight and dependency-free (Tkinter only).

    Robust to duplicate DataFrame column labels by keeping internal Treeview ids
    per column position while preserving the user-facing header text.
    """

    def __init__(self, parent, *, df: Optional[pd.DataFrame] = None, title: str = ""):
        super().__init__(parent)
        self._df_full = pd.DataFrame() if df is None else df.copy()
        self._df_view = self._df_full.copy()
        self._sort_col: Optional[int] = None
        self._sort_ascending: bool = True
        self._column_widths: Dict[str, int] = {}
        self._tree_col_ids: List[str] = []
        self._tree_col_labels: List[str] = []

        # Top bar: title + filter
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(1, weight=1)

        self.title_lbl = ttk.Label(top, text=title, font=("TkDefaultFont", 11, "bold"))
        self.title_lbl.grid(row=0, column=0, sticky="w")

        self.filter_var = tk.StringVar(value="")
        ttk.Label(top, text="Filter:").grid(row=0, column=1, sticky="e", padx=(10, 4))
        self.filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=28)
        self.filter_entry.grid(row=0, column=2, sticky="e")
        self.filter_entry.bind("<Return>", lambda _e: self.apply_filter())
        self.filter_entry.bind("<Escape>", lambda _e: self.clear_filter())

        btns = ttk.Frame(top)
        btns.grid(row=0, column=3, sticky="e", padx=(8, 0))
        ttk.Button(btns, text="Apply", command=self.apply_filter).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Clear", command=self.clear_filter).grid(row=0, column=1)

        self.filter_help_var = tk.StringVar(
            value=(
                "Use column N as the selection index for PL2D-centered projects. "
                "Filter searches the typed text across all visible columns. "
                "Click a column header to sort in ascending/descending order."
            )
        )
        ttk.Label(top, textvariable=self.filter_help_var, foreground="#555", wraplength=900, justify="left").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        # Treeview + scrollbars
        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(body, show="headings")
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # Bottom bar
        bottom = ttk.Frame(self)
        bottom.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        self.info_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.info_var, foreground="#555").grid(row=0, column=0, sticky="w")

        self._build_from_df(self._df_full)

    def set_df(self, df: pd.DataFrame, title: Optional[str] = None):
        self._df_full = pd.DataFrame() if df is None else df.copy()
        self._df_view = self._df_full.copy()
        self._sort_col = None
        self._sort_ascending = True
        if title is not None:
            self.title_lbl.configure(text=title)
        self.filter_var.set("")
        self._build_from_df(self._df_full)

    def _series_at(self, df: pd.DataFrame, idx: int) -> pd.Series:
        return df.iloc[:, idx]

    def _label_at(self, idx: int) -> str:
        try:
            return str(self._tree_col_labels[idx])
        except Exception:
            return str(idx)

    def _tree_id_at(self, idx: int) -> str:
        return f"c{idx}"

    def _column_anchor(self, colname: str, series: Optional[pd.Series] = None) -> str:
        if str(colname).upper() == "N":
            return "center"
        if series is not None:
            try:
                if pd.api.types.is_numeric_dtype(series):
                    return "e"
            except Exception:
                pass
        return "w"

    def _heading_text(self, col_idx: int) -> str:
        label = self._label_at(col_idx)
        if self._sort_col == col_idx:
            label += " ↑" if self._sort_ascending else " ↓"
        return label

    def _preferred_width(self, colname: str, series: Optional[pd.Series] = None) -> int:
        name = str(colname)
        up = name.upper()
        if up == "N":
            return 70
        if up in {"BCP_ELEM", "ELEM1", "ELEM2"}:
            return 90
        if "ANG" in up:
            return 90
        if up in {"RHO", "GRHO", "GKIN", "KKIN", "VIRIAL", "ELF", "ELLIP", "LAP", "LAMI1", "LAMI2", "LAMI3"}:
            return 90
        if up in {"ADIM_RATIO", "BOND_DEGREE"}:
            return 110

        header_w = max(90, min(220, 10 * len(name) + 18))
        value_w = 0
        if series is not None:
            try:
                sample = series.head(80)
                max_len = max((len(str(v)) for v in sample), default=0)
                value_w = min(220, max(0, 9 * max_len + 18))
            except Exception:
                value_w = 0
        return max(header_w, value_w)

    def _build_from_df(self, df: pd.DataFrame):
        # Clear tree
        for col in self.tree["columns"]:
            self.tree.heading(col, text="")
        self.tree.delete(*self.tree.get_children())
        self._tree_col_ids = []
        self._tree_col_labels = []

        if df is None or df.empty:
            self.tree["columns"] = ("(empty)",)
            self.tree.heading("(empty)", text="(no data)")
            self.tree.column("(empty)", width=200, minwidth=120, anchor="w", stretch=False)
            self.info_var.set("Rows: 0")
            return

        self._tree_col_labels = [str(c) for c in list(df.columns)]
        self._tree_col_ids = [self._tree_id_at(i) for i in range(len(self._tree_col_labels))]
        self.tree["columns"] = self._tree_col_ids

        # Configure columns (preserve widths across sorting/filtering; do not auto-shrink to fit window)
        for i, cid in enumerate(self._tree_col_ids):
            label = self._label_at(i)
            series = self._series_at(df, i)
            width_key = f"{label}__{i}"
            self.tree.heading(cid, text=self._heading_text(i), command=lambda idx=i: self.sort_by(idx))
            width = self._column_widths.get(width_key, self._preferred_width(label, series))
            self._column_widths[width_key] = width
            self.tree.column(cid, width=width, minwidth=max(60, min(width, 90)), anchor=self._column_anchor(label, series), stretch=False)

        # Insert rows (stringified; keep light)
        max_rows = 2000  # guard: avoid freezing UI on huge tables
        shown = min(len(df), max_rows)
        for i in range(shown):
            row = df.iloc[i]
            vals = []
            for j in range(len(self._tree_col_ids)):
                v = row.iloc[j]
                if isinstance(v, float):
                    vals.append(f"{v:.6g}")
                else:
                    vals.append(str(v))
            self.tree.insert("", "end", values=vals)

        sort_msg = ""
        if self._sort_col is not None:
            sort_msg = f" | Sorted by {self._label_at(self._sort_col)} ({'ascending' if self._sort_ascending else 'descending'})"
        if len(df) > max_rows:
            self.info_var.set(f"Rows: {shown} shown (of {len(df)}). Refine filter to see others.{sort_msg}")
        else:
            self.info_var.set(f"Rows: {len(df)}{sort_msg}")

    def _apply_sort_to_view(self):
        df = self._df_view.copy()
        if df is None or df.empty or self._sort_col is None or self._sort_col >= len(df.columns):
            self._build_from_df(df)
            return
        col_idx = self._sort_col
        ascending = self._sort_ascending
        try:
            sort_series = self._series_at(df, col_idx)
            numeric = pd.to_numeric(sort_series, errors="coerce")
            if numeric.notna().sum() > 0:
                tmp = df.copy()
                tmp["__sort_key__"] = numeric.to_numpy()
                tmp = tmp.sort_values(by="__sort_key__", ascending=ascending, kind="mergesort", na_position="last")
                df = tmp.drop(columns=["__sort_key__"])
            else:
                tmp = df.copy()
                tmp["__sort_key__"] = sort_series.astype(str).str.lower().to_numpy()
                tmp = tmp.sort_values(by="__sort_key__", ascending=ascending, kind="mergesort", na_position="last")
                df = tmp.drop(columns=["__sort_key__"])
        except Exception:
            pass
        self._df_view = df.reset_index(drop=True)
        self._build_from_df(self._df_view)

    def sort_by(self, col_idx: int):
        if self._sort_col == col_idx:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_col = col_idx
            self._sort_ascending = True
        self._apply_sort_to_view()

    def apply_filter(self):
        q = (self.filter_var.get() or "").strip()
        if not q:
            self._df_view = self._df_full.copy()
            self._apply_sort_to_view()
            return

        df = self._df_full
        # Simple contains filter across all columns (string form)
        mask = np.zeros(len(df), dtype=bool)
        for j in range(len(df.columns)):
            try:
                mask |= self._series_at(df, j).astype(str).str.contains(q, case=False, na=False).to_numpy()
            except Exception:
                continue
        self._df_view = df.loc[mask].copy()
        self._apply_sort_to_view()

    def clear_filter(self):
        self.filter_var.set("")
        self._df_view = self._df_full.copy()
        self._apply_sort_to_view()


class ReportViewerWindow(tk.Toplevel):
    """Professional report viewer for TRHO/Topological CP data."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        _ensure_floating_window(self)
        self.title("TopIso3D v2026 — Reports Viewer")
        self.geometry("1100x720")
        self.minsize(980, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Reports Viewer", font=("TkDefaultFont", 16, "bold")).pack(side="left")
        self.subtitle_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.subtitle_var, foreground="#555").pack(side="left", padx=(12, 0))

        btns = ttk.Frame(header)
        btns.pack(side="right")
        self.btn_export_excel = ttk.Button(btns, text="Export Excel…", command=self._export_excel)
        self.btn_export_excel.pack(side="left", padx=(0, 8))
        self.btn_export_csv = ttk.Button(btns, text="Export CSV…", command=self._export_bcp_csv)
        self.btn_export_csv.pack(side="left")

        ttk.Separator(outer).pack(fill="x", pady=(10, 10))

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)

        # Tabs
        self.tab_summary = ttk.Frame(self.nb)
        self.tab_atoms = ttk.Frame(self.nb)
        self.tab_nna = ttk.Frame(self.nb)
        self.tab_bcp = ttk.Frame(self.nb)
        self.tab_rcp = ttk.Frame(self.nb)
        self.tab_ccp = ttk.Frame(self.nb)

        self.nb.add(self.tab_summary, text="Summary")
        self.nb.add(self.tab_atoms, text="TRUE atoms")
        self.nb.add(self.tab_nna, text="NNA")
        self.nb.add(self.tab_bcp, text="BCP")
        self.nb.add(self.tab_rcp, text="RCP")
        self.nb.add(self.tab_ccp, text="CCP")

        # Summary content
        self.summary_text = tk.Text(self.tab_summary, wrap="word", height=20)
        self.summary_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.summary_text.configure(state="disabled")

        # Tables
        self.tbl_atoms = DataFrameTable(self.tab_atoms, title="TRUE atoms")
        self.tbl_atoms.pack(fill="both", expand=True, padx=8, pady=8)

        self.nna_info_var = tk.StringVar(value="")
        ttk.Label(self.tab_nna, textvariable=self.nna_info_var, foreground="#555", justify="left").pack(anchor="w", padx=8, pady=(8, 0))
        self.tbl_nna = DataFrameTable(self.tab_nna, title="NNA")
        self.tbl_nna.pack(fill="both", expand=True, padx=8, pady=8)

        # --- BCP tab: evaluation plots + table ---
        bcp_outer = ttk.Frame(self.tab_bcp)
        bcp_outer.pack(fill="both", expand=True, padx=8, pady=8)
        bcp_top = ttk.Frame(bcp_outer)
        bcp_top.pack(fill="x")
        ttk.Button(bcp_top, text="Open BCP evaluation plots (Plotly)", command=self._open_bcp_eval_plots).pack(side="left")
        ttk.Label(bcp_top, text="(hover shows BCP id)", foreground="#555").pack(side="left", padx=(10,0))
        self.tbl_bcp = DataFrameTable(bcp_outer, title="BCP properties")
        self.tbl_bcp.pack(fill="both", expand=True, pady=(8,0))

        self.tbl_rcp = DataFrameTable(self.tab_rcp, title="RCP properties")
        self.tbl_rcp.pack(fill="both", expand=True, padx=8, pady=8)

        self.tbl_ccp = DataFrameTable(self.tab_ccp, title="CCP properties")
        self.tbl_ccp.pack(fill="both", expand=True, padx=8, pady=8)

        # Defer the first refresh until Tk has finished mapping the window.
        # On macOS this avoids a partially constructed/blank Toplevel if a table
        # refresh is triggered while the window is still being created.
        try:
            self.after_idle(self.refresh)
        except Exception:
            self.refresh()

    def _on_close(self):
        """Close Reports Viewer cleanly and clear the app-side reference.

        The window protocol references this method during __init__.  Without it,
        Tk creates the Toplevel and then raises AttributeError before the widgets
        are fully built, which appears on macOS as a blank Reports window.
        """
        try:
            if getattr(self.app, "_report_viewer_win", None) is self:
                self.app._report_viewer_win = None
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def refresh(self):
        ctx = self.app.ctx
        method = str(getattr(ctx, "report_method", "TRHO") or "TRHO").strip().upper()
        if method == "TLAP":
            self._refresh_tlap()
        else:
            self._refresh_trho()

    def _refresh_trho(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        try:
            self.btn_export_csv.configure(text="Export BCP CSV…")
            self.nb.tab(self.tab_summary, text="Summary")
            self.nb.tab(self.tab_atoms, text="TRUE atoms")
            self.nb.tab(self.tab_nna, text="NNA")
            self.nb.tab(self.tab_bcp, text="BCP")
            self.nb.tab(self.tab_rcp, text="RCP")
            self.nb.tab(self.tab_ccp, text="CCP")
        except Exception:
            pass
        if parsed is None or not ctx.trho_done:
            self.subtitle_var.set("(no TRHO parsed yet)")
            self._set_summary("No TRHO data parsed yet. Run TRHO first (Compute → TRHO).")
            self.tbl_atoms.set_df(pd.DataFrame(), title="TRUE atoms")
            self.tbl_nna.set_df(pd.DataFrame(), title="NNA")
            self.tbl_bcp.set_df(pd.DataFrame(), title="BCP")
            self.tbl_rcp.set_df(pd.DataFrame(), title="RCP")
            self.tbl_ccp.set_df(pd.DataFrame(), title="CCP")
            return
        df_true = getattr(parsed, "df_true_atoms", pd.DataFrame())
        df_nna = getattr(parsed, "df_att_nao_nucl", pd.DataFrame())
        df_bcp = getattr(parsed, "df_bcp_props", pd.DataFrame())
        df_rcp = getattr(parsed, "df_rcp_props", pd.DataFrame())
        df_ccp = getattr(parsed, "df_ccp_props", pd.DataFrame())
        nbcp = len(df_bcp) if df_bcp is not None else 0
        nnna = len(df_nna) if df_nna is not None else 0
        nrcp = len(df_rcp) if df_rcp is not None else 0
        nccp = len(df_ccp) if df_ccp is not None else 0
        ntrue = len(df_true) if df_true is not None else 0
        self.subtitle_var.set("")
        try:
            self.nb.tab(self.tab_atoms, text=f"TRUE atoms ({ntrue})")
            self.nb.tab(self.tab_nna, text=f"NNA ({nnna})")
            self.nb.tab(self.tab_bcp, text=f"BCP ({nbcp})")
            self.nb.tab(self.tab_rcp, text=f"RCP ({nrcp})")
            self.nb.tab(self.tab_ccp, text=f"CCP ({nccp})")
        except Exception:
            pass
        def _with_seq(df: pd.DataFrame, colname: str = "N") -> pd.DataFrame:
            if df is None:
                df = pd.DataFrame()
            out = df.copy()
            if out.empty:
                return out
            if colname not in out.columns:
                out.insert(0, colname, np.arange(1, len(out) + 1))
            return out
        def _reorder_bcp_columns(df: pd.DataFrame) -> pd.DataFrame:
            if df is None or df.empty:
                return df
            hidden_in_report = {"ATTR1_ATOM_ID", "ATTR2_ATOM_ID", "ATTR1_TRAJ_LEN_ANG", "ATTR2_TRAJ_LEN_ANG", "ATTR1_X_ANGSTROM", "ATTR1_Y_ANGSTROM", "ATTR1_Z_ANGSTROM", "ATTR2_X_ANGSTROM", "ATTR2_Y_ANGSTROM", "ATTR2_Z_ANGSTROM"}
            preferred = ["N", "BCP_ELEM", "ELEM1", "DIST_ELEM1_ANG", "ELEM2", "DIST_ELEM2_ANG"]
            visible_cols = [c for c in df.columns if c not in hidden_in_report]
            cols = [c for c in preferred if c in visible_cols] + [c for c in visible_cols if c not in preferred]
            return _compact_bcp_dist_headers(df.loc[:, cols].copy())
        self.tbl_atoms.set_df(_with_seq(df_true), title=f"TRUE atoms ({ntrue}) — selection index: N")
        nna_cutoff = float(getattr(parsed, "nna_cutoff_ang", getattr(ctx, "nna_cutoff_ang", 0.35)) or 0.35)
        self.nna_info_var.set(f"Classification cutoff: {nna_cutoff:.3f} Å. Flagged (3,-3) attractors with d_min ≤ cutoff are labeled 'likely pseudopotential artifact'; otherwise 'likely NNA'.")
        self.tbl_nna.set_df(_with_seq(df_nna), title=f"NNA ({nnna}) — selection index: N")
        self.tbl_bcp.set_df(_reorder_bcp_columns(_with_seq(df_bcp)), title=f"BCP ({nbcp}) — selection index: N")
        self.tbl_rcp.set_df(_with_seq(df_rcp), title=f"RCP ({nrcp}) — selection index: N")
        self.tbl_ccp.set_df(_with_seq(df_ccp), title=f"CCP ({nccp}) — selection index: N")
        lines = []
        ws = ctx.workspace_dir if ctx.workspace_dir else None
        if ws:
            lines.append(f"Workspace: {ws}")
        lines.append(f"TRHO done: {ctx.trho_done}")
        lines.append(f"NNA flagged in TRHO: {nnna}")
        cp_count_txt = int(getattr(parsed, "nna_count", 0) or 0)
        lines.append(f"NNA textual count in trho.out: {cp_count_txt}")
        lines.append(f"NNA classification cutoff (Å): {float(getattr(parsed, 'nna_cutoff_ang', getattr(ctx, 'nna_cutoff_ang', 0.35)) or 0.35):.3f}")
        lines.append("")
        lines.append("Tips:")
        lines.append("  • Use the Filter box in each tab (press Enter) to quickly find atoms/CPs.")
        lines.append("  • Export buttons write files to your workspace directory.")
        lines.append("  • In the NNA tab, CP_ID should match the CP numbering reported by the external terminal script.")
        self._set_summary("\n".join(lines))

    def _refresh_tlap(self):
        ctx = self.app.ctx
        self.app.ensure_active_tlap_parsed()
        parsed = getattr(ctx, "tlap_parsed", None)
        try:
            self.btn_export_csv.configure(text="Export TLAP CSV…")
            self.nb.tab(self.tab_summary, text="Summary")
            self.nb.tab(self.tab_atoms, text="TRUE atoms")
            self.nb.tab(self.tab_nna, text="Primitive")
            self.nb.tab(self.tab_bcp, text="TLAP CP")
            self.nb.tab(self.tab_rcp, text="By NEA")
            self.nb.tab(self.tab_ccp, text="Run info")
        except Exception:
            pass
        if parsed is None:
            self.subtitle_var.set("(no TLAP parsed yet)")
            err = getattr(ctx, "tlap_parse_error", None)
            self._set_summary(f"No TLAP data parsed yet. {err or 'Run TLAP first.'}")
            self.tbl_atoms.set_df(pd.DataFrame(), title="TRUE atoms")
            self.tbl_nna.set_df(pd.DataFrame(), title="Primitive")
            self.tbl_bcp.set_df(pd.DataFrame(), title="TLAP CP")
            self.tbl_rcp.set_df(pd.DataFrame(), title="By NEA")
            self.tbl_ccp.set_df(pd.DataFrame(), title="Run info")
            self.nna_info_var.set("TLAP report mode: primitive atoms are shown in this tab for structural context.")
            return
        df_true = getattr(parsed, "df_true_atoms", pd.DataFrame())
        df_prim = getattr(parsed, "df_primitive", pd.DataFrame())
        df_cp = getattr(parsed, "df_cp_props", pd.DataFrame())
        df_nea = getattr(parsed, "df_by_nea", pd.DataFrame())
        summary = getattr(parsed, "summary", {}) or {}
        self.subtitle_var.set("")
        def _with_seq(df: pd.DataFrame, colname: str = "N") -> pd.DataFrame:
            if df is None:
                df = pd.DataFrame()
            out = df.copy()
            if out.empty:
                return out
            if colname not in out.columns:
                out.insert(0, colname, np.arange(1, len(out) + 1))
            return out
        self.tbl_atoms.set_df(_with_seq(_report_display_df(df_true)), title=f"TRUE atoms ({len(df_true)}) — selection index: N")
        self.tbl_nna.set_df(_with_seq(_report_display_df(df_prim)), title=f"Primitive atoms ({len(df_prim)}) — selection index: N")
        self.tbl_bcp.set_df(_with_seq(_tlap_report_cp_df(df_cp)), title=f"TLAP CP ({len(df_cp)}) — selection index: N")
        self.tbl_rcp.set_df(_with_seq(_report_display_df(df_nea)), title=f"By NEA ({len(df_nea)}) — selection index: N")
        self.tbl_ccp.set_df(pd.DataFrame(), title="Run info")
        self.nna_info_var.set("TLAP report mode: primitive atoms are shown in this tab for structural context.")
        lines = []
        ws = ctx.workspace_dir if ctx.workspace_dir else None
        if ws:
            lines.append(f"Workspace: {ws}")
        lines.append(f"TLAP active run: {getattr(ctx, 'active_tlap_label', '—')}")
        if parsed.source_trho_run:
            lines.append(f"Source TRHO run: {parsed.source_trho_run}")
        lines.append(f"Algorithm: {summary.get('algorithm') or '—'}")
        lines.append(f"IAUTO: {summary.get('iauto') if summary.get('iauto') is not None else '—'}")
        lines.append(f"CP type: {summary.get('itype') or '—'}")
        lines.append(f"Angular grid (theta, phi): {summary.get('nt')}, {summary.get('np')}")
        lines.append(f"NEAs analyzed: {summary.get('n_neas', 0)}")
        lines.append(f"NEAs with CPs: {summary.get('n_neas_with_cps', 0)}")
        lines.append(f"Total CPs found: {summary.get('total_cps', 0)}")
        lines.append("")
        lines.append("Tips:")
        lines.append("  • TLAP CPs belong to -∇²ρ and are not BCP/RCP/CCP objects from TRHO.")
        lines.append("  • The 'By NEA' tab summarizes how many TLAP CPs were found for each non-equivalent atom.")
        self._set_summary("\n".join(lines))

    def _open_bcp_eval_plots(self):
        """Open the 3 classic BCP evaluation plots (like v1-6) with BCP identification on hover."""
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        if parsed is None or getattr(parsed, "df_bcp_props", None) is None or parsed.df_bcp_props.empty:
            messagebox.showwarning("BCP evaluation", "No BCP properties available (run/parse TRHO first).")
            return
        df = parsed.df_bcp_props.copy()
        # Ensure BCP id and label exist for hover
        df["BCP_ID"] = df.index.astype(int)
        df["BCP_LABEL"] = df["BCP_ID"].apply(lambda i: f"BCP{i}")
        if "BCP_ELEM" not in df.columns:
            df["BCP_ELEM"] = "BCP"

        # Choose a concise set of hover fields (only if present)
        if "DIST_ELEM1_ANG" in df.columns:
            df["DIST_(ANG)_1"] = df["DIST_ELEM1_ANG"]
        if "DIST_ELEM2_ANG" in df.columns:
            df["DIST_(ANG)_2"] = df["DIST_ELEM2_ANG"]

        base_hover = {}
        for col in ("BCP_ID", "ELEM1", "DIST_(ANG)_1", "ELEM2", "DIST_(ANG)_2", "RHO", "LAP", "ADIM_RATIO", "BOND_DEGREE", "ELLIP"):
            if col in df.columns:
                base_hover[col] = True

        try:
            fig2 = px.scatter(
                df, x="LAP", y="BOND_DEGREE", color="BCP_ELEM",
                hover_name="BCP_LABEL", hover_data=base_hover,
                title="∇²ρ (a.u.) × H/ρ (a.u.)"
            )
            fig2.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

            fig3 = px.scatter(
                df, x="ADIM_RATIO", y="BOND_DEGREE", color="BCP_ELEM",
                hover_name="BCP_LABEL", hover_data=base_hover,
                title="|V|/G (a.u.) × H/ρ (a.u.)"
            )
            fig3.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

            fig4 = px.scatter(
                df, x="ADIM_RATIO", y="LAP", color="BCP_ELEM",
                hover_name="BCP_LABEL", hover_data=base_hover,
                title="|V|/G (a.u.) × ∇²ρ (a.u.)"
            )
            fig4.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

            _show_plotly_figure(fig2)
            _show_plotly_figure(fig3)
            _show_plotly_figure(fig4)
        except Exception as e:
            messagebox.showerror("BCP evaluation", f"Failed to build plots: {e}")

    def _set_summary(self, text: str):
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", text)
        self.summary_text.configure(state="disabled")

    def _export_excel(self):
        # Reuse ReportsPage logic (write to workspace)
        page = self.app.pages.get("Reports")
        if page and hasattr(page, "export_xlsx"):
            page.export_xlsx()

    def _export_bcp_csv(self):
        page = self.app.pages.get("Reports")
        if page and hasattr(page, "export_csv"):
            page.export_csv()



# -----------------------------
# BCP Evaluation Page (3 classic plots)
# -----------------------------


# -----------------------------
# ATBP Page (STD-only, Phase 1)
# -----------------------------
class ATBPPage(BasePage):
    title = "ATBP"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.out_path: Optional[Path] = None
        self.run_dir: Optional[Path] = None
        self.df_atbp: Optional[pd.DataFrame] = None
        self._last_workspace_dir: Optional[Path] = None
        self._atbp_run_options: Dict[str, Path] = {}

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="ATBP (TOPOND) — default STD with optional UNI Balanced and UNI Fast",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        frm_out = ttk.LabelFrame(body, text="ATBP run / output (workspace/atbp_runs)")
        frm_out.pack(fill="x", pady=(0, 10))

        self.var_include_topo = tk.BooleanVar(value=True)
        self.var_atbp_mode = tk.StringVar(value="STD")
        self.var_out = tk.StringVar(value="")
        self.var_atbp_run = tk.StringVar(value="—")

        row_runs = ttk.Frame(frm_out)
        row_runs.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(row_runs, text="Existing ATBP runs (workspace/atbp_runs)").pack(side="left")
        self.cmb_atbp_runs = ttk.Combobox(row_runs, textvariable=self.var_atbp_run, values=(), state="readonly", width=42)
        self.cmb_atbp_runs.pack(side="left", fill="x", expand=True, padx=(10, 8))
        self.cmb_atbp_runs.bind("<<ComboboxSelected>>", self._on_select_run)
        self.btn_refresh_runs = ttk.Button(row_runs, text="Refresh list", command=self._refresh_run_selector)
        self.btn_refresh_runs.pack(side="left")

        row1 = ttk.Frame(frm_out)
        row1.pack(fill="x", padx=10, pady=10)

        ttk.Checkbutton(
            row1,
            text="Include TOPO wrapper (tolerant default)",
            variable=self.var_include_topo
        ).pack(side="left")

        ttk.Label(row1, text="Mode:").pack(side="left", padx=(14, 6))
        self.cmb_atbp_mode = ttk.Combobox(
            row1,
            textvariable=self.var_atbp_mode,
            values=("STD", "UNI Balanced", "UNI Fast"),
            width=14,
            state="readonly",
        )
        self.cmb_atbp_mode.pack(side="left")
        try:
            self.cmb_atbp_mode.current(0)
        except Exception:
            pass

        self.btn_abort = ttk.Button(row1, text="Abort", command=lambda: self.app.abort_current_job("ATBP"))
        self.btn_abort.pack(side="right")
        self.btn_abort.configure(state="disabled")

        self.btn_run = ttk.Button(row1, text="Run ATBP", command=self._run_atbp)
        self.btn_run.pack(side="right", padx=(0, 8))

        self._pb_row = ttk.Frame(frm_out)
        self._pb_row.pack(fill="x", padx=10, pady=(0, 10))

        self.lbl_runhint = ttk.Label(self._pb_row, text=" ")
        self.lbl_runhint.pack(side="left")

        self.pb = ttk.Progressbar(self._pb_row, mode="indeterminate")
        self.pb.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.pb.stop()

        frm_res = ttk.LabelFrame(body, text="Parsed results")
        frm_res.pack(fill="both", expand=True)

        toolbar = ttk.Frame(frm_res)
        toolbar.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Button(toolbar, text="Export CSV…", command=self._export_csv).pack(side="right")
        ttk.Button(toolbar, text="Export JSON…", command=self._export_json).pack(side="right", padx=(8, 0))

        self.lbl_status = ttk.Label(toolbar, text="—")
        self.lbl_status.pack(side="left")

        cols = ("atom_index", "symbol", "n_omega", "charge", "volume", "source")
        self.tree = ttk.Treeview(frm_res, columns=cols, show="headings", height=14)
        for c in cols:
            self.tree.heading(c, text=("Bader charge" if c == "charge" else c))
            self.tree.column(c, width=120, anchor="center")
        self.tree.column("symbol", width=90)
        self.tree.column("source", width=90)

        vsb = ttk.Scrollbar(frm_res, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        vsb.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))

    def on_show(self):
        self._refresh_run_selector()
        self._sync_output_path()
        self.refresh_state()

    def _refresh_run_selector(self):
        try:
            values, options, active_label = self.app._get_atbp_run_selector_data()
        except Exception:
            values, options, active_label = [], {}, "—"
        self._atbp_run_options = options
        try:
            self.cmb_atbp_runs.configure(values=values)
        except Exception:
            pass
        if values:
            try:
                if self.var_atbp_run.get() not in options:
                    self.var_atbp_run.set(active_label)
            except Exception:
                self.var_atbp_run.set(active_label)
        else:
            self.var_atbp_run.set("—")

    def _on_select_run(self, _event=None):
        label = str(self.var_atbp_run.get() or "").strip()
        run_dir = self._atbp_run_options.get(label)
        if run_dir is None:
            return
        try:
            self.app._set_active_atbp_run(run_dir)
        except Exception:
            return
        self._sync_output_path()
        try:
            self._parse_output(silent=True)
        except Exception:
            pass
        self.refresh_state()

    def _clear_current_results(self) -> None:
        self.df_atbp = None
        try:
            self.app.ctx.df_atbp = None
        except Exception:
            pass
        try:
            self.app.ctx.atbp_out_path = None
        except Exception:
            pass
        try:
            self._refresh_tree()
        except Exception:
            pass

    def _sync_output_path(self) -> None:
        try:
            ws = self.app.ctx.workspace_dir
            ws_changed = ws != self._last_workspace_dir
            if ws_changed:
                self._last_workspace_dir = ws
                self._clear_current_results()
                self._refresh_run_selector()
                try:
                    self.lbl_status.config(text="—")
                except Exception:
                    pass

            if ws and ws.exists():
                active_run = None
                try:
                    sel = self._atbp_run_options.get(str(self.var_atbp_run.get() or "").strip())
                    active_run = sel if sel is not None else self.app._get_active_atbp_dir()
                except Exception:
                    active_run = None
                cand = self.app._find_matching_output_in_dirs([active_run], self.app._configured_output_names("atbp")) if active_run is not None else None
                if cand is not None and cand.exists():
                    if self.out_path != cand:
                        self._clear_current_results()
                    self.out_path = cand
                    self.run_dir = cand.parent
                    self.var_out.set(str(cand))
                    self.app.ctx.atbp_out_path = cand
                    try:
                        self.app.ctx.atbp_run_dir = self.run_dir
                    except Exception:
                        pass
                else:
                    had_results = self.df_atbp is not None and not getattr(self.df_atbp, "empty", True)
                    self.out_path = None
                    base_dir = (ws / "atbp_runs")
                    self.run_dir = active_run if active_run is not None else base_dir
                    preferred_name = self.app._preferred_output_name("atbp", "atbp.out")
                    default_dir = self.run_dir if self.run_dir is not None else (base_dir / "atbp_001")
                    self.var_out.set(str(default_dir / preferred_name))
                    self.app.ctx.atbp_out_path = None
                    try:
                        self.app.ctx.atbp_run_dir = self.run_dir
                    except Exception:
                        pass
                    if had_results or ws_changed:
                        self._clear_current_results()
                    try:
                        if active_run is not None:
                            self.lbl_status.config(text=f"No ATBP output found for selected run: {Path(active_run).name}")
                        else:
                            self.lbl_status.config(text=f"No ATBP output found yet for current workspace: {base_dir}")
                    except Exception:
                        pass
            else:
                self.out_path = None
                self.run_dir = None
                self.var_out.set("")
                self.app.ctx.atbp_out_path = None
                try:
                    self.app.ctx.atbp_run_dir = None
                except Exception:
                    pass
                if ws_changed:
                    try:
                        self.lbl_status.config(text="—")
                    except Exception:
                        pass
        except Exception:
            pass

    def _set_running(self, running: bool, hint: str = "") -> None:
        try:
            if running:
                self.lbl_runhint.configure(text=hint or "Running… (ATBP may take a long time)")
                self.pb.start(12)
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state="disabled")
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="normal")
            else:
                self.pb.stop()
                self.lbl_runhint.configure(text=" ")
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state=("normal" if (self.app.ctx.workspace_ok and self.app.ctx.trho_done and (not self.app._job_running)) else "disabled"))
                if hasattr(self, "btn_abort"):
                    self.btn_abort.configure(state="disabled")
        except Exception:
            pass

    def _prompt_missing_uni_tols(self, mode: str, true_atoms_df: Optional[pd.DataFrame]) -> Dict[str, float]:
        if normalize_atbp_mode(mode) == "STD":
            return {}
        cache = getattr(self, "_atbp_manual_tol_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._atbp_manual_tol_cache = cache
        missing = _atbp_missing_tol_symbols(true_atoms_df)
        missing = [sym for sym in missing if sym not in cache]
        if not missing:
            return dict(cache)
        vals: Dict[str, float] = dict(cache)
        for sym in missing:
            dlg = _TolPromptDialog(self, sym)
            if dlg.value is None:
                raise ValueError("ATBP run cancelled: missing TOL value(s) for UNI mode.")
            vals[sym] = float(dlg.value)
            cache[sym] = float(dlg.value)
        return vals

    def _build_current_snippet(self, *, prompt_missing_tols: bool = True) -> str:
        mode = normalize_atbp_mode(self.var_atbp_mode.get())
        try:
            self.var_atbp_mode.set(mode)
        except Exception:
            pass
        include_topo = bool(self.var_include_topo.get())
        true_atoms_df = getattr(self.app.ctx, "df_true_atoms", None)
        if true_atoms_df is None or getattr(true_atoms_df, "empty", True):
            parsed = getattr(self.app.ctx, "trho_parsed", None)
            true_atoms_df = getattr(parsed, "df_true_atoms", None) if parsed is not None else None
        tol_overrides = (
            self._prompt_missing_uni_tols(mode, true_atoms_df)
            if prompt_missing_tols
            else dict(getattr(self, "_atbp_manual_tol_cache", {}) or {})
        )
        return build_atbp_input(
            mode=mode,
            include_topo_wrapper=include_topo,
            include_nna_section=False,
            true_atoms_df=true_atoms_df,
            tol_overrides=tol_overrides,
        )

    def _run_atbp(self) -> None:
        if not messagebox.askyesno(
            "Run ATBP",
            "ATBP calculations can be computationally demanding and may take a long time to finish.\n\nDo you want to continue?",
        ):
            return
        self._set_running(True)
        try:
            snippet = self._build_current_snippet(prompt_missing_tols=True)
            self._last_atbp_snippet = snippet
            self.app.run_atbp(snippet)
        except Exception as e:
            self._set_running(False)
            messagebox.showerror("ATBP", str(e))

    def on_atbp_done(self, out_path: Optional[Path] = None) -> None:
        try:
            self._refresh_run_selector()
            if out_path is not None:
                self.out_path = Path(out_path)
                self.run_dir = self.out_path.parent
                self.var_out.set(str(self.out_path))
                self.app.ctx.atbp_out_path = self.out_path
                try:
                    self.app.ctx.atbp_run_dir = self.run_dir
                except Exception:
                    pass
                # keep selector aligned with the new active run
                try:
                    values, options, active_label = self.app._get_atbp_run_selector_data()
                    self._atbp_run_options = options
                    self.cmb_atbp_runs.configure(values=values)
                    if active_label != "—":
                        self.var_atbp_run.set(active_label)
                except Exception:
                    pass
                self._parse_output(silent=True)
        finally:
            self._set_running(False)

    def on_atbp_fail(self, msg: Optional[str] = None) -> None:
        self._set_running(False)
        if msg and ("aborted" not in str(msg).lower()):
            messagebox.showerror("ATBP", msg)

    def _parse_output(self, silent: bool = False):
        try:
            ws = self.app.ctx.workspace_dir
            if not ws:
                if not silent:
                    messagebox.showwarning("ATBP", "No workspace selected. Go to Workspace page first.")
                return

            active_run = self._atbp_run_options.get(str(self.var_atbp_run.get() or "").strip())
            if active_run is None:
                active_run = self.app._get_active_atbp_dir()
            if active_run is None:
                active_run = self.run_dir if self.run_dir is not None else (ws / "atbp_runs")
            self.run_dir = active_run
            outp = self.app._find_matching_output_in_dirs([self.run_dir], self.app._configured_output_names("atbp"))
            if outp is None:
                outp = self.run_dir / self.app._preferred_output_name("atbp", "atbp.out")
            self.out_path = outp
            self.var_out.set(str(outp))
            self.app.ctx.atbp_out_path = outp if outp.exists() else None

            if not outp.exists():
                self._clear_current_results()
                self.out_path = None
                self.app.ctx.atbp_out_path = None
                try:
                    self.lbl_status.config(text=f"No ATBP output found for selected run: {Path(self.run_dir).name}")
                except Exception:
                    self.lbl_status.config(text="No ATBP output found yet.")
                if not silent:
                    messagebox.showwarning("ATBP", f"ATBP output not found:\n{outp}")
                return

            snip_src = getattr(self, "_last_atbp_snippet", None)
            if isinstance(snip_src, str) and snip_src.strip():
                snip = snip_src.strip() + "\n"
            else:
                snip = self._build_current_snippet(prompt_missing_tols=False).strip() + "\n"
            (self.run_dir / "atbp_snippet.txt").write_text(snip, encoding="utf-8")
            (self.run_dir / "source_output_path.txt").write_text(str(outp), encoding="utf-8")

            df = parse_atbp_output(outp)
            self.df_atbp = df
            try:
                self.app.ctx.df_atbp = df
            except Exception:
                pass
            (self.run_dir / "atbp_parse.json").write_text(df.to_json(orient="records", indent=2), encoding="utf-8")

            self._refresh_tree()
            if df.empty:
                self.lbl_status.config(text=f"Parsed 0 rows (no recognizable ATBP tables). Run saved: {self.run_dir.name}")
                if not silent:
                    messagebox.showinfo(
                        "ATBP",
                        "I couldn't recognize an ATBP results table in this output.\n\n"
                        "If you send me a real atbp.out snippet, I can harden the parser quickly."
                    )
            else:
                self.lbl_status.config(text=f"Parsed {len(df)} atoms automatically. Run saved: {self.run_dir.name}")
        except Exception as e:
            self.lbl_status.config(text=f"Parse failed: {e}")
            if not silent:
                messagebox.showerror("ATBP", f"Parse failed:\n{e}\n\n{traceback.format_exc()}")

    def _refresh_tree(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        df = self.df_atbp
        if df is None or df.empty:
            return

        def fmt(x):
            if x is None or (isinstance(x, float) and np.isnan(x)):
                return ""
            if isinstance(x, (float, np.floating)):
                return f"{x:.6f}"
            return str(x)

        for _, r in df.iterrows():
            vals = (
                int(r.get("atom_index")) if pd.notna(r.get("atom_index")) else "",
                str(r.get("symbol") or ""),
                fmt(r.get("n_omega")),
                fmt(r.get("charge")),
                fmt(r.get("volume")),
                str(r.get("source") or ""),
            )
            self.tree.insert("", "end", values=vals)

    def refresh_state(self):
        self._refresh_run_selector()
        self._sync_output_path()
        ready = self.app.ctx.workspace_ok and self.app.ctx.trho_done and (not self.app._job_running)
        try:
            self.btn_run.configure(state=("normal" if ready else "disabled"))
        except Exception:
            pass
        try:
            if self.out_path is not None and self.out_path.exists() and self.df_atbp is None:
                self._parse_output(silent=True)
        except Exception:
            pass
        if self.app._job_running and str(getattr(self.app, "_active_job_kind", "") or "").upper() == "ATBP":
            self._set_running(True, "Running… (ATBP may take a long time)")
        else:
            self._set_running(False)

    def _export_csv(self):
        if self.df_atbp is None or self.df_atbp.empty:
            messagebox.showinfo("ATBP", "Nothing to export yet.")
            return
        initial = None
        try:
            if self.run_dir:
                initial = str(self.run_dir)
        except Exception:
            initial = None

        fp = filedialog.asksaveasfilename(
            title="Save ATBP table as CSV",
            initialdir=initial,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if fp:
            self.df_atbp.to_csv(fp, index=False)
            self.lbl_status.config(text=f"CSV exported: {Path(fp).name}")

    def _export_json(self):
        if self.df_atbp is None or self.df_atbp.empty:
            messagebox.showinfo("ATBP", "Nothing to export yet.")
            return
        initial = None
        try:
            if self.run_dir:
                initial = str(self.run_dir)
        except Exception:
            initial = None

        fp = filedialog.asksaveasfilename(
            title="Save ATBP table as JSON",
            initialdir=initial,
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if fp:
            Path(fp).write_text(self.df_atbp.to_json(orient="records", indent=2), encoding="utf-8")
            self.lbl_status.config(text=f"JSON exported: {Path(fp).name}")

class BCPEvalPage(BasePage):
    title = "BCP Evaluation"

    def _build(self):
        ttk.Label(self, text="BCP Evaluation — classic QTAIM classification plots", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=14, pady=(14, 8))
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=8)

        msg = (
            "These three plots are the classic TopIso3D BCP evaluation set:\n"
            "  • ∇²ρ × H/ρ\n"
            "  • |V|/G × H/ρ\n"
            "  • |V|/G × ∇²ρ\n\n"
            "Hover shows BCP id (BCP1, BCP2, …) and key properties."
        )
        ttk.Label(body, text=msg, justify="left").pack(anchor="w")

        controls_row = ttk.Frame(body)
        controls_row.pack(anchor="w", pady=(10, 0), fill="x")

        scale_row = ttk.Frame(controls_row)
        scale_row.pack(side="left", anchor="w")
        ttk.Label(scale_row, text="Axis scaling:").pack(side="left")
        self.var_scale_mode = tk.StringVar(value="Threshold-guided")
        self.cmb_scale_mode = ttk.Combobox(
            scale_row,
            textvariable=self.var_scale_mode,
            state="readonly",
            width=24,
            values=("Auto", "Auto + thresholds", "Threshold-guided"),
        )
        self.cmb_scale_mode.pack(side="left", padx=(8, 0))


        btns = ttk.Frame(body)
        btns.pack(anchor="w", pady=(12, 0), fill="x")
        btns.columnconfigure(0, weight=0)
        btns.columnconfigure(1, weight=0)
        btns.columnconfigure(2, weight=0)

        ttk.Label(btns, text="Plot").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(btns, text="Open").grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Label(btns, text="Save HTML").grid(row=0, column=2, sticky="w")

        ttk.Label(btns, text="∇²ρ × H/ρ").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_open_lap_bd = ttk.Button(btns, text="Open", command=self._open_lap_bd)
        self.btn_open_lap_bd.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_save_lap_bd = ttk.Button(btns, text="Save HTML", command=self._save_lap_bd)
        self.btn_save_lap_bd.grid(row=1, column=2, sticky="w", pady=(6, 0))

        ttk.Label(btns, text="|V|/G × H/ρ").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_open_adim_bd = ttk.Button(btns, text="Open", command=self._open_adim_bd)
        self.btn_open_adim_bd.grid(row=2, column=1, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_save_adim_bd = ttk.Button(btns, text="Save HTML", command=self._save_adim_bd)
        self.btn_save_adim_bd.grid(row=2, column=2, sticky="w", pady=(6, 0))

        ttk.Label(btns, text="|V|/G × ∇²ρ").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_open_adim_lap = ttk.Button(btns, text="Open", command=self._open_adim_lap)
        self.btn_open_adim_lap.grid(row=3, column=1, sticky="w", padx=(0, 10), pady=(6, 0))
        self.btn_save_adim_lap = ttk.Button(btns, text="Save HTML", command=self._save_adim_lap)
        self.btn_save_adim_lap.grid(row=3, column=2, sticky="w", pady=(6, 0))
        self.status_var = tk.StringVar(value="(Run TRHO / parse trho.out first.)")
        ttk.Label(body, textvariable=self.status_var, foreground="#555").pack(anchor="w", pady=(12, 0))

    def refresh_state(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        ok = bool(ctx.trho_done and parsed is not None and getattr(parsed, "df_bcp_props", None) is not None and (not parsed.df_bcp_props.empty))
        self.btn_open_lap_bd.configure(state=("normal" if ok else "disabled"))
        self.btn_open_adim_bd.configure(state=("normal" if ok else "disabled"))
        self.btn_open_adim_lap.configure(state=("normal" if ok else "disabled"))
        self.btn_save_lap_bd.configure(state=("normal" if ok and bool(ctx.workspace_dir) else "disabled"))
        self.btn_save_adim_bd.configure(state=("normal" if ok and bool(ctx.workspace_dir) else "disabled"))
        self.btn_save_adim_lap.configure(state=("normal" if ok and bool(ctx.workspace_dir) else "disabled"))
        try:
            self.cmb_scale_mode.configure(state=("readonly" if ok else "disabled"))
        except Exception:
            pass
        if ok:
            self.status_var.set(f"BCPs available: {len(parsed.df_bcp_props)}")
        else:
            self.status_var.set("Run TRHO and parse trho.out first.")

    def _descriptor_thresholds(self, key: str) -> tuple[list[float], list[str], bool]:
        key = str(key or "").strip().upper()
        if key == "BOND_DEGREE":
            return [0.0], ["H/ρ = 0"], True
        if key == "LAP":
            return [0.0], ["∇²ρ = 0"], True
        if key == "ADIM_RATIO":
            return [1.0, 2.0], ["|V|/G = 1", "|V|/G = 2"], False
        return [], [], False

    def _classify_descriptor_value(self, key: str, value) -> str:
        try:
            v = float(value)
        except Exception:
            return "—"
        if not np.isfinite(v):
            return "—"
        key = str(key or "").strip().upper()
        tol = 1e-12
        if key == "LAP":
            if abs(v) <= tol:
                return "Boundary (∇²ρ = 0)"
            return "Concentration" if v < 0 else "Depletion"
        if key == "BOND_DEGREE":
            if abs(v) <= tol:
                return "Transient"
            return "Covalent" if v < 0 else "Ionic / vdW"
        if key == "ADIM_RATIO":
            if v < 1.0 - tol:
                return "Covalent"
            if v > 2.0 + tol:
                return "Ionic / vdW"
            return "Transient"
        return "—"

    def _add_classification_region_labels(self, fig, *, x_key: str, y_key: str, x_range: Optional[list[float]], y_range: Optional[list[float]]):
        # v6: remove region labels from the plot area to avoid cross-descriptor conflicts.
        return

    def _guided_axis_range(self, series: pd.Series, thresholds: list[float], *, symmetric_zero: bool = False) -> Optional[list[float]]:
        vals = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
        vals = vals[np.isfinite(vals)]
        extras = np.asarray(list(thresholds or []), dtype=float)
        extras = extras[np.isfinite(extras)] if extras.size else extras
        if vals.size == 0 and extras.size == 0:
            return None
        if vals.size:
            vmin = float(np.min(vals))
            vmax = float(np.max(vals))
        else:
            vmin = vmax = float(extras[0])
        if extras.size:
            vmin = min(vmin, float(np.min(extras)))
            vmax = max(vmax, float(np.max(extras)))
        if symmetric_zero:
            lim = max(abs(vmin), abs(vmax), *(abs(float(x)) for x in extras.tolist() if np.isfinite(x)))
            if lim == 0:
                lim = 1.0
            pad = max(0.05 * lim, 0.02)
            lim += pad
            return [-lim, lim]
        span = vmax - vmin
        if span <= 0:
            pad = max(0.05 * max(abs(vmin), abs(vmax), 1.0), 0.02)
        else:
            pad = max(0.07 * span, 0.02)
        return [vmin - pad, vmax + pad]

    def _anchor_for_range(self, lo: float, hi: float, axis_min: float, axis_max: float, *, fallback: float = 0.5) -> float:
        lo_eff = axis_min if not np.isfinite(lo) else max(axis_min, float(lo))
        hi_eff = axis_max if not np.isfinite(hi) else min(axis_max, float(hi))
        if hi_eff <= lo_eff:
            return fallback
        frac = (0.5 * (lo_eff + hi_eff) - axis_min) / max(axis_max - axis_min, 1e-12)
        return max(0.06, min(0.94, float(frac)))

    def _apply_bcp_eval_guides(self, fig, df: pd.DataFrame, *, x_key: str, y_key: str):
        mode = str(getattr(self, "var_scale_mode", tk.StringVar(value="Threshold-guided")).get() or "Threshold-guided").strip().lower()
        x_thresholds, x_labels, x_sym = self._descriptor_thresholds(x_key)
        y_thresholds, y_labels, y_sym = self._descriptor_thresholds(y_key)
        x_range = None
        y_range = None

        if mode == "threshold-guided":
            x_range = self._guided_axis_range(df[x_key], x_thresholds, symmetric_zero=x_sym) if x_key in df.columns else None
            y_range = self._guided_axis_range(df[y_key], y_thresholds, symmetric_zero=y_sym) if y_key in df.columns else None
            if x_range is not None:
                fig.update_xaxes(range=x_range)
            if y_range is not None:
                fig.update_yaxes(range=y_range)
        else:
            try:
                xr = fig.layout.xaxis.range
                yr = fig.layout.yaxis.range
                x_range = [float(xr[0]), float(xr[1])] if xr and len(xr) == 2 else None
                y_range = [float(yr[0]), float(yr[1])] if yr and len(yr) == 2 else None
            except Exception:
                x_range = None
                y_range = None

        if mode in ("auto + thresholds", "threshold-guided"):
            guide_color = "rgba(60,60,60,0.85)"
            ann_bg = "rgba(255,255,255,0.72)"
            for thr, label in zip(x_thresholds, x_labels):
                fig.add_vline(x=float(thr), line_width=2, line_dash="dash", line_color=guide_color)
                fig.add_annotation(
                    x=float(thr), y=1.0,
                    xref="x", yref="paper",
                    text=label,
                    showarrow=False,
                    yanchor="bottom",
                    yshift=8,
                    font=dict(size=11, color="black"),
                    bgcolor=ann_bg,
                    bordercolor="rgba(80,80,80,0.25)",
                    borderpad=2,
                )
            for thr, label in zip(y_thresholds, y_labels):
                fig.add_hline(y=float(thr), line_width=2, line_dash="dash", line_color=guide_color)
                fig.add_annotation(
                    x=1.0, y=float(thr),
                    xref="paper", yref="y",
                    text=label,
                    showarrow=False,
                    xanchor="left",
                    xshift=8,
                    font=dict(size=11, color="black"),
                    bgcolor=ann_bg,
                    bordercolor="rgba(80,80,80,0.25)",
                    borderpad=2,
                )
            if x_range is None:
                try:
                    xr = fig.layout.xaxis.range
                    x_range = [float(xr[0]), float(xr[1])] if xr and len(xr) == 2 else None
                except Exception:
                    x_range = None
            if y_range is None:
                try:
                    yr = fig.layout.yaxis.range
                    y_range = [float(yr[0]), float(yr[1])] if yr and len(yr) == 2 else None
                except Exception:
                    y_range = None
            fig.update_layout(margin=dict(t=80, r=90, b=60, l=70))
        else:
            fig.update_layout(margin=dict(t=60, r=40, b=60, l=70))
        return fig

    def _build_figs(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        if parsed is None or getattr(parsed, "df_bcp_props", None) is None or parsed.df_bcp_props.empty:
            raise RuntimeError("No BCP properties available.")
        df = parsed.df_bcp_props.copy()
        df["BCP_ID"] = df.index.astype(int)
        df["BCP_LABEL"] = df["BCP_ID"].apply(lambda i: f"BCP{i}")
        if "BCP_ELEM" not in df.columns:
            df["BCP_ELEM"] = "BCP"

        hover = {}
        hover_fmt = {
            "BCP_ID": True,
            "BCP_ELEM": True,
            "ELEM1": True,
            "ELEM2": True,
            "RHO": ":.4f",
            "LAP": ":.4f",
            "ADIM_RATIO": ":.4f",
            "BOND_DEGREE": ":.4f",
            "ELLIP": ":.4e",
        }
        for col, fmt in hover_fmt.items():
            if col in df.columns:
                hover[col] = fmt

        if "BOND_DEGREE" in df.columns:
            df["H_RHO_CLASS"] = df["BOND_DEGREE"].apply(lambda v: self._classify_descriptor_value("BOND_DEGREE", v))
            hover["H/ρ class"] = True
            df["H/ρ class"] = df.pop("H_RHO_CLASS")
        if "ADIM_RATIO" in df.columns:
            df["VG_CLASS"] = df["ADIM_RATIO"].apply(lambda v: self._classify_descriptor_value("ADIM_RATIO", v))
            hover["|V|/G class"] = True
            df["|V|/G class"] = df.pop("VG_CLASS")
        if "LAP" in df.columns:
            df["LAP_CLASS"] = df["LAP"].apply(lambda v: self._classify_descriptor_value("LAP", v))
            hover["Laplacian class"] = True
            df["Laplacian class"] = df.pop("LAP_CLASS")

        fig2 = px.scatter(df, x="LAP", y="BOND_DEGREE", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="∇²ρ (a.u.) × H/ρ (a.u.)")
        fig2.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))
        self._apply_bcp_eval_guides(fig2, df, x_key="LAP", y_key="BOND_DEGREE")

        fig3 = px.scatter(df, x="ADIM_RATIO", y="BOND_DEGREE", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="|V|/G (a.u.) × H/ρ (a.u.)")
        fig3.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))
        self._apply_bcp_eval_guides(fig3, df, x_key="ADIM_RATIO", y_key="BOND_DEGREE")

        fig4 = px.scatter(df, x="ADIM_RATIO", y="LAP", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="|V|/G (a.u.) × ∇²ρ (a.u.)")
        fig4.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))
        self._apply_bcp_eval_guides(fig4, df, x_key="ADIM_RATIO", y_key="LAP")

        return fig2, fig3, fig4

    def _open_lap_bd(self):
        try:
            fig2, _, _ = self._build_figs()
            _show_plotly_figure(fig2)
        except Exception as e:
            messagebox.showerror("BCP Evaluation", str(e))

    def _open_adim_bd(self):
        try:
            _, fig3, _ = self._build_figs()
            _show_plotly_figure(fig3)
        except Exception as e:
            messagebox.showerror("BCP Evaluation", str(e))

    def _open_adim_lap(self):
        try:
            _, _, fig4 = self._build_figs()
            _show_plotly_figure(fig4)
        except Exception as e:
            messagebox.showerror("BCP Evaluation", str(e))

    def _save_lap_bd(self):
        ctx = self.app.ctx
        if not ctx.workspace_dir:
            messagebox.showwarning("BCP Evaluation", "No workspace directory set.")
            return
        try:
            fig2, _, _ = self._build_figs()
            pdir = Path(ctx.workspace_dir) / "bcp_evaluation"
            pdir.mkdir(parents=True, exist_ok=True)
            fig2.write_html(str(pdir / "BCP_eval_LAP_x_BOND_DEGREE.html"), include_plotlyjs=True)
            self.status_var.set("Saved: " + str(pdir / "BCP_eval_LAP_x_BOND_DEGREE.html"))
        except Exception as e:
            messagebox.showerror("BCP Evaluation", f"Failed to save plot: {e}")

    def _save_adim_bd(self):
        ctx = self.app.ctx
        if not ctx.workspace_dir:
            messagebox.showwarning("BCP Evaluation", "No workspace directory set.")
            return
        try:
            _, fig3, _ = self._build_figs()
            pdir = Path(ctx.workspace_dir) / "bcp_evaluation"
            pdir.mkdir(parents=True, exist_ok=True)
            fig3.write_html(str(pdir / "BCP_eval_ADIM_RATIO_x_BOND_DEGREE.html"), include_plotlyjs=True)
            self.status_var.set("Saved: " + str(pdir / "BCP_eval_ADIM_RATIO_x_BOND_DEGREE.html"))
        except Exception as e:
            messagebox.showerror("BCP Evaluation", f"Failed to save plot: {e}")

    def _save_adim_lap(self):
        ctx = self.app.ctx
        if not ctx.workspace_dir:
            messagebox.showwarning("BCP Evaluation", "No workspace directory set.")
            return
        try:
            _, _, fig4 = self._build_figs()
            pdir = Path(ctx.workspace_dir) / "bcp_evaluation"
            pdir.mkdir(parents=True, exist_ok=True)
            fig4.write_html(str(pdir / "BCP_eval_ADIM_RATIO_x_LAP.html"), include_plotlyjs=True)
            self.status_var.set("Saved: " + str(pdir / "BCP_eval_ADIM_RATIO_x_LAP.html"))
        except Exception as e:
            messagebox.showerror("BCP Evaluation", f"Failed to save plot: {e}")

class ReportsPage(ttk.Frame):
    """Step B: generate reports right after TRHO is available."""

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Reports", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Separator(self).pack(fill="x", pady=(10, 14))

        outer, canvas, vbar, content = _make_scrollable_frame(self)
        outer.pack(fill="both", expand=True)
        self._scroll_outer = outer
        self._scroll_canvas = canvas
        self._scroll_vbar = vbar
        self._scroll_inner = content
        self.content = content
        self.content.columnconfigure(0, weight=1)

        self.summary_var = tk.StringVar(value="No report data parsed yet.")
        ttk.Label(self.content, textvariable=self.summary_var, wraplength=900).grid(row=2, column=0, sticky="w", pady=(0, 8))

        self._build_method_and_run_selectors()
        self.content.columnconfigure(0, weight=1)

        self.topology_title_var = tk.StringVar(value="")
        self.topology_formula_var = tk.StringVar(value="")
        self.topology_expected_var = tk.StringVar(value="")
        self.topology_status_var = tk.StringVar(value="")
        self.topology_note_var = tk.StringVar(value="")

        topo_box = ttk.Frame(self.content, padding=(12, 10))
        topo_box.grid(row=4, column=0, sticky="ew", pady=(2, 16))
        topo_box.columnconfigure(0, weight=1)

        ttk.Label(topo_box, textvariable=self.topology_title_var, font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(topo_box, textvariable=self.topology_formula_var, wraplength=900, justify="left").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(topo_box, textvariable=self.topology_expected_var, wraplength=900, justify="left").grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )
        self.topology_status_label = ttk.Label(
            topo_box, textvariable=self.topology_status_var, wraplength=900, justify="left", font=("TkDefaultFont", 10, "bold")
        )
        self.topology_status_label.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.topology_note_label = ttk.Label(
            topo_box, textvariable=self.topology_note_var, wraplength=900, justify="left", foreground="#555"
        )
        self.topology_note_label.grid(row=4, column=0, sticky="w", pady=(4, 0))

        btns = ttk.Frame(self.content)
        btns.grid(row=5, column=0, sticky="w")
        self.btn_export_xlsx = ttk.Button(btns, text="Export Excel report (final_report.xlsx)", command=self.export_xlsx)
        self.btn_export_xlsx.grid(row=0, column=0, sticky="w")
        self.btn_export_csv = ttk.Button(btns, text="Export CSV", command=self.export_csv)
        self.btn_export_csv.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.btn_open_viewer = ttk.Button(btns, text="Open Reports Viewer", command=self.open_viewer)
        self.btn_open_viewer.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.hint_var = tk.StringVar(value="Use Method + Active run to inspect TRHO or TLAP results.")
        ttk.Label(self.content, textvariable=self.hint_var, foreground="#555").grid(row=6, column=0, sticky="w", pady=(12, 0))

        self.refresh()

    def refresh_state(self):
        self.refresh()

    def _build_method_and_run_selectors(self):
        parent = getattr(self, "content", self)
        frm = ttk.LabelFrame(parent, text="Report source", padding=(12, 6))
        frm.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        frm.columnconfigure(0, weight=1)

        row1 = ttk.Frame(frm)
        row1.pack(fill="x")
        ttk.Label(row1, text="Method:").pack(side="left", padx=(0, 8))
        self.var_report_method = tk.StringVar(value=str(getattr(self.app.ctx, "report_method", "TRHO") or "TRHO"))
        self.cmb_report_method = ttk.Combobox(row1, textvariable=self.var_report_method, values=("TRHO", "TLAP"), state="readonly", width=10)
        self.cmb_report_method.pack(side="left")
        self.cmb_report_method.bind("<<ComboboxSelected>>", lambda _e: self._on_select_report_method())

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Using TRHO run:").pack(side="left", padx=(0, 8))
        self.var_active_trho_run = tk.StringVar(value="—")
        self.cmb_active_trho = ttk.Combobox(row2, textvariable=self.var_active_trho_run, values=(), state="readonly", width=46)
        self.cmb_active_trho.pack(side="left", fill="x", expand=True)
        self.cmb_active_trho.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_trho_run())

        row3 = ttk.Frame(frm)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Using TLAP run:").pack(side="left", padx=(0, 8))
        self.var_active_tlap_run = tk.StringVar(value="—")
        self.cmb_active_tlap = ttk.Combobox(row3, textvariable=self.var_active_tlap_run, values=(), state="readonly", width=46)
        self.cmb_active_tlap.pack(side="left", fill="x", expand=True)
        self.cmb_active_tlap.bind("<<ComboboxSelected>>", lambda _e: self._on_select_active_tlap_run())

        self._trho_run_options = {}
        self._tlap_run_options = {}

    def _on_select_report_method(self):
        self.app.ctx.report_method = (self.var_report_method.get() or "TRHO").strip().upper()
        self.refresh()

    def _refresh_run_selectors(self):
        app = self.app
        self.var_report_method.set(str(getattr(app.ctx, "report_method", "TRHO") or "TRHO").strip().upper())
        values, options, active_label = app._get_trho_run_selector_data()
        self._trho_run_options = options
        self.cmb_active_trho.configure(values=values)
        self.var_active_trho_run.set(active_label if values else "—")
        values_t, options_t, active_label_t = app._get_tlap_run_selector_data()
        self._tlap_run_options = options_t
        self.cmb_active_tlap.configure(values=values_t)
        self.var_active_tlap_run.set(active_label_t if values_t else "—")
        method = self.var_report_method.get().strip().upper()
        self.cmb_active_trho.configure(state=("readonly" if (method == "TRHO" and values and not app._job_running) else "disabled"))
        self.cmb_active_tlap.configure(state=("readonly" if (method == "TLAP" and values_t and not app._job_running) else "disabled"))

    def _on_select_active_trho_run(self):
        choice = (self.var_active_trho_run.get() or "").strip()
        run_dir = self._trho_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_trho_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_trho_run(Path(run_dir), refresh=True)
        self.app.set_status(f"Active TRHO run: {self.app._friendly_trho_run_label(Path(run_dir))}")
        self.refresh()

    def _on_select_active_tlap_run(self):
        choice = (self.var_active_tlap_run.get() or "").strip()
        run_dir = self._tlap_run_options.get(choice)
        if run_dir is None:
            return
        try:
            current = getattr(self.app.state, "active_tlap_run", None)
            if current is not None and Path(current).resolve() == Path(run_dir).resolve():
                return
        except Exception:
            pass
        self.app._set_active_tlap_run(Path(run_dir), refresh=True)
        self.app.set_status(f"Active TLAP run: {self.app._friendly_tlap_run_label(Path(run_dir))}")
        self.refresh()

    def refresh(self):
        self._refresh_run_selectors()
        method = (self.var_report_method.get() or "TRHO").strip().upper()
        self.app.ctx.report_method = method
        if method == "TLAP":
            self._refresh_tlap_report()
        else:
            self._refresh_trho_report()

    def _refresh_trho_report(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        if not ctx.trho_done:
            self.summary_var.set("No TRHO data parsed yet. Run TRHO (or parse an existing trho.out) first.")
            self.topology_title_var.set("")
            self.topology_formula_var.set("")
            self.topology_expected_var.set("")
            self.topology_status_var.set("")
            self.topology_note_var.set("")
            self.btn_export_xlsx.configure(state="disabled")
            self.btn_export_csv.configure(state="disabled", text="Export CSV")
            self.btn_open_viewer.configure(state="disabled")
            return
        if parsed is None:
            err = getattr(ctx, "trho_parse_error", None)
            self.summary_var.set(f"TRHO finished, but parsing failed: {err}" if err else "TRHO finished, but no parsed data is available. (Try re-running TRHO or check the output file.)")
            self.topology_title_var.set("")
            self.topology_formula_var.set("")
            self.topology_expected_var.set("")
            self.topology_status_var.set("")
            self.topology_note_var.set("")
            self.btn_export_xlsx.configure(state="disabled")
            self.btn_export_csv.configure(state="disabled", text="Export CSV")
            self.btn_open_viewer.configure(state="disabled")
            return
        nbcp = len(parsed.df_bcp_props) if hasattr(parsed, "df_bcp_props") else 0
        nrcp = len(parsed.df_ring) if hasattr(parsed, "df_ring") else 0
        nccp = len(parsed.df_cage) if hasattr(parsed, "df_cage") else 0
        ntrue = len(parsed.df_true_atoms) if hasattr(parsed, "df_true_atoms") else 0
        nna_count = int(getattr(parsed, "nna_count", 0) or 0)
        summary = f"TRHO parsed OK. TRUE atoms: {ntrue} | BCPs: {nbcp} | RCPs: {nrcp} | CCPs: {nccp}"
        if nna_count > 0:
            summary += f" | Possible NNAs: {nna_count}"
        self.summary_var.set(summary)
        morse_value = ntrue - nbcp + nrcp - nccp
        self.topology_title_var.set("Primitive-cell Morse balance")
        self.topology_formula_var.set(f"TRUE atoms - BCPs + RCPs - CCPs = {ntrue} - {nbcp} + {nrcp} - {nccp} = {morse_value}.")
        self.topology_expected_var.set("Expected value for the primitive-cell representation: 0")
        if morse_value == 0:
            self.topology_status_var.set("Primitive-cell Morse balance: satisfied")
            self.topology_status_label.configure(foreground="#1f7a1f")
            note = "The reported critical-point network satisfies the expected Morse balance in the primitive-cell representation used by TopIso3D."
            if nna_count > 0:
                note += f" TOPOND also flagged {nna_count} possible non-nuclear attractor(s) in trho.out."
            self.topology_note_var.set(note)
        else:
            self.topology_status_var.set("Primitive-cell Morse balance: not satisfied")
            self.topology_status_label.configure(foreground="#9c1c1c")
            note = "The reported critical-point network does not satisfy the expected Morse balance in the primitive-cell representation used by TopIso3D. This may indicate incomplete CP recovery under the current TRHO settings, incomplete parsing, missing CPs, or an interrupted calculation. This status is restricted to the primitive-cell representation and should not be interpreted as a full unit-cell diagnosis. Consider rerunning TRHO with a more sensitive IAUTO = -1 setup or using TOPOND directly for additional checks."
            if nna_count > 0:
                note += f" TOPOND flagged {nna_count} possible non-nuclear attractor(s) in trho.out."
            self.topology_note_var.set(note)
        self.btn_export_xlsx.configure(state="normal")
        self.btn_export_csv.configure(state="normal", text="Export CSV (BCP properties)")
        self.btn_open_viewer.configure(state="normal")

    def _refresh_tlap_report(self):
        ctx = self.app.ctx
        ok = self.app.ensure_active_tlap_parsed()
        parsed = getattr(ctx, "tlap_parsed", None)
        if (not ok) or parsed is None:
            err = getattr(ctx, "tlap_parse_error", None)
            self.summary_var.set(f"No TLAP data parsed yet. {err or 'Run TLAP first.'}")
            self.topology_title_var.set("TLAP critical points summary")
            self.topology_formula_var.set("")
            self.topology_expected_var.set("")
            self.topology_status_var.set("")
            self.topology_note_var.set("TLAP reports become available once an active TLAP run with tlap.out exists.")
            self.btn_export_xlsx.configure(state="disabled")
            self.btn_export_csv.configure(state="disabled", text="Export CSV")
            self.btn_open_viewer.configure(state="disabled")
            return
        summary = getattr(parsed, "summary", {}) or {}
        ntrue = len(parsed.df_true_atoms) if hasattr(parsed, "df_true_atoms") else 0
        nprim = len(parsed.df_primitive) if hasattr(parsed, "df_primitive") else 0
        ncp = len(parsed.df_cp_props) if hasattr(parsed, "df_cp_props") else 0
        nneas = int(summary.get("n_neas", 0) or 0)
        nneas_ok = int(summary.get("n_neas_with_cps", 0) or 0)
        self.summary_var.set(f"TLAP parsed OK. Primitive atoms: {nprim} | TRUE atoms: {ntrue} | NEAs analyzed: {nneas} | NEAs with CPs: {nneas_ok} | TLAP CPs: {ncp}")
        self.topology_title_var.set("TLAP critical points summary")
        self.topology_formula_var.set(f"Method = TLAP | Algorithm = {summary.get('algorithm') or '—'} | CP type = {summary.get('itype') or '—'} | Angular grid = ({summary.get('nt')}, {summary.get('np')})")
        self.topology_expected_var.set(f"Source TRHO run: {parsed.source_trho_run or getattr(ctx, 'active_trho_label', '—')}")
        self.topology_status_var.set("Status: Informative")
        self.topology_status_label.configure(foreground="#1f3f7a")
        self.topology_note_var.set("TLAP reports summarize critical points of -∇²ρ by non-equivalent atom. These objects are distinct from BCP/RCP/CCP data reported by TRHO.")
        self.btn_export_xlsx.configure(state="normal")
        self.btn_export_csv.configure(state="normal", text="Export CSV (TLAP CP properties)")
        self.btn_open_viewer.configure(state="normal")

    def open_viewer(self):
        try:
            win = getattr(self.app, "_report_viewer_win", None)
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                try:
                    win.refresh()
                except Exception:
                    pass
                return
        except Exception:
            pass
        win = ReportViewerWindow(self.app)
        self.app._report_viewer_win = win

    def _report_output_dir(self, method: str) -> Path:
        method = str(method or "TRHO").strip().upper()
        if method == "TLAP":
            run_dir = getattr(self.app.ctx, "active_tlap_run", None)
        else:
            run_dir = getattr(self.app.ctx, "active_trho_run", None)
        try:
            if run_dir is not None:
                return Path(run_dir)
        except Exception:
            pass
        ws = getattr(self.app.ctx, "workspace_dir", None)
        return Path(ws) if ws is not None else Path.cwd()

    def export_xlsx(self):
        ctx = self.app.ctx
        method = str(getattr(ctx, "report_method", "TRHO") or "TRHO").strip().upper()
        if not ctx.workspace_dir:
            messagebox.showwarning("Reports", "No workspace selected.")
            return
        out_dir = self._report_output_dir(method)
        out_path = out_dir / "final_report.xlsx"
        try:
            with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
                if method == "TLAP":
                    parsed: TlapParsed = getattr(ctx, "tlap_parsed", None)
                    if parsed is None:
                        raise ValueError("No parsed TLAP data available.")
                    _report_display_df(parsed.df_primitive).to_excel(writer, sheet_name="primitive")
                    _report_display_df(parsed.df_true_atoms).to_excel(writer, sheet_name="true_atoms")
                    _tlap_report_cp_df(parsed.df_cp_props).to_excel(writer, sheet_name="TLAP_CP_prop")
                    _report_display_df(parsed.df_by_nea).to_excel(writer, sheet_name="TLAP_by_NEA")
                    _report_display_df(_tlap_run_info_df(parsed, ctx)).to_excel(writer, sheet_name="TLAP_run_info")
                else:
                    parsed: TrhoParsed = getattr(ctx, "trho_parsed", None)
                    if parsed is None:
                        raise ValueError("No parsed TRHO data available.")
                    _report_display_df(parsed.df_primitive).to_excel(writer, sheet_name="primitive")
                    _report_display_df(parsed.df_true_atoms).to_excel(writer, sheet_name="true_atoms")
                    _report_display_df(parsed.df_bcp_coords).to_excel(writer, sheet_name="bcp_coords")
                    _report_display_df(parsed.df_attr).to_excel(writer, sheet_name="attr")
                    _report_display_df(parsed.df_ring).to_excel(writer, sheet_name="rcp")
                    _report_display_df(parsed.df_cage).to_excel(writer, sheet_name="ccp")
                    _report_display_df(_compact_bcp_dist_headers(parsed.df_bcp_props)).to_excel(writer, sheet_name="BCP_prop")
                    if parsed.df_rcp_props is not None:
                        _report_display_df(parsed.df_rcp_props).to_excel(writer, sheet_name="RCP_prop")
                    if parsed.df_ccp_props is not None:
                        _report_display_df(parsed.df_ccp_props).to_excel(writer, sheet_name="CCP_prop")
                    if not parsed.df_att_nao_nucl.empty:
                        _report_display_df(parsed.df_att_nao_nucl).to_excel(writer, sheet_name="att_nao_nucl")
            messagebox.showinfo("Reports", f"Excel report written:\n{out_path}")
            self.app._job_queue.put(("log", f"[REPORT] wrote {out_path}"))
        except Exception as e:
            messagebox.showerror("Reports", f"Failed to write Excel report: {e}")

    def export_csv(self):
        ctx = self.app.ctx
        method = str(getattr(ctx, "report_method", "TRHO") or "TRHO").strip().upper()
        if not ctx.workspace_dir:
            messagebox.showwarning("Reports", "No workspace selected.")
            return
        if method == "TLAP":
            parsed: TlapParsed = getattr(ctx, "tlap_parsed", None)
            if parsed is None:
                messagebox.showwarning("Reports", "No parsed TLAP data available.")
                return
            out_dir = self._report_output_dir(method)
            out_path = out_dir / "tlap_cp_properties.csv"
            try:
                _tlap_report_cp_df(parsed.df_cp_props).to_csv(out_path, index=False)
                messagebox.showinfo("Reports", f"CSV written:\n{out_path}")
                self.app._job_queue.put(("log", f"[REPORT] wrote {out_path}"))
            except Exception as e:
                messagebox.showerror("Reports", f"Failed to write CSV: {e}")
        else:
            parsed: TrhoParsed = getattr(ctx, "trho_parsed", None)
            if parsed is None:
                messagebox.showwarning("Reports", "No parsed TRHO data available.")
                return
            out_dir = self._report_output_dir(method)
            out_path = out_dir / "bcp_properties.csv"
            try:
                _report_display_df(_compact_bcp_dist_headers(parsed.df_bcp_props)).to_csv(out_path, index=False)
                messagebox.showinfo("Reports", f"CSV written:\n{out_path}")
                self.app._job_queue.put(("log", f"[REPORT] wrote {out_path}"))
            except Exception as e:
                messagebox.showerror("Reports", f"Failed to write CSV: {e}")

    def on_show(self):
        # Called by App.show_page(). We keep it lightweight and just refresh button states.
        try:
            self.app.refresh_ui_state()
        except Exception:
            pass



# --- Late bindings for PL2D campaign helpers (Phase 2) ---
# These helpers are defined at module level and attached here so the PL2DPage
# can use them as instance methods without changing the working PL2D layout.
try:
    PL2DPage._create_pl2d_run_dir = _create_pl2d_run_dir
    PL2DPage._compute_pl2d_zs = _compute_pl2d_zs
    PL2DPage._pl2d_flags_line = _pl2d_flags_line
    PL2DPage._write_pl2d_input_for_slice = _write_pl2d_input_for_slice
    PL2DPage._write_pl2d_unix_scripts = _write_pl2d_unix_scripts
    PL2DPage._export_pl2d_campaign = _export_pl2d_campaign
    PL2DPage._run_pl2d = _run_pl2d
except Exception:
    pass

if __name__ == "__main__":
    main()

# --- Hotfix v5: rebind PL2DPage campaign methods into the class ---
def _pl2dpage_run_pl2d_fixed(self):
    log_event(self.app.ctx, 'PL2D started')
    self.app.state.pl2d_running = True
    self.lbl_status.configure(text='⏳ PL2D running…')
    self.btn_run.configure(state='disabled')

    if not (self.app.state.workspace_ok and self.app.state.trho_parsed is not None):
        self.app.state.pl2d_running = False
        messagebox.showwarning('PL2D', 'Run/parse TRHO first.')
        return
    try:
        cfg = self._build_config()
    except Exception as e:
        self.app.state.pl2d_running = False
        messagebox.showerror('PL2D', f'Invalid configuration: {e}')
        return

    ctx = self.app.state
    prop_exe = getattr(ctx, 'properties_exe', None)
    exe_path = _best_effort_make_executable(str(prop_exe) if prop_exe is not None else None)
    if not exe_path:
        self.app.state.pl2d_running = False
        messagebox.showerror('PL2D', f'properties executable not found: {prop_exe}')
        return

    fort9_src = None
    if ctx.workspace_dir:
        cand = ctx.workspace_dir / 'fort.9'
        if cand.exists():
            fort9_src = cand
    if fort9_src is None:
        self.app.state.pl2d_running = False
        messagebox.showerror('PL2D', 'fort.9 not found in workspace. Make sure the workspace has fort.9 first.')
        return

    sig = self._signature(cfg)
    root = self._runs_root()
    root.mkdir(parents=True, exist_ok=True)
    ts, run_dir = self._create_pl2d_run_dir(root, cfg, sig)

    try:
        f9_stat = fort9_src.stat()
        f9_fp = {'size': int(f9_stat.st_size), 'mtime': int(f9_stat.st_mtime)}
    except Exception:
        f9_fp = {}

    mf = {
        'signature': sig,
        'created_at': ts,
        'config': cfg,
        'engine': 'properties',
        'properties_exe': str(exe_path),
        'project_name': str(cfg.get('project_name', '') or '') if bool(cfg.get('project_name_custom', False)) else '',
        'source': {'fort9': str(fort9_src), 'fort9_fp': f9_fp},
        'status': 'running',
    }
    (run_dir / 'manifest.json').write_text(json.dumps(mf, indent=2), encoding='utf-8')

    zs = self._compute_pl2d_zs(cfg)
    out_name = str(cfg.get('project_name') or (ctx.workspace_dir.name if ctx.workspace_dir else 'PL2D'))

    try:
        self.app.set_task(active=False)
    except Exception:
        pass

    ok_all = True
    try:
        self.pb.configure(maximum=len(zs), value=0)
        self.lbl_pb.configure(text=f'Slice 0/{len(zs)}')
        self.update_idletasks()
    except Exception:
        pass

    for i, z in enumerate(zs):
        sdir = run_dir / f'slice{i:03d}'
        sdir.mkdir()
        try:
            shutil.copy2(fort9_src, sdir / 'fort.9')
        except Exception as e:
            ok_all = False
            self.app._job_queue.put(('log', f'[PL2D] Failed to copy fort.9 to {sdir}: {e}'))
            break

        try:
            self._write_pl2d_input_for_slice(sdir, z, cfg, out_name=out_name)
        except Exception as e:
            ok_all = False
            self.app._job_queue.put(('log', f'[PL2D] Failed to write pl2d.inp in {sdir}: {e}'))
            break

        inp = sdir / 'pl2d.inp'
        out = sdir / 'pl2d.out'
        err = sdir / 'pl2d.err'
        try:
            with open(inp, 'r', encoding='utf-8', errors='ignore') as fin, \
                 open(out, 'w', encoding='utf-8') as fout, \
                 open(err, 'w', encoding='utf-8') as ferr:
                proc = subprocess.run(
                    [str(exe_path)],
                    stdin=fin,
                    stdout=fout,
                    stderr=ferr,
                    cwd=str(sdir),
                    **_windows_subprocess_silent_kwargs(),
                )
            if proc.returncode != 0:
                ok_all = False
                self.app._job_queue.put(('log', f'[PL2D] properties returned {proc.returncode} on slice {i:03d}'))
                try:
                    if err.exists():
                        err_txt = err.read_text(errors='ignore')
                        self.app._job_queue.put(('log', '[PL2D] STDERR:\n' + err_txt[-1000:]))
                    if out.exists():
                        out_txt = out.read_text(errors='ignore')
                        self.app._job_queue.put(('log', '[PL2D] STDOUT tail:\n' + out_txt[-1000:]))
                except Exception:
                    pass
                break
        except Exception as e:
            ok_all = False
            self.app._job_queue.put(('log', f'[PL2D] Failed to run properties on slice {i:03d}: {e}'))
            try:
                (sdir / 'pl2d.err').write_text('EXCEPTION\n' + str(e) + '\n\n' + traceback.format_exc(), encoding='utf-8')
            except Exception:
                pass
            break

        for fn in ('fort.3', 'fort.9', 'fort.11', 'fort.13'):
            try:
                p = sdir / fn
                if p.exists():
                    p.unlink()
            except Exception:
                pass

        try:
            self.pb['value'] = i + 1
            self.lbl_pb.configure(text=f'Slice {i+1}/{len(zs)}')
            self.update_idletasks()
        except Exception:
            pass

    try:
        if ok_all:
            self.pb['value'] = len(zs)
            self.lbl_pb.configure(text=f'Done ({len(zs)}/{len(zs)})')
        self.update_idletasks()
    except Exception:
        pass

    mf['status'] = 'complete' if ok_all else 'failed'
    (run_dir / 'manifest.json').write_text(json.dumps(mf, indent=2), encoding='utf-8')

    if not ok_all:
        log_event(ctx, f'PL2D finished FAIL: {run_dir.name}')
        self.app.state.pl2d_running = False
        messagebox.showerror('PL2D', 'PL2D failed. Check slice folders and pl2d.out for details.')
        self.lbl_status.configure(text='▶ PL2D not run')
        return

    self.app.state.pl2d_run_dir = run_dir
    display_name = str(cfg.get('project_name') or '').strip() if bool(cfg.get('project_name_custom', False)) else run_dir.name
    log_event(ctx, f'PL2D finished OK: {run_dir.name}')
    self.lbl_status.configure(text='✔ PL2D existing')
    self.app.set_status(f'PL2D finished: {display_name}')
    self.app.state.pl2d_running = False
    self.app.refresh_all_pages()

# Bind fixed methods into the class.
try:
    PL2DPage._export_pl2d_campaign = _export_pl2d_campaign
    PL2DPage._run_pl2d = _pl2dpage_run_pl2d_fixed
except Exception:
    pass

