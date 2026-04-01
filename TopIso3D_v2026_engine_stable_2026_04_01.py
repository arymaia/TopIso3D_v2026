#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TopIso3D v2026 - Workspace + TRHO Runner (auto-validate, no Validate button)

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
import time
import queue
import shutil
import json
import threading
import subprocess
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
# ----------------------------
from datetime import datetime

# -----------------------------
# Window manager helpers
# -----------------------------
def _ensure_floating_window(win: tk.Misc) -> None:
    """Best-effort: ensure a Tk/Toplevel has normal decorations and can be moved.

    On some VM/window-manager combinations (and occasionally after PyInstaller
    builds), Tk windows may appear borderless/undecorated, which also makes
    dialogs feel "stuck". Explicitly disabling override-redirect and fullscreen
    usually restores the title bar and window borders.
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

        # Hint: treat as normal window type (supported by some X11 WMs).
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

# ----------------------------
# Settings (Phase 0 - minimal)
# ----------------------------
APP_NAME = "TopIso3D"
SETTINGS_FILENAME = "settings.json"

def _config_dir() -> Path:
    """Return per-user config dir (Linux-friendly, Windows/macOS fallback)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    d = base / APP_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to home dir (best-effort)
        d = Path.home() / f".{APP_NAME.lower()}"
        d.mkdir(parents=True, exist_ok=True)
    return d

def settings_path() -> Path:
    return _config_dir() / SETTINGS_FILENAME

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

def resolve_executable(exe: str | os.PathLike | None) -> Optional[Path]:
    """Resolve an executable that may be an absolute path or a command in PATH."""
    if exe is None:
        return None
    s = str(exe).strip()
    if not s:
        return None
    pth = Path(s)
    if pth.is_file():
        return pth
    w = shutil.which(s)
    return Path(w) if w else None

# Sidebar button sizing (ttk uses Style for font; width is in text units)
SIDEBAR_BTN_WIDTH = 18

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
    trho_mode: str = "relaxed"

    # Parsed TRHO (filled after successful TRHO + parse)
    trho_parsed: Optional["TrhoParsed"] = None
    df_bcp_props: Optional[pd.DataFrame] = None
    df_true_atoms: Optional[pd.DataFrame] = None

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

    # Minimal CP coordinate tables (Angstrom)
    df_bcp_coords: pd.DataFrame
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
    adim_ratio = np.abs(vir) / gkin
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

        adim_ratio_ring = np.where(np.isfinite(gkin_ring) & (gkin_ring != 0), np.abs(vir_ring) / gkin_ring, np.nan)
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

        adim_ratio_cage = np.where(np.isfinite(gkin_cage) & (gkin_cage != 0), np.abs(vir_cage) / gkin_cage, np.nan)
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

        def do_test():
            exe = self.var_prop.get().strip()
            rp = resolve_executable(exe)
            if rp and rp.exists():
                self.lbl_test.configure(text=f"✔ OK: {rp}")
            else:
                self.lbl_test.configure(text="✖ Not found. Use an absolute path or ensure 'properties' is in PATH.")

        btnrow = ttk.Frame(body)
        btnrow.grid(row=10, column=0, columnspan=3, sticky="e", pady=(12, 0))

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

            # Refresh UI hints/status
            try:
                self.app.set_status("Settings saved ✓")
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        # Ensure the main window is decorated and movable (important on some VMs).
        _ensure_floating_window(self)
        self.title("TopIso3D v2026 - Workspace + TRHO Runner (auto-validate)")
        # Standard initial window size (avoid resize jumps across pages)
        self.geometry("1200x820")
        self.minsize(1100, 720)
        self.resizable(True, True)

        # Make sure the window can always be closed from the window manager.
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # v2-like color palette for ttk widgets
        self._apply_theme()
        self.state = ProjectContext()

        # Backward-compatible alias (some pages expect app.ctx)
        self.ctx = self.state
        # CRYSTAL/TOPOND executable (used by TRHO/PL2D/ATBP).
        # Loaded from Settings; fallback to a sensible default.
        self._settings = load_settings()
        pexe = (self._settings.get("properties_exe") or "").strip()
        if pexe:
            self.state.properties_exe = Path(pexe)
        else:
            # Keep your current default (Crystal VM), but allow PATH-based command too.
            default_abs = Path("/usr/crysprop/CRYSTAL_f_orb/properties")
            self.state.properties_exe = default_abs if default_abs.exists() else Path("properties")
        # User-configurable visualization defaults
        self.state.laplacian_scheme = (self._settings.get("laplacian_scheme") or "blue_red").strip() or "blue_red"
        try:
            self.state.nna_cutoff_ang = float(self._settings.get("nna_cutoff_ang", 0.35) or 0.35)
        except Exception:
            self.state.nna_cutoff_ang = 0.35

        # job plumbing
        self._job_thread: Optional[threading.Thread] = None
        self._job_queue: "queue.Queue[tuple]" = queue.Queue()
        self._job_running = False

        self._build_layout()
        self._create_pages()
        self._build_sidebar()

        self.show_page("Workspace")

        # start queue polling
        self.after(100, self._poll_job_queue)
        self.after(200, self.refresh_ui_state)

        self.update_idletasks()
        self.after(10, self.deiconify)

    def _apply_theme(self):
        """Apply a v2-like color palette to ttk widgets (clam theme for better styling)."""
        try:
            style = ttk.Style(self)
            # 'clam' is the most predictable for custom colors across platforms
            try:
                style.theme_use("clam")
            except Exception:
                pass

            # Root background (covers non-ttk areas)
            self.configure(bg=UI_BG_MAIN)

            style.configure(".", background=UI_BG_MAIN, foreground=UI_FG_MAIN)
            style.configure("TFrame", background=UI_BG_MAIN)
            style.configure("Sidebar.TFrame", background=UI_BG_MAIN)
            style.configure("Content.TFrame", background=UI_BG_MAIN)
            style.configure("Status.TFrame", background=UI_BG_MAIN)

            style.configure("TLabel", background=UI_BG_MAIN, foreground=UI_FG_MAIN)
            style.configure("Muted.TLabel", background=UI_BG_MAIN, foreground=UI_FG_MUTED)
            style.configure(
                "Title.TLabel",
                background=UI_BG_DARK,
                foreground=UI_ACCENT,
                font=("Arial", 13, "bold"),
                padding=(8, 6),
            )
            # Same as Title.TLabel, but centers the text while allowing the label to stretch.
            style.configure(
                "TitleCenter.TLabel",
                background=UI_BG_DARK,
                foreground=UI_ACCENT,
                font=("Arial", 13, "bold"),
                padding=(8, 6),
                anchor="center",
            )

            style.configure("TLabelframe", background=UI_BG_MAIN, foreground=UI_FG_MAIN)
            style.configure("TLabelframe.Label", background=UI_BG_MAIN, foreground=UI_FG_MAIN)

            style.configure("TButton", background=UI_ACCENT, foreground=UI_FG_MAIN, padding=(10, 6))
            style.map(
                "TButton",
                background=[("active", UI_ACCENT), ("pressed", UI_ACCENT), ("disabled", UI_BG_PANEL)],
                foreground=[("disabled", UI_FG_MUTED)],
            )

            # Sidebar navigation buttons (font via Style; avoids ttk "-font" error)
            style.configure("SidebarNav.TButton", font=("TkDefaultFont", 10, "bold"), padding=(10, 6))
            style.configure("SidebarNavLeft.TButton", font=("TkDefaultFont", 10, "bold"), padding=(10, 6), anchor="w")
            style.configure("SidebarNavCenter.TButton", font=("TkDefaultFont", 10, "bold"), padding=(10, 6), anchor="center")

            style.configure("TCheckbutton", background=UI_BG_MAIN, foreground=UI_FG_MAIN)

            style.configure("TEntry", fieldbackground=UI_BG_FIELD, foreground=UI_FG_MAIN)
            style.configure("TCombobox", fieldbackground=UI_BG_FIELD, foreground=UI_FG_MAIN)
            style.map("TCombobox", fieldbackground=[("readonly", UI_BG_FIELD)])

            style.configure("TSeparator", background=UI_BG_DARK)

            style.configure("TProgressbar", troughcolor=UI_BG_PANEL, background=UI_ACCENT)

            style.configure("Treeview", background=UI_BG_FIELD, fieldbackground=UI_BG_FIELD, foreground=UI_FG_MAIN)
            style.configure("Treeview.Heading", background=UI_BG_DARK, foreground=UI_ACCENT)
            style.map("Treeview.Heading", background=[("active", UI_BG_DARK)])
        except Exception:
            # If styling fails for any reason, keep the default theme.
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
        settings_row.columnconfigure(1, weight=1)
        ttk.Label(settings_row, text=" ", width=2).grid(row=0, column=0, sticky="w")
        ttk.Button(
            settings_row,
            text="⚙ Settings",
            command=self.open_settings,
            style="SidebarNavCenter.TButton",
            width=SIDEBAR_BTN_WIDTH,
        ).grid(row=0, column=1, sticky="ew")

        ttk.Separator(self.sidebar).grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.nav_frame = ttk.Frame(self.sidebar)
        self.nav_frame.grid(row=4, column=0, sticky="nsew")
        self.nav_frame.columnconfigure(0, weight=1)

        # Quick help/about (kept at bottom)
        ttk.Separator(self.sidebar).grid(row=5, column=0, sticky="ew", pady=(10, 10))
        qa = ttk.Frame(self.sidebar)
        qa.grid(row=6, column=0, sticky="ew")
        qa.columnconfigure(1, weight=1)
        # Keep the same visual width as the workflow buttons (badge + button).
        ttk.Label(qa, text=" ", width=2).grid(row=0, column=0, rowspan=2, sticky="nw")
        ttk.Button(qa, text="Help", command=self._help, style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH).grid(row=0, column=1, sticky="ew")
        ttk.Button(qa, text="About", command=self._about, style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH).grid(row=1, column=1, sticky="ew", pady=(6, 0))

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
            ("PL2D", "PL2D"),
            ("PL2D Viewer", "PL2D Viewer"),
            ("ATBP", "ATBP"),
            ("BCP Evaluation", "BCP Evaluation"),
            ("Reports", "Reports"),
        ]
        self.nav_buttons = {}
        self.nav_badges = {}

        for r, (label, key) in enumerate(self.nav_items):
            row = ttk.Frame(self.nav_frame)
            row.grid(row=r, column=0, sticky="ew", pady=3)
            row.columnconfigure(1, weight=1)

            badge = ttk.Label(row, text=" ", width=2)
            badge.grid(row=0, column=0, sticky="w")
            btn = ttk.Button(row, text=label, command=lambda k=key: self.show_page(k), style="SidebarNav.TButton", width=SIDEBAR_BTN_WIDTH)
            btn.grid(row=0, column=1, sticky="ew")

            self.nav_badges[key] = badge
            self.nav_buttons[key] = btn

    # ---------- Navigation ----------
    def show_page(self, key: str):
        page = self.pages[key]
        page.tkraise()
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

        # Auto-detect existing TRHO output and parse silently (enables Reports immediately),
        # even if the folder is not suitable for launching a new TRHO run.
        existing_trho = self._find_existing_trho_out()
        if existing_trho is not None:
            base_msg = ctx.workspace_msg
            if not ctx.workspace_ok:
                ctx.workspace_msg = base_msg + " | existing trho.out found (reports available)"
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

        # Prefer relative symlink fort.9 -> src.name (same folder)
        try:
            fort9.symlink_to(src.name)
            return True, f"created symlink fort.9 -> {src.name}"
        except Exception:
            # fallback: copy
            try:
                shutil.copy2(src, fort9)
                return True, f"copied {src.name} -> fort.9"
            except Exception as e:
                return False, f"failed to create fort.9 from {src.name}: {e}"

    # ---------- UI state rules ----------

    def _resolve_executable(self, exe: str) -> str:
        """Resolve an executable either as an absolute path or via PATH."""
        if not exe:
            return ""
        try:
            p = Path(str(exe))
            if p.exists():
                return str(p)
        except Exception:
            pass
        try:
            import shutil
            found = shutil.which(str(exe))
            return found or ""
        except Exception:
            return ""

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

    def refresh_ui_state(self):
            ctx = self.state

            full_ws = str(ctx.workspace_dir) if ctx.workspace_dir else ""

            self.lbl_workspace.config(text=self._format_workspace_path(full_ws) if full_ws else "No workspace selected")
            if hasattr(self, "_ws_tooltip"):
                self._ws_tooltip.update_text(full_ws if full_ws else "")
            self.status_var.set(ctx.status)

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
            self.lbl_bits.config(text=" | ".join(bits) if bits else "—")

            rules: Dict[str, Callable[[], bool]] = {
                "Workspace": lambda: True,
                "Compute": lambda: ctx.workspace_ok and (not self._job_running),
                "PL2D": lambda: ctx.workspace_ok and ctx.trho_done and (not self._job_running),
                # PL2D Viewer must work even when TRHO prerequisites are missing
                # (e.g., user selects an already computed PL2D run folder with sliceXXX).
                "PL2D Viewer": lambda: (ctx.workspace_dir is not None) and self._has_any_pl2d_runs() and (not self._job_running),
                "ATBP": lambda: ctx.workspace_ok and ctx.trho_done and (not self._job_running),
                "BCP Evaluation": lambda: ctx.trho_done and (getattr(ctx, "trho_parsed", None) is not None) and (getattr(getattr(ctx, "trho_parsed", None), "df_bcp_props", None) is not None) and (not getattr(ctx.trho_parsed, "df_bcp_props").empty) and (not self._job_running),
                "Reports": lambda: ctx.trho_done,
            }
            badges = {
                "Workspace": "✓" if ctx.workspace_ok else ("!" if ctx.workspace_dir else "!"),
                "Compute": "✓" if ctx.trho_done else ("!" if ctx.workspace_ok else "🔒"),
                "PL2D": "✓" if getattr(ctx, "pl2d_run_dir", None) else ("!" if ctx.trho_done else "🔒"),
                # PL2D Viewer should NOT depend on fort.9/TRHO prerequisites; it only needs a folder with runs.
                "PL2D Viewer": "✓" if self._has_any_pl2d_runs() else ("!" if ctx.workspace_dir else "🔒"),
                "ATBP": "✓" if getattr(ctx, "atbp_out_path", None) else ("!" if (ctx.workspace_ok and ctx.trho_done) else "🔒"),
                "BCP Evaluation": "✓" if (ctx.trho_done and (getattr(ctx, "trho_parsed", None) is not None) and (getattr(getattr(ctx, "trho_parsed", None), "df_bcp_props", None) is not None) and (not getattr(ctx.trho_parsed, "df_bcp_props").empty)) else ("!" if ctx.workspace_ok else "🔒"),
                "Reports": "✓" if ctx.trho_done else "🔒",
            }

            for key, btn in self.nav_buttons.items():
                btn.state(["!disabled"] if rules.get(key, lambda: True)() else ["disabled"])
                self.nav_badges[key].config(text=badges.get(key, " "))

            for p in self.pages.values():
                p.refresh_state()

            self.after(250, self.refresh_ui_state)

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
        """Refresh navigation badges and page widgets after state changes."""
        self.refresh_ui_state()
        for p in self.pages.values():
            if hasattr(p, "refresh"):
                try:
                    p.refresh()
                except Exception:
                    pass

    def _find_existing_trho_out(self) -> Optional[Path]:
        """Return the best existing trho.out candidate for the current workspace.

        Supported layouts:
        - <workspace>/trho.out
        - <workspace>/trho/trho.out
        - selecting the TRHO run folder itself (same as first case)
        """
        ctx = self.state
        ws = getattr(ctx, "workspace_dir", None)
        if not ws:
            return None
        candidates = [
            ws / "trho.out",
            ws / "trho" / "trho.out",
        ]
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return cand
            except Exception:
                pass
        return None

    def build_trho_command(self) -> List[str]:
        """
        Replace this with your real TRHO invocation.
        For now returns a MOCK marker.
        """
        return ["__MOCK_TRHO__"]


    def auto_parse_trho_if_exists(self):
        """If an existing trho.out is found, parse it silently and enable Reports.

        Accepted locations:
        - <workspace>/trho.out
        - <workspace>/trho/trho.out
        """
        ctx = self.state
        if not ctx.workspace_dir:
            return

        out_path = self._find_existing_trho_out()
        if out_path is None or not out_path.exists():
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
            messagebox.showwarning("TRHO", "trho.out not found in workspace root or workspace/trho/.")
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

        cmd = self.build_trho_command()

        def worker():
            try:
                import subprocess

                trho_dir = self.prepare_trho_folder()
                inp_path = self.write_trho_input(trho_dir)
                out_path = trho_dir / "trho.out"

                exe = self.state.properties_exe
                exe_str = str(exe) if exe is not None else ""
                exe_resolved = None
                try:
                    import shutil, os
                    # Allow either absolute path or command available in PATH.
                    if exe_str and (os.path.isabs(exe_str) or "/" in exe_str):
                        exe_resolved = exe_str if Path(exe_str).exists() else None
                    else:
                        exe_resolved = shutil.which(exe_str) if exe_str else None
                except Exception:
                    exe_resolved = exe_str if exe_str else None

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
                    )

                    # progresso indeterminado (até você ter métrica real)
                    self._job_queue.put(("progress", 0, 0))

                    # stream de saída pro log
                    for line in p.stdout:
                        self._job_queue.put(("log", line.rstrip("\n")))
                        fout.write(line)

                    p.wait()

                    rc = p.returncode
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
                            self._job_queue.put(("parsed", summary))

                        except Exception as e:
                            # TRHO ran (rc=0), but parsing failed. Keep TRHO as done and expose error in Reports.
                            self.state.trho_parsed = None
                            self.state.trho_done = True
                            self.state.trho_parse_error = str(e)
                            self._job_queue.put(("log", f"[TRHO] parsing failed: {e}"))
                            self._job_queue.put(("parse_error", str(e)))

                    self._job_queue.put(("done", rc))


            except Exception as e:
                self._job_queue.put(("error", str(e)))

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
                    try:
                        p = self.pages.get("PL2D")
                        if p is not None:
                            if hasattr(p, "on_atbp_done"):
                                p.on_atbp_done(out_path)
                            p.lbl_status.configure(text="✔ PL2D finished")
                            p.btn_run.configure(state="normal")
                            if hasattr(p, "lbl_pb"):
                                p.lbl_pb.configure(text="Done.")
                    except Exception:
                        pass
                    self._job_running = False
                    self.set_task(active=False)
                    self.set_status("PL2D finished ✓")
                    self.task_log("[PL2D] finished OK")

                elif kind == "pl2d_fail":
                    msg, run_dir = item[1], item[2]
                    self._job_running = False
                    self.set_task(active=False)
                    self.set_status("PL2D failed")
                    self.task_log("[PL2D] failed: " + str(msg))
                    try:
                        p = self.pages.get("PL2D")
                        if p is not None:
                            p.btn_run.configure(state="normal")
                            p.lbl_status.configure(text="▶ PL2D not run")
                            if hasattr(p, "lbl_pb"):
                                p.lbl_pb.configure(text="Failed.")
                    except Exception:
                        pass
                    messagebox.showerror("PL2D", str(msg))

                # -----------------------------
                # ATBP job events
                # -----------------------------
                elif kind == "atbp_done":
                    out_path, run_dir = item[1], item[2]
                    self._job_running = False
                    self.set_task(active=False)
                    self.set_status("ATBP finished ✓")
                    self.task_log("[ATBP] finished OK")

                    # Update ATBP page widgets (if present)
                    try:
                        p = self.pages.get("ATBP")
                        if p is not None:
                            # Stop page-local progress bar + re-enable buttons
                            if hasattr(p, "on_atbp_done"):
                                try:
                                    p.on_atbp_done(Path(out_path) if out_path else None)
                                except Exception:
                                    pass

                            if hasattr(p, "var_out"):
                                p.var_out.set(str(out_path))
                            if hasattr(p, "lbl_status"):
                                p.lbl_status.configure(text=f"✔ Prepared/ran: {run_dir}")
                            # Enable parse/export buttons if they exist
                            if hasattr(p, "btn_parse"):
                                p.btn_parse.configure(state="normal")
                            if hasattr(p, "btn_export_json"):
                                p.btn_export_json.configure(state="normal")
                            if hasattr(p, "btn_export_csv"):
                                p.btn_export_csv.configure(state="normal")
                    except Exception:
                        pass

                    self.refresh_all_pages()

                elif kind == "atbp_fail":
                    msg, run_dir = item[1], item[2]
                    self._job_running = False
                    self.set_task(active=False)
                    self.set_status("ATBP failed")
                    self.task_log("[ATBP] failed: " + str(msg))

                    try:
                        p = self.pages.get("ATBP")
                        if p is not None and hasattr(p, "on_atbp_fail"):
                            try:
                                p.on_atbp_fail(str(msg))
                            except Exception:
                                pass
                        if p is not None and hasattr(p, "lbl_status"):
                            p.lbl_status.configure(text=f"✖ Failed: {run_dir}" if run_dir else "✖ Failed")
                    except Exception:
                        pass

                    self.refresh_all_pages()
                    messagebox.showerror("ATBP", str(msg))

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
                    self._job_running = False
                    self.set_task(active=False)
                    try:
                        p = self.pages.get("Compute")
                        if p is not None and hasattr(p, "_set_running"):
                            p._set_running(False)
                    except Exception:
                        pass

                    if rc == 0:
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
                        self.set_status(f"TRHO failed (rc={rc})")
                        self.task_log(f"[TRHO] failed (rc={rc})")
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
                                    p.set_completion_text(f"TRHO failed ✖ (rc={rc})")
                        except Exception:
                            pass

                    # Ensure pages update enable/disable rules immediately
                    self.refresh_all_pages()

                elif kind == "error":
                    self._job_running = False
                    self.set_task(active=False)
                    try:
                        p = self.pages.get("Compute")
                        if p is not None and hasattr(p, "_set_running"):
                            p._set_running(False)
                    except Exception:
                        pass
                    self.set_status("TRHO error")
                    self.task_log(f"[ERROR] {item[1]}")
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
                                p.set_completion_text("TRHO failed ✖")
                    except Exception:
                        pass

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
            "fort.9 creation is automatic ONLY when TRHO starts (if needed)."
        )

    
    def _build_menubar(self) -> None:
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=m_file)

        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="Settings…", command=self.open_settings)
        menubar.add_cascade(label="Tools", menu=m_tools)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=m_help)

        self.config(menu=menubar)

    def open_settings(self) -> None:
        SettingsDialog(self)

    def _about(self):
        messagebox.showinfo("About", "TopIso3D v2026 template: auto-validate Workspace + TRHO runner (mock).")

    def prepare_trho_folder(self) -> Path:
        """Create trho/ folder and ensure fort.9 is inside it."""
        workdir = self.state.workspace_dir
        if not workdir:
            raise RuntimeError("No workspace_dir")

        trho_dir = workdir / "trho"
        trho_dir.mkdir(parents=True, exist_ok=True)

        src_fort9 = workdir / "fort.9"
        if not src_fort9.exists():
            raise FileNotFoundError("fort.9 not found in workspace (should have been created).")

        dst_fort9 = trho_dir / "fort.9"
        if not dst_fort9.exists():
            shutil.copy2(src_fort9, dst_fort9)

        return trho_dir

    def build_trho_input_text(self) -> str:
        """Return the TRHO input according to the mode selected in the GUI."""
        mode = str(getattr(self.state, "trho_mode", "relaxed") or "relaxed").strip().lower()
        if mode == "sensitive":
            params = "1,0,1,0,30,15,12.0,6.0"
        else:
            params = "1,0,1,0,30,10,10.,5."
        return f"TOPO\nTRHO\n-1\n{params}\nEND\n"

    def write_trho_input(self, trho_dir: Path) -> Path:
        """
        Write trho.inp according to the GUI-selected IAUTO=-1 mode.

        Modes currently exposed by the GUI:
        - relaxed  : 1,0,1,0,30,10,10.,5.
        - sensitive: 1,0,1,0,30,15,12.0,6.0
        """
        inp = trho_dir / "trho.inp"
        template = self.build_trho_input_text()
        with open(inp, "w", encoding="utf-8") as f:
            f.write(template)
        return inp


    def prepare_atbp_folder(self) -> Path:
        """Create atbp/ folder and ensure fort.9 is inside it (same logic as TRHO)."""
        workdir = self.state.workspace_dir
        if not workdir:
            raise RuntimeError("No workspace_dir")

        atbp_dir = workdir / "atbp"
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

    def run_atbp(self, snippet: str) -> None:
        """Run ATBP via properties, creating workspace/atbp with atbp.inp and atbp.out."""
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

                exe = self.state.properties_exe
                exe_path = self._resolve_executable(str(exe))
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
                    )

                    for line in p.stdout:
                        self._job_queue.put(("log", line.rstrip("\n")))
                        fout.write(line)

                    p.wait()

                    rc = p.returncode
                    if rc == 0 and out_path.exists():
                        self._job_queue.put(("atbp_done", str(out_path), str(atbp_dir)))
                    else:
                        self._job_queue.put(("atbp_fail", f"ATBP failed (rc={rc})", str(atbp_dir)))

            except Exception as e:
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
    atbp_dir = workspace_dir / "atbp"
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
        self.app.ctx.df_bcp_props = None
        self.app.ctx.df_true_atoms = None
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

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Run TRHO (TOPOND/Properties) using the wavefunction available in the workspace.").pack(anchor="w")

        self.mode_box = ttk.LabelFrame(body, text="TRHO mode (IAUTO = -1)", padding=(12, 8))
        self.mode_box.pack(fill="x", pady=(12, 0))

        self.var_trho_mode = tk.StringVar(value=str(getattr(self.app.state, "trho_mode", "relaxed") or "relaxed"))
        self._trho_mode_labels = {
            "Relaxed": "relaxed",
            "Sensitive": "sensitive",
        }
        self._trho_mode_labels_inv = {v: k for k, v in self._trho_mode_labels.items()}
        self.var_trho_mode_label = tk.StringVar(
            value=self._trho_mode_labels_inv.get(self.var_trho_mode.get().strip() or "relaxed", "Relaxed")
        )

        row_mode = ttk.Frame(self.mode_box)
        row_mode.pack(fill="x")
        ttk.Label(row_mode, text="Mode:").pack(side="left", padx=(0, 8))
        self.cmb_trho_mode = ttk.Combobox(
            row_mode,
            textvariable=self.var_trho_mode_label,
            values=list(self._trho_mode_labels.keys()),
            state="readonly",
            width=18,
        )
        self.cmb_trho_mode.pack(side="left")
        self.cmb_trho_mode.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_changed())

        self.lbl_mode_params = ttk.Label(self.mode_box, text="")
        self.lbl_mode_params.pack(anchor="w", pady=(8, 0))

        self.lbl_mode_help = ttk.Label(
            self.mode_box,
            text=(
                "Both modes use IAUTO = -1. The relaxed option is faster; the sensitive option "
                "uses a more permissive CP search and can improve Morse consistency in harder systems."
            ),
            wraplength=880,
            justify="left",
        )
        self.lbl_mode_help.pack(anchor="w", pady=(6, 0))
        self._update_mode_description()

        self.btn_run = ttk.Button(body, text="Run TRHO", command=self.app.run_trho)
        self.btn_run.pack(anchor="w", pady=(12, 0))

        ttk.Label(body, text="TRHO may take some time for certain systems.").pack(anchor="w", pady=(10, 0))

        self._pb_row = ttk.Frame(body)
        self._pb_row.pack(fill="x", pady=(10, 0))

        self.lbl_runhint = ttk.Label(self._pb_row, text=" ")
        self.lbl_runhint.pack(side="left")

        self.pb = ttk.Progressbar(self._pb_row, mode="indeterminate")
        self.pb.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.pb.stop()

        self.lbl_runtime = ttk.Label(body, text=" ")
        self.lbl_runtime.pack(anchor="w", pady=(8, 0))

    def _update_mode_description(self) -> None:
        mode = self.var_trho_mode.get().strip() or "relaxed"
        params = {
            "relaxed": "Relaxed parameters: 1,0,1,0,30,10,10.,5.",
            "sensitive": "Sensitive parameters: 1,0,1,0,30,15,12.0,6.0",
        }.get(mode, "")
        try:
            self.lbl_mode_params.configure(text=params)
        except Exception:
            pass

    def _on_mode_changed(self) -> None:
        label = (self.var_trho_mode_label.get() or "").strip()
        mode = self._trho_mode_labels.get(label, "relaxed")
        self.var_trho_mode.set(mode)
        self.app.state.trho_mode = mode
        self._update_mode_description()

    def _sync_output_path(self) -> None:
        try:
            ws = self.app.ctx.workspace_dir
            if ws and ws.exists():
                cand = ws / "atbp" / "atbp.out"
                if cand.exists():
                    self.out_path = cand
                    self.var_out.set(str(cand))
                    self.run_dir = cand.parent
                    self.app.ctx.atbp_out_path = cand
                else:
                    self.out_path = None
                    self.var_out.set("")
            else:
                self.out_path = None
                self.var_out.set("")
        except Exception:
            pass


    def _set_running(self, running: bool, hint: str = "") -> None:
        """Start/stop an indeterminate progressbar for long TRHO runs."""
        try:
            if running:
                self.lbl_runhint.configure(text=hint or "Running… (TRHO may take a long time)")
                if hasattr(self, "lbl_runtime"):
                    self.lbl_runtime.configure(text=" ")
                self.pb.start(12)
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state="disabled")
            else:
                self.pb.stop()
                if hasattr(self, "btn_run"):
                    self.btn_run.configure(state=("normal" if self.app.ctx.workspace_ok and (not self.app._job_running) else "disabled"))
        except Exception:
            pass

    def set_completion_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runhint"):
                self.lbl_runhint.configure(text=text or " ")
        except Exception:
            pass

    def set_runtime_text(self, text: str = "") -> None:
        try:
            if hasattr(self, "lbl_runtime"):
                self.lbl_runtime.configure(text=text or " ")
        except Exception:
            pass

    def refresh_state(self):
        try:
            mode = str(getattr(self.app.state, "trho_mode", "relaxed") or "relaxed")
            self.var_trho_mode.set(mode)
            self.var_trho_mode_label.set(self._trho_mode_labels_inv.get(mode, "Relaxed"))
            self._update_mode_description()
        except Exception:
            pass
        ready_run = self.app.ctx.workspace_ok and (not self.app._job_running)
        self.btn_run.state(["!disabled"] if ready_run else ["disabled"])

        if self.app._job_running:
            self._set_running(True, "Running… (TRHO may take a long time)")
        else:
            self._set_running(False)

        # Parser test can run if a trho.out exists (and no job running)
        out_path = self.app._find_existing_trho_out() if self.app.ctx.workspace_dir else None
        ready_parse = (out_path is not None) and out_path.exists() and (not self.app._job_running)


def main():
    app = App()
    app.mainloop()


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

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=8)

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
        self.var_Lz = tk.StringVar(value="5.0")

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

        # All isosurfaces toggle
        ttk.Checkbutton(
            grid,
            text="All isosurfaces",
            variable=self.var_all_iso,
            command=self._toggle_all_isosurfaces,
        ).grid(row=0, column=0, sticky="w", padx=(0, 16), pady=(0, 6))

        for i, (key, label) in enumerate(self.ISO_TYPES):
            var = tk.BooleanVar(value=(key == "SURFRHOO"))
            self.iso_vars[key] = var
            cb = ttk.Checkbutton(grid, text=f"{key} — {label}", variable=var, command=self._on_iso_changed)
            cb.grid(row=1 + (i // 2), column=i % 2, sticky="w", padx=(0, 16), pady=2)

        # If all are selected at startup, reflect it (normally false because only SURFRHOO starts checked)
        self._sync_all_isosurfaces_var()

# --- Actions ---
        frm_act = ttk.Frame(body)
        frm_act.pack(fill="x", pady=(6, 0))

        self.var_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_act, text="Force run (ignore existing)", variable=self.var_force, command=self._on_params_changed).pack(anchor="w")

        self.btn_run = ttk.Button(frm_act, text="Run PL2D", command=self._run_pl2d)
        self.btn_run.pack(anchor="w", pady=(8, 0))

        # Progress (per-slice)
        frm_pb = ttk.Frame(frm_act)
        frm_pb.pack(anchor="w", pady=(6, 0), fill="x")

        self.pb = ttk.Progressbar(frm_pb, orient="horizontal", mode="determinate", length=320)
        self.pb.pack(side="left")
        # Progress text (e.g., Done (12/100)) stays visible even when the window is narrow
        self.lbl_pb = ttk.Label(frm_pb, text="")
        self.lbl_pb.pack(side="left", padx=(10, 0))

        self.lbl_status = ttk.Label(frm_act, text="▶ PL2D not run", font=("TkDefaultFont", 10, "bold"))
        self.lbl_status.pack(anchor="w", pady=(8, 0))

        # Bind changes
        for var in (
            self.var_xy_mode, self.var_xmin, self.var_ymin, self.var_xmax, self.var_ymax, self.var_L, self.var_inc,
            self.var_ref_kind, self.var_ref_id,
            self.var_z_mode, self.var_zmin, self.var_zmax, self.var_zc, self.var_Lz,
            self.var_slice_custom,
        ):
            var.trace_add("write", lambda *_: self._on_params_changed())

        self._on_slice_mode()
        self._on_xy_mode()
        self._on_z_mode()
        self._on_params_changed()
        self.refresh()

    def refresh(self):
        # Enable only if workspace is ready and TRHO is available/parsed
        ready = self.app.state.workspace_ok and self.app.state.trho_parsed is not None
        # While running, keep a clear status message
        if getattr(self.app.state, 'pl2d_running', False):
            self.lbl_status.configure(text='⏳ PL2D running…')
            self.btn_run.configure(state='disabled')
            return

        self.btn_run.configure(state=("normal" if ready else "disabled"))

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

    def _run_pl2d(self):
        log_event(self.app.ctx, 'PL2D started')
        # Mark as running so UI doesn't show 'not run' while slices are being generated
        self.app.state.pl2d_running = True
        self.lbl_status.configure(text='⏳ PL2D running…')
        self.btn_run.configure(state='disabled')
        """Run PL2D using CRYSTAL properties (same default input logic as v2).

        Creates a run directory under workspace/pl2d_runs/ and generates sliceXXX folders,
        each containing pl2d.inp, pl2d.out and the selected *.DAT files produced by properties.
        """
        if not (self.app.state.workspace_ok and self.app.state.trho_parsed is not None):
            messagebox.showwarning("PL2D", "Run/parse TRHO first.")
            return
        try:
            cfg = self._build_config()
        except Exception as e:
            messagebox.showerror("PL2D", f"Invalid configuration: {e}")
            return

        ctx = self.app.state
        prop_exe = getattr(ctx, "properties_exe", None)
        exe_path = resolve_executable(str(prop_exe) if prop_exe is not None else None)
        if not exe_path:
            messagebox.showerror("PL2D", f"properties executable not found: {prop_exe}")
            return

        # Require fort.9
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

        ts = time.strftime("%Y%m%d_%H%M%S")
        run_dir = root / f"{sig[:10]}_{cfg['n_slices']:03d}_{ts}"
        run_dir.mkdir(parents=True, exist_ok=False)

        # Prepare manifest (robust reuse detection)
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
            "source": {"fort9": str(fort9_src), "fort9_fp": f9_fp},
            "status": "running",
        }
        (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")

        # Slice positions along z
        # In TopIso3D v2, choosing N slices means N intervals and (N+1) slice planes
        ns = int(cfg["n_slices"])
        zmin = float(cfg["zmin"])
        zmax = float(cfg["zmax"])
        if ns <= 0:
            zs = [zmin]
        else:
            dz = (zmax - zmin) / ns
            zs = [zmin + i * dz for i in range(ns + 1)]

        # Output flags (order must match v2/default)
        # rhoo, spde, lapp, lapm, grho, kkin, gkin, viri, elfb, trajg, molg, trajm
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

        # Plane definition (keep exactly as v2 defaults)
        bohr_to_ang = float(getattr(ctx, "bohr_to_ang", 0.5291772083))
        a_coord = (0.0, 0.0)
        b_coord = (1.0, 0.0)
        c_coord = (0.0, 1.0)

        # XY ranges in Angstrom (as in v2)
        xmin = float(cfg["xmin"])
        xmax = float(cfg["xmax"])
        ymin = float(cfg["ymin"])
        ymax = float(cfg["ymax"])
        inc = float(cfg["inc"])

        # Output name (same as v2: workspace folder basename)
        out_name = ctx.workspace_dir.name if ctx.workspace_dir else "PL2D"

        # Run slices sequentially (safer; avoids spawning 100 processes at once)
        # IMPORTANT: PL2D uses ONLY the per-page progress bar (self.pb).
        # Do not update the global statusbar progress here to avoid duplicated progress bars.
        # Also clear any previously active global task indicator.
        try:
            self.app.set_task(active=False)
        except Exception:
            pass
        ok_all = True

        # Init per-page progress bar
        try:
            self.pb.configure(maximum=len(zs), value=0)
            self.lbl_pb.configure(text=f"Slice 0/{len(zs)}")
            self.update_idletasks()
        except Exception:
            pass

        for i, z in enumerate(zs):
            sdir = run_dir / f"slice{i:03d}"
            sdir.mkdir()

            # Copy fort.9 required by properties
            try:
                shutil.copy2(fort9_src, sdir / "fort.9")
            except Exception as e:
                ok_all = False
                self.app._job_queue.put(("log", f"[PL2D] Failed to copy fort.9 to {sdir}: {e}"))
                break

            # Write pl2d.inp (same template as v2)
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
                self.app._job_queue.put(("log", f"[PL2D] Failed to write pl2d.inp in {sdir}: {e}"))
                break

            # Run properties
            out = sdir / "pl2d.out"
            try:
                err = sdir / "pl2d.err"
                # Run properties (robust I/O capture, avoids shell redirection issues)
                with open(inp, "r", encoding="utf-8", errors="ignore") as fin,                      open(out, "w", encoding="utf-8") as fout,                      open(err, "w", encoding="utf-8") as ferr:
                    proc = subprocess.run([str(exe_path)], stdin=fin, stdout=fout, stderr=ferr, cwd=str(sdir))
                if proc.returncode != 0:
                    ok_all = False
                    self.app._job_queue.put(("log", f"[PL2D] properties returned {proc.returncode} on slice {i:03d}"))
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
                ok_all = False
                self.app._job_queue.put(("log", f"[PL2D] Failed to run properties on slice {i:03d}: {e}"))
                try:
                    (sdir / "pl2d.err").write_text("EXCEPTION\n" + str(e) + "\n\n" + traceback.format_exc(), encoding="utf-8")
                except Exception:
                    pass
                break

            # Cleanup (optional, but keeps folders tidy). Ignore failures.
            for fn in ("fort.3", "fort.9", "fort.11", "fort.13"):
                try:
                    p = sdir / fn
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

            # (global progress intentionally not updated)

            # Update per-page progress bar (per slice)
            try:
                self.pb["value"] = i + 1
                self.lbl_pb.configure(text=f"Slice {i+1}/{len(zs)}")
                self.update_idletasks()
            except Exception:
                pass

        # (global progress intentionally not used for PL2D)
        try:
            # Finalize per-page progress bar
            if ok_all:
                self.pb["value"] = len(zs)
                self.lbl_pb.configure(text=f"Done ({len(zs)}/{len(zs)})")
            self.update_idletasks()
        except Exception:
            pass

        mf["status"] = "complete" if ok_all else "failed"
        (run_dir / "manifest.json").write_text(json.dumps(mf, indent=2), encoding="utf-8")

        if not ok_all:
            log_event(ctx, f"PL2D finished FAIL: {run_dir.name}")
            messagebox.showerror("PL2D", "PL2D failed. Check slice folders and pl2d.out for details.")
            self.lbl_status.configure(text="▶ PL2D not run")
            return

        self.app.state.pl2d_run_dir = run_dir
        log_event(ctx, f"PL2D finished OK: {run_dir.name}")
        self.lbl_status.configure(text="✔ PL2D existing")
        self.app.set_status(f"PL2D finished: {run_dir.name}")
        self.app.state.pl2d_running = False
        self.app.refresh_all_pages()



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
        body.grid_columnconfigure(1, minsize=260)

        # --- Run selection ---
        frm_run = ttk.LabelFrame(body, text="Existing PL2D runs (workspace/pl2d_runs)")
        frm_run.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        frm_run.columnconfigure(1, weight=1)

        ttk.Label(frm_run, text="Run:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))
        self.run_var = tk.StringVar(value="")
        self.cmb_runs = ttk.Combobox(frm_run, textvariable=self.run_var, state="readonly", width=55)
        self.cmb_runs.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(10, 6))
        self.cmb_runs.bind("<<ComboboxSelected>>", lambda e: self._on_run_selected())

        ttk.Button(frm_run, text="Refresh list", command=self.refresh_runs).grid(row=0, column=2, sticky="e", padx=(0, 10), pady=(10, 6))

        self.lbl_run_info = ttk.Label(frm_run, text="—", foreground="#444")
        self.lbl_run_info.grid(row=1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 10))

        # --- Surface selection + plot controls ---
        frm_ctl = ttk.Frame(body)
        frm_ctl.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        frm_ctl.columnconfigure(0, weight=1)

        frm_surf = ttk.LabelFrame(frm_ctl, text="Topological isosurface (.DAT)")
        frm_surf.grid(row=0, column=0, sticky="ew")
        frm_surf.columnconfigure(1, weight=1)

        self.surf_var = tk.StringVar(value="SURFRHOO")

        ttk.Label(frm_surf, text="Surface:").grid(row=0, column=0, sticky="w", padx=(10, 8), pady=10)
        self.cmb_surface = ttk.Combobox(
            frm_surf,
            textvariable=self.surf_var,
            state="readonly",
            width=42,
            values=(),
        )
        self.cmb_surface.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=10)
        self.cmb_surface.bind("<<ComboboxSelected>>", lambda e: self._on_surf_selected())

        # --- Plot params (minimal; v2-like) ---
        frm_params = ttk.LabelFrame(frm_ctl, text="Plot parameters")
        frm_params.grid(row=1, column=0, sticky="ew", pady=(10, 0))

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
        row_shared.pack(fill="x", padx=10, pady=(10, 6))
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
        frm_mode.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(frm_mode, text="Mode:").pack(side="left")
        self.rb_linear = ttk.Radiobutton(frm_mode, text="Linear", variable=self.var_mode, value="linear", command=self._on_mode_change)
        self.rb_linear.pack(side="left", padx=(8, 0))
        self.rb_geo = ttk.Radiobutton(frm_mode, text="Geometric (Gatti)", variable=self.var_mode, value="geometric", command=self._on_mode_change)
        self.rb_geo.pack(side="left", padx=(10, 0))

        # Linear levels controls
        self.frm_linear_box = ttk.LabelFrame(frm_params, text="Linear levels")
        self.frm_linear_box.pack(fill="x", padx=10, pady=(0, 8))
        self.frm_linear = ttk.Frame(self.frm_linear_box)
        self.frm_linear.pack(fill="x", padx=10, pady=8)
        _labeled_entry(self.frm_linear, "#Isosurfaces", self.var_count, width=8).pack(side="left", padx=(0, 18))
        self.ent_isomin = _labeled_entry(self.frm_linear, "Min iso", self.var_isomin, width=12)
        self.ent_isomin.pack(side="left", padx=(0, 18))
        self.ent_isomax = _labeled_entry(self.frm_linear, "Max iso", self.var_isomax, width=12)
        self.ent_isomax.pack(side="left", padx=(0, 0))

        # Geometric (Gatti) controls
        self.frm_geo_box = ttk.LabelFrame(frm_params, text="Geometric levels (Gatti)")
        self.frm_geo_box.pack(fill="x", padx=10, pady=(0, 8))
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
        self.hint.pack(anchor="w", padx=10, pady=(0, 10))

        # --- Overlays (TRUE atoms + BCPs) ---
        # Lightweight overlays using Scatter3d (no Mesh3d spheres).
        frm_ov = ttk.LabelFrame(frm_params, text="Overlays (3D)")
        frm_ov.pack(fill="x", padx=10, pady=(0, 10))
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
                self.frm_geo_box.pack(fill="x", padx=10, pady=(0, 8))
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
                self.frm_linear_box.pack(fill="x", padx=10, pady=(0, 8))
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
                    pad = 0.10  # Å

                    dfp = df_atoms[
                        (df_atoms["X_ANGSTROM"] >= xmin_b - pad) & (df_atoms["X_ANGSTROM"] <= xmax_b + pad) &
                        (df_atoms["Y_ANGSTROM"] >= ymin_b - pad) & (df_atoms["Y_ANGSTROM"] <= ymax_b + pad) &
                        (df_atoms["Z_ANGSTROM"] >= zmin_b - pad) & (df_atoms["Z_ANGSTROM"] <= zmax_b + pad)
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
        nna_legend_flag = False
        nna_legend_group = "cp_nna"
        try:
            if getattr(self, "var_show_bcps", None) is not None and bool(self.var_show_bcps.get()):
                df_bcp = getattr(self.app.state, "df_bcp_props", None)
                if df_bcp is not None and hasattr(df_bcp, "empty") and (not df_bcp.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    pad = 0.10  # Å

                    dfp = df_bcp[
                        (df_bcp["X_ANGSTROM"] >= xmin_b - pad) & (df_bcp["X_ANGSTROM"] <= xmax_b + pad) &
                        (df_bcp["Y_ANGSTROM"] >= ymin_b - pad) & (df_bcp["Y_ANGSTROM"] <= ymax_b + pad) &
                        (df_bcp["Z_ANGSTROM"] >= zmin_b - pad) & (df_bcp["Z_ANGSTROM"] <= zmax_b + pad)
                    ].copy()

                    if not dfp.empty:
                        cp_legend_flags["bcp"] = True
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
                    pad = 0.10  # Å


                    # Ensure numeric CP coordinates (some CRYSTAL outputs carry them as strings)
                    _x = pd.to_numeric(df_rcp.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_rcp.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_rcp.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    # Filter by current PL2D box (with a small padding)
                    mask = (_x >= (xmin_b - pad)) & (_x <= (xmax_b + pad)) & (_y >= (ymin_b - pad)) & (_y <= (ymax_b + pad)) & (_z >= (zmin_b - pad)) & (_z <= (zmax_b + pad))
                    dfp = df_rcp.loc[mask].copy()
                    dfp["X_ANGSTROM"] = _x.loc[mask].astype(float)
                    dfp["Y_ANGSTROM"] = _y.loc[mask].astype(float)
                    dfp["Z_ANGSTROM"] = _z.loc[mask].astype(float)

                    if not dfp.empty:
                        cp_legend_flags["rcp"] = True
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
                df_ccp = (getattr(parsed_trho, "df_ccp_props", None) if parsed_trho is not None else None)
                if df_ccp is not None and hasattr(df_ccp, "empty") and (not df_ccp.empty):
                    xmin_b, xmax_b = float(np.min(x_coords)), float(np.max(x_coords))
                    ymin_b, ymax_b = float(np.min(y_coords)), float(np.max(y_coords))
                    zmin_b, zmax_b = float(np.min(z_coords)), float(np.max(z_coords))
                    pad = 0.10  # Å


                    # Ensure numeric CP coordinates (some CRYSTAL outputs carry them as strings)
                    _x = pd.to_numeric(df_ccp.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_ccp.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_ccp.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    # Filter by current PL2D box (with a small padding)
                    mask = (_x >= (xmin_b - pad)) & (_x <= (xmax_b + pad)) & (_y >= (ymin_b - pad)) & (_y <= (ymax_b + pad)) & (_z >= (zmin_b - pad)) & (_z <= (zmax_b + pad))
                    dfp = df_ccp.loc[mask].copy()
                    dfp["X_ANGSTROM"] = _x.loc[mask].astype(float)
                    dfp["Y_ANGSTROM"] = _y.loc[mask].astype(float)
                    dfp["Z_ANGSTROM"] = _z.loc[mask].astype(float)

                    if not dfp.empty:
                        cp_legend_flags["ccp"] = True
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
                    pad = 0.10  # Å

                    _x = pd.to_numeric(df_nna.get("X_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _y = pd.to_numeric(df_nna.get("Y_ANGSTROM", pd.Series(dtype=float)), errors="coerce")
                    _z = pd.to_numeric(df_nna.get("Z_ANGSTROM", pd.Series(dtype=float)), errors="coerce")

                    mask = (
                        (_x >= (xmin_b - pad)) & (_x <= (xmax_b + pad)) &
                        (_y >= (ymin_b - pad)) & (_y <= (ymax_b + pad)) &
                        (_z >= (zmin_b - pad)) & (_z <= (zmax_b + pad))
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
        try:
            if nna_legend_flag:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="NNA",
                    marker=dict(size=9, symbol="cross", color="black", line=dict(width=2, color="black")),
                    legendgroup=nna_legend_group,
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_flags.get("bcp", False):
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="BCP",
                    marker=dict(size=9, symbol="diamond-open", color="#000000", line=dict(width=2, color="black")),
                    legendgroup=cp_legend_groups["bcp"],
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_flags.get("rcp", False):
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode="markers",
                    name="RCP",
                    marker=dict(size=9, symbol="square-open", color="#7f7f7f", line=dict(width=1, color="DarkSlateGrey")),
                    legendgroup=cp_legend_groups["rcp"],
                    showlegend=True, hoverinfo="skip"
                ))
            if cp_legend_flags.get("ccp", False):
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

                fig.write_html(str(html_path), include_plotlyjs="directory")
                self.lbl_status.config(text=f"Saved HTML: {html_path}")
            except Exception as e:
                self.lbl_status.config(text=f"HTML export failed: {e}")
        fig.show()
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

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Reports Viewer", font=("TkDefaultFont", 16, "bold")).pack(side="left")
        self.subtitle_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.subtitle_var, foreground="#555").pack(side="left", padx=(12, 0))

        btns = ttk.Frame(header)
        btns.pack(side="right")
        ttk.Button(btns, text="Export Excel…", command=self._export_excel).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Export BCP CSV…", command=self._export_bcp_csv).pack(side="left")

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

        self.refresh()

    def refresh(self):
        ctx = self.app.ctx
        parsed = getattr(ctx, "trho_parsed", None)
        if parsed is None or not ctx.trho_done:
            self.subtitle_var.set("(no TRHO parsed yet)")
            try:
                self.nb.tab(self.tab_atoms, text="TRUE atoms")
                self.nb.tab(self.tab_nna, text="NNA")
                self.nb.tab(self.tab_bcp, text="BCP")
                self.nb.tab(self.tab_rcp, text="RCP")
                self.nb.tab(self.tab_ccp, text="CCP")
            except Exception:
                pass
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
            hidden_in_report = {
                "ATTR1_ATOM_ID", "ATTR2_ATOM_ID",
                "ATTR1_TRAJ_LEN_ANG", "ATTR2_TRAJ_LEN_ANG",
                "ATTR1_X_ANGSTROM", "ATTR1_Y_ANGSTROM", "ATTR1_Z_ANGSTROM",
                "ATTR2_X_ANGSTROM", "ATTR2_Y_ANGSTROM", "ATTR2_Z_ANGSTROM",
            }
            preferred = ["N", "BCP_ELEM", "ELEM1", "DIST_ELEM1_ANG", "ELEM2", "DIST_ELEM2_ANG"]
            visible_cols = [c for c in df.columns if c not in hidden_in_report]
            cols = [c for c in preferred if c in visible_cols] + [c for c in visible_cols if c not in preferred]
            return _compact_bcp_dist_headers(df.loc[:, cols].copy())

        self.tbl_atoms.set_df(_with_seq(df_true), title=f"TRUE atoms ({ntrue}) — selection index: N")
        nna_cutoff = float(getattr(parsed, "nna_cutoff_ang", getattr(ctx, "nna_cutoff_ang", 0.35)) or 0.35)
        try:
            self.nna_info_var.set(
                f"Classification cutoff: {nna_cutoff:.3f} Å. Flagged (3,-3) attractors with d_min ≤ cutoff are labeled 'likely pseudopotential artifact'; otherwise 'likely NNA'."
            )
        except Exception:
            pass
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

            fig2.show()
            fig3.show()
            fig4.show()
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

    def _build(self):
        super()._build()

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="ATBP (TOPOND) — default STD with optional UNI Balanced and UNI Fast",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        frm_out = ttk.LabelFrame(body, text="ATBP run / output (workspace/atbp)")
        frm_out.pack(fill="x", pady=(0, 10))

        self.var_include_topo = tk.BooleanVar(value=True)
        self.var_atbp_mode = tk.StringVar(value="STD")
        self.var_out = tk.StringVar(value="")

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

        self.btn_run = ttk.Button(row1, text="Run ATBP", command=self._run_atbp)
        self.btn_run.pack(side="right")

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
        self._sync_output_path()
        self.refresh_state()

    def _clear_current_results(self) -> None:
        self.df_atbp = None
        try:
            self.app.ctx.df_atbp = None
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
                try:
                    self.lbl_status.config(text="—")
                except Exception:
                    pass

            if ws and ws.exists():
                cand = ws / "atbp" / "atbp.out"
                if cand.exists():
                    if self.out_path != cand:
                        self._clear_current_results()
                    self.out_path = cand
                    self.run_dir = cand.parent
                    self.var_out.set(str(cand))
                    self.app.ctx.atbp_out_path = cand
                else:
                    self.out_path = None
                    self.run_dir = ws / "atbp"
                    self.var_out.set(str(self.run_dir / "atbp.out"))
                    self.app.ctx.atbp_out_path = None
                    if ws_changed:
                        try:
                            self.lbl_status.config(text=f"No ATBP output found yet for current workspace: {self.run_dir}")
                        except Exception:
                            pass
            else:
                self.out_path = None
                self.run_dir = None
                self.var_out.set("")
                self.app.ctx.atbp_out_path = None
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
                self.btn_run.configure(state="disabled")
            else:
                self.pb.stop()
                self.lbl_runhint.configure(text=" ")
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
            if out_path is not None:
                self.out_path = Path(out_path)
                self.run_dir = self.out_path.parent
                self.var_out.set(str(self.out_path))
                self.app.ctx.atbp_out_path = self.out_path
                self._parse_output(silent=True)
        finally:
            self._set_running(False)

    def on_atbp_fail(self, msg: Optional[str] = None) -> None:
        self._set_running(False)
        if msg:
            messagebox.showerror("ATBP", msg)

    def _parse_output(self, silent: bool = False):
        try:
            ws = self.app.ctx.workspace_dir
            if not ws:
                if not silent:
                    messagebox.showwarning("ATBP", "No workspace selected. Go to Workspace page first.")
                return

            self.run_dir = ensure_atbp_dir(ws)
            outp = self.run_dir / "atbp.out"
            self.out_path = outp
            self.var_out.set(str(outp))
            self.app.ctx.atbp_out_path = outp if outp.exists() else None

            if not outp.exists():
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
        if self.app._job_running:
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
        if ok:
            self.status_var.set(f"BCPs available: {len(parsed.df_bcp_props)}")
        else:
            self.status_var.set("Run TRHO and parse trho.out first.")

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

                # Format numeric descriptors in hover (cleaner, consistent decimals)
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

        fig2 = px.scatter(df, x="LAP", y="BOND_DEGREE", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="∇²ρ (a.u.) × H/ρ (a.u.)")
        fig2.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

        fig3 = px.scatter(df, x="ADIM_RATIO", y="BOND_DEGREE", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="|V|/G (a.u.) × H/ρ (a.u.)")
        fig3.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

        fig4 = px.scatter(df, x="ADIM_RATIO", y="LAP", color="BCP_ELEM", hover_name="BCP_LABEL", hover_data=hover, title="|V|/G (a.u.) × ∇²ρ (a.u.)")
        fig4.update_traces(marker=dict(size=12, line=dict(width=1, color="DarkSlateGrey")))

        return fig2, fig3, fig4
    def _open_lap_bd(self):
        try:
            fig2, _, _ = self._build_figs()
            fig2.show()
        except Exception as e:
            messagebox.showerror("BCP Evaluation", str(e))

    def _open_adim_bd(self):
        try:
            _, fig3, _ = self._build_figs()
            fig3.show()
        except Exception as e:
            messagebox.showerror("BCP Evaluation", str(e))

    def _open_adim_lap(self):
        try:
            _, _, fig4 = self._build_figs()
            fig4.show()
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
            fig2.write_html(str(pdir / "BCP_eval_LAP_x_BOND_DEGREE.html"), include_plotlyjs="directory")
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
            fig3.write_html(str(pdir / "BCP_eval_ADIM_RATIO_x_BOND_DEGREE.html"), include_plotlyjs="directory")
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
            fig4.write_html(str(pdir / "BCP_eval_ADIM_RATIO_x_LAP.html"), include_plotlyjs="directory")
            self.status_var.set("Saved: " + str(pdir / "BCP_eval_ADIM_RATIO_x_LAP.html"))
        except Exception as e:
            messagebox.showerror("BCP Evaluation", f"Failed to save plot: {e}")
class ReportsPage(ttk.Frame):
    """Step B: generate reports right after TRHO is available."""

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Reports", font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Separator(self).grid(row=1, column=0, sticky="ew", pady=(10, 14))
        self.columnconfigure(0, weight=1)

        self.summary_var = tk.StringVar(value="No TRHO data parsed yet.")
        ttk.Label(self, textvariable=self.summary_var, wraplength=900).grid(row=2, column=0, sticky="w", pady=(0, 8))

        self.topology_title_var = tk.StringVar(value="")
        self.topology_formula_var = tk.StringVar(value="")
        self.topology_expected_var = tk.StringVar(value="")
        self.topology_status_var = tk.StringVar(value="")
        self.topology_note_var = tk.StringVar(value="")

        topo_box = ttk.Frame(self, padding=(12, 10))
        topo_box.grid(row=3, column=0, sticky="ew", pady=(2, 16))
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

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, sticky="w")
        self.btn_export_xlsx = ttk.Button(btns, text="Export Excel report (final_report.xlsx)", command=self.export_xlsx)
        self.btn_export_xlsx.grid(row=0, column=0, sticky="w")
        self.btn_export_csv = ttk.Button(btns, text="Export CSV (BCP properties)", command=self.export_csv)
        self.btn_export_csv.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.btn_open_viewer = ttk.Button(btns, text="Open Reports Viewer", command=self.open_viewer)
        self.btn_open_viewer.grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.hint_var = tk.StringVar(value="(Step B) After TRHO runs, this page is enabled and you can export reports.")
        ttk.Label(self, textvariable=self.hint_var, foreground="#555").grid(row=5, column=0, sticky="w", pady=(12, 0))

        self.refresh()

    def refresh_state(self):
        self.refresh()

    def refresh(self):
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
            self.btn_export_csv.configure(state="disabled")
            self.btn_open_viewer.configure(state="disabled")
            return

        if parsed is None:
            err = getattr(ctx, "trho_parse_error", None)
            if err:
                self.summary_var.set(f"TRHO finished, but parsing failed: {err}")
            else:
                self.summary_var.set("TRHO finished, but no parsed data is available. (Try re-running TRHO or check the output file.)")
            self.topology_title_var.set("")
            self.topology_formula_var.set("")
            self.topology_expected_var.set("")
            self.topology_status_var.set("")
            self.topology_note_var.set("")
            self.btn_export_xlsx.configure(state="disabled")
            self.btn_export_csv.configure(state="disabled")
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
        self.topology_title_var.set("Morse topological relation (periodic systems)")
        self.topology_formula_var.set(
            f"TRUE atoms - BCPs + RCPs - CCPs = {ntrue} - {nbcp} + {nrcp} - {nccp} = {morse_value}."
        )
        self.topology_expected_var.set("Expected value for periodic systems: 0")
        if morse_value == 0:
            self.topology_status_var.set("Status: Consistent")
            try:
                self.topology_status_label.configure(foreground="#1f7a1f")
            except Exception:
                pass
            note = "The reported critical-point network satisfies the expected Morse relation for periodic solids."
            if nna_count > 0:
                note += f" TOPOND also flagged {nna_count} possible non-nuclear attractor(s) in trho.out."
            self.topology_note_var.set(note)
        else:
            self.topology_status_var.set("Status: Inconsistent")
            try:
                self.topology_status_label.configure(foreground="#9c1c1c")
            except Exception:
                pass
            note = "This topological inconsistency may indicate incomplete CP recovery under the current TRHO settings, incomplete parsing, missing CPs, or an interrupted calculation. Consider rerunning TRHO with a more sensitive IAUTO = -1 setup or with IAUTO = 3."
            if nna_count > 0:
                note += f" TOPOND flagged {nna_count} possible non-nuclear attractor(s) in trho.out."
            self.topology_note_var.set(note)

        self.btn_export_xlsx.configure(state="normal")
        self.btn_export_csv.configure(state="normal")
        self.btn_open_viewer.configure(state="normal")
    def open_viewer(self):
        # One viewer per app instance (reuse if already open)
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

    def export_xlsx(self):
        ctx = self.app.ctx
        parsed: TrhoParsed = getattr(ctx, "trho_parsed", None)
        if parsed is None or not ctx.workspace_dir:
            messagebox.showwarning("Reports", "No parsed TRHO data available.")
            return

        out_path = ctx.workspace_dir / "final_report.xlsx"
        try:
            with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
                # Keep names close to the v2 script (easier to compare)
                parsed.df_primitive.to_excel(writer, sheet_name="primitive")
                parsed.df_true_atoms.to_excel(writer, sheet_name="true_atoms")

                parsed.df_bcp_coords.to_excel(writer, sheet_name="bcp_coords")
                parsed.df_attr.to_excel(writer, sheet_name="attr")
                parsed.df_ring.to_excel(writer, sheet_name="rcp")
                parsed.df_cage.to_excel(writer, sheet_name="ccp")

                _compact_bcp_dist_headers(parsed.df_bcp_props).to_excel(writer, sheet_name="BCP_prop")
                if parsed.df_rcp_props is not None:
                    parsed.df_rcp_props.to_excel(writer, sheet_name="RCP_prop")
                if parsed.df_ccp_props is not None:
                    parsed.df_ccp_props.to_excel(writer, sheet_name="CCP_prop")

                if not parsed.df_att_nao_nucl.empty:
                    parsed.df_att_nao_nucl.to_excel(writer, sheet_name="att_nao_nucl")

            messagebox.showinfo("Reports", f"Excel report written:\n{out_path}")
            self.app._job_queue.put(("log", f"[REPORT] wrote {out_path}"))

        except Exception as e:
            messagebox.showerror("Reports", f"Failed to write Excel report: {e}")

    def export_csv(self):
        ctx = self.app.ctx
        parsed: TrhoParsed = getattr(ctx, "trho_parsed", None)
        if parsed is None or not ctx.workspace_dir:
            messagebox.showwarning("Reports", "No parsed TRHO data available.")
            return

        out_path = ctx.workspace_dir / "bcp_properties.csv"
        try:
            _compact_bcp_dist_headers(parsed.df_bcp_props).to_csv(out_path, index=False)
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


if __name__ == "__main__":
    main()