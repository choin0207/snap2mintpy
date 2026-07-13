#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snap2mintpy_gui.py v2 — SNAP/GPT interferogram pipeline → MintPy SBAS GUI

Three tabs:
  [1 Input & Pairs]  Project / SNAP / SLC / DEM / AOI → pair strategy + table
  [2 Run (SNAP)]     snap2gpt per pair: split → coreg+ifg → progress + log
  [3 MintPy]         S1_smallbaseline.cfg editor → run smallbaselineApp.py + log

v2 changes:
  - nodata=0 set on all output GeoTIFFs (MintPy/QGIS friendly)
  - snaphu -d conncomp.img: connected components generated per pair
  - per-pair diagnostic PNG: {tc_dir}/{pair}_{iw}_diagnostic.png
  - MintPy cfg fixed: paths point to tc/*/*_unw_tc.dim (geocoded BEAM-DIMAP)
  - IW sub-frame lat-coverage check for correct zip selection
  - JAVA_TOOL_OPTIONS headless (no X11 crash on SNAP GUI pop-up)
  - pairs persist across sessions (saved to prefs JSON)
  - Tab-2 pair list syncs after baseline network edits

Output layout:
  $project/
  ├── graphs/    (temp GPT xml per pair)
  ├── logs/
  ├── split/     ({date}_{iw}.dim  reused across pairs)
  ├── ifg_ml/    ({pair}/{pair}_{iw}_ml.dim  and  _sml.dim)
  ├── snaphu/    ({pair}/{iw}/  SNAPHU export + unw + conncomp.img)
  ├── tc/        ({pair}/{pair}_{iw}_wrapped_tc.dim  + _unw_tc.dim)
  │              ({pair}/{pair}_{iw}_phase_ifg_VV.tif  nodata=0)
  │              ({pair}/{pair}_{iw}_coh_VV.tif         nodata=0)
  │              ({pair}/{pair}_{iw}_Unw_Phase_ifg_VV.tif nodata=0)
  │              ({pair}/{pair}_{iw}_diagnostic.png)
  ├── geometry/  (DEM.tif  local_incidence_angle.tif  nodata=0)
  └── mintpy/
      └── S1_smallbaseline.cfg
"""
from __future__ import annotations

import os
import re
import sys
import json
import shlex
import shutil
import signal
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass, field


def _open_url(url: str) -> None:
    """Open *url* in the user's browser, WSL-aware."""
    try:
        with open('/proc/sys/kernel/osrelease') as _f:
            _rel = _f.read().lower()
    except OSError:
        _rel = ''
    if 'microsoft' in _rel or 'wsl' in _rel:
        # In WSL there is no Linux browser; delegate to Windows via cmd.exe
        subprocess.Popen(['cmd.exe', '/c', 'start', '', url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).parent
# Preferences live in the directory the GUI is launched from (the startup
# working directory), captured once at import. This lets one shared script
# keep separate settings per working directory — launch the same
# main-dir/snap2mintpy_gui.py from different work dirs and each keeps its own
# snap2mintpy_gui_para_*.json. Script resources (DEM, graphs, worker) still
# resolve against _SCRIPT_DIR.
_PREFS_DIR   = Path(os.getcwd())
PREFS_PATH      = str(_PREFS_DIR / 'snap2mintpy_gui_para.txt')   # legacy fallback
_PREFS_PATTERN  = 'snap2mintpy_gui_para_*.json'


def _latest_prefs_file() -> Optional[str]:
    """Return the most-recent snap2mintpy_gui_para_{datetime}.json path, or None."""
    files = sorted(_PREFS_DIR.glob(_PREFS_PATTERN))
    return str(files[-1]) if files else None


def _read_netrc_asf() -> tuple:
    """Read ASF/Earthdata credentials from ~/.netrc.

    Looks for machine 'urs.earthdata.nasa.gov'.
    Returns (username, password) or ('', '') if not found.
    """
    import netrc as _netrc
    netrc_path = os.path.expanduser('~/.netrc')
    if not os.path.exists(netrc_path):
        return '', ''
    try:
        nr = _netrc.netrc(netrc_path)
        for host in ('urs.earthdata.nasa.gov', 'earthdata.nasa.gov'):
            auth = nr.authenticators(host)
            if auth:
                login, _, password = auth
                return login or '', password or ''
    except Exception:
        pass
    return '', ''


_GRAPHS_DIR  = str(_SCRIPT_DIR / 'snap2stamps' / 'graphs')
# sudo 密碼不再硬編在原始碼；需要 sudo 自動安裝套件時，從環境變數讀取。
# 未設定則跳過 sudo 安裝 (改提示使用者手動 pip install)。
_SUDO_PASS   = os.environ.get('SNAP2MINTPY_SUDO_PASS', '')

# 所有 gpt 呼叫共用的 JVM 選項。
# -XX:CompileCommand=exclude,...FastDelaunayTriangulator::extractUniqueVertices:
#   SNAP/jlinda 的 ESD Delaunay 三角化在 C2 JIT 編譯下會 SIGSEGV 崩潰
#   (曾造成某遠端 worker 整批失敗)。排除該方法的 JIT 編譯 → 改用解譯器執行, 避開崩潰
#   frame; 該方法非熱點迴圈, 對整體速度影響極小。對所有 worker 生效。
_GPT_JAVA_OPTS = ('-Djava.awt.headless=true '
                  '-XX:CompileCommand=exclude,'
                  'org.jlinda.core.delaunay.FastDelaunayTriangulator::extractUniqueVertices')

# gpt 韌性參數 ──────────────────────────────────────────────────────────────────
# 本機 WSL autoMemoryReclaim=gradual 會偷走 SNAP JVM 頁面, 造成兩種暫時性故障:
#   (1) 卡死(hang): gpt 不再輸出也不結束 → 若無看門狗, 整個 worker 永久凍結,
#       master 端的 work-stealing 因偵測不到「host 結束」而永不重試 (實測卡 45 分)。
#   (2) JVM SIGSEGV 崩潰 (hs_err_pid*.log) → 該步驟一次失敗。
# 看門狗: gpt 連續 _GPT_STALL_TIMEOUT 秒「無任何輸出」即視為卡死 → kill 後重試。
#   (filter_ml 正常運作會週期性印 "....10%...." 進度, 真正卡死則完全靜默。)
_GPT_STALL_TIMEOUT = 1800   # 秒 (30 分鐘無輸出 = 卡死)
_GPT_MAX_ATTEMPTS  = 3      # 卡死/崩潰屬暫時性記憶體壓力, 同一 graph 最多嘗試 3 次

# filter_ml(Goldstein) 是記憶體炸彈步驟: 全域 tileCache(-c) 35G + 10 執行緒的原生
# tile 緩衝, 疊在 50G heap 上 → peak 85G+, 在 64G 機器被 OS OOM-killer 砍掉整個
# worker (看門狗來不及反應; RESUME_STATUS「硬骨頭對」根因)。多視後影像很小, heap
# 不缺, 缺的是原生 tile 記憶體 → 只對此步驟把 tileCache 與執行緒壓到安全上限,
# 大幅降低 peak RAM, 易跑對完全不受影響 (它們本來就不會碰到上限)。
_FILTER_ML_CACHE_CEIL_GB = 12   # filter_ml 的 -c tileCache 上限
_FILTER_ML_CPU_CEIL      = 6    # filter_ml 的 -q 執行緒上限


def _cap_gb(value: str, ceiling_gb: int) -> str:
    """把 '35G'/'24000M' 之類的記憶體字串夾到 ceiling_gb (GB) 以下, 回同格式 'NG'。

    解析不出數字時 (空字串/格式怪) → 回 'ceiling_gbG' 當保守預設。
    只縮不放: 原值已 <= ceiling 時原樣回傳, 不會把使用者調低的值改大。
    """
    s = (value or '').strip().upper()
    try:
        if s.endswith('G'):
            gb = float(s[:-1])
        elif s.endswith('M'):
            gb = float(s[:-1]) / 1024.0
        elif s.endswith('K'):
            gb = float(s[:-1]) / (1024.0 * 1024.0)
        else:
            gb = float(s) / (1024.0 ** 3)   # 純 bytes
    except (ValueError, TypeError):
        return f'{ceiling_gb}G'
    capped = min(gb, float(ceiling_gb))
    # 取整數 G (gpt 接受整數 G; 避免 '11.6G')
    return f'{max(1, int(round(capped)))}G'


def _cap_int(value: str, ceiling: int) -> str:
    """把 '10' 之類的執行緒字串夾到 ceiling 以下, 回字串。解析失敗 → str(ceiling)。"""
    try:
        return str(min(int(str(value).strip()), ceiling))
    except (ValueError, TypeError):
        return str(ceiling)


def _proc_group_cpu_ticks(pgid: int) -> int:
    """Sum utime+stime (clock ticks) over all live processes in process group pgid.

    看門狗用此判定『真實活性』: SNAP 的進度是 '....10%....20%' 同一行的點且在 pipe 下
    區塊緩衝 → 不換行就不會被 `for line in stdout` 讀到 → 看門狗誤以為『無輸出』。
    但這種正在運算的 gpt 仍持續燒 CPU。只在『無輸出 _且_ CPU 也停止增長』時才判卡死,
    就不會誤殺正在跑的 filter_ml/Goldstein (它可能靜默數十分鐘), 又仍能抓到真正的
    記憶體卡死/I-O hang (無輸出且 CPU 不動)。
    """
    total = 0
    try:
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/stat', 'rb') as f:
                    data = f.read()
                # comm (field 2) 可能含空白/括號 → 取最後一個 ')' 之後再切。
                rp = data.rfind(b')')
                rest = data[rp + 2:].split()
                # rest[0]=state(f3); pgrp=f5→rest[2]; utime=f14→rest[11]; stime=f15→rest[12]
                if int(rest[2]) == pgid:
                    total += int(rest[11]) + int(rest[12])
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    return total


def _pip_install(package: str) -> Tuple[bool, str]:
    """Install a pip package; tries user-install first, then sudo if needed."""
    import importlib
    # Try plain pip install --user first (no sudo needed)
    for cmd in (
        [sys.executable, '-m', 'pip', 'install', '--user', '-q', package],
        [sys.executable, '-m', 'pip', 'install', '-q', package],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            try:
                importlib.import_module(package.replace('-', '_'))
                return True, result.stdout + result.stderr
            except ImportError:
                pass  # installed but import still fails — try sudo

    # Fall back to sudo pip install (僅在有設定 SNAP2MINTPY_SUDO_PASS 時)
    if not _SUDO_PASS:
        return False, ((f'pip --user 安裝 {package} 失敗，且未設定 '
                       f'SNAP2MINTPY_SUDO_PASS 環境變數 → 跳過 sudo。\n'
                       f'請手動執行: pip install {package}' if LANG == 'zh' else f'pip --user install {package} failed, and SNAP2MINTPY_SUDO_PASS env var not set '
                       f'→ skip sudo.\n'
                       f'Please run manually: pip install {package}'))
    sudo_cmd = ['sudo', '-S', sys.executable, '-m', 'pip', 'install', '-q', package]
    result = subprocess.run(sudo_cmd, input=_SUDO_PASS + '\n',
                            capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    if result.returncode == 0:
        return True, out
    return False, out
DAY_INTERVALS_ALL = list(range(6, 367, 6))
SAFE_DATE_RE = re.compile(r'_(\d{8})T\d{6}_')
ALL_IW = ['IW1', 'IW2', 'IW3']

# ─────────────────────────────────────────────────────────────────────────
# i18n — language support
# ─────────────────────────────────────────────────────────────────────────
LANG: str = 'zh'   # module-level; set by main() before App is created

_STRINGS: dict = {
    # Window & tabs
    'win_title':        {'zh': 'snap2mintpy GUI v2  —  SNAP/GPT → MintPy SBAS',
                         'en': 'snap2mintpy GUI v2  —  SNAP/GPT → MintPy SBAS'},
    'tab1':             {'zh': '1. 輸入 & 干涉對',   'en': '1. Input & Pairs'},
    'tab2':             {'zh': '2. 執行 (SNAP)',      'en': '2. Run (SNAP)'},
    'tab3':             {'zh': '3. MintPy',           'en': '3. MintPy'},
    # LabelFrames
    'lf_project':       {'zh': '專案設定',            'en': 'Project'},
    'lf_snap':          {'zh': 'SNAP 設定',           'en': 'SNAP'},
    'lf_ssd_swap':      {'zh': 'SSD 快取 Swap (防 OOM)',  'en': 'SSD Cache Swap (OOM guard)'},
    'lbl_xmx':          {'zh': 'JVM Heap (-Xmx):',    'en': 'JVM Heap (-Xmx):'},
    'lbl_swap_path':    {'zh': 'Swapfile 目錄:',       'en': 'Swapfile dir:'},
    'lbl_swap_size':    {'zh': '大小:',                'en': 'Size:'},
    'lbl_swap_status':  {'zh': '狀態:',                'en': 'Status:'},
    'btn_swap_create':  {'zh': '建立 Swapfile',        'en': 'Create Swapfile'},
    'btn_swap_enable':  {'zh': '啟用 Swap',            'en': 'Enable Swap'},
    'btn_swap_disable': {'zh': '停用 Swap',            'en': 'Disable Swap'},
    'chk_swap_auto':    {'zh': '處理前自動啟用',       'en': 'Auto-enable before processing'},
    'lf_aoi':           {'zh': '興趣區域 (AOI)',       'en': 'AOI'},
    'lf_processing':    {'zh': '處理參數',             'en': 'Processing'},
    'lf_slc_dates':     {'zh': 'SLC 日期',            'en': 'SLC Dates'},
    'lf_asf':           {'zh': 'ASF 下載 (Earthdata 帳號，補缺 SLC)',
                         'en': 'ASF Download (Earthdata account, fill missing SLC)'},
    'lf_pair_strategy': {'zh': '干涉對策略',          'en': 'Pair Strategy'},
    'lf_summary':       {'zh': '摘要',                'en': 'Summary'},
    'lf_pairs':         {'zh': '干涉對',              'en': 'Pairs'},
    'lf_live_log':      {'zh': 'Live Log（可選取複製）', 'en': 'Live Log (select to copy)'},
    'lf_cfg':           {'zh': 'S1_smallbaseline.cfg  （確認後點 ▶ 執行）',
                         'en': 'S1_smallbaseline.cfg  (confirm then click ▶ Run)'},
    'lf_mintpy_out':    {'zh': 'smallbaselineApp.py  輸出',
                         'en': 'smallbaselineApp.py  output'},
    # Buttons - Tab 1
    'btn_check_project':{'zh': '[?] 檢查',            'en': '[?] Check'},
    'btn_mkdir':        {'zh': '[+] 建立資料夾',       'en': '[+] Create folder'},
    'btn_auto_dem':     {'zh': '[DL] 自動下載 DEM',   'en': '[DL] Auto-download DEM'},
    'btn_scan_slc':     {'zh': 'Scan SLC dir → dates','en': 'Scan SLC dir → dates'},
    'btn_check_slc':    {'zh': '[?] 檢查 SLC 完整性', 'en': '[?] Check SLC integrity'},
    'btn_asf_dl':       {'zh': '補ASF SLC',           'en': 'Fill ASF SLC'},
    'btn_compute_pairs':{'zh': '↻ 計算干涉對',        'en': '↻ Compute pairs'},
    'btn_baseline_plot':{'zh': '[~] 基線網絡圖',       'en': '[~] Baseline network'},
    'btn_confirm':      {'zh': '✓ 確認 → 進入 Tab 2 Run',
                         'en': '✓ Confirm → Go to Tab 2 Run'},
    # Buttons - Tab 2
    'btn_start':        {'zh': '▶ 開始',              'en': '▶ Start'},
    'btn_stop':         {'zh': '⏹ 停止',              'en': '⏹ Stop'},
    # Buttons - Tab 3
    'btn_reload':       {'zh': '↻ 重新載入',           'en': '↻ Reload'},
    'btn_save_cfg':     {'zh': '儲存 cfg',             'en': 'Save cfg'},
    'btn_run_mintpy':   {'zh': '▶ Run smallbaselineApp.py',
                         'en': '▶ Run smallbaselineApp.py'},
    'btn_stop_mintpy':  {'zh': '⏹ Stop',              'en': '⏹ Stop'},
    # Basemap
    'lbl_basemap':      {'zh': '底圖疊加:',            'en': 'Basemap:'},
    'bm_satellite':     {'zh': '衛星 (Esri)',          'en': 'Satellite (Esri)'},
    'bm_google':        {'zh': '衛星 (Google)',        'en': 'Satellite (Google)'},
    'bm_osm':           {'zh': 'OpenStreetMap',        'en': 'OpenStreetMap'},
    'bm_topo':          {'zh': '地形 (Topo)',           'en': 'Topo'},
    'bm_cartodb':       {'zh': 'CartoDB',              'en': 'CartoDB'},
    'lbl_vel_coh':      {'zh': 'Coh遮罩≥',             'en': 'Coh mask ≥'},
    'btn_view_vel':     {'zh': 'View velocity.h5',     'en': 'View velocity.h5'},
    'btn_save_png':     {'zh': '儲存 PNG',             'en': 'Save PNG'},
    # Log pane
    'btn_copy_all':     {'zh': '複製全部',             'en': 'Copy all'},
    'btn_copy_sel':     {'zh': '複製選取',             'en': 'Copy selected'},
    'btn_clear':        {'zh': '清除',                 'en': 'Clear'},
    # Labels - Tab 1
    'lbl_pair_preview': {'zh': '干涉對預覽:',          'en': 'Pair preview:'},
    'lbl_n_equals':     {'zh': 'N =',                  'en': 'N ='},
    'lbl_day_intervals':{'zh': '時間間隔 (天):',        'en': 'Day intervals:'},
    'lbl_workdir':      {'zh': 'MintPy workdir:',      'en': 'MintPy workdir:'},
    'strategy_nearest_n':{'zh': 'Nearest N',           'en': 'Nearest N'},
    'strategy_grid':    {'zh': '自選天數',             'en': 'Day-interval grid'},
    # Dialogs
    'dlg_project_ready_title':{'zh': '偵測到已處理的干涉對',
                               'en': 'Processed interferograms detected'},
    'dlg_project_ready_msg':  {
        'zh': ('專案資料夾：\n  {d}\n\n'
               '已有完整 SNAP 干涉對輸出 {suf}。\n\n'
               '直接進入 MintPy 流程？\n'
               '（選「否」從 Tab 1 重新設定 SNAP 參數）'),
        'en': ('Project folder:\n  {d}\n\n'
               'Complete SNAP interferogram output found {suf}.\n\n'
               'Go directly to MintPy?\n'
               '(Select "No" to reconfigure SNAP parameters from Tab 1)'),
    },
    'dlg_cfg_ok':       {'zh': '且有 S1_smallbaseline.cfg',
                         'en': 'with S1_smallbaseline.cfg'},
    'dlg_cfg_missing':  {'zh': '（尚未產生 MintPy cfg）',
                         'en': '(MintPy cfg not yet generated)'},
    'loaded_prefs':     {'zh': '已載入: {f}',          'en': 'Loaded: {f}'},
    # Export tab
    'tab_export':        {'zh': '匯出 Export',         'en': 'Export'},
    'lf_geotiff':        {'zh': 'GeoTIFF 匯出',        'en': 'GeoTIFF Export'},
    'lf_ts_csv':         {'zh': '時間序列 CSV 匯出',   'en': 'Time-series CSV Export'},
    'lbl_source':        {'zh': '來源檔:',             'en': 'Source:'},
    'lbl_unit_detect':   {'zh': '輸出單位:',           'en': 'Output unit:'},
    'lbl_mask':          {'zh': '套用遮罩 maskTempCoh.h5', 'en': 'Apply mask maskTempCoh.h5'},
    'lbl_out_path':      {'zh': '輸出路徑:',           'en': 'Output path:'},
    'btn_export_gtiff':  {'zh': '匯出 GeoTIFF',        'en': 'Export GeoTIFF'},
    'btn_export_csv':    {'zh': '匯出 CSV',            'en': 'Export CSV'},
    'lbl_deramp':        {'zh': 'DeRamp（去線性趨勢，velocity only）',
                          'en': 'DeRamp (velocity only)'},
    'lbl_coh_thresh':    {'zh': 'Coh 遮罩門檻 (temporalCoherence):',
                          'en': 'Coh mask threshold (temporalCoherence):'},
    'lbl_coh_hint':      {'zh': '空白 = 不遮罩',       'en': 'blank = no mask'},
}


def _T(key: str, **kw) -> str:
    """Return translated string for the current LANG (falls back to 'zh')."""
    entry = _STRINGS.get(key, {})
    text  = entry.get(LANG) or entry.get('zh') or key
    return text.format(**kw) if kw else text


def _ask_language(default: str = 'zh') -> str:
    """Show a language-selection dialog before the main App window opens.

    Always shown on every startup; uses *default* as the pre-selected choice
    so pressing Enter or closing keeps the previous setting.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        root.tk.call('encoding', 'system', 'utf-8')
    except Exception:
        pass
    _setup_cjk_font(root)

    dlg = tk.Toplevel(root)
    dlg.title('Language / 語言')
    dlg.resizable(False, False)
    dlg.grab_set()
    # Center on screen
    dlg.update_idletasks()
    w, h = 320, 160
    sw = dlg.winfo_screenwidth(); sh = dlg.winfo_screenheight()
    dlg.geometry(f'{w}x{h}+{(sw-w)//2}+{(sh-h)//2}')

    result = tk.StringVar(value=default)

    tk.Label(dlg,
             text='請選擇介面語言\nSelect interface language',
             font=('TkDefaultFont', 12), pady=10).pack(padx=20)

    bf = tk.Frame(dlg); bf.pack(pady=10, padx=20)

    def _pick(lang):
        result.set(lang)
        dlg.destroy()

    style_zh = 'raised' if default == 'zh' else 'flat'
    style_en = 'raised' if default == 'en' else 'flat'
    tk.Button(bf, text='繁體中文', width=12, height=2, relief=style_zh,
              command=lambda: _pick('zh')).pack(side='left', padx=12)
    tk.Button(bf, text='English',  width=12, height=2, relief=style_en,
              command=lambda: _pick('en')).pack(side='left', padx=12)

    # Close = keep default
    dlg.protocol('WM_DELETE_WINDOW', dlg.destroy)

    root.wait_window(dlg)
    root.destroy()
    return result.get()


# ─────────────────────────────────────────────────────────────────────────
# Clipboard + dark log pane  (same pattern as FastISCE_topsApp_gui.py)
# ─────────────────────────────────────────────────────────────────────────
def _copy_to_clipboard(widget: tk.Widget, text: str) -> str:
    """Copy text to the OS clipboard.

    On WSLg the Tk X11 clipboard path (clipboard_clear/append + update()) can
    trigger a fatal `XIO: fatal IO error on X server` that kills the whole GUI
    — observed as "每次複製文字 GUI 就當掉".  So we use an OS clipboard tool
    (clip.exe on WSL, wl-copy/xclip on native Linux) FIRST and never touch the
    Tk X selection unless no tool exists.  When falling back to Tk we also drop
    the update() call (it pumps the X event loop synchronously, which is what
    surfaces the XIO crash).
    """
    import shutil as _sh
    import subprocess as _sp
    for cmd, name in [(['clip.exe'], 'clip.exe'),
                      (['wl-copy'], 'wl-copy'),
                      (['xclip', '-selection', 'clipboard'], 'xclip')]:
        if not _sh.which(cmd[0]):
            continue
        try:
            data = text.encode('utf-16le') if name == 'clip.exe' else text.encode('utf-8')
            p = _sp.run(cmd, input=data, capture_output=True, timeout=5, check=False)
            if p.returncode == 0:
                return f'[clipboard] {len(text)} chars → {name}'
        except Exception as exc:
            return f'[clipboard] {name} failed: {exc}'
    # Fallback only when no OS clipboard tool exists (native Linux without
    # xclip/wl-copy). No update() — that is the WSLg crash trigger.
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        return f'[clipboard] {len(text)} chars → Tk'
    except tk.TclError as exc:
        return f'[clipboard] failed: {exc}'


def _install_clipboard_guard(root: tk.Misc) -> None:
    """Route every Text/Entry Ctrl+C through the OS clipboard, app-wide.

    Without this, copying from text widgets that keep Tk's default binding
    (cfg editor, output panes) goes through Tk's X11 clipboard, which crashes
    the whole GUI on WSLg ('XIO: fatal IO error').  We grab the selected text
    via the widget's own 'sel' range (never selection_get(), which reads the
    X PRIMARY selection and can itself crash), hand it to _copy_to_clipboard
    (clip.exe path), and return 'break' to suppress Tk's default handler.
    """
    def _on_copy(ev):
        w = ev.widget
        s = None
        # Text widget: has a 'sel' tag range
        try:
            if w.tag_ranges('sel'):
                s = w.get('sel.first', 'sel.last')
        except (tk.TclError, AttributeError):
            # Entry-like: read selection by index, not selection_get()
            try:
                if w.selection_present():
                    s = w.get()[w.index('sel.first'):w.index('sel.last')]
            except Exception:
                s = None
        if not s:
            return 'break'
        _copy_to_clipboard(w, s)
        return 'break'

    for cls in ('Text', 'Entry', 'TEntry'):
        for seq in ('<Control-c>', '<Control-C>'):
            try:
                root.bind_class(cls, seq, _on_copy)
            except tk.TclError:
                pass


def _make_log(parent: tk.Widget, height: int = 12,
              font_size: int = 10) -> scrolledtext.ScrolledText:
    btnrow = ttk.Frame(parent)
    btnrow.pack(fill='x', pady=(0, 4))
    text = scrolledtext.ScrolledText(
        parent, height=height,
        background='#1e1e1e', foreground='#00ff00',
        insertbackground='#00ff00', font=('Consolas', font_size),
        exportselection=False)   # 不搶 X PRIMARY → 避免 WSLg 選取/複製時 XIO 崩潰
    text.pack(fill='both', expand=True)

    def _guard(event):
        if event.state & 0x0004 and event.keysym.lower() in ('c', 'a'):
            return None
        if event.keysym in ('Left', 'Right', 'Up', 'Down', 'Home', 'End',
                             'Prior', 'Next', 'Shift_L', 'Shift_R',
                             'Control_L', 'Control_R'):
            return None
        return 'break'
    text.bind('<Key>', _guard)

    def _ctrl_c(ev):
        try:
            s = text.get('sel.first', 'sel.last')
        except tk.TclError:
            return 'break'
        _copy_to_clipboard(text, s)
        return 'break'
    text.bind('<Control-c>', _ctrl_c)
    text.bind('<Control-C>', _ctrl_c)

    def _flash(msg):
        text.insert('1.0', msg + '\n')

    def _copy_all():
        _flash(_copy_to_clipboard(text, text.get('1.0', 'end-1c')))

    def _copy_sel():
        try:
            s = text.get('sel.first', 'sel.last')
        except tk.TclError:
            return
        _flash(_copy_to_clipboard(text, s))

    ttk.Button(btnrow, text=_T('btn_copy_all'), command=_copy_all).pack(side='left', padx=2)
    ttk.Button(btnrow, text=_T('btn_copy_sel'), command=_copy_sel).pack(side='left', padx=2)
    ttk.Button(btnrow, text=_T('btn_clear'),
               command=lambda: text.delete('1.0', 'end')).pack(side='left', padx=2)

    menu = tk.Menu(text, tearoff=0)
    menu.add_command(label=_T('btn_copy_sel'), command=_copy_sel)
    menu.add_command(label=_T('btn_copy_all'), command=_copy_all)
    menu.add_separator()
    menu.add_command(label=('清除' if LANG == 'zh' else 'Clear'), command=lambda: text.delete('1.0', 'end'))
    text.bind('<Button-3>',
              lambda e: menu.tk_popup(e.x_root, e.y_root))
    return text


# ─────────────────────────────────────────────────────────────────────────
# Date / SLC helpers
# ─────────────────────────────────────────────────────────────────────────
def scan_safe_dates(slc_dir: str,
                    satellites: 'Optional[List[str]]' = None) -> List[str]:
    """Return sorted dates that have at least one *valid* IW-mode SLC.

    Non-IW products (WV, EW, SM) and corrupted/incomplete IW files are
    silently excluded — only dates where SNAP GPT can actually open the
    product are returned.

    Args:
        slc_dir: SLC 資料夾。
        satellites: 若給定 (如 ['S1A'])，只納入檔名前綴在此清單的衛星之日期；
                    None 表示不限。用於限制干涉處理的衛星 (框幅/覆蓋一致性)。
    """
    p = Path(slc_dir)
    if not p.is_dir():
        return []

    # Collect candidate dates from IW-mode files only
    iw_dates: set = set()
    for item in _list_dir_cached(slc_dir):
        if '_IW_' not in item.name:
            continue
        if satellites and not any(item.name.startswith(s) for s in satellites):
            continue
        m = SAFE_DATE_RE.search(item.name)
        if m:
            iw_dates.add(m.group(1))

    # Keep only dates where find_slc_for_date returns a valid, readable product
    valid: List[str] = []
    for date in sorted(iw_dates):
        path = find_slc_for_date(slc_dir, date)
        if path is not None:
            ok, _ = validate_slc(path)
            if ok:
                valid.append(date)
    return valid


def _safe_covers_lat(safe_dir: Path, target_lat: float) -> bool:
    """Return True if any IW sub-swath annotation in safe_dir covers target_lat."""
    import glob as _glob
    import xml.etree.ElementTree as _ET
    anns = _glob.glob(str(safe_dir / 'annotation' / '*iw*vv*.xml'))
    if not anns:
        anns = _glob.glob(str(safe_dir / 'annotation' / '*iw*.xml'))
    for ann in anns:
        try:
            root = _ET.parse(ann).getroot()
            pts = root.findall('.//geolocationGridPoint')
            lats = [float(g.find('latitude').text) for g in pts]
            if lats and min(lats) <= target_lat <= max(lats):
                return True
        except Exception:
            pass
    return False


def _zip_covers_lat(zip_path: Path, target_lat: float) -> bool:
    """Return True if any IW annotation inside a zip covers target_lat."""
    import zipfile as _zf
    import xml.etree.ElementTree as _ET
    try:
        with _zf.ZipFile(zip_path) as zf:
            names = zf.namelist()
            ann_names = [n for n in names
                         if '/annotation/' in n and n.endswith('.xml')
                         and '/calibration/' not in n and '/rfi/' not in n
                         and 'iw' in n.lower() and 'vv' in n.lower()]
            if not ann_names:
                ann_names = [n for n in names
                             if '/annotation/' in n and n.endswith('.xml')
                             and '/calibration/' not in n and '/rfi/' not in n
                             and 'iw' in n.lower()]
            for ann in ann_names:
                with zf.open(ann) as f:
                    root = _ET.parse(f).getroot()
                pts = root.findall('.//geolocationGridPoint')
                lats = [float(g.find('latitude').text) for g in pts]
                if lats and min(lats) <= target_lat <= max(lats):
                    return True
    except Exception:
        pass
    return False


def _slc_lat_range(p: Path) -> 'Optional[Tuple[float, float]]':
    """Return (lat_min, lat_max) for a .SAFE dir or .zip archive, or None."""
    import zipfile as _zf
    import xml.etree.ElementTree as _ET
    try:
        if p.suffix == '.SAFE' and p.is_dir():
            ann_dir = p / 'annotation'
            xmls = [f for f in ann_dir.iterdir()
                    if f.suffix == '.xml' and 'iw' in f.name.lower()
                    and 'vv' in f.name.lower()] if ann_dir.is_dir() else []
            if not xmls:
                xmls = [f for f in ann_dir.iterdir()
                        if f.suffix == '.xml' and 'iw' in f.name.lower()
                        ] if ann_dir.is_dir() else []
            lats = []
            for x in xmls:
                root = _ET.parse(str(x)).getroot()
                lats += [float(g.find('latitude').text)
                         for g in root.findall('.//geolocationGridPoint')]
            return (min(lats), max(lats)) if lats else None
        if p.suffix == '.zip' and p.is_file():
            with _zf.ZipFile(p) as zf:
                names = zf.namelist()
                ann = [n for n in names
                       if '/annotation/' in n and n.endswith('.xml')
                       and '/calibration/' not in n and '/rfi/' not in n
                       and 'iw' in n.lower() and 'vv' in n.lower()]
                if not ann:
                    ann = [n for n in names
                           if '/annotation/' in n and n.endswith('.xml')
                           and '/calibration/' not in n and '/rfi/' not in n
                           and 'iw' in n.lower()]
                lats = []
                for a in ann:
                    with zf.open(a) as f:
                        root = _ET.parse(f).getroot()
                    lats += [float(g.find('latitude').text)
                             for g in root.findall('.//geolocationGridPoint')]
                return (min(lats), max(lats)) if lats else None
    except Exception:
        pass
    return None


def find_slcs_covering_lat_range(slc_dir: str, date: str,
                                 lat_min: float, lat_max: float) -> List[Path]:
    """Return all valid SAFE/zip files for ``date`` whose burst coverage
    overlaps the latitude range [lat_min, lat_max].

    Used to detect cross-frame acquisitions: if two adjacent frames both
    overlap the AOI, both paths are returned so the caller can use
    SliceAssembly to merge them before TOPSAR-Split.

    Falls back to an empty list when lat info is unavailable (caller should
    then use the single-frame find_slc_for_date path).
    """
    import zipfile as _zf
    import xml.etree.ElementTree as _ET

    def _lat_range_of_zip(p: Path):
        try:
            with _zf.ZipFile(p) as zf:
                names = zf.namelist()
                ann = [n for n in names
                       if '/annotation/' in n and n.endswith('.xml')
                       and '/calibration/' not in n and '/rfi/' not in n
                       and 'iw' in n.lower() and 'vv' in n.lower()]
                if not ann:
                    ann = [n for n in names
                           if '/annotation/' in n and n.endswith('.xml')
                           and '/calibration/' not in n and '/rfi/' not in n
                           and 'iw' in n.lower()]
                lats = []
                for a in ann:
                    with zf.open(a) as f:
                        root = _ET.parse(f).getroot()
                    lats += [float(g.find('latitude').text)
                              for g in root.findall('.//geolocationGridPoint')]
                return (min(lats), max(lats)) if lats else None
        except Exception:
            return None

    def _lat_range_of_safe(p: Path):
        try:
            ann_dir = p / 'annotation'
            xmls = [f for f in ann_dir.iterdir()
                    if f.suffix == '.xml' and 'iw' in f.name.lower()
                    and 'vv' in f.name.lower()] if ann_dir.is_dir() else []
            if not xmls:
                xmls = [f for f in ann_dir.iterdir()
                        if f.suffix == '.xml' and 'iw' in f.name.lower()
                        ] if ann_dir.is_dir() else []
            lats = []
            for x in xmls:
                root = _ET.parse(str(x)).getroot()
                lats += [float(g.find('latitude').text)
                          for g in root.findall('.//geolocationGridPoint')]
            return (min(lats), max(lats)) if lats else None
        except Exception:
            return None

    results: List[Path] = []
    for f in _list_dir_cached(slc_dir):
        if date not in f.name or '_IW_' not in f.name:
            continue
        if f.suffix == '.zip' and f.is_file():
            ok, _ = validate_slc(str(f))
            if not ok:
                continue
            r = _lat_range_of_zip(f)
        elif f.suffix == '.SAFE' and f.is_dir():
            ok, _ = validate_slc(str(f))
            if not ok:
                continue
            r = _lat_range_of_safe(f)
        else:
            continue
        if r is None:
            continue
        f_min, f_max = r
        # overlap: frame covers any part of [lat_min, lat_max]
        if f_min <= lat_max and f_max >= lat_min:
            results.append(f)

    return sorted(results)


def _safe_iw_lat_range(safe_path: Path, iw: str,
                       lon_min: 'Optional[float]' = None,
                       lon_max: 'Optional[float]' = None
                       ) -> 'Optional[Tuple[float, float]]':
    """讀單一 frame 中『特定 IW』(如 'iw1') 的緯度範圍 (min, max)。

    find_slcs_covering_lat_range 用所有 IW 的聯集緯度判定 frame 覆蓋,當只處理
    單一 IW 時會誤選 (例如某 frame 的 IW3 伸進 AOI 但 IW1 沒有)。本函式限定目標
    IW,用於『只處理單一 IW 時,正確判定哪個 frame 真正覆蓋 AOI』。

    當提供 lon_min/lon_max 時,只取「經度落在 AOI 經度帶」那幾個 range column
    (geolocationGridPoint 的 pixel 分組) 的緯度,而非整個 swath 寬度的極值 ——
    S1 swath 是斜的平行四邊形,窄 AOI 在特定經度處的實際方位向(緯度)涵蓋範圍,
    可能遠小於整個 swath 寬度上的緯度極值 (同 detect_iws_from_slc 的
    _iw_covers_aoi 修過的同類陷阱,只是這裡是緯度/經度角色互換)。找不到任何落
    在經度帶內的 column 時,退而使用經度最接近 AOI 中心的那個 column。
    """
    import xml.etree.ElementTree as _ET
    ann = safe_path / 'annotation'
    if not ann.is_dir():
        return None
    xmls = [x for x in ann.iterdir()
            if x.suffix == '.xml' and iw.lower() in x.name.lower()
            and 'vv' in x.name.lower()]
    if not xmls:
        xmls = [x for x in ann.iterdir()
                if x.suffix == '.xml' and iw.lower() in x.name.lower()]
    if not xmls:
        return None
    try:
        root = _ET.parse(str(xmls[0])).getroot()
    except Exception:
        return None

    if lon_min is None or lon_max is None:
        lats = [float(g.find('latitude').text)
                for g in root.findall('.//geolocationGridPoint')]
        return (min(lats), max(lats)) if lats else None

    cols: 'Dict[int, list]' = {}
    for g in root.findall('.//geolocationGridPoint'):
        try:
            px = int(g.find('pixel').text)
            la = float(g.find('latitude').text)
            lo = float(g.find('longitude').text)
        except Exception:
            continue
        cols.setdefault(px, []).append((la, lo))
    if not cols:
        return None

    col_lons = {px: sum(lo for _, lo in pts) / len(pts) for px, pts in cols.items()}
    in_band = [px for px, lo in col_lons.items() if lon_min <= lo <= lon_max]
    if not in_band:
        lon_mid = (lon_min + lon_max) / 2.0
        in_band = [min(col_lons, key=lambda px: abs(col_lons[px] - lon_mid))]
    lats = [la for px in in_band for la, _ in cols[px]]
    return (min(lats), max(lats)) if lats else None


# 目錄列舉快取：1224 項的 SMB iterdir 很慢，但每個日期都呼叫 find_slc_for_date
# 會重複列舉整個目錄 N 次。依目錄 mtime 失效，讓列舉每個 slc_dir 只跑一次。
_DIR_LIST_CACHE: 'Dict[str, Tuple[float, list]]' = {}


def _list_dir_cached(slc_dir: str) -> list:
    """Return cached list of Path entries in slc_dir, refreshed on mtime change."""
    try:
        mtime = Path(slc_dir).stat().st_mtime
    except OSError:
        return []
    hit = _DIR_LIST_CACHE.get(slc_dir)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        entries = list(Path(slc_dir).iterdir())
    except OSError:
        entries = []
    _DIR_LIST_CACHE[slc_dir] = (mtime, entries)
    return entries


def find_slc_for_date(slc_dir: str, date: str,
                      aoi_lat: Optional[float] = None) -> Optional[str]:
    """Return the best available SLC input path for SNAP GPT.

    Collects ALL candidates (zip + SAFE) that contain the date string,
    validates each, and returns the best one:
      1. valid .zip           (ASF download, guaranteed complete)
      2. valid .SAFE          (extracted, manifest.safe present)
      3. any .zip as fallback (SNAP can sometimes recover partial zips)
      4. any .SAFE as fallback

    When aoi_lat is given and multiple candidates exist, prefers the frame
    whose IW burst coverage includes that latitude.

    For .SAFE directories returns {safe_dir}/manifest.safe so that all
    SNAP versions can open the product.
    """
    zips_valid:  List[Path] = []
    safes_valid: List[Path] = []
    zips_any:    List[Path] = []
    safes_any:   List[Path] = []

    for f in _list_dir_cached(slc_dir):
        if date not in f.name:
            continue
        # Only accept IW-mode SLC products (skip WV, EW, SM, etc.)
        if '_IW_' not in f.name:
            continue
        if f.suffix == '.zip' and f.is_file():
            zips_any.append(f)
            ok, _ = validate_slc(str(f))
            if ok:
                zips_valid.append(f)
        elif f.suffix == '.SAFE' and f.is_dir():
            safes_any.append(f)
            ok, _ = validate_slc(str(f))
            if ok:
                safes_valid.append(f)

    # Auto-repair: if a valid zip exists with the same stem as an invalid SAFE,
    # extract the missing files from the zip to fix the SAFE in-place.
    for z in zips_valid:
        for s in safes_any:
            if z.stem == s.stem and s not in safes_valid:
                if repair_safe_from_zip(s, z):
                    safes_valid.append(s)
                    safes_any.remove(s)
                break

    def _pick_best(candidates: List[Path]) -> Path:
        if aoi_lat is not None and len(candidates) > 1:
            covering = []
            for p in candidates:
                if p.suffix == '.SAFE' and _safe_covers_lat(p, aoi_lat):
                    covering.append(p)
                elif p.suffix == '.zip' and _zip_covers_lat(p, aoi_lat):
                    covering.append(p)
            if covering:
                return sorted(covering)[0]
        return sorted(candidates)[0]

    # pick best candidate in priority order
    for candidates in (zips_valid, safes_valid, zips_any, safes_any):
        if candidates:
            best = _pick_best(candidates)
            if best.suffix == '.SAFE':
                manifest = best / 'manifest.safe'
                return str(manifest) if manifest.exists() else str(best)
            return str(best)
    return None


def validate_slc(path: str) -> Tuple[bool, str]:
    """Check whether an SLC file/directory passes integrity checks.

    Returns (True, 'ok') or (False, reason).

    SAFE directory checks:
      1. manifest.safe present
      2. measurement/ directory exists and contains .tiff files
      3. all .tiff measurement files are non-zero size (catches partial extractions)

    ZIP archive checks:
      1. file size > 1 MB
      2. zip is not corrupt (can be opened)
      3. contains manifest.safe entry inside the archive
    """
    import zipfile
    p = Path(path)
    if not p.exists():
        return False, 'not found'
    if p.name == 'manifest.safe':
        p = p.parent
    if p.suffix == '.SAFE':
        manifest = p / 'manifest.safe'
        meas = p / 'measurement'
        if not manifest.exists():
            return False, 'manifest.safe missing'
        if not meas.is_dir():
            return False, 'measurement/ directory missing'
        tiffs = list(meas.glob('*.tiff'))
        if not tiffs:
            return False, 'measurement/ contains no .tiff files'
        empty_tiffs = [t.name for t in tiffs if t.stat().st_size == 0]
        if empty_tiffs:
            return False, f'{len(empty_tiffs)} .tiff file(s) are 0-byte (incomplete extraction): {empty_tiffs[0]}'
        return True, 'ok'
    if p.suffix == '.zip':
        if p.stat().st_size < 1_000_000:
            return False, f'zip too small ({p.stat().st_size} bytes)'
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
            if not any('manifest.safe' in n for n in names):
                return False, 'zip missing manifest.safe entry'
        except zipfile.BadZipFile as exc:
            return False, f'corrupt zip: {exc}'
        return True, 'ok'
    return False, f'unrecognised format: {p.suffix}'


def repair_safe_from_zip(safe_dir: Path, zip_path: Path,
                         log_fn=None) -> bool:
    """Fill missing files in a SAFE directory using a same-product zip.

    Only extracts files that are absent or zero-byte in the SAFE, so the
    operation is fast even for large zips (typically only manifest.safe and
    a handful of XML files are missing after a partial download).

    Returns True if the SAFE passes validate_slc after repair.
    """
    import zipfile as _zf
    safe_name = safe_dir.name
    if not zip_path.is_file() or not safe_dir.is_dir():
        return False
    # The zip stem and SAFE stem must match (same product)
    if zip_path.stem != safe_dir.stem:
        return False
    try:
        with _zf.ZipFile(zip_path) as z:
            members = z.namelist()
            prefix = safe_name + '/'
            to_extract = []
            for m in members:
                if not m.startswith(prefix):
                    continue
                target = safe_dir.parent / m
                if target.is_dir():
                    continue
                if not target.exists() or target.stat().st_size == 0:
                    to_extract.append(m)
            if not to_extract:
                if log_fn:
                    log_fn((f'[repair] {safe_name}: 無缺漏檔案\n' if LANG == 'zh' else f'[repair] {safe_name}: no missing files\n'))
            else:
                if log_fn:
                    log_fn((f'[repair] 從 zip 補齊 {len(to_extract)} 個缺漏檔案 ...\n' if LANG == 'zh' else f'[repair] filling {len(to_extract)} missing files from zip ...\n'))
                z.extractall(safe_dir.parent, members=to_extract)
        ok, reason = validate_slc(str(safe_dir))
        if log_fn:
            log_fn((f'[repair] {"✓ 修復完成" if ok else f"✗ 仍有問題: {reason}"}\n' if LANG == 'zh' else f'[repair] {"✓ Repair complete" if ok else f"✗ still has issues: {reason}"}\n'))
        return ok
    except Exception as exc:
        if log_fn:
            log_fn((f'[repair] 失敗: {exc}\n' if LANG == 'zh' else f'[repair] failed: {exc}\n'))
        return False


def check_slc_completeness(slc_dir: str,
                            dates: List[str],
                            lat_min: Optional[float] = None,
                            lat_max: Optional[float] = None) -> Dict[str, str]:
    """Return {date: status} where status is 'ok', 'missing', or a failure reason.

    Accepts both .zip archives and .SAFE directories; both are validated via
    validate_slc().  Non-IW products (WV, EW, SM) are always ignored.

    When lat_min/lat_max are provided (AOI mode), the function is cross-frame
    aware: it uses find_slcs_covering_lat_range() to count how many valid frames
    overlap the AOI, then verifies that those frames collectively cover the full
    latitude range.  Dates that need two frames but only have one are reported as
    'partial (cross-frame: 1/2 frames)' instead of 'ok'.
    """
    result: Dict[str, str] = {}
    p = Path(slc_dir)
    all_items = list(p.iterdir())

    cross_frame_mode = lat_min is not None and lat_max is not None

    for d in dates:
        if cross_frame_mode:
            # ── Cross-frame-aware check ───────────────────────────────────
            valid_frames = find_slcs_covering_lat_range(
                slc_dir, d, lat_min, lat_max)  # type: ignore[arg-type]
            if not valid_frames:
                # No valid frame covers the AOI — check if anything exists
                iw_any = [f for f in all_items
                          if d in f.name and '_IW_' in f.name
                          and (f.suffix in ('.zip', '.SAFE')
                               or f.name.endswith('.SAFE'))]
                if iw_any:
                    reasons = []
                    offtrack = False
                    for f in sorted(iw_any):
                        ok, reason = validate_slc(str(f))
                        if not ok:
                            reasons.append(f'{f.name}: {reason}')
                            continue
                        # 有效 IW SLC，但其緯度範圍與 AOI 完全不相交
                        # → 該日影像不覆蓋 AOI（同軌不同衛星/datatake 未涵蓋此緯段）。
                        #   屬「不適用」而非「缺漏」：ASF 此日該 AOI 本就無景，
                        #   不該誤報缺、也無從補。
                        lr = _slc_lat_range(f)
                        if lr is not None and (lr[1] < lat_min or lr[0] > lat_max):
                            offtrack = True
                    if reasons:
                        result[d] = ' | '.join(reasons)
                    elif offtrack:
                        result[d] = ('n/a (off-track: 影像不覆蓋 AOI)' if LANG == 'zh' else 'n/a (off-track: image does not cover AOI)')
                    else:
                        result[d] = 'missing'
                else:
                    result[d] = 'missing'
                continue

            # Check whether valid_frames collectively cover [lat_min, lat_max].
            # A single frame that fully spans the AOI is sufficient; only flag
            # as cross-frame incomplete when the union of frame lat ranges does
            # NOT cover the full AOI range (real gap or missing second frame).
            lat_ranges = [r for r in (_slc_lat_range(f) for f in valid_frames)
                          if r is not None]
            if lat_ranges:
                cov_min = min(r[0] for r in lat_ranges)
                cov_max = max(r[1] for r in lat_ranges)
                fully_covered = cov_min <= lat_min and cov_max >= lat_max
            else:
                # Can't read lat metadata; fall back to count check
                all_iw_for_date = [f for f in all_items
                                   if d in f.name and '_IW_' in f.name
                                   and (f.suffix in ('.zip', '.SAFE')
                                        or f.name.endswith('.SAFE'))]
                fully_covered = len(valid_frames) >= len(all_iw_for_date)

            if fully_covered:
                result[d] = 'ok'
            else:
                all_iw_for_date = [f for f in all_items
                                   if d in f.name and '_IW_' in f.name
                                   and (f.suffix in ('.zip', '.SAFE')
                                        or f.name.endswith('.SAFE'))]
                result[d] = (f'cross-frame: {len(valid_frames)}/'
                             f'{len(all_iw_for_date)} frames ok')
            continue

        # ── Standard single-frame check (no AOI) ─────────────────────────
        iw_candidates = [
            f for f in all_items
            if d in f.name and '_IW_' in f.name
            and (f.suffix in ('.zip', '.SAFE') or f.name.endswith('.SAFE'))
        ]
        non_iw = [f for f in all_items if d in f.name and '_IW_' not in f.name]

        if not iw_candidates:
            if non_iw:
                result[d] = (f'IW SLC 不存在（找到非IW產品: {non_iw[0].name}）' if LANG == 'zh' else f'IW SLC not found (found non-IW product: {non_iw[0].name})')
            else:
                result[d] = 'missing'
            continue

        # Validate each candidate; use the first that passes
        reasons = []
        passed = False
        for cand in sorted(iw_candidates):
            check_path = str(cand)
            ok, reason = validate_slc(check_path)
            if ok:
                result[d] = 'ok'
                passed = True
                break
            reasons.append(f'{cand.name}: {reason}')

        if not passed:
            result[d] = ' | '.join(reasons)
    return result


def _asf_iso_day(d: str, end: bool = False) -> Optional[str]:
    """把任意分隔符的日期 (YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD) 正規化成
    asf_search 接受的 ISO 字串。剝除非數字後取前 8 碼 YYYYMMDD。

    end=True → 當日 23:59:59Z (結束)；否則 00:00:00Z (開始)。
    無法取得 8 碼數字時回 None (上游據此回報錯誤，而非送出畸形日期)。
    """
    digits = re.sub(r'\D', '', d or '')
    if len(digits) < 8:
        return None
    y, m, dd = digits[:4], digits[4:6], digits[6:8]
    return f'{y}-{m}-{dd}T{"23:59:59" if end else "00:00:00"}Z'


def download_slc_from_asf(date: str, dest_dir: str,
                           username: str, password: str,
                           platform: str = 'SENTINEL-1',
                           frame: Optional[int] = None,
                           log_fn=None) -> Optional[str]:
    """Search ASF for a scene matching *date* and download it to dest_dir.

    Uses the asf_search package (pip install asf-search).
    Returns the path to the downloaded file, or None on failure.
    """
    try:
        import asf_search as asf
    except ImportError:
        if log_fn:
            log_fn(('[ASF] asf_search 未安裝，自動安裝中...\n' if LANG == 'zh' else '[ASF] asf_search not installed, auto-installing...\n'))
        ok, out = _pip_install('asf-search')
        if not ok:
            if log_fn:
                log_fn((f'[ASF] 安裝失敗: {out[-200:]}\n' if LANG == 'zh' else f'[ASF] install failed: {out[-200:]}\n'))
            return None
        import asf_search as asf  # type: ignore[import]
        if log_fn:
            log_fn(('[ASF] asf_search 安裝完成\n' if LANG == 'zh' else '[ASF] asf_search installed\n'))

    try:
        if log_fn:
            log_fn((f'[ASF] 搜尋 {platform} date={date} ...\n' if LANG == 'zh' else f'[ASF] searching {platform} date={date} ...\n'))
        opts = dict(platform=[platform], processingLevel='SLC',
                    start=_asf_iso_day(date, end=False),
                    end=_asf_iso_day(date, end=True))
        if frame is not None:
            opts['frame'] = [frame]
        results = asf.search(**opts)
        if not results:
            if log_fn:
                log_fn((f'[ASF] 找不到 {date} 的場景\n' if LANG == 'zh' else f'[ASF] no scene found for {date}\n'))
            return None

        granule = results[0]
        if log_fn:
            log_fn((f'[ASF] 找到: {granule.properties["fileID"]}\n' if LANG == 'zh' else f'[ASF] found: {granule.properties["fileID"]}\n'))

        # asf_search ≤8.0.1 checks old cookie names ('urs_user_already_logged')
        # but the server now returns 'asf-urs'/'urs-access-token'.
        # Patch auth_cookie_names before authenticating so the check passes.
        _session = asf.ASFSession()
        _session.auth_cookie_names = ['asf-urs', 'urs-access-token', 'urs-user-id']
        session = _session.auth_with_creds(username, password)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        granule.download(path=dest, session=session)
        downloaded = list(dest.glob(f'*{date}*.zip'))
        if not downloaded:
            downloaded = list(dest.glob(f'*{date}*.SAFE'))
        if downloaded:
            if log_fn:
                log_fn((f'[ASF] 下載完成: {downloaded[0]}\n' if LANG == 'zh' else f'[ASF] download complete: {downloaded[0]}\n'))
            return str(downloaded[0])
        if log_fn:
            log_fn(('[ASF] 下載後找不到檔案\n' if LANG == 'zh' else '[ASF] file not found after download\n'))
        return None
    except Exception as exc:
        if log_fn:
            log_fn((f'[ASF] 下載失敗: {exc}\n' if LANG == 'zh' else f'[ASF] download failed: {exc}\n'))
        return None


def query_asf_dates(start_date: str, end_date: str,
                    wkt: str,
                    platform: str = 'SENTINEL-1',
                    frame: Optional[int] = None,
                    relative_orbit: Optional[int] = None,
                    log_fn=None) -> Optional[List[str]]:
    """Query ASF for IW SLC dates within date range + AOI polygon.

    Returns sorted list of YYYYMMDD strings, or None on failure.
    Anonymous search (no credentials required).

    relative_orbit: filter by Sentinel-1 relative orbit number (path number).
    Without this filter, scenes from ALL tracks crossing the AOI are returned,
    causing false "missing" reports for tracks other than the one collected locally.
    """
    try:
        import asf_search as asf  # type: ignore[import]
    except ImportError:
        ok, _ = _pip_install('asf-search')
        if not ok:
            return None
        try:
            import asf_search as asf  # type: ignore[import]
        except ImportError:
            return None

    # 空/非法 WKT 會讓 asf.search 長時間卡住而非快速失敗 → 先擋掉
    if not wkt or 'POLYGON' not in str(wkt).upper():
        if log_fn:
            log_fn(('[ASF] 查詢失敗: AOI WKT 為空或非法 '
                   '(請確認已設定 AOI 範圍)\n' if LANG == 'zh' else '[ASF] query failed: AOI WKT is empty or invalid (please confirm AOI range is set)\n'))
        return None

    try:
        s = _asf_iso_day(start_date, end=False)
        e = _asf_iso_day(end_date, end=True)
        if s is None or e is None:
            if log_fn:
                log_fn((f'[ASF] 查詢失敗: 日期格式無法解析 '
                       f'(start={start_date!r}, end={end_date!r})\n' if LANG == 'zh' else f'[ASF] query failed: could not parse date format (start={start_date!r}, end={end_date!r})\n'))
            return None
        opts: dict = dict(platform=[platform], processingLevel=['SLC'],
                          beamMode=['IW'], intersectsWith=wkt, start=s, end=e)
        if frame is not None:
            opts['frame'] = [frame]
        if relative_orbit is not None:
            opts['relativeOrbit'] = [relative_orbit]
        if log_fn:
            orb_note = f'  orbit={relative_orbit}' if relative_orbit else ''
            log_fn((f'[ASF] 查詢 {platform} IW SLC {start_date}~{end_date}{orb_note} ...\n' if LANG == 'zh' else f'[ASF] querying {platform} IW SLC {start_date}~{end_date}{orb_note} ...\n'))
        results = asf.search(**opts)
        dates: set = set()
        for r in results:
            file_id = r.properties.get('fileID', '')
            m = re.search(r'_(\d{8})T\d{6}_', file_id)
            if m:
                dates.add(m.group(1))
        return sorted(dates)
    except Exception as exc:
        if log_fn:
            log_fn((f'[ASF] 查詢失敗: {exc}\n' if LANG == 'zh' else f'[ASF] query failed: {exc}\n'))
        return None


def detect_iws_from_slc(slc_dir: str,
                         lon_min: float = -180.0, lat_min: float = -90.0,
                         lon_max: float = 180.0, lat_max: float = 90.0) -> List[str]:
    """Return the minimum set of subswath IDs needed to cover the AOI.

    Strategy (in priority order):
    1. Containment: if the AOI is fully contained within a single IW, return
       only that IW — avoids pulling in an adjacent IW that merely clips the
       AOI edge near a sub-swath boundary.
    2. Overlap: return all IWs whose bbox overlaps the AOI.
    3. Fallback: return all detected IWs if parsing fails.

    IW bbox is computed from geolocationGridPoints filtered to the AOI
    latitude band (±1°), so the lon boundaries at the relevant latitude are
    used rather than the full-scene extremes (which can extend the bbox by
    ~0.1° due to burst-level scan geometry variation with latitude).

    Fixes vs. original implementation
    ──────────────────────────────────
    • Zip files: calibration/ and rfi/ sub-directory XMLs (which sort before
      main annotation files and have no geolocationGridPoint) are excluded.
    • Sub-frame: find_slc_for_date is used to select the sub-frame that
      actually covers the AOI latitude instead of the first alphabetical file.
    """
    import xml.etree.ElementTree as ET
    import zipfile as _zf

    iw_vv_re = re.compile(r'-(iw\d)-slc-vv-', re.I)
    iw_any_re = re.compile(r'-(iw\d)-slc-',   re.I)
    p = Path(slc_dir)
    if not p.is_dir():
        return list(ALL_IW)

    use_full_globe = (lon_min == -180.0 and lat_min == -90.0 and
                      lon_max == 180.0  and lat_max == 90.0)

    # ── helpers ──────────────────────────────────────────────────────────
    def _iw_covers_aoi(xml_bytes: bytes) -> bool:
        """True iff this IW's swath actually reaches the AOI longitudes *at the
        AOI latitudes* — matching SNAP TOPSAR-Split's per-burst WKT check.

        A Sentinel-1 swath is a slanted parallelogram, so an IW's all-latitude
        (or even ±0.3° lat-band) lon bbox over-states its eastern/western reach
        at any single latitude.  Here we interpolate, per range column (constant
        pixel index along azimuth), the lon at the AOI's low/mid/high latitude,
        then test those interpolated lons against [lon_min, lon_max].  This
        excludes an IW that merely clips the AOI band at other latitudes (the
        bug that gave a small AOI [IW1,IW2] when only IW2 truly covers it, then
        failed split with 'wktAOI does not overlap any burst').
        """
        if use_full_globe:
            return True
        try:
            root = ET.fromstring(xml_bytes)
            cols: Dict[int, list] = {}
            lat_all: List[float] = []
            for g in root.findall('.//geolocationGridPoint'):
                px = int(g.find('pixel').text)        # type: ignore[union-attr]
                la = float(g.find('latitude').text)   # type: ignore[union-attr]
                lo = float(g.find('longitude').text)  # type: ignore[union-attr]
                cols.setdefault(px, []).append((la, lo))
                lat_all.append(la)
            if not lat_all:
                return False
            # latitude must overlap the IW's azimuth extent at all
            if not (min(lat_all) <= lat_max and max(lat_all) >= lat_min):
                return False
            lat_lo = max(lat_min, min(lat_all))
            lat_hi = min(lat_max, max(lat_all))
            targets = (lat_lo, (lat_lo + lat_hi) / 2.0, lat_hi)
            lons: List[float] = []
            for col in cols.values():
                col.sort()                            # by latitude (ascending)
                las = [c[0] for c in col]
                los = [c[1] for c in col]
                for t in targets:
                    if not (las[0] <= t <= las[-1]):
                        continue
                    for i in range(len(las) - 1):
                        if las[i] <= t <= las[i + 1]:
                            span = las[i + 1] - las[i]
                            f = (t - las[i]) / span if span else 0.0
                            lons.append(los[i] + f * (los[i + 1] - los[i]))
                            break
            if not lons:
                return False
            return min(lons) <= lon_max and max(lons) >= lon_min
        except Exception:
            return False

    def _read_iw_xmls(slc_path: str) -> Dict[str, bytes]:
        """Read main IW annotation XMLs from a SAFE dir or zip (VV preferred)."""
        iw_xmls: Dict[str, bytes] = {}
        safe_path = Path(slc_path.replace('/manifest.safe', ''))

        if safe_path.is_dir() and safe_path.suffix == '.SAFE':
            ann = safe_path / 'annotation'
            if ann.is_dir():
                for f in sorted(ann.iterdir()):
                    if not f.is_file() or not f.name.endswith('.xml'):
                        continue
                    m = iw_vv_re.search(f.name) or iw_any_re.search(f.name)
                    if m:
                        iw = m.group(1).upper()
                        if iw not in iw_xmls:
                            iw_xmls[iw] = f.read_bytes()

        elif safe_path.suffix == '.zip' and safe_path.is_file():
            try:
                with _zf.ZipFile(safe_path) as z:
                    for name in sorted(z.namelist()):
                        if not name.endswith('.xml') or '/annotation/' not in name:
                            continue
                        if '/annotation/calibration/' in name or '/annotation/rfi/' in name:
                            continue
                        m = iw_vv_re.search(name) or iw_any_re.search(name)
                        if m:
                            iw = m.group(1).upper()
                            if iw not in iw_xmls:
                                iw_xmls[iw] = z.read(name)
            except Exception:
                pass
        return iw_xmls

    def _select(iw_xmls: Dict[str, bytes]) -> List[str]:
        """Return the IW subswaths whose *bursts* actually reach the AOI.

        Uses _iw_covers_aoi (lon interpolated at the AOI latitude per range
        column) instead of a lat-band bbox.  A bbox over-includes an IW whose
        slanted swath edge clips the AOI band at *other* latitudes — e.g. a
        small AOI near IW1's eastern edge gets [IW1,IW2], then SNAP TOPSAR-Split
        rejects IW1 ('wktAOI does not overlap any burst') and the whole pair
        fails.  Matching SNAP's per-burst geometry here keeps only IWs that
        TOPSAR-Split will accept.

        Fallback: if nothing covers (e.g. non-covering frame), return all IWs.
        """
        if use_full_globe or not iw_xmls:
            return sorted(iw_xmls.keys())
        covering = [iw for iw in sorted(iw_xmls)
                    if _iw_covers_aoi(iw_xmls[iw])]
        return covering if covering else sorted(iw_xmls.keys())

    # ── find representative SLCs covering the AOI; take the union ──────────
    # Sample several dates spread across the archive.  Because framing can
    # vary across years, take the UNION of all detected IWs rather than the
    # intersection or the minimum — this ensures that any IW ever needed to
    # cover the AOI is included.  A frame that doesn't cover the AOI at all
    # returns an empty/fallback set which we ignore.
    aoi_lat = (lat_min + lat_max) / 2.0 if not use_full_globe else None

    if not use_full_globe:
        all_dates = sorted(set(
            m.group(1) for f in p.iterdir()
            if '_IW_' in f.name
            for m in [SAFE_DATE_RE.search(f.name)] if m
        ))
        if all_dates:
            n = len(all_dates)
            # spread + recent + earliest (recent framing matches campaigns best)
            idxs = sorted(set(
                [n - 1, n - 2, n - 3, 3 * n // 4, n // 2, n // 4, 0, 1, 2]))
            sample = [all_dates[i] for i in idxs if 0 <= i < n]
            union: set = set()
            for d in sample:
                candidate = find_slc_for_date(slc_dir, d, aoi_lat)
                if not candidate:
                    continue
                iw_xmls = _read_iw_xmls(candidate)
                if not iw_xmls:
                    continue
                sel = _select(iw_xmls)
                if sel and sel != sorted(iw_xmls.keys()):
                    # only count results that are genuinely AOI-driven
                    # (not the all-IW fallback from a non-covering frame)
                    union.update(sel)
            if union:
                return sorted(union)

    # ── fallback: scan directory ──────────────────────────────────────────
    for item in sorted(p.iterdir()):
        iw_xmls = _read_iw_xmls(str(item))
        if iw_xmls:
            return _select(iw_xmls)

    return list(ALL_IW)


def download_snap_dem(lon_min: float, lat_min: float,
                      lon_max: float, lat_max: float,
                      out_dir: str, log_fn=None) -> Optional[str]:
    """Download Copernicus GLO-30 as GeoTIFF suitable for SNAP/GPT (0.1° padded)."""
    import math
    try:
        from osgeo import gdal
    except ImportError:
        if log_fn:
            log_fn(('[DEM] osgeo/gdal 未安裝，無法自動下載\n' if LANG == 'zh' else '[DEM] osgeo/gdal not installed, cannot auto-download\n'))
        return None

    pad = 0.1
    s, n, w, e = lat_min - pad, lat_max + pad, lon_min - pad, lon_max + pad
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    tif = out_path / f'snap_dem_{w:.3f}_{s:.3f}_{e:.3f}_{n:.3f}.tif'
    if tif.exists():
        if log_fn:
            log_fn((f'[DEM] 已存在，重複使用: {tif}\n' if LANG == 'zh' else f'[DEM] already exists, reusing: {tif}\n'))
        return str(tif)

    def _aws_url(la: int, lo: int) -> str:
        lp = f'N{la:02d}' if la >= 0 else f'S{abs(la):02d}'
        lop = f'E{lo:03d}' if lo >= 0 else f'W{abs(lo):03d}'
        fn = f'Copernicus_DSM_COG_10_{lp}_00_{lop}_00_DEM'
        return f'/vsis3/copernicus-dem-30m/{fn}/{fn}.tif'

    gdal.SetConfigOption('AWS_NO_SIGN_REQUEST', 'YES')
    srcs: List[str] = []
    for la in range(math.floor(s), math.ceil(n)):
        for lo in range(math.floor(w), math.ceil(e)):
            url = _aws_url(la, lo)
            ds = gdal.Open(url)
            if ds:
                srcs.append(url)
                ds = None

    if not srcs:
        if log_fn:
            log_fn(('[DEM] 找不到 GLO-30 tile，請確認網路或 AWS_NO_SIGN_REQUEST\n' if LANG == 'zh' else '[DEM] GLO-30 tile not found, please check network or AWS_NO_SIGN_REQUEST\n'))
        return None

    if log_fn:
        log_fn((f'[DEM] 合併 {len(srcs)} tile → {tif}\n' if LANG == 'zh' else f'[DEM] merging {len(srcs)} tiles → {tif}\n'))

    vrt = str(tif.with_suffix('.vrt'))
    gdal.BuildVRT(vrt, srcs)
    gdal.Warp(str(tif), vrt, format='GTiff',
              outputBounds=(w, s, e, n), dstSRS='EPSG:4326',
              resampleAlg='bilinear',
              creationOptions=['COMPRESS=DEFLATE', 'TILED=YES', 'BIGTIFF=IF_SAFER'])
    try:
        Path(vrt).unlink()
    except Exception:
        pass

    if tif.exists():
        if log_fn:
            log_fn((f'[DEM] 完成: {tif}\n' if LANG == 'zh' else f'[DEM] done: {tif}\n'))
        return str(tif)
    return None


def _norm_date(s: str) -> str:
    s = (s or '').strip()
    if not s:
        return ''
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y%m%d')
        except ValueError:
            continue
    raise ValueError(f"unrecognized date '{s}'")


def filter_dates(dates: List[str], start: str, end: str) -> List[str]:
    s, e = _norm_date(start), _norm_date(end)
    return [d for d in dates if (not s or d >= s) and (not e or d <= e)]


def pairs_sequential(dates: List[str]) -> List[Tuple[str, str]]:
    return [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]


def pairs_nearest_n(dates: List[str], n: int) -> List[Tuple[str, str]]:
    out = []
    for i, d in enumerate(dates):
        for k in range(1, n + 1):
            if i + k < len(dates):
                out.append((d, dates[i + k]))
    return out


def pairs_grid(dates: List[str], day_list: List[int]) -> List[Tuple[str, str]]:
    day_set = set(day_list)
    fmt = '%Y%m%d'
    dts = [datetime.strptime(d, fmt) for d in dates]
    out = []
    for i in range(len(dts)):
        for j in range(i + 1, len(dts)):
            if (dts[j] - dts[i]).days in day_set:
                out.append((dates[i], dates[j]))
    return out


def delta_days(d1: str, d2: str) -> int:
    fmt = '%Y%m%d'
    return abs((datetime.strptime(d2, fmt) - datetime.strptime(d1, fmt)).days)


# ─────────────────────────────────────────────────────────────────────────
# Perpendicular baseline estimation from orbit state vectors
# ─────────────────────────────────────────────────────────────────────────
# 軌道狀態向量對同一個 SLC 永不改變 → 記憶化 + 落地 JSON。
# 重複解析 900KB annotation XML 是基線圖最大的成本 (SMB I/O + ElementTree)。
# 記憶體快取消除單次作業內的重複；JSON 快取讓 GUI 重啟後仍秒開。
_SV_MEM_CACHE: 'Dict[str, list]' = {}
_SV_DISK_CACHE: 'Optional[dict]' = None
_SV_DISK_PATH = Path.home() / '.cache' / 'snap2mintpy' / 'orbit_sv_cache.json'


def _sv_disk_load() -> dict:
    global _SV_DISK_CACHE
    if _SV_DISK_CACHE is None:
        try:
            _SV_DISK_CACHE = json.loads(_SV_DISK_PATH.read_text(encoding='utf-8'))
        except Exception:
            _SV_DISK_CACHE = {}
    return _SV_DISK_CACHE


def _sv_disk_save() -> None:
    try:
        _SV_DISK_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SV_DISK_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(_SV_DISK_CACHE), encoding='utf-8')
        tmp.replace(_SV_DISK_PATH)
    except Exception:
        pass


def _read_state_vectors(safe_dir: str) -> list:
    """Cached front-end for orbit state vector reads (see _read_state_vectors_raw).

    Cache key = annotation path + size (內容不變 → 安全)。記憶體未命中時查
    落地 JSON，再未命中才真正解析 XML，並同時寫回兩層快取。
    """
    key = safe_dir.replace('/manifest.safe', '')
    cached = _SV_MEM_CACHE.get(key)
    if cached is not None:
        return cached

    disk = _sv_disk_load()
    entry = disk.get(key)
    if entry is not None:
        # 還原 (datetime, x, y, z)；time 以 iso 字串存
        svs = []
        for t, x, y, z in entry:
            try:
                svs.append((datetime.fromisoformat(t), x, y, z))
            except Exception:
                pass
        if svs:
            _SV_MEM_CACHE[key] = svs
            return svs

    svs = _read_state_vectors_raw(safe_dir)
    _SV_MEM_CACHE[key] = svs
    if svs:
        disk[key] = [[t.isoformat(), x, y, z] for (t, x, y, z) in svs]
        _sv_disk_save()
    return svs


def _read_state_vectors_raw(safe_dir: str) -> list:
    """Read IW orbit state vectors from SAFE annotation XML.
    Returns list of (datetime, x, y, z) in ECEF metres.
    Handles S1A/S1B/S1C annotation schema (generalAnnotation/orbitList/orbit).
    Supports both extracted .SAFE directories and .zip archives.
    """
    import glob as _gl, xml.etree.ElementTree as _ET
    safe_dir = safe_dir.replace('/manifest.safe', '')

    root = None
    if safe_dir.lower().endswith('.zip'):
        import zipfile as _zf
        try:
            with _zf.ZipFile(safe_dir) as zf:
                names = zf.namelist()
                # Direct annotation XMLs: SAFE_DIR/annotation/FILE.xml (exactly 2 slashes)
                # Excludes calibration/, rfi/, noise/ subdirectories
                direct = [n for n in names
                          if n.endswith('.xml') and '/annotation/' in n
                          and n.count('/') == 2]
                ann_names = [n for n in direct if 'iw2' in n and 'vv' in n]
                if not ann_names:
                    ann_names = [n for n in direct if 'iw' in n and 'vv' in n]
                if not ann_names:
                    ann_names = direct
                if ann_names:
                    with zf.open(ann_names[0]) as f:
                        root = _ET.parse(f).getroot()
        except Exception:
            return []
    else:
        anns = _gl.glob(safe_dir + '/annotation/*iw2*vv*.xml')
        if not anns:
            anns = _gl.glob(safe_dir + '/annotation/*iw*.xml')
        if anns:
            try:
                root = _ET.parse(anns[0]).getroot()
            except Exception:
                return []

    if root is None:
        return []
    svs = []
    for sv in root.findall('./generalAnnotation/orbitList/orbit'):
        try:
            t_str = sv.find('time').text
            fmt = '%Y-%m-%dT%H:%M:%S.%f' if '.' in t_str else '%Y-%m-%dT%H:%M:%S'
            t = datetime.strptime(t_str, fmt)
            x = float(sv.find('position/x').text)
            y = float(sv.find('position/y').text)
            z = float(sv.find('position/z').text)
            svs.append((t, x, y, z))
        except Exception:
            continue
    return svs


def _interp_pos(svs: list, t_target: datetime) -> Optional['np.ndarray']:
    """Linear interpolation of ECEF position to t_target."""
    try:
        import numpy as np
        t0 = svs[0][0]
        ts = [(sv[0] - t0).total_seconds() for sv in svs]
        tt = (t_target - t0).total_seconds()
        pos = np.array([[sv[1], sv[2], sv[3]] for sv in svs], dtype=float)
        return np.array([np.interp(tt, ts, pos[:, i]) for i in range(3)])
    except Exception:
        return None


def _scene_center_time(safe_dir: str) -> Optional[datetime]:
    """Return mid-scene sensing time from annotation XML (adsHeader/startTime+stopTime)."""
    import glob as _gl, xml.etree.ElementTree as _ET
    safe_dir = safe_dir.replace('/manifest.safe', '')
    anns = _gl.glob(safe_dir + '/annotation/*iw2*vv*.xml')
    if not anns:
        anns = _gl.glob(safe_dir + '/annotation/*iw*.xml')
    if not anns:
        return None
    try:
        root = _ET.parse(anns[0]).getroot()
        hdr = root.find('adsHeader')
        if hdr is None:
            return None
        t0_el = hdr.find('startTime')
        t1_el = hdr.find('stopTime')
        if t0_el is None or t1_el is None:
            return None
        fmt = '%Y-%m-%dT%H:%M:%S.%f'
        dt0 = datetime.strptime(t0_el.text, fmt)
        dt1 = datetime.strptime(t1_el.text, fmt)
        return dt0 + (dt1 - dt0) / 2
    except Exception:
        return None


def _pos_at_ref_lat(svs: list, ref_lat_deg: float,
                    max_extrap_deg: float = 5.0) -> Optional['np.ndarray']:
    """Return the satellite ECEF position when it crosses ref_lat_deg.

    First tries exact interpolation between bracketing state vectors.
    If ref_lat_deg falls outside the covered range by at most max_extrap_deg,
    linearly extrapolates from the nearest endpoint pair — acceptable because
    S1 orbits are near-circular and baseline varies slowly along track.
    Returns None only if vectors are empty or gap exceeds max_extrap_deg.
    """
    try:
        import numpy as np
        lats = [np.degrees(np.arcsin(sv[3] / np.linalg.norm([sv[1], sv[2], sv[3]])))
                for sv in svs]
        # ── exact interpolation ───────────────────────────────────────────
        for i in range(len(lats) - 1):
            l0, l1 = lats[i], lats[i + 1]
            if (l0 - ref_lat_deg) * (l1 - ref_lat_deg) <= 0:
                frac = (ref_lat_deg - l0) / (l1 - l0) if (l1 - l0) else 0.5
                x = svs[i][1] + frac * (svs[i + 1][1] - svs[i][1])
                y = svs[i][2] + frac * (svs[i + 1][2] - svs[i][2])
                z = svs[i][3] + frac * (svs[i + 1][3] - svs[i][3])
                return np.array([x, y, z])
        # ── linear extrapolation from nearest endpoint ────────────────────
        lat_min, lat_max = min(lats), max(lats)
        if ref_lat_deg < lat_min:
            gap = lat_min - ref_lat_deg
            i0, i1 = 0, 1          # ascending: use first two vectors
        else:
            gap = ref_lat_deg - lat_max
            i0, i1 = -2, -1        # use last two vectors
        if gap > max_extrap_deg:
            return None
        l0, l1 = lats[i0], lats[i1]
        frac = (ref_lat_deg - l0) / (l1 - l0) if (l1 - l0) else 0.0
        x = svs[i0][1] + frac * (svs[i1][1] - svs[i0][1])
        y = svs[i0][2] + frac * (svs[i1][2] - svs[i0][2])
        z = svs[i0][3] + frac * (svs[i1][3] - svs[i0][3])
        return np.array([x, y, z])
    except Exception:
        return None


def compute_bperp(safe_ref: str, safe_sec: str,
                  aoi_lat: float = 23.83) -> Optional[float]:
    """Estimate perpendicular baseline (metres) between two SAFE acquisitions.

    Compares satellite ECEF positions at the moment each pass crosses
    aoi_lat (the scene-centre latitude).  This removes timing offsets
    between different sub-frame acquisitions, leaving only the true
    cross-track orbital deviation (~metres to ~hundreds of metres for S1).

    Returns None if orbit data is unavailable.
    """
    try:
        import numpy as np
        svs_ref = _read_state_vectors(safe_ref)
        svs_sec = _read_state_vectors(safe_sec)
        if not svs_ref or not svs_sec:
            return None

        # Satellite position when each pass crosses the scene centre latitude
        r_ref = _pos_at_ref_lat(svs_ref, aoi_lat)
        r_sec = _pos_at_ref_lat(svs_sec, aoi_lat)
        if r_ref is None or r_sec is None:
            return None

        B = r_sec - r_ref                           # baseline vector in ECEF (metres)
        Re = 6_371_000.0
        r_hat = r_ref / np.linalg.norm(r_ref)       # radial (up) at ref satellite
        ground = r_hat * Re                          # nadir ground point
        los = ground - r_ref                         # LOS vector (sat → ground)
        los_hat = los / np.linalg.norm(los)

        B_par     = float(np.dot(B, los_hat))
        Bperp_mag = float(np.sqrt(max(0.0, np.dot(B, B) - B_par ** 2)))

        # Sign: positive if secondary orbit is farther from scene in cross-track
        # direction (right-hand rule: cross_track = velocity × radial)
        vel_diff = np.array([svs_ref[-1][i] - svs_ref[-2][i] for i in (1, 2, 3)],
                            dtype=float)
        if np.linalg.norm(vel_diff) < 1e-9:
            return Bperp_mag
        vel_hat = vel_diff / np.linalg.norm(vel_diff)
        cross_track = np.cross(vel_hat, r_hat)
        sign = float(np.sign(np.dot(B, cross_track))) or 1.0
        return sign * Bperp_mag
    except Exception:
        return None


def find_bridge_pairs(pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Detect disconnected sub-networks and return bridge pairs to reconnect them.

    For each date that belongs to a disconnected component, creates a pair with
    the temporally nearest date **before** it AND the nearest date **after** it
    that belong to a *different* component (1 before + 1 after per date).
    Only pairs not already present in *pairs* are returned.
    Returns [] when the network is already fully connected.
    """
    all_dates = sorted(set(d for p in pairs for d in p))
    if len(all_dates) < 2:
        return []

    # Build adjacency list and find connected components via BFS
    adj: Dict[str, set] = {d: set() for d in all_dates}
    for r, s in pairs:
        adj[r].add(s); adj[s].add(r)

    visited: set = set()
    comp_of: Dict[str, int] = {}
    components: List[set] = []
    for start in all_dates:
        if start in visited:
            continue
        comp: set = set()
        q = [start]
        while q:
            node = q.pop()
            if node in visited:
                continue
            visited.add(node); comp.add(node)
            q.extend(adj[node] - visited)
        ci = len(components)
        for d in comp:
            comp_of[d] = ci
        components.append(comp)

    if len(components) <= 1:
        return []   # already fully connected

    existing = set(f'{r}_{s}' for r, s in pairs)
    new_pairs: List[Tuple[str, str]] = []
    seen_new: set = set()

    for d in all_dates:
        ci   = comp_of[d]
        idx  = all_dates.index(d)

        # nearest date before d from a different component
        before = [od for od in all_dates[:idx]   if comp_of[od] != ci]
        # nearest date after  d from a different component
        after  = [od for od in all_dates[idx+1:] if comp_of[od] != ci]

        for partner in ([before[-1]] if before else []) + ([after[0]] if after else []):
            r, s = (partner, d) if partner < d else (d, partner)
            key  = f'{r}_{s}'
            if key not in existing and key not in seen_new:
                new_pairs.append((r, s))
                seen_new.add(key)

    return new_pairs


def pair_mintpy_complete(pair_dir: 'Path', pair: str) -> bool:
    """該對 MintPy 產物是否『齊全且完整』(非只看資料夾名)。

    要求三個最終地理編碼產物都存在且通過 dimap_product_complete (含 .data/每個
    波段 .img 存在、size>0、抽樣非全零)：
      {pair}_coh_tc.dim   (同調性, MintPy 加權必需)
      {pair}_filt_tc.dim  (包裹相位)
      {pair}_unw_tc.dim   (解纏相位, MintPy SBAS 必需)
    刻意不收 {pair}_IW*_unw_tc.dim 等中間產物 → 避免「中間檔在、最終檔缺」誤判完成。
    任一不完整 → 回 False (該對需重跑)。
    """
    for name in (f'{pair}_coh_tc.dim', f'{pair}_filt_tc.dim', f'{pair}_unw_tc.dim'):
        f = pair_dir / name
        if not (f.exists() and dimap_product_complete(f)):
            return False
    return True


def pair_done_after(pair_dir: 'Path', pair: str, since_ts: float) -> bool:
    """force 重跑進度判定: 該對完整, 且最終產物 (unw_tc.dim) 是『本次開跑後』才寫的
    才算完成。since_ts<=0 → 退化為純 disk-truth (= pair_mintpy_complete)。

    用途: 「重跑全部」時, 舊產物仍在磁碟上會被 disk-truth 誤算成已完成, 進度從
    既有完成數起跳。改用開跑時間為界, 只算本次真正重做完成的對 → 進度從 0 起算;
    且若某對重跑失敗 (舊產物未被覆蓋), 其 mtime 仍是舊的 → 正確列為未完成。
    """
    if not pair_mintpy_complete(pair_dir, pair):
        return False
    if since_ts <= 0:
        return True
    try:
        return (pair_dir / f'{pair}_unw_tc.dim').stat().st_mtime >= since_ts
    except OSError:
        return False


def scan_processed_pairs(project_dir: str) -> List[Tuple[str, str]]:
    """Return (ref, sec) pairs whose full MintPy product set is complete on disk
    under interferograms/{ref}_{sec}/ (coh_tc + filt_tc + unw_tc, all complete)."""
    out: List[Tuple[str, str]] = []
    if not project_dir:
        return out
    ifg = Path(project_dir) / 'interferograms'
    if not ifg.is_dir():
        return out
    for d in sorted(ifg.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split('_')
        if len(parts) == 2 and all(len(x) == 8 and x.isdigit() for x in parts):
            if pair_mintpy_complete(d, d.name):
                out.append((parts[0], parts[1]))
    return out


def processed_day_intervals(processed_pairs: List[Tuple[str, str]]) -> set:
    """Day-interval values (from DAY_INTERVALS_ALL) that exactly match the Δdays
    of at least one already-processed pair."""
    ivs: set = set()
    for r, s in processed_pairs:
        try:
            d = delta_days(r, s)
        except ValueError:
            continue
        if d in DAY_INTERVALS_ALL:
            ivs.add(d)
    return ivs


def count_iw_scenes_in_range(slc_dir: str, start_date: str, end_date: str,
                             satellites: 'Optional[List[str]]' = None) -> int:
    """Count valid IW SLC acquisition dates within [start_date, end_date] for the
    given (checked) satellites in a local slc_dir."""
    if not (slc_dir and start_date and end_date):
        return 0
    return sum(1 for d in scan_safe_dates(slc_dir, satellites)
               if start_date <= d <= end_date)


def compute_new_image_pairs(existing_pairs: List[Tuple[str, str]],
                            available_dates: List[str],
                            strategy: str,
                            intervals: List[int],
                            nearest_n: int) -> List[Tuple[str, str]]:
    """Candidate pairs that involve at least one acquisition not yet present in
    the existing network, generated with the current pairing strategy. Used to
    extend an already-built network when new images arrive. Existing pairs are
    preserved (never returned); the result is the new pairs to add."""
    existing = set(existing_pairs)
    existing_dates = {d for p in existing_pairs for d in p}
    new_dates = {d for d in available_dates if d not in existing_dates}
    if not new_dates:
        return []
    if strategy == 'nearest_n':
        cand = pairs_nearest_n(available_dates, nearest_n)
    else:
        cand = pairs_grid(available_dates, intervals)
    return [p for p in cand
            if p not in existing and (p[0] in new_dates or p[1] in new_dates)]


def plot_baseline_network(
        pairs: List[Tuple[str, str]],
        slc_dir: str,
        aoi_lat: Optional[float] = None,
        title: str = 'InSAR Baseline Network',
        processed_pairs: Optional[List[Tuple[str, str]]] = None,
        return_edges: bool = False):
    """Return a matplotlib Figure with the time-Bperp baseline network.

    return_edges=True 時改回傳 (fig, {(ref,sec): Line2D})，供呼叫端在叢集執行
    中即時把某條干涉對線段改色 (完成綠/失敗紅)，不需重畫整張。

    Nodes = acquisition dates.  Edges = interferometric pairs.
    X-axis = acquisition date.
    Y-axis = perpendicular baseline directly computed from orbit state
             vectors relative to the most-connected date (not accumulated
             along a chain — avoids cumulative error for long networks).
    """
    import numpy as np
    from matplotlib.figure import Figure
    import matplotlib.dates as mdates

    # All unique dates that appear in at least one pair
    all_pair_dates = sorted(set(d for pair in pairs for d in pair))
    if not all_pair_dates:
        # 無 pair → 回空圖 (避免 all_pair_dates[0] IndexError)
        fig = Figure(figsize=(10, 5), constrained_layout=True)
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, ('(無干涉對)' if LANG == 'zh' else '(no pairs)'), ha='center', va='center')
        ax.set_axis_off()
        return (fig, {}) if return_edges else fig

    # Also collect ALL dates from slc_dir (needed for orbit lookup of dates
    # that appear in pairs but whose scan might differ from all_pair_dates)
    # For each date in the pairs, we need a SAFE path.
    lat = aoi_lat if aoi_lat is not None else 23.83

    # Build SLC path cache
    slc_cache: Dict[str, Optional[str]] = {}
    for d in all_pair_dates:
        slc_cache[d] = find_slc_for_date(slc_dir, d, aoi_lat)

    # ── reference date: earliest acquisition ──────────────────────────────
    # MintPy's SVD references every perpendicular baseline to the first
    # acquisition, so the earliest date sits at Bperp = 0.  all_pair_dates
    # is already sorted ascending, hence [0] is the earliest.
    ref_date = all_pair_dates[0]
    ref_slc  = slc_cache.get(ref_date)

    # ── Y = direct Bperp(ref_date, date) from orbit state vectors ────────
    # This is the CORRECT method: each Y is an independent measurement,
    # not accumulated along a chain (which would multiply errors).
    date_y: Dict[str, float] = {}
    for d in all_pair_dates:
        if d == ref_date:
            date_y[d] = 0.0
            continue
        d_slc = slc_cache.get(d)
        if ref_slc and d_slc:
            bp = compute_bperp(ref_slc, d_slc, aoi_lat=lat)
            date_y[d] = bp if bp is not None else 0.0
        else:
            date_y[d] = 0.0

    # ── detect disconnected sub-networks (for visual warning) ─────────────
    adj: Dict[str, set] = {d: set() for d in all_pair_dates}
    for ref, sec in pairs:
        adj[ref].add(sec); adj[sec].add(ref)
    visited_conn: set = set()
    components: List[set] = []
    for start in all_pair_dates:
        if start in visited_conn:
            continue
        comp: set = set()
        q = [start]
        while q:
            node = q.pop()
            if node in visited_conn:
                continue
            visited_conn.add(node); comp.add(node)
            q.extend(adj[node] - visited_conn)
        components.append(comp)

    # Assign a color per component
    comp_colors = ['steelblue', 'darkorange', 'forestgreen',
                   'purple', 'brown', 'teal']
    date_color: Dict[str, str] = {}
    for ci, comp in enumerate(components):
        c = comp_colors[ci % len(comp_colors)]
        for d in comp:
            date_color[d] = c

    date_dt = {d: datetime.strptime(d, '%Y%m%d') for d in all_pair_dates}

    # ── plot ─────────────────────────────────────────────────────────────
    # Use Figure() directly (not plt.subplots) so it is safe to call from
    # a background thread without triggering the "GUI outside main thread" warning.
    # constrained_layout reserves room for the y tick labels / axis titles and
    # re-flows them on window resize so they are never clipped.
    fig = Figure(figsize=(max(12, len(all_pair_dates) * 0.55), 6),
                 constrained_layout=True)
    ax = fig.add_subplot(111)
    fig.patch.set_facecolor('#f8f8f8')
    ax.set_facecolor('#f8f8f8')

    # When a processed-pair set is supplied, colour already-processed edges
    # distinctly (green) from new/pending ones (steelblue); otherwise fall back
    # to the per-component colouring.
    processed_set = {f'{r}_{s}' for r, s in (processed_pairs or [])}
    # 與即時改色 (_mark_edge / _BASELINE_EDGE_COLOR) 一致: 已完成綠 / 未處理灰
    PROC_C, NEW_C = '#00cc44', '#cccccc'

    # Draw pair lines
    edges: Dict[Tuple[str, str], object] = {}   # (ref,sec) → Line2D (供即時改色)
    for ref, sec in pairs:
        x1, x2 = date_dt[ref], date_dt[sec]
        y1, y2 = date_y[ref], date_y[sec]
        if processed_pairs is not None:
            color = PROC_C if f'{ref}_{sec}' in processed_set else NEW_C
        else:
            color = date_color.get(ref, 'steelblue')
        line, = ax.plot([x1, x2], [y1, y2], color=color,
                        linewidth=1.2, alpha=0.65, zorder=2)
        edges[(ref, sec)] = line
        bp_label = date_y[sec] - date_y[ref]
        xm = x1 + (x2 - x1) / 2
        ym = (y1 + y2) / 2
        if abs(bp_label) > 5:
            ax.text(xm, ym, f'{bp_label:+.0f}', fontsize=6, ha='center',
                    color=color, alpha=0.9, zorder=3,
                    bbox=dict(fc='white', ec='none', alpha=0.6, pad=0.5))

    # Draw date nodes
    for d in all_pair_dates:
        is_ref = (d == ref_date)
        marker = '*' if is_ref else 'o'
        sz     = 120 if is_ref else 45
        ec     = 'gold' if is_ref else 'white'
        ax.scatter(date_dt[d], date_y[d], s=sz,
                   color=date_color.get(d, 'crimson'),
                   marker=marker, zorder=5,
                   edgecolors=ec, linewidths=0.8)
        ax.text(date_dt[d], date_y[d],
                f'  {d[2:4]}/{d[4:6]}/{d[6:]}',
                fontsize=6.5, ha='left', va='center',
                color='#222', rotation=0, zorder=6)

    # Axis formatting — a tick mark every month so the spacing is readable,
    # but a label only every 3 months, drawn horizontally so they are not
    # clipped at the bottom edge.
    ax.xaxis.set_minor_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.tick_params(axis='x', which='major', length=7, labelrotation=0)
    ax.tick_params(axis='x', which='minor', length=3)
    for _lbl in ax.get_xticklabels():
        _lbl.set_ha('center')
    ax.set_xlabel('Acquisition Date', fontsize=10)
    ax.set_ylabel(f'Bperp vs {ref_date} (m)', fontsize=10)

    n_comp = len(components)
    warn = f'  [!] {n_comp} disconnected sub-networks' if n_comp > 1 else ''
    ax.set_title(
        f'{title}  [{len(pairs)} pairs, {len(all_pair_dates)} dates]{warn}',
        fontsize=11, color='red' if n_comp > 1 else 'black')
    ax.grid(True, which='major', alpha=0.3, linestyle='--')
    ax.grid(True, axis='x', which='minor', alpha=0.15, linestyle=':')
    ax.axhline(0, color='gray', linewidth=0.8, linestyle=':', zorder=1)

    from matplotlib.lines import Line2D
    # 三狀態圖例 (顏色與即時改色一致): 已完成綠 / 未處理灰 / 處理中橘。
    # 數字以「目前圖上 pairs」為準: 已完成 = 在 processed_set 內者; 其餘為未處理。
    if processed_pairs is not None:
        from matplotlib.legend import Legend
        proc_n = sum(1 for r, s in pairs if f'{r}_{s}' in processed_set)
        pend_n = len(pairs) - proc_n
        _status_handles = [
            Line2D([0], [0], color='#00cc44', lw=3, label=(f'已完成 Done ({proc_n})' if LANG == 'zh' else f'Done ({proc_n})')),
            Line2D([0], [0], color='#cccccc', lw=3, label=(f'未處理 Pending ({pend_n})' if LANG == 'zh' else f'Pending ({pend_n})')),
            Line2D([0], [0], color='#ff8c00', lw=3, label=('處理中 Running (0)' if LANG == 'zh' else 'Running (0)')),
            Line2D([0], [0], color='#ff4444', lw=3, label=('失敗 Failed (0)' if LANG == 'zh' else 'Failed (0)')),
        ]
        # 用獨立 Legend 物件 + add_artist 只加「一次」。
        # 不可用 `ax.legend(...)` 再接 `ax.add_artist(proc_leg)`: ax.legend() 本身
        # 已把該 legend 註冊成 axes 的 child, 再 add_artist 會把「同一物件」重複加入,
        # 繪圖時圖例被畫兩次 → 即時更新計數時文字產生疊影 (「處理中」疊在一起)。
        proc_leg = Legend(ax, _status_handles,
                          [h.get_label() for h in _status_handles],
                          fontsize=8, loc='upper right', framealpha=0.85)
        ax.add_artist(proc_leg)   # 加一次; 多 component 圖例由下方 ax.legend() 另建
        fig._status_legend = proc_leg   # 供即時依線段顏色更新計數 (4 行: 綠/灰/橘/紅)

    # Legend for multi-component case
    if n_comp > 1:
        legend_elements = [
            Line2D([0], [0], color=comp_colors[i % len(comp_colors)],
                   lw=2, label=f'Component {i + 1} ({len(c)} dates)')
            for i, c in enumerate(components)]
        ax.legend(handles=legend_elements, fontsize=8,
                  loc='upper left', framealpha=0.8)

    # Layout handled by constrained_layout=True (set on the Figure); calling
    # tight_layout() here would conflict with it.
    if return_edges:
        return fig, edges
    return fig


# ─────────────────────────────────────────────────────────────────────────
# SNAP helpers
# ─────────────────────────────────────────────────────────────────────────
def _find_snap_gpt() -> Optional[str]:
    """Try common SNAP installations in preference order:
    1. ~/esa-snap  (user-specific install, usually newest)
    2. ~/tools/esa-snap
    3. /opt/esa-snap, /opt/snap  (system-wide)
    4. shutil.which('gpt')  (whatever is in PATH)
    Returns None if not found."""
    candidates_home = [
        Path.home() / 'esa-snap' / 'bin' / 'gpt',
        Path.home() / 'tools' / 'esa-snap' / 'bin' / 'gpt',
    ]
    for c in candidates_home:
        if c.exists():
            return str(c)
    found = shutil.which('gpt') or shutil.which('gpt.sh')
    if found:
        return found
    candidates_sys = [
        Path('/opt/esa-snap/bin/gpt'),
        Path('/opt/snap/bin/gpt'),
        Path('/usr/local/bin/gpt'),
        Path('/usr/bin/gpt'),
    ]
    for c in candidates_sys:
        if c.exists():
            return str(c)
    return None


def snaphu_exe(snaphu_path: str) -> str:
    """Return the best available snaphu binary path.

    Tries the configured path first, then shutil.which, then common locations.
    """
    if snaphu_path and Path(snaphu_path).exists():
        return snaphu_path
    found = shutil.which('snaphu')
    if found:
        return found
    _SNAPHU_CANDIDATES = [
        Path.home() / 'tools' / 'snaphu' / 'bin' / 'snaphu',
        Path('/usr/local/bin/snaphu'),
        Path('/usr/bin/snaphu'),
        Path('/opt/snaphu/bin/snaphu'),
    ]
    for c in _SNAPHU_CANDIDATES:
        if c.exists():
            return str(c)
    return snaphu_path  # fallback: original (will fail with clear error)


def gpt_exe(snap_dir: str) -> str:
    p = Path(snap_dir) / 'bin' / ('gpt.exe' if os.name == 'nt' else 'gpt')
    if p.exists():
        return str(p)
    # snap_dir doesn't exist here (remote worker with different home dir).
    # Use _find_snap_gpt() which prefers ~/esa-snap over system PATH to avoid
    # using older system-wide SNAP installations (e.g. /usr/local/bin/gpt
    # pointing to SNAP 10 with JDK 11 G1GC bug on some machines).
    found = _find_snap_gpt()
    return found if found else str(p)


# ─────────────────────────────────────────────────────────────────────────
# SSD Swap helpers (OOM guard)
# ─────────────────────────────────────────────────────────────────────────
_SWAP_IMG_NAME = 'snap_swap.img'


def _swap_img_path(swap_dir: str) -> str:
    """Resolve the swapfile path, rebasing a peer machine's home onto THIS
    machine's home.

    Cluster note: dist_config.json carries the controller's *absolute*
    ssd_swap_path (e.g. /home/alice). Remote workers whose login user
    differs keep their swap at their own ~/snap_swap.img. So when the
    configured dir is a per-user home directory (/home/<user>, /Users/<user>,
    /root), always rebase it on the local Path.home(); explicit non-home dirs
    (e.g. /mnt/ssd) are left untouched.
    """
    swap_dir = os.path.expanduser((swap_dir or '').strip())
    home = str(Path.home())
    if not swap_dir:
        swap_dir = home
    else:
        parts = Path(swap_dir).parts
        if len(parts) >= 3 and parts[1] in ('home', 'Users'):
            # /home/<user>[/sub...] → <local home>[/sub...]
            swap_dir = str(Path(home, *parts[3:]))
        elif swap_dir == '/root':
            swap_dir = home
    return str(Path(swap_dir) / _SWAP_IMG_NAME)


def _run_sudo_cmd(args: list, sudo_pass: str,
                  timeout: int = 300) -> Tuple[int, str]:
    cmd = ['sudo', '-n'] + args  # -n: non-interactive (no password prompt)
    if sudo_pass:
        cmd = ['sudo', '-S'] + args
    stdin = (sudo_pass + '\n') if sudo_pass else None
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, 'timeout'
    except Exception as e:
        return -1, str(e)


def _swap_status(img_path: str) -> Tuple[bool, str]:
    """Return (is_active, info) for the given swapfile path."""
    try:
        r = subprocess.run(['swapon', '--show', '--noheadings', '--bytes'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if img_path in line:
                parts = line.split()
                info = ''
                if len(parts) >= 4:
                    size_g = int(parts[2]) / (1024 ** 3)
                    used_g = int(parts[3]) / (1024 ** 3)
                    info = f'{size_g:.0f}G, used={used_g:.1f}G'
                return True, info
        return False, ''
    except Exception as e:
        return False, str(e)


def _create_swapfile(img_path: str, size: str,
                     sudo_pass: str) -> Tuple[bool, str]:
    """fallocate + chmod 600 + mkswap.  Returns (ok, message)."""
    p = Path(img_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f'[ERROR] mkdir: {e}'
    if p.exists():
        return False, (f'[SKIP] 已存在: {img_path}' if LANG == 'zh' else f'[SKIP] already exists: {img_path}')
    rc, out = _run_sudo_cmd(['fallocate', '-l', size, img_path],
                            sudo_pass, timeout=600)
    if rc != 0:
        return False, f'[ERROR] fallocate: {out}'
    _run_sudo_cmd(['chmod', '600', img_path], sudo_pass)
    rc, out = _run_sudo_cmd(['mkswap', img_path], sudo_pass)
    if rc != 0:
        return False, f'[ERROR] mkswap: {out}'
    return True, (f'[OK] 建立完成: {img_path}' if LANG == 'zh' else f'[OK] created: {img_path}')


def _enable_swap(img_path: str, sudo_pass: str) -> Tuple[bool, str]:
    active, _ = _swap_status(img_path)
    if active:
        return True, (f'[OK] 已在使用中: {img_path}' if LANG == 'zh' else f'[OK] already in use: {img_path}')
    if not Path(img_path).exists():
        return False, (f'[ERROR] Swapfile 不存在: {img_path}' if LANG == 'zh' else f'[ERROR] swapfile does not exist: {img_path}')
    rc, out = _run_sudo_cmd(['swapon', img_path], sudo_pass)
    if rc != 0:
        return False, f'[ERROR] swapon: {out}'
    return True, (f'[OK] Swap 已啟用: {img_path}' if LANG == 'zh' else f'[OK] swap enabled: {img_path}')


def _disable_swap(img_path: str, sudo_pass: str) -> Tuple[bool, str]:
    active, _ = _swap_status(img_path)
    if not active:
        return True, ('[OK] Swap 未啟用，無需停用' if LANG == 'zh' else '[OK] swap not enabled, no need to disable')
    rc, out = _run_sudo_cmd(['swapoff', img_path], sudo_pass)
    if rc != 0:
        return False, f'[ERROR] swapoff: {out}'
    return True, (f'[OK] Swap 已停用: {img_path}' if LANG == 'zh' else f'[OK] swap disabled: {img_path}')



def bbox_to_wkt(lonmin, latmin, lonmax, latmax) -> str:
    return (f'POLYGON(({lonmin} {latmin},{lonmax} {latmin},'
            f'{lonmax} {latmax},{lonmin} {latmax},{lonmin} {latmin}))')


def pad_wkt_bbox(wkt: str, deg: 'Optional[float]' = None) -> str:
    """Expand a bbox-POLYGON WKT outward on all sides by `deg` degrees.

    The Goldstein/Multilook step crops the interferogram to the AOI polygon
    **in radar (slant) coordinates** (filter_ml / mergeIW Subset).  A S1 swath
    is a slanted parallelogram, so cropping to the exact lon/lat rectangle in
    radar space leaves the AOI's corner outside the kept data → after geocoding
    the corner is empty (the 'east/NE cut off' bug).  Padding the radar-crop
    polygon keeps enough surrounding data that the geocoded product fully covers
    the AOI rectangle; the FINAL Terrain-Correction Subset still crops to the
    exact AOI, so every pair's output extent is unchanged.

    deg=None → auto: 0.4×(AOI latitude span), clamped to [0.02, 0.06]°.  The
    corner offset scales with the AOI's azimuth (latitude) extent, so the pad
    does too; the clamp keeps tiny AOIs safe and bounds the extra Goldstein
    memory for large AOIs.  Empirically ±0.02° turned a clipped 65%-valid AOI
    into a full 100%-valid rectangle.
    """
    nums = [float(x) for x in re.findall(r'[-+]?\d+\.?\d*', wkt or '')]
    if len(nums) < 8:
        return wkt
    lons, lats = nums[0::2], nums[1::2]
    if deg is None:
        deg = min(0.06, max(0.02, 0.4 * (max(lats) - min(lats))))
    return bbox_to_wkt(min(lons) - deg, min(lats) - deg,
                       max(lons) + deg, max(lats) + deg)


def _coerce_to_wkt(raw: str) -> str:
    """Normalize loose coordinate input to a valid WKT POLYGON string.

    Accepts any of:
      [lon lat], [lon lat], ...
      lon lat, lon lat, ...
      POLYGON((lon lat, ...))   ← returned unchanged
    """
    import re as _re
    s = raw.strip()
    if not s:
        return s
    upper = s.upper()
    if upper.startswith('POLYGON') or upper.startswith('MULTIPOLYGON'):
        return s
    nums = _re.findall(r'[-+]?\d+\.?\d*', s)
    if len(nums) < 6 or len(nums) % 2 != 0:
        return s   # can't parse — return as-is, let downstream complain
    pairs = [(float(nums[i]), float(nums[i + 1])) for i in range(0, len(nums), 2)]
    if pairs[0] != pairs[-1]:
        pairs.append(pairs[0])
    return 'POLYGON((' + ', '.join(f'{lon} {lat}' for lon, lat in pairs) + '))'


def fill_graph(template: str, subs: Dict[str, str]) -> str:
    result = template
    for k, v in subs.items():
        result = result.replace(k, v)
    return result


def build_assemble_tpl(tpl_text: str, n: int) -> str:
    """Expand the 2-source SliceAssembly graph template to n Read nodes /
    n-source SliceAssembly, so cross-frame dates covered by 3+ overlapping
    SAFE files can all be fed into SliceAssembly (not just the first two).

    n == 2 returns tpl_text unchanged (identity) so the original 2-frame
    graph is byte-for-byte preserved. For n > 2, the "Read(2)" node block
    is duplicated for Read(3)..Read(n) (each with its own INPUTFILE{k}
    placeholder), and the SliceAssembly <sources> block gains a matching
    sourceProduct.{k-1} entry for each extra Read node. The applicationData
    presentation block is extended the same way on a best-effort basis
    (cosmetic only; SNAP gpt execution does not depend on it).
    """
    if n < 2:
        raise ValueError(f'build_assemble_tpl requires n >= 2, got {n}')
    if n == 2:
        return tpl_text

    # 1) Duplicate the "Read(2)" node block for frames 3..n.
    m = re.search(r'\n(\s*<node id="Read\(2\)">.*?</node>\n)', tpl_text, re.DOTALL)
    if not m:
        raise ValueError('assemble template missing Read(2) node block')
    read2_block = m.group(1)
    extra_read_blocks = ''.join(
        read2_block.replace('Read(2)', f'Read({k})').replace('INPUTFILE2', f'INPUTFILE{k}')
        for k in range(3, n + 1)
    )
    tpl_text = tpl_text[:m.end(1)] + extra_read_blocks + tpl_text[m.end(1):]

    # 2) Extend SliceAssembly <sources> with sourceProduct.2 .. sourceProduct.(n-1).
    m2 = re.search(r'([ \t]*)<sourceProduct\.1 refid="Read\(2\)"/>\s*\n', tpl_text)
    if not m2:
        raise ValueError('assemble template missing SliceAssembly sources block')
    indent = m2.group(1)
    extra_sources = ''.join(
        f'{indent}<sourceProduct.{k - 1} refid="Read({k})"/>\n' for k in range(3, n + 1)
    )
    tpl_text = tpl_text[:m2.end()] + extra_sources + tpl_text[m2.end():]

    # 3) Best-effort: extend the cosmetic applicationData presentation block too.
    m3 = re.search(r'(\s*<node id="Read\(2\)">\s*\n\s*<displayPosition[^\n]*\n\s*</node>\n)',
                   tpl_text)
    if m3:
        extra_presentation = ''.join(
            m3.group(1).replace('Read(2)', f'Read({k})') for k in range(3, n + 1)
        )
        tpl_text = tpl_text[:m3.end(1)] + extra_presentation + tpl_text[m3.end(1):]

    return tpl_text


def _img_all_zero(img_path: str, sample_bytes: int = 1 << 20) -> bool:
    """抽樣判斷 raw float32 .img 是否全零 (偵測崩潰/中斷留下的零填充檔)。

    只讀檔頭/中段/檔尾各一段 (預設各 1MB)，任一段有非零有限值即非全零。
    讀不到檔時回 False (不誤判為零，交由其他檢查處理)。
    """
    import numpy as np
    import os
    try:
        sz = os.path.getsize(img_path)
        if sz == 0:
            return True
        chunk = min(sample_bytes, sz)
        offsets = sorted(set([0, (sz - chunk) // 2, sz - chunk]))
        with open(img_path, 'rb') as f:
            for off in offsets:
                f.seek(off - (off % 4))
                a = np.frombuffer(f.read(chunk), dtype='>f4')
                if a.size and np.any(np.isfinite(a) & (a != 0)):
                    return False
        return True
    except Exception:
        return False


def dimap_product_complete(dim_path: 'Path') -> bool:
    """Return True only if a BEAM-DIMAP product is fully written.

    A bare ``.dim`` header can exist while the ``.data/*.img`` raster bands
    are missing or zero-length — e.g. when a SNAP gpt run is killed mid-way
    or runs out of memory. Such a product passes a naive ``.dim.exists()``
    check yet breaks every downstream step (SnaphuExport/Import, TC) with a
    silent FileNotFoundError, which then "passes in seconds". This validates:

      1. the ``.dim`` file exists and is non-empty;
      2. the sibling ``.data`` directory exists;
      3. every band declared in the ``.dim`` has a matching ``.img`` file
         whose size is > 0 bytes.

    Args:
        dim_path: Path to the BEAM-DIMAP ``.dim`` header.

    Returns:
        bool: True if the product is complete and usable; False on any
        missing/empty band or parse error.
    """
    import xml.etree.ElementTree as ET
    dim_path = Path(dim_path)
    try:
        if not dim_path.exists() or dim_path.stat().st_size == 0:
            return False
    except OSError:
        return False
    data_dir = dim_path.with_suffix('.data')
    if not data_dir.is_dir():
        return False
    try:
        root = ET.parse(str(dim_path)).getroot()
    except Exception:
        return False
    bands = root.findall('.//Spectral_Band_Info')
    if not bands:
        return False
    checked_content = False
    for bi in bands:
        name = (bi.findtext('BAND_NAME') or '').strip()
        if not name:
            continue
        # Virtual bands are computed from an expression and have no .img on
        # disk (e.g. Phase_*, Intensity_*_db); they must not fail the check.
        if (bi.findtext('VIRTUAL_BAND') or '').strip().lower() == 'true' \
                or (bi.findtext('EXPRESSION') or '').strip():
            continue
        img = data_dir / f'{name}.img'
        if not img.exists():
            cand = list(data_dir.glob(f'{name}*.img'))
            img = cand[0] if cand else None
        try:
            if img is None or not img.exists() or img.stat().st_size == 0:
                return False
        except OSError:
            return False
        # 偵測 gpt 崩潰/中斷留下的「零填充」產物：.dim/.img 都在、size>0，
        # 但內容全零 → dimap 會誤判完整而被下游 skip。抽樣第一個實體波段，
        # 全零視為不完整 (只查一個波段即可，崩潰會把所有波段歸零)。
        if not checked_content:
            if _img_all_zero(str(img)):
                return False
            checked_content = True
    return True


def _auto_memory_defaults() -> Tuple[str, str]:
    """Read /proc/meminfo and return (xmx, cache) targeting 80% of available RAM.

    Allocation: Xmx = 40% of RAM, Cache = 30% of RAM (total 70%, +10% headroom).
    Both rounded to nearest 5G.  Fallback: ('40g', '30G').
    """
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    total_gb = int(line.split()[1]) / 1024 / 1024
                    xmx_gb   = max(4,  round(total_gb * 0.40 / 5) * 5)
                    cache_gb  = max(4,  round(total_gb * 0.30 / 5) * 5)
                    return f'{xmx_gb}g', f'{cache_gb}G'
    except Exception:
        pass
    return '40g', '30G'


# ─────────────────────────────────────────────────────────────────────────
# Application state
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class AppState:
    language: str = 'zh'
    project_dir: str = field(default_factory=lambda: os.getcwd())
    slc_dir: str = '/mnt/SARDB/SARIMAGE/ST1/SAR_A69'
    ext_dem: str = ''
    snap_dir: str = os.path.expanduser('~/esa-snap')
    snap_home: str = os.path.expanduser('~/.snap')
    cpu: str = '10'
    cache: str = field(default_factory=lambda: _auto_memory_defaults()[1])
    xmx: str   = field(default_factory=lambda: _auto_memory_defaults()[0])
    ssd_swap_path: str = field(default_factory=lambda: str(Path.home()))
    ssd_swap_size: str = '100G'
    ssd_swap_auto: bool = True
    rg_looks: int = 6
    az_looks: int = 1
    smart_ml_n: int = 2
    smart_ml_coh: float = 0.6
    # MintPy 網路反演加權函數: var=同調(相位變異)加權LS, no=均權LS(低同調/植被),
    # coh=同調加權, fim=Fisher。實測 TSBS 泥炭盆地: var 使速度更接近 GNSS
    # (N=6 由 -20.1 → -21.1 ≈ GNSS -21.7), 但無法修正過長基線的整數解纏錯誤。
    mp_weight_func: str = 'var'
    snaphu_path: str = os.path.expanduser('~/tools/snaphu/bin/snaphu')

    asf_username: str = ''
    asf_password: str = ''
    asf_frame: str = ''
    asf_relative_orbit: str = ''

    aoi_mode: str = 'BBOX'
    lonmin: str = '120.843543'
    latmin: str = '23.801739'
    lonmax: str = '120.949544'
    latmax: str = '23.855914'
    wkt: str = ''

    polarisation: str = 'VV'
    do_esd: bool = True
    iw_list: List[str] = field(default_factory=lambda: list(ALL_IW))
    # IW 選擇模式: 'auto' = 由 SLC 掃描自動偵測 (執行時取偵測∩設定);
    # 'manual' = 完全照使用者勾選的 iw_list 跑, 略過自動偵測覆蓋
    # (用於某對某 IW 退化, 想強制只跑特定子帶, 例如只跑 IW1)。
    iw_mode: str = 'auto'
    # 參與干涉處理的衛星 (依檔名前綴 S1A/S1B/S1C/S1D 篩選日期)。
    # 同一軌道不同衛星框幅/覆蓋可能不同 — 限制衛星可確保幾何一致。
    satellites: List[str] = field(
        default_factory=lambda: ['S1A', 'S1B', 'S1C', 'S1D'])

    start_date: str = ''
    end_date: str = ''
    available_dates: List[str] = field(default_factory=list)

    pair_strategy: str = 'nearest_n'
    nearest_n: int = 3
    selected_day_intervals: List[int] = field(default_factory=lambda: [12, 24])
    pairs: List[Tuple[str, str]] = field(default_factory=list)
    # 使用者手動移除的日期 / 干涉對 (key 'ref_sec')；自動擴充(新影像/橋接)時
    # 一律排除，讓「移除/篩選」跨重畫與跨 GUI 重開都持久 (不被當新影像加回)。
    excluded_dates: List[str] = field(default_factory=list)
    excluded_pairs: List[str] = field(default_factory=list)

    def wkt_polygon(self) -> str:
        if self.aoi_mode == 'WKT':
            return _coerce_to_wkt(self.wkt)
        return bbox_to_wkt(self.lonmin, self.latmin,
                            self.lonmax, self.latmax)

    def aoi_bbox(self) -> Tuple[float, float, float, float]:
        """Return (lon_min, lat_min, lon_max, lat_max) for the active AOI.

        When aoi_mode == 'WKT', derives the extent from the WKT polygon so
        that IW detection uses the same coordinate space as SNAP processing.
        Falls back to the bbox fields when WKT parsing fails.
        """
        if self.aoi_mode == 'WKT':
            try:
                nums = [float(x) for x in re.findall(r'[-+]?\d+\.?\d*', self.wkt)]
                if len(nums) >= 4:
                    lons = nums[0::2]
                    lats = nums[1::2]
                    return min(lons), min(lats), max(lons), max(lats)
            except Exception:
                pass
        try:
            return (float(self.lonmin), float(self.latmin),
                    float(self.lonmax), float(self.latmax))
        except (TypeError, ValueError):
            return -180.0, -90.0, 180.0, 90.0

    def baseline_fingerprint(self) -> dict:
        """Return a dict of the settings that determine the baseline network.

        Used to detect whether cached baseline PNG is still valid.
        """
        return {
            'slc_dir':                self.slc_dir,
            'start_date':             self.start_date,
            'end_date':               self.end_date,
            'pair_strategy':          self.pair_strategy,
            'nearest_n':              self.nearest_n,
            'selected_day_intervals': sorted(self.selected_day_intervals),
            'aoi_mode':               self.aoi_mode,
            'lonmin':                 self.lonmin,
            'latmin':                 self.latmin,
            'lonmax':                 self.lonmax,
            'latmax':                 self.latmax,
            'wkt':                    self.wkt,
            'pairs_count':            len(self.pairs),
            # Bump when the baseline plot rendering changes so stale cached
            # PNGs (drawn with the old style) are invalidated and redrawn.
            'plot_version':           2,
        }

    def to_dict(self) -> dict:
        # 不序列化 asf_password → 避免明文密碼寫進 prefs/dist_config (共享 /mnt/SARDB)。
        # 密碼改由 ~/.netrc 持久 (download 已優先讀 netrc); GUI 欄位僅存活於當次 session。
        d = {k: (list(v) if isinstance(v, list) else v)
             for k, v in self.__dict__.items()
             if k not in ('available_dates', 'asf_password')}
        # pairs: serialize as list-of-lists (JSON-compatible)
        d['pairs'] = [list(p) for p in self.pairs]
        return d

    def from_dict(self, d: dict):
        def _valid_date(s):
            try:
                datetime.strptime(str(s), '%Y%m%d')
                return True
            except (ValueError, TypeError):
                return False

        for k, v in d.items():
            if not hasattr(self, k):
                continue
            if k == 'pairs':
                valid = [tuple(p) for p in v
                         if len(p) == 2 and _valid_date(p[0]) and _valid_date(p[1])]
                skipped = len(v) - len(valid)
                if skipped:
                    print(f'[AppState] WARNING: skipped {skipped} pair(s) with '
                          f'invalid date strings while loading prefs')
                self.pairs = valid
            else:
                setattr(self, k, v)


# ─────────────────────────────────────────────────────────────────────────
# Smart Multilook helpers
# ─────────────────────────────────────────────────────────────────────────
def block_max_coh_decimate(i_arr: 'np.ndarray', q_arr: 'np.ndarray',
                           coh_arr: 'np.ndarray', n: int):
    """非重疊 n×n 區塊降採樣：每格取區塊內 coh 最大像素的 (i, q, coh)。

    這是「smart multilook」的核心：把 (H, W) 的 ifg 實部/虛部/同調性，
    以不重疊的 n×n 方塊降採樣到 (H//n, W//n)，每個輸出格採用該方塊內
    同調性最高那個像素的 i/q/coh（相位與振幅都來自同一像素）。

    Args:
        i_arr:   全解析度 i_ifg (H, W) float。
        q_arr:   全解析度 q_ifg (H, W) float。
        coh_arr: 全解析度 coherence (H, W) float。
        n:       降採樣因子（azimuth 與 range 同為 n）。
    Returns:
        (new_i, new_q, new_coh): 各為 (H//n, W//n) float32。
    """
    import numpy as np
    H, W = coh_arr.shape
    oh, ow = H // n, W // n
    Hc, Wc = oh * n, ow * n

    def _blocks(a):
        # (H, W) → (oh, ow, n*n)，每列為一個 n×n 方塊攤平
        return (a[:Hc, :Wc].reshape(oh, n, ow, n)
                .transpose(0, 2, 1, 3).reshape(oh, ow, n * n))

    coh_b = _blocks(coh_arr)
    i_b   = _blocks(i_arr)
    q_b   = _blocks(q_arr)
    # NaN coh → -1，確保不會被選為最大
    idx = np.argmax(np.where(np.isnan(coh_b), -1.0, coh_b), axis=2)  # (oh, ow)
    ii, jj = np.indices((oh, ow))
    new_coh = coh_b[ii, jj, idx].astype(np.float32)
    new_i   = i_b[ii, jj, idx].astype(np.float32)
    new_q   = q_b[ii, jj, idx].astype(np.float32)
    return new_i, new_q, new_coh


def apply_smart_ml(ml_dim: str, sml_dim: str, n: int, log_fn=None) -> bool:
    """以「n×n 區塊 max-coh」覆寫 SNAP Multilook 產物的 i/q/coh 波段。

    先決條件：sml_dim 已由 SNAP ``Multilook nAzLooks=nRgLooks=n`` 產生
    ——尺寸、間距、行時距等 metadata 都已由 SNAP 正確降採樣，且 elevation /
    orthorectifiedLat/Lon 等幾何波段已被 SNAP **平均** (符合幾何用平均的需求)。

    本函式只做一件事：把 i_ifg / q_ifg / coh 三個波段，從 SNAP 的「平均」值
    換成「每個 n×n 方塊取 coh 最大像素」的值 (smart multilook 定義)。其餘
    波段與全部 metadata 保持 SNAP 多視結果不變。

    Args:
        ml_dim:  原始全解析度 ifg_ml 的 .dim (提供 max-coh 選取的來源)。
        sml_dim: 已多視的 .dim；其 .data 內 i/q/coh .img 將被覆寫。
        n:       降採樣因子。
    Returns:
        bool: True 表示覆寫成功。
    """
    import numpy as np
    import xml.etree.ElementTree as ET
    from pathlib import Path

    ml_data  = Path(ml_dim).with_suffix('.data')
    sml_data = Path(sml_dim).with_suffix('.data')

    def _bands(dim_path: str) -> List[dict]:
        try:
            root = ET.parse(str(dim_path)).getroot()
        except Exception:
            return []
        out: List[dict] = []
        for bi in root.findall('.//Spectral_Band_Info'):
            name = (bi.findtext('BAND_NAME') or '').strip()
            w    = int(bi.findtext('BAND_RASTER_WIDTH') or 0)
            h    = int(bi.findtext('BAND_RASTER_HEIGHT') or 0)
            if name and w and h:
                out.append({'name': name, 'w': w, 'h': h})
        return out

    ml_bands  = _bands(ml_dim)
    sml_bands = _bands(sml_dim)
    if not ml_bands or not sml_bands:
        if log_fn:
            log_fn(('[SML] 無法解析 ml/sml 波段資訊\n' if LANG == 'zh' else '[SML] cannot parse ml/sml band info\n'))
        return False

    def _find(bands: List[dict], prefix: str) -> Optional[dict]:
        for b in bands:
            if b['name'].startswith(prefix):
                return b
        return None

    ml_i, ml_q, ml_c = (_find(ml_bands, 'i_ifg'),
                        _find(ml_bands, 'q_ifg'),
                        _find(ml_bands, 'coh_'))
    sm_i, sm_q, sm_c = (_find(sml_bands, 'i_ifg'),
                        _find(sml_bands, 'q_ifg'),
                        _find(sml_bands, 'coh_'))
    if None in (ml_i, ml_q, ml_c, sm_i, sm_q, sm_c):
        if log_fn:
            log_fn(('[SML] 找不到 i_ifg/q_ifg/coh 波段\n' if LANG == 'zh' else '[SML] i_ifg/q_ifg/coh bands not found\n'))
        return False

    def _read(data_dir: 'Path', info: dict) -> Optional['np.ndarray']:
        img = data_dir / f"{info['name']}.img"
        if not img.exists():
            cand = list(data_dir.glob(f"{info['name']}*.img"))
            if not cand:
                return None
            img = cand[0]
        try:
            # SNAP BEAM-DIMAP byte_order=1 → big-endian float32
            return np.fromfile(str(img), dtype='>f4').reshape(info['h'], info['w'])
        except Exception:
            return None

    i_full = _read(ml_data, ml_i)
    q_full = _read(ml_data, ml_q)
    c_full = _read(ml_data, ml_c)
    if i_full is None or q_full is None or c_full is None:
        if log_fn:
            log_fn(('[SML] 讀取 ml .img 失敗\n' if LANG == 'zh' else '[SML] failed to read ml .img\n'))
        return False

    new_i, new_q, new_c = block_max_coh_decimate(i_full, q_full, c_full, n)

    # 對齊 SNAP Multilook 的輸出尺寸 (floor 對齊；若差 1 列/行則裁切)
    th, tw = sm_i['h'], sm_i['w']
    if new_i.shape != (th, tw):
        if log_fn:
            log_fn((f'[SML] 尺寸對齊 {new_i.shape} → ({th},{tw})\n' if LANG == 'zh' else f'[SML] resizing {new_i.shape} → ({th},{tw})\n'))
        new_i, new_q, new_c = new_i[:th, :tw], new_q[:th, :tw], new_c[:th, :tw]
        if new_i.shape != (th, tw):
            if log_fn:
                log_fn(('[SML] 尺寸無法對齊，放棄覆寫\n' if LANG == 'zh' else '[SML] cannot align size, aborting overwrite\n'))
            return False

    def _write(info: dict, arr: 'np.ndarray'):
        img = sml_data / f"{info['name']}.img"
        if not img.exists():
            cand = list(sml_data.glob(f"{info['name']}*.img"))
            if cand:
                img = cand[0]
        arr.astype('>f4').tofile(str(img))
        if log_fn:
            log_fn((f'[SML] 覆寫 {img.name} ({arr.shape[1]}×{arr.shape[0]})\n' if LANG == 'zh' else f'[SML] overwriting {img.name} ({arr.shape[1]}×{arr.shape[0]})\n'))

    _write(sm_i, new_i)
    _write(sm_q, new_q)
    _write(sm_c, new_c)
    if log_fn:
        log_fn((f'[SML] max-coh 覆寫完成 n={n}\n' if LANG == 'zh' else f'[SML] max-coh overwrite complete n={n}\n'))
    return True


def _hdr_is_big_endian(img_path: 'Path') -> bool:
    """讀 ENVI .hdr 的 byte order；1=big-endian。找不到時預設 big-endian。"""
    import re
    from pathlib import Path
    hdr = Path(str(img_path)[:-4] + '.hdr') if str(img_path).endswith('.img') \
        else Path(img_path).with_suffix('.hdr')
    try:
        m = re.search(r'byte\s*order\s*=\s*(\d)', hdr.read_text(), re.I)
        return (m is None) or (m.group(1) == '1')
    except Exception:
        return True


def split_burst_count(split_dim: str):
    """從 split .dim 的 first/lastBurstIndex 推算 burst 數。

    單 burst (回傳 1) 時 TOPS 的 ESD 無重疊區可用，會破壞配準 → 干涉圖全零；
    此時 ifg 圖須改用 noESD 變體。回傳 None 表示無法判定。
    """
    import re
    from pathlib import Path
    try:
        t = Path(split_dim).read_text()
        fb = re.search(r'name="firstBurstIndex"[^>]*>\s*(\d+)', t)
        lb = re.search(r'name="lastBurstIndex"[^>]*>\s*(\d+)', t)
        if fb and lb:
            return int(lb.group(1)) - int(fb.group(1)) + 1
    except Exception:
        pass
    return None


def gapfill_low_coh_phase(phase_img: str, coh_img: str, width: int,
                          coh_min: float, log_fn=None):
    """unwrap 前：把 wrapped phase 中 coh<coh_min 的像素以複數線性內插填補。

    相位是環狀量，直接線性內插角度會在 ±π 跳變處出錯；改為對 cos(φ)、sin(φ)
    各自線性內插，再 ``atan2`` 還原 → 結果天生落在 (-π, π]，無需裁切。凸包外
    的洞用最近鄰補。填補後就地覆寫 phase_img，供 snaphu 在連續相位上解纏。

    Args:
        phase_img: snaphu 匯出的 wrapped phase .img (raw float32)。
        coh_img:   對應 coherence .img (同尺寸)。
        width:     影像寬度 (samples)。
        coh_min:   同調性門檻；< 此值者視為缺洞。
    Returns:
        (mask, be): mask 為低同調遮罩 (h, w) bool，供解纏後還原 NaN；
                    be 為 phase_img 的位元序 (big-endian?)。失敗回 (None, be)。
    """
    import numpy as np
    # snaphu export .img 實際是 native little-endian，但其 ENVI .hdr 常謊報
    # byte order=1 (big)。不信 hdr，改用 coherence 值域 (必落在 ~0..1) 自動
    # 判定位元序，避免讀寫錯 endian 害 snaphu 讀到 NaN/inf 而 Abort。
    def _valid_coh(a):
        fin = a[np.isfinite(a)]
        return bool(fin.size) and fin.min() >= -0.01 and fin.max() <= 1.5
    coh_le = np.fromfile(coh_img, dtype='<f4')
    coh_be = np.fromfile(coh_img, dtype='>f4')
    if _valid_coh(coh_le):
        dt, coh = '<f4', coh_le
    elif _valid_coh(coh_be):
        dt, coh = '>f4', coh_be
    else:
        dt, coh = '<f4', coh_le
    phase = np.fromfile(phase_img, dtype=dt)
    if width <= 0 or phase.size % width or phase.size != coh.size:
        if log_fn:
            log_fn(('[nan] phase/coh 尺寸不符，跳過 gap-fill\n' if LANG == 'zh' else '[nan] phase/coh size mismatch, skipping gap-fill\n'))
        return None, (dt == ">f4")
    h = phase.size // width
    phase = phase.reshape(h, width).astype(np.float64)
    coh   = coh.reshape(h, width)
    mask  = (coh < coh_min) | ~np.isfinite(phase)        # id(x,y)
    valid = ~mask & np.isfinite(phase)
    if not mask.any() or valid.sum() < 4:
        if log_fn:
            log_fn((f'[nan] 無需/無法 gap-fill (mask={int(mask.sum())}, '
                   f'valid={int(valid.sum())})\n' if LANG == 'zh' else f'[nan] no gap-fill needed/possible (mask={int(mask.sum())}, valid={int(valid.sum())})\n'))
        return mask, (dt == ">f4")
    try:
        from scipy.interpolate import griddata
    except ImportError:
        if log_fn:
            log_fn(('[nan] 無 scipy，跳過 gap-fill (snaphu 用原始相位)\n' if LANG == 'zh' else '[nan] no scipy, skipping gap-fill (snaphu uses raw phase)\n'))
        return mask, (dt == ">f4")
    yy, xx = np.indices((h, width))
    pts  = np.column_stack([yy[valid], xx[valid]])
    qpts = np.column_stack([yy[mask],  xx[mask]])
    cos_v, sin_v = np.cos(phase[valid]), np.sin(phase[valid])
    cos_i = griddata(pts, cos_v, qpts, method='linear')
    sin_i = griddata(pts, sin_v, qpts, method='linear')
    out = np.isnan(cos_i) | np.isnan(sin_i)              # 凸包外
    if out.any():
        cos_i[out] = griddata(pts, cos_v, qpts[out], method='nearest')
        sin_i[out] = griddata(pts, sin_v, qpts[out], method='nearest')
    phase[mask] = np.arctan2(sin_i, cos_i)              # ∈ (-π, π]
    phase.astype(dt).tofile(phase_img)
    if log_fn:
        log_fn((f'[nan] gap-fill 完成: {int(mask.sum())} 像素 (coh<{coh_min})\n' if LANG == 'zh' else f'[nan] gap-fill complete: {int(mask.sum())} pixels (coh<{coh_min})\n'))
    return mask, (dt == ">f4")


def mask_low_coh_to_nan(unw_img: str, coh_img: str, coh_min: float,
                        log_fn=None) -> bool:
    """unwrap 後：把解纏相位中 coh<coh_min 的像素設為 NaN (MintPy no-data)。

    在地理編碼後的格網上操作 (unw 與 coh 同格)，把低同調像素標成 NaN——
    MintPy 反演會逐像素跳過 NaN，不影響最小二乘。就地覆寫 unw_img。
    """
    import numpy as np
    be = _hdr_is_big_endian(unw_img)
    dt = '>f4' if be else '<f4'
    unw = np.fromfile(unw_img, dtype=dt)
    coh = np.fromfile(coh_img, dtype=dt)
    if unw.size != coh.size:
        if log_fn:
            log_fn(('[nan] unw/coh 尺寸不符，跳過遮罩\n' if LANG == 'zh' else '[nan] unw/coh size mismatch, skipping mask\n'))
        return False
    m = coh < coh_min
    unw[m] = np.nan
    unw.astype(dt).tofile(unw_img)
    if log_fn:
        log_fn((f'[nan] 解纏遮罩: {int(m.sum())} 像素設為 NaN (coh<{coh_min})\n' if LANG == 'zh' else f'[nan] unwrap mask: {int(m.sum())} pixels set to NaN (coh<{coh_min})\n'))
    return True


def _read_phase_coh_auto_endian(phase_img: str, coh_img: str, width: int):
    """讀 snaphu export 的 wrapped phase + coh，用 coh 值域(~0..1)自動判位元序。

    snaphu export .img 實際 little-endian 但 .hdr 常謊報 big；不信 hdr。
    回 (dt, phase[h,w] float64, coh[h,w] float32)；尺寸不符回 (dt, None, None)。
    """
    import numpy as np

    def _valid_coh(a):
        fin = a[np.isfinite(a)]
        return bool(fin.size) and fin.min() >= -0.01 and fin.max() <= 1.5

    coh_le = np.fromfile(coh_img, dtype='<f4')
    coh_be = np.fromfile(coh_img, dtype='>f4')
    if _valid_coh(coh_le):
        dt, coh = '<f4', coh_le
    elif _valid_coh(coh_be):
        dt, coh = '>f4', coh_be
    else:
        dt, coh = '<f4', coh_le
    phase = np.fromfile(phase_img, dtype=dt)
    if width <= 0 or phase.size % width or phase.size != coh.size:
        return dt, None, None
    h = phase.size // width
    return dt, phase.reshape(h, width).astype(np.float64), coh.reshape(h, width)


def fill_phase_by_mode(phase, coh, coh_min: float, mode: str = 'linear',
                       log_fn=None):
    """回 (filled_phase float64 [h,w], mask bool [h,w])，供 snaphu 解纏。

    相位是環狀量 → 對 cos/sin 內插再 atan2，結果天生落在 (-π, π]。
    最終一律對「整張」做 atan2(sin,cos) re-wrap，保證所有像素都在 (-π, π]
    (含 valid 像素，避免 export 原始相位偶有越界值漏網)。
    mode:
      'none'   不內插，低 coh 保留原值 (snaphu 自行處理低同調)。
      'linear' coh<coh_min 像素用 cos/sin 線性內插，凸包外最近鄰補。
      'smooth' 在 linear 之上，對 cos/sin 全圖高斯平滑(σ=1)再 atan2 → 較平滑。
    """
    import numpy as np
    h, w = phase.shape
    mask = (coh < coh_min) | ~np.isfinite(phase)
    out = phase.copy().astype(np.float64)

    def _ret(o):
        # 最終保證：整張 wrap phase 都落在 (-π, π] (NaN→0 後對全圖 atan2)。
        o = np.where(np.isfinite(o), o, 0.0)
        return np.arctan2(np.sin(o), np.cos(o)), mask

    if mode == 'none' or not mask.any():
        return _ret(out)
    valid = ~mask & np.isfinite(phase)
    if valid.sum() < 4:
        return _ret(out)
    try:
        from scipy.interpolate import griddata
    except ImportError:
        if log_fn:
            log_fn(('[nan] 無 scipy，mode 退回 none\n' if LANG == 'zh' else '[nan] no scipy, mode falls back to none\n'))
        return _ret(out)
    yy, xx = np.indices((h, w))
    pts  = np.column_stack([yy[valid], xx[valid]])
    qpts = np.column_stack([yy[mask],  xx[mask]])
    cos_v, sin_v = np.cos(phase[valid]), np.sin(phase[valid])
    cos_i = griddata(pts, cos_v, qpts, method='linear')
    sin_i = griddata(pts, sin_v, qpts, method='linear')
    hull_out = np.isnan(cos_i) | np.isnan(sin_i)
    if hull_out.any():
        cos_i[hull_out] = griddata(pts, cos_v, qpts[hull_out], method='nearest')
        sin_i[hull_out] = griddata(pts, sin_v, qpts[hull_out], method='nearest')
    out[mask] = np.arctan2(sin_i, cos_i)
    if mode == 'smooth':
        try:
            from scipy.ndimage import gaussian_filter
            c = gaussian_filter(np.cos(out), sigma=1.0)
            s = gaussian_filter(np.sin(out), sigma=1.0)
            out = np.arctan2(s, c)
        except ImportError:
            if log_fn:
                log_fn(('[nan] 無 scipy.ndimage，smooth 退回 linear\n' if LANG == 'zh' else '[nan] no scipy.ndimage, smooth falls back to linear\n'))
    return _ret(out)


def save_preunwrap_qc(phase_orig, phase_used, coh, out_png: str,
                      title: str = '', mode: str = ''):
    """低 DPI 三面板 QC：原始 wrap | 送 snaphu 的 wrap | coh → out_png。

    讓使用者開圖檢查內插品質：若中間面板有破洞/不連續，後面 unwrap 易失敗。
    """
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    def _wrap(a):
        return np.arctan2(np.sin(a), np.cos(a))   # 顯示用包裹到 -π..π

    panels = [
        (('原始 wrap phase' if LANG == 'zh' else 'Original wrap phase'),        _wrap(phase_orig), 'twilight', (-np.pi, np.pi)),
        ((f'送 snaphu wrap ({mode})' if LANG == 'zh' else f'wrap sent to snaphu ({mode})'), _wrap(phase_used), 'twilight', (-np.pi, np.pi)),
        ('coherence',              coh,               'viridis',  (0, 1)),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
    for ax, (t, arr, cmap, clim) in zip(axes, panels):
        im = ax.imshow(arr, cmap=cmap, vmin=clim[0], vmax=clim[1],
                       interpolation='nearest', origin='upper')
        ax.set_title(t, fontsize=8)
        ax.set_xticks([]); ax.set_yticks([])
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
    if title:
        fig.suptitle(title, fontsize=9)
    fig.tight_layout()
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=60, bbox_inches='tight')   # 低 DPI 縮圖
    plt.close(fig)


def _hdr_dims(hdr_path: 'Path'):
    """從 ENVI .hdr 讀 (samples, lines)。失敗回 (None, None)。"""
    import re
    from pathlib import Path
    try:
        t = Path(hdr_path).read_text()
        s = re.search(r'samples\s*=\s*(\d+)', t, re.I)
        l = re.search(r'lines\s*=\s*(\d+)', t, re.I)
        return (int(s.group(1)), int(l.group(1))) if s and l else (None, None)
    except Exception:
        return None, None


def _copy_band_hdr(src_hdr: 'Path', dst_hdr: 'Path', dst_band_name: str):
    """複製 ENVI .hdr 並把 'band names = {...}' 改成新波段名 (保留 map info)。"""
    import re
    from pathlib import Path
    txt = Path(src_hdr).read_text()
    if re.search(r'band names\s*=\s*\{[^}]*\}', txt, re.I):
        txt = re.sub(r'band names\s*=\s*\{[^}]*\}',
                     f'band names = {{ {dst_band_name} }}', txt, flags=re.I)
    else:
        txt = txt.rstrip() + f'\nband names = {{ {dst_band_name} }}\n'
    Path(dst_hdr).write_text(txt)


def make_single_band_product(src_dim: str, dst_dim: str, src_band_prefix: str,
                             dst_band_name: str, data_array=None,
                             log_fn=None) -> bool:
    """從多波段 BEAM-DIMAP 抽單一波段，產生符合 MintPy 慣例的單波段產物。

    保留地理編碼 (.dim 的 Geoposition/CRS 與 .hdr 的 map info 原樣複製)。
    ``data_array`` 為 None 時直接複製原波段 .img；否則寫入給定陣列 (例如由
    i/q 計算出的 Phase)。

    Args:
        src_dim:         來源多波段 .dim (如 wrapped_tc.dim)。
        dst_dim:         目標單波段 .dim (如 {pair}_coh_tc.dim)。
        src_band_prefix: 來源波段名前綴 ('coh_' / 'i_ifg' / 'elevation' / 'Unw')。
        dst_band_name:   目標波段與檔名 ('coh_VV' / 'Phase_ifg_VV' / 'dem' / ...)。
        data_array:      選用 numpy 陣列 (H×W float)，覆寫輸出 .img 內容。
    Returns:
        bool: True 表示成功。
    """
    import xml.etree.ElementTree as ET
    import shutil
    from pathlib import Path
    src_dim, dst_dim = Path(src_dim), Path(dst_dim)
    src_data, dst_data = src_dim.with_suffix('.data'), dst_dim.with_suffix('.data')
    try:
        tree = ET.parse(str(src_dim))
        root = tree.getroot()
    except Exception as exc:
        if log_fn:
            log_fn((f'[mintpy] 解析 {src_dim.name} 失敗: {exc}\n' if LANG == 'zh' else f'[mintpy] failed to parse {src_dim.name}: {exc}\n'))
        return False

    ii = root.find('.//Image_Interpretation')
    target = None
    for b in (ii.findall('Spectral_Band_Info') if ii is not None else []):
        if (b.findtext('BAND_NAME') or '').startswith(src_band_prefix):
            target = b
            break
    if target is None:
        if log_fn:
            log_fn((f'[mintpy] 找不到波段 {src_band_prefix} in {src_dim.name}\n' if LANG == 'zh' else f'[mintpy] band {src_band_prefix} not found in {src_dim.name}\n'))
        return False

    src_band = target.findtext('BAND_NAME')
    src_img = src_data / f'{src_band}.img'
    src_hdr = src_data / f'{src_band}.hdr'
    if not src_img.exists():
        cand = list(src_data.glob(f'{src_band}*.img'))
        if cand:
            src_img, src_hdr = cand[0], cand[0].with_suffix('.hdr')

    # 1) Image_Interpretation 只留目標波段，改名、index=0
    for b in list(ii.findall('Spectral_Band_Info')):
        if b is not target:
            ii.remove(b)
    if target.find('BAND_INDEX') is not None:
        target.find('BAND_INDEX').text = '0'
    target.find('BAND_NAME').text = dst_band_name

    # 2) Data_Access 只留一個 Data_File，href 指向新 .hdr
    da = root.find('.//Data_Access')
    if da is not None:
        dfs = da.findall('Data_File')
        for df in dfs[1:]:
            da.remove(df)
        if dfs:
            dfp = dfs[0].find('DATA_FILE_PATH')
            if dfp is not None:
                dfp.set('href', f'{dst_data.name}/{dst_band_name}.hdr')
            bi = dfs[0].find('BAND_INDEX')
            if bi is not None:
                bi.text = '0'

    # 3) 寫出 .data (.img + .hdr) 與 .dim
    dst_data.mkdir(parents=True, exist_ok=True)
    be = _hdr_is_big_endian(src_img)
    if data_array is None:
        shutil.copy2(str(src_img), str(dst_data / f'{dst_band_name}.img'))
    else:
        data_array.astype('>f4' if be else '<f4').tofile(
            str(dst_data / f'{dst_band_name}.img'))
    _copy_band_hdr(src_hdr, dst_data / f'{dst_band_name}.hdr', dst_band_name)
    tree.write(str(dst_dim), encoding='UTF-8', xml_declaration=True)
    if log_fn:
        log_fn(f'[mintpy] {dst_dim.name} ← {dst_band_name}\n')
    return True


def normalize_to_mintpy(wrapped_tc_dim: str, unw_tc_dim, pair_name: str,
                        coh_min: float, make_dem: bool = False,
                        project_dir=None, log_fn=None) -> bool:
    """從 wrapped_tc(+unw_tc) 產生 MintPy 官方慣例的單波段 _tc 產物。

    產出於 interferograms/{pair}/ 內:
      {pair}_coh_tc.dim/.data   ← coh 波段 (band 'coh_VV')
      {pair}_filt_tc.dim/.data  ← 包裹相位 Phase_ifg_VV (= atan2(q_ifg, i_ifg))
      {pair}_unw_tc.dim/.data   ← Unw_Phase_ifg_VV (若 unw_tc 存在)；並把
                                   coh<coh_min 的像素設為 NaN (Q3 步驟5)
    若 make_dem，從本對 wrapped_tc 的 elevation 產生 dem_tc.data/dem.img。

    Returns:
        bool: 全部成功才 True。
    """
    import numpy as np
    from pathlib import Path
    wdim = Path(wrapped_tc_dim)
    wdata = wdim.with_suffix('.data')
    outdir = wdim.parent
    ok = True

    # coh_tc
    ok &= make_single_band_product(
        str(wdim), str(outdir / f'{pair_name}_coh_tc.dim'),
        'coh_', 'coh_VV', log_fn=log_fn)

    # filt_tc：Phase = atan2(q, i)
    ib = list(wdata.glob('i_ifg*.img'))
    qb = list(wdata.glob('q_ifg*.img'))
    if ib and qb:
        be = _hdr_is_big_endian(ib[0])
        dt = '>f4' if be else '<f4'
        W, H = _hdr_dims(ib[0].with_suffix('.hdr'))
        if W and H:
            i_a = np.fromfile(str(ib[0]), dtype=dt).reshape(H, W).astype(np.float64)
            q_a = np.fromfile(str(qb[0]), dtype=dt).reshape(H, W).astype(np.float64)
            phase = np.arctan2(q_a, i_a).astype(np.float32)
            ok &= make_single_band_product(
                str(wdim), str(outdir / f'{pair_name}_filt_tc.dim'),
                'i_ifg', 'Phase_ifg_VV', data_array=phase, log_fn=log_fn)
        else:
            ok = False
            if log_fn:
                log_fn(('[mintpy] 讀不到 i_ifg 尺寸，filt_tc 失敗\n' if LANG == 'zh' else '[mintpy] cannot read i_ifg size, filt_tc failed\n'))
    else:
        ok = False
        if log_fn:
            log_fn(('[mintpy] 找不到 i_ifg/q_ifg，無法產生 filt_tc\n' if LANG == 'zh' else '[mintpy] i_ifg/q_ifg not found, cannot generate filt_tc\n'))

    # unw_tc：抽 Unw_Phase 波段，並用地理編碼後 coh 把低同調設 NaN
    if unw_tc_dim and Path(unw_tc_dim).exists():
        unw_out = outdir / f'{pair_name}_unw_tc.dim'
        if make_single_band_product(
                str(unw_tc_dim), str(unw_out),
                'Unw', 'Unw_Phase_ifg_VV', log_fn=log_fn):
            coh_imgs = list((outdir / f'{pair_name}_coh_tc.data').glob('coh*.img'))
            unw_imgs = list(unw_out.with_suffix('.data').glob('Unw*.img'))
            if coh_imgs and unw_imgs:
                mask_low_coh_to_nan(str(unw_imgs[0]), str(coh_imgs[0]),
                                    coh_min, log_fn=log_fn)
        else:
            ok = False

    # dem_tc.data/dem.img (僅首對)
    if make_dem and project_dir:
        dem_dim = Path(project_dir) / 'dem_tc.dim'
        ok &= make_single_band_product(
            str(wdim), str(dem_dim), 'elevation', 'dem', log_fn=log_fn)

    return ok


# ─────────────────────────────────────────────────────────────────────────
# Per-pair SNAP worker
# ─────────────────────────────────────────────────────────────────────────
class SnapPairWorker(threading.Thread):
    """Runs snap2gpt for one (ref, sec) pair: split both dates, then coreg+ifg."""

    def __init__(self, ref: str, sec: str, state: AppState,
                 on_event: Callable[[str, dict], None],
                 stop_event: threading.Event,
                 force: bool = False, make_dem: bool = True):
        super().__init__(daemon=True)
        self.ref = ref
        self.sec = sec
        self.st = state
        self.on_event = on_event
        self._cancel = stop_event
        self._proc: Optional[subprocess.Popen] = None
        # force=True: 忽略既有完整輸出，每個 step 都重跑 (使用者選「重跑全部」)。
        # 只繞過「輸出已存在就 skip」的檢查，不影響「輸入不完整就警告」的檢查。
        self.force = force
        # make_dem: 是否由本 worker 產生專案級 dem_tc (MintPy 幾何用)。叢集模式下
        # 只指派給「第一台被勾選的機器」, 其餘 False → 避免多台同時寫 dem_tc 競態。
        self.make_dem = make_dem

    def _log(self, text: str):
        self.on_event('log', {'text': text})

    def _emit(self, kind: str, **kw):
        self.on_event(kind, {'ref': self.ref, 'sec': self.sec, **kw})

    def _run_gpt(self, xml_path: str, log_path: str,
                 cache: Optional[str] = None, cpu: Optional[str] = None) -> int:
        """跑 SNAP gpt graph, 內建卡死看門狗 + 暫時性故障自動重試。

        cache/cpu: 覆寫該次 gpt 的 -c tileCache / -q 執行緒數 (None=用全域 st 值)。
                   記憶體炸彈步驟 (如 filter_ml) 用此把 peak RAM 壓到機器能撐的範圍,
                   避免 OS OOM-killer 砍掉整個 worker。

        回傳值:
          0       成功
          -2      使用者取消 (cancel) — 不重試, 維持原行為
          其它!=0  失敗 (用盡重試次數仍卡死/崩潰, 或為非暫時性的 graph 錯誤)

        卡死(看門狗 kill) 與 JVM SIGSEGV 崩潰 視為「暫時性記憶體壓力」→ 重試最多
        _GPT_MAX_ATTEMPTS 次; 真正的 graph 邏輯錯誤 (非崩潰/卡死的 rc!=0) 不重試。
        """
        st = self.st
        _cache = cache if cache is not None else st.cache
        _cpu = cpu if cpu is not None else st.cpu
        args = [gpt_exe(st.snap_dir), xml_path,
                '-c', _cache, '-q', _cpu]
        java_opts = _GPT_JAVA_OPTS
        _xmx = getattr(st, 'xmx', '').strip()
        if _xmx:
            java_opts = f'-Xmx{_xmx} ' + java_opts
        env = dict(os.environ, LD_LIBRARY_PATH='.',
                   JAVA_TOOL_OPTIONS=java_opts)

        rc = -1
        for attempt in range(1, _GPT_MAX_ATTEMPTS + 1):
            if self._cancel.is_set():
                return -2
            if attempt > 1:
                self._log((f'  ↻ gpt 重試 {attempt}/{_GPT_MAX_ATTEMPTS}\n' if LANG == 'zh' else f'  ↻ gpt retry {attempt}/{_GPT_MAX_ATTEMPTS}\n'))
            self._log(f'  $ {" ".join(args)}\n')
            rc, crashed, stalled = self._run_gpt_once(args, env, log_path)
            if rc == -2:                       # 取消
                return -2
            if rc == 0:                        # 成功
                return 0
            if not (crashed or stalled):       # 非暫時性錯誤 → 不重試
                return rc
            reason = ('卡死(看門狗 kill)' if LANG == 'zh' else 'stalled (watchdog kill)') if stalled else ('JVM 崩潰(SIGSEGV)' if LANG == 'zh' else 'JVM crash (SIGSEGV)')
            self._log((f'  ✗ gpt {reason} rc={rc} '
                      f'(嘗試 {attempt}/{_GPT_MAX_ATTEMPTS})\n' if LANG == 'zh' else f'  ✗ gpt {reason} rc={rc} (attempt {attempt}/{_GPT_MAX_ATTEMPTS})\n'))
        return rc                              # 用盡重試仍失敗

    def _run_gpt_once(self, args, env, log_path) -> Tuple[int, bool, bool]:
        """跑一次 gpt。回傳 (returncode, crashed, stalled)。

        crashed: 輸出含 JVM fatal-error 標記 (SIGSEGV)。
        stalled: 看門狗因連續 _GPT_STALL_TIMEOUT 秒無輸出而 kill。
        取消時回 (-2, False, False)。
        """
        last_out = [time.monotonic()]   # list 以便看門狗執行緒可改寫
        stalled = {'v': False}
        crashed = False
        with open(log_path, 'a') as lf:
            # start_new_session=True: gpt 自成新 process group (proc.pid 即 pgid)。
            # 卡死時必須殺「整個 group」, 否則 gpt launcher 衍生的 JVM 子程序會繼承
            # stdout 管線 → 即使殺了 launcher, 管線不關, 讀取迴圈仍永久阻塞 →
            # 看門狗形同虛設。殺 group 才能連 JVM 一起收掉, 讓管線關閉、迴圈結束。
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, text=True, env=env, start_new_session=True)
            self._proc = proc

            def _killpg(sig):
                try:
                    os.killpg(proc.pid, sig)
                except (OSError, ProcessLookupError):
                    pass

            def _watchdog():
                # start_new_session=True → proc.pid 即為 process group id。
                # 以『程序群組 CPU 是否仍在增長』作為活性依據, 避免誤殺正在靜默運算
                # 的 filter_ml (SNAP 進度點被緩衝, 看門狗看不到輸出但 gpt 在燒 CPU)。
                last_cpu = _proc_group_cpu_ticks(proc.pid)
                while proc.poll() is None:
                    if self._cancel.is_set():
                        return
                    cur_cpu = _proc_group_cpu_ticks(proc.pid)
                    if cur_cpu > last_cpu:        # 仍在燒 CPU = 活著 → 重置卡死計時
                        last_cpu = cur_cpu
                        last_out[0] = time.monotonic()
                    if time.monotonic() - last_out[0] > _GPT_STALL_TIMEOUT:
                        stalled['v'] = True
                        _killpg(signal.SIGKILL)    # 真卡死(無輸出且無CPU) → 連 JVM 強殺
                        return
                    time.sleep(min(30, _GPT_STALL_TIMEOUT))

            wd = threading.Thread(target=_watchdog, daemon=True)
            wd.start()
            for line in proc.stdout:   # type: ignore[union-attr]
                last_out[0] = time.monotonic()
                if self._cancel.is_set():
                    _killpg(signal.SIGTERM)
                    return -2, False, False
                if ('A fatal error has been detected' in line
                        or 'SIGSEGV' in line):
                    crashed = True
                self._log(line)
                lf.write(line)
            rc = proc.wait()
        return rc, crashed, stalled['v']

    @staticmethod
    def _sync_product_metadata(dim_paths: List[Path]) -> None:
        """Force all given BEAM-DIMAP products to share the same PRODUCT metadata.

        `TOPSARMergeOp.checkSourceProductValidity()` (SNAP) rejects merging IW
        products whose Abstracted_Metadata `PRODUCT` attribute differs. This
        happens when, for the same date, one IW is split via SliceAssembly
        (PRODUCT inherited from frame1) while a sibling IW only needs a single
        frame and is split from a *different* frame alone (PRODUCT from that
        frame) — even though both represent the same acquisition date and
        `merge_iw` should be allowed to combine them. Rewriting every IW's
        PRODUCT to match the first one is safe: it is a free-text metadata
        label used only for this cross-check, not for any geometric or
        radiometric computation downstream.
        """
        pat = re.compile(r'(<MDATTR name="PRODUCT"[^>]*>)([^<]*)(</MDATTR>)')
        texts: Dict[Path, str] = {}
        canonical: Optional[str] = None
        for p in dim_paths:
            try:
                texts[p] = p.read_text()
            except Exception:
                continue
            if canonical is None:
                m = pat.search(texts[p])
                if m:
                    canonical = m.group(2)
        if canonical is None:
            return
        for p, text in texts.items():
            m = pat.search(text)
            if m and m.group(2) != canonical:
                p.write_text(text[:m.start(2)] + canonical + text[m.end(2):])

    def _split_date(self, date: str, split_dir: Path,
                    graph_tmp: Path, log_dir: Path,
                    iw_list: Optional[List[str]] = None) -> bool:
        """Split + ApplyOrbit for one date.  Handles single-frame and
        cross-frame (SliceAssembly) cases automatically.  Returns True on
        success."""
        st = self.st
        iw_list = iw_list or list(st.iw_list) or list(ALL_IW)
        lon_min, lat_min, lon_max, lat_max = st.aoi_bbox()

        # Detect cross-frame: find all SAFE/zip files that overlap AOI lat range.
        try:
            covering = find_slcs_covering_lat_range(
                st.slc_dir, date, float(lat_min), float(lat_max))
        except (TypeError, ValueError):
            covering = []

        graphs = Path(_GRAPHS_DIR)
        wkt = st.wkt_polygon()

        # find_slcs_covering 用「所有 IW 聯集緯度」判定,可能誤把『目標 IW 不覆蓋
        # AOI』的 frame 也算進來 (如某 frame 的 IW3 伸進 AOI 但 IW1 沒有)。只處理
        # 單一 IW 時,優先找『目標 IW 就完整涵蓋 AOI 的單一 frame』,用單 frame、
        # 跳過多餘的 SliceAssembly (省記憶體 + 加速)。
        if len(covering) >= 2:
            _tiw = (iw_list[0] if iw_list else 'IW1').lower()
            _single = []
            for _f in covering:
                _r = _safe_iw_lat_range(_f, _tiw, float(lon_min), float(lon_max))
                if _r and _r[0] <= float(lat_min) and _r[1] >= float(lat_max):
                    _single.append(_f)
            if _single:
                covering = [_single[0]]
                self._log((f'[split] {date}: {_tiw} 單 frame 即涵蓋 AOI,'
                          f' 跳過 SliceAssembly → {covering[0].name}\n' if LANG == 'zh' else f'[split] {date}: {_tiw} single frame already covers AOI, skipping SliceAssembly → {covering[0].name}\n'))

        if len(covering) >= 2:
            # At least one IW (iw_list[0], checked above) needs both frames.
            # Other IWs in iw_list can have DIFFERENT per-frame coverage
            # (each IW sub-swath spans a different azimuth/latitude range
            # within the same frame) — e.g. frame1's IW2 may not reach the
            # AOI at all even though frame1's IW1 does. Re-check per IW so we
            # don't force SliceAssembly (and feed TOPSAR-Split a frame with
            # zero relevant bursts for that IW) when a single frame already
            # covers this specific IW.
            slcs = [str(p) for p in covering]
            assemble_tpl_raw = (
                graphs / 'topsar_secondaries_assemble_split_applyorbit.xml'
            ).read_text()
            assemble_tpl = build_assemble_tpl(assemble_tpl_raw, len(slcs))
            split_graph_tpl = (graphs / 'topsar_master_split_applyorbit.xml').read_text()
            for iw in iw_list:
                out_dim = split_dir / f'{date}_{iw}.dim'
                if not self.force and dimap_product_complete(out_dim):
                    self._log(f'[skip] split {date} {iw} (exists)\n')
                    continue
                if out_dim.exists():
                    self._log(f'[warn] split {date} {iw} incomplete, reprocessing\n')
                if self._cancel.is_set():
                    return False

                _tiw = iw.lower()
                _single_for_iw = next(
                    (_f for _f in covering
                     if (_r := _safe_iw_lat_range(_f, _tiw, float(lon_min), float(lon_max)))
                     and _r[0] <= float(lat_min) and _r[1] >= float(lat_max)),
                    None)

                if _single_for_iw is not None:
                    self._log((f'[split] {date} {iw}: 單 frame 即涵蓋 AOI,'
                              f' 跳過 SliceAssembly → {_single_for_iw.name}\n' if LANG == 'zh' else f'[split] {date} {iw}: single frame already covers AOI, skipping SliceAssembly → {_single_for_iw.name}\n'))
                    _slc_path = (str(_single_for_iw / 'manifest.safe')
                                 if _single_for_iw.suffix == '.SAFE' else str(_single_for_iw))
                    xml = fill_graph(split_graph_tpl, {
                        'INPUTFILE':   _slc_path,
                        'IWs':         iw,
                        'POLARISATION': st.polarisation,
                        'POLYGON':     wkt,
                        'OUTPUTFILE':  str(out_dim),
                    })
                else:
                    frame_log = ''.join(
                        f'  frame{i}: {f.name}\n' for i, f in enumerate(covering, start=1))
                    self._log(f'[split-assemble] {date} {iw} ({len(slcs)} frames) ...\n'
                              f'{frame_log}')
                    # Build INPUTFILE{k} substitutions in descending numeric
                    # order so fill_graph's sequential str.replace() never
                    # lets a shorter placeholder (e.g. INPUTFILE1) clobber a
                    # longer one that contains it as a prefix (INPUTFILE10).
                    input_subs = {f'INPUTFILE{i}': slc
                                  for i, slc in reversed(list(enumerate(slcs, start=1)))}
                    xml = fill_graph(assemble_tpl, {
                        **input_subs,
                        'IWs':         iw,
                        'POLARISATION': st.polarisation,
                        'POLYGON':     wkt,
                        'OUTPUTFILE':  str(out_dim),
                    })

                tmp_xml = str(graph_tmp / f'split_{date}_{iw}.xml')
                Path(tmp_xml).write_text(xml)
                rc = self._run_gpt(tmp_xml, str(log_dir / f'split_{date}_{iw}.log'))
                if rc != 0:
                    self._log(f'[ERROR] split {date} {iw} failed (rc={rc})\n')
                    return False

            # Some IWs above may have come from a single frame while others
            # needed SliceAssembly — their PRODUCT metadata then differs,
            # which TOPSARMergeOp rejects at the mergeIW step even though
            # they're the same acquisition date. Normalize before returning.
            self._sync_product_metadata(
                [split_dir / f'{date}_{iw}.dim' for iw in iw_list])
            return True

        # Single-frame path. If `covering` already narrowed down to exactly the
        # one frame whose target IW covers the AOI (via the IW-aware check
        # above), use it directly — re-deriving through find_slc_for_date()
        # would re-run its aoi_lat check against ANY IW in the frame (not the
        # target IW), which can pick a different, wrong frame when one frame's
        # target IW misses the AOI but a different IW in that same frame
        # reaches it (e.g. IW3 extends further than IW1).
        if len(covering) == 1:
            _f = covering[0]
            slc_path = str(_f / 'manifest.safe') if _f.suffix == '.SAFE' else str(_f)
        else:
            try:
                aoi_lat = (float(lat_min) + float(lat_max)) / 2.0
            except (TypeError, ValueError):
                aoi_lat = None
            slc_path = find_slc_for_date(st.slc_dir, date, aoi_lat=aoi_lat)
        if slc_path is None:
            self._log(f'[ERROR] SLC not found for {date} in {st.slc_dir}\n')
            return False

        split_graph_tpl = (graphs / 'topsar_master_split_applyorbit.xml').read_text()
        for iw in iw_list:
            out_dim = split_dir / f'{date}_{iw}.dim'
            if not self.force and dimap_product_complete(out_dim):
                self._log(f'[skip] split {date} {iw} (exists)\n')
                continue
            if out_dim.exists():
                self._log(f'[warn] split {date} {iw} incomplete output, reprocessing\n')
            if self._cancel.is_set():
                return False
            self._log(f'[split] {date} {iw} ...\n')
            xml = fill_graph(split_graph_tpl, {
                'INPUTFILE': slc_path,
                'IWs': iw,
                'POLARISATION': st.polarisation,
                'POLYGON': wkt,
                'OUTPUTFILE': str(out_dim),
            })
            tmp_xml = str(graph_tmp / f'split_{date}_{iw}.xml')
            Path(tmp_xml).write_text(xml)
            rc = self._run_gpt(tmp_xml, str(log_dir / f'split_{date}_{iw}.log'))
            if rc != 0:
                self._log(f'[ERROR] split {date} {iw} failed (rc={rc})\n')
                return False
        return True

    def run(self):  # noqa: C901  (complexity accepted — linear pipeline)
        import shutil
        st = self.st
        self._emit('pair_start')
        t0 = time.time()

        # Auto-enable SSD swap before heavy GPT processing (OOM guard).
        # Only acts on the first pair that runs on this host; subsequent
        # pairs skip silently because swap is already active.
        if getattr(st, 'ssd_swap_auto', False):
            _sp = getattr(st, 'ssd_swap_path', '').strip()
            if _sp:
                _img = _swap_img_path(_sp)
                ok, msg = _enable_swap(_img, _SUDO_PASS)
                self._log(f'[swap] {msg}\n')

        try:
            project    = Path(st.project_dir)
            split_dir  = project / 'split'
            ifg_ml_dir = project / 'ifg_ml'
            snaphu_base = project / 'snaphu'
            # MintPy SNAP convention: interferograms/{pair}/ for per-pair TC outputs
            tc_base    = project / 'interferograms'
            geo_dir    = project / 'geometry'   # GeoTIFFs + diagnostic PNGs
            graph_tmp  = project / 'graphs'
            log_dir    = project / 'logs'
            for d in (split_dir, ifg_ml_dir, snaphu_base, tc_base,
                      geo_dir, graph_tmp, log_dir):
                d.mkdir(parents=True, exist_ok=True)

            pair_name = f'{self.ref}_{self.sec}'
            graphs    = Path(_GRAPHS_DIR)
            wkt       = st.wkt_polygon()

            if getattr(st, 'iw_mode', 'auto') == 'manual' and st.iw_list:
                # 手動模式: 完全照使用者勾選的 iw_list 跑, 略過自動偵測覆蓋。
                # (用於某對某子帶退化 — 如 IW2 ESD 重疊權重 0 → 干涉圖全零 →
                #  merge 失敗 — 想強制只跑 IW1 的情況。)
                iw_list = list(st.iw_list)
                self._log((f'[IW] 手動指定 {", ".join(iw_list)} (略過自動偵測)\n' if LANG == 'zh' else f'[IW] manually specified {", ".join(iw_list)} (skipping auto-detection)\n'))
            else:
                # 自動模式: 偵測 AOI 實際覆蓋的子帶, 避免用 prefs 裡 stale 的 iw_list
                # (e.g. IW2 saved when AOI is in IW1)。
                _aoi = st.aoi_bbox()
                _iws = detect_iws_from_slc(st.slc_dir, *_aoi)
                if _iws:
                    # 尊重 config iw_list:取「偵測 ∩ 設定」交集,避免 detect 把 AOI
                    # 邊界附近的多餘 IW(如 IW2)也算進來、無視使用者明確設的 IW1。
                    # 交集空(config 真的 stale,如 AOI 在 IW1 卻存了 IW2)才退回偵測值。
                    if st.iw_list:
                        _inter = [iw for iw in st.iw_list if iw in _iws]
                        iw_list = _inter if _inter else list(_iws)
                    else:
                        iw_list = list(_iws)
                    if iw_list != list(st.iw_list):
                        self._log(
                            (f'[IW] 使用 {", ".join(iw_list)}'
                            f'  (設定 {", ".join(st.iw_list) or "無"} / 偵測 {", ".join(_iws)})\n' if LANG == 'zh' else f'[IW] using {", ".join(iw_list)}  (configured {", ".join(st.iw_list) or "none"} / detected {", ".join(_iws)})\n'))
                else:
                    iw_list = list(st.iw_list) or list(ALL_IW)

            # Compute TC pixel spacing from ML + SmartML:
            #   S1 IW single-look: ~2.33m range × ~13.97m azimuth
            #   After Multilook(rg×az): ml_rg ≈ rg_looks×2.33, ml_az ≈ az_looks×13.97
            #   After SmartML(n×n): effective grid = ml × n  (n×n neighbourhood)
            #   TC output at this effective resolution (not the over-sampled ML spacing)
            _S1_RG = 2.3296    # S1 IW single-look range pixel spacing [m]
            _S1_AZ = 13.9374   # S1 IW single-look azimuth pixel spacing [m]
            ml_rg = st.rg_looks * _S1_RG
            ml_az = st.az_looks * _S1_AZ
            tc_px_rg = ml_rg * st.smart_ml_n
            tc_px_az = ml_az * st.smart_ml_n
            # use the larger of range / azimuth to keep square pixels
            tc_pixel_spacing = f'{max(tc_px_rg, tc_px_az):.2f}'
            self._log(f'[TC] pixel spacing: ML({ml_rg:.1f}m × {ml_az:.1f}m) '
                      f'× SmartML(n={st.smart_ml_n}) = {tc_pixel_spacing}m\n')

            # 是否由本對產生專案級 DEM (dem_tc, MintPy 幾何用):
            #   只有被指派為 DEM 機器(make_dem) 且 dem_tc.dim 尚未存在時才產。
            #   → 確定性指派(避免多台競態), 且 dem 缺漏時下一對會補產(可重生)。
            is_first_pair = (self.make_dem
                             and not (project / 'dem_tc.dim').exists())

            # ── Step 1: Split both dates ──────────────────────────────────
            if not self._split_date(self.ref, split_dir, graph_tmp, log_dir, iw_list):
                raise RuntimeError(f'split failed for {self.ref}')
            if not self._split_date(self.sec, split_dir, graph_tmp, log_dir, iw_list):
                raise RuntimeError(f'split failed for {self.sec}')
            if self._cancel.is_set():
                raise RuntimeError('cancelled')

            # ── Step 2: IFG + Goldstein + Multilook ──────────────────────
            pair_ml_dir = ifg_ml_dir / pair_name
            pair_ml_dir.mkdir(exist_ok=True)
            multi_iw = len(iw_list) > 1

            def _ifg_graph_tpl(single_burst: bool):
                """選 ifg 圖：單 burst → noESD 變體 (ESD 需 ≥2 burst 重疊區)。"""
                name = ('snap2mintpy_ifg_goldstein_ml'
                        + ('_extDEM' if st.ext_dem else '')
                        + ('_noESD' if single_burst else '')
                        + '.xml')
                gp = graphs / name
                if not gp.exists():
                    raise RuntimeError(f'IFG graph not found: {gp}')
                return name, gp.read_text()

            def _ifg_deburst_graph_tpl(single_burst: bool):
                """Per-IW deburst only (for multi-IW merge path)."""
                name = ('snap2mintpy_ifg_deburst'
                        + ('_extDEM' if st.ext_dem else '')
                        + ('_noESD' if single_burst else '')
                        + '.xml')
                gp = graphs / name
                if not gp.exists():
                    raise RuntimeError(f'IFG deburst graph not found: {gp}')
                return name, gp.read_text()

            if multi_iw:
                # ── Step 2a: Per-IW BackGeocoding+ESD+IFG+Deburst ────────
                per_iw_ifg_dims: List[Path] = []
                for iw in iw_list:
                    ifg_dim = pair_ml_dir / f'{pair_name}_{iw}_ifg.dim'
                    if not self.force and dimap_product_complete(ifg_dim):
                        self._log(f'[skip] ifg_deburst {pair_name} {iw} (exists)\n')
                    else:
                        if ifg_dim.exists():
                            self._log(
                                f'[warn] ifg_deburst {pair_name} {iw} incomplete, reprocessing\n')
                        if self._cancel.is_set():
                            raise RuntimeError('cancelled')
                        ref_dim = split_dir / f'{self.ref}_{iw}.dim'
                        sec_dim = split_dir / f'{self.sec}_{iw}.dim'
                        if (not dimap_product_complete(ref_dim)
                                or not dimap_product_complete(sec_dim)):
                            self._log(
                                f'[warn] split dim missing/incomplete for {iw}, skipping\n')
                            continue
                        nb = split_burst_count(str(ref_dim))
                        single_burst = (nb == 1)
                        gname, tpl = _ifg_deburst_graph_tpl(single_burst)
                        self._log(f'[ifg_deburst] {pair_name} {iw} '
                                  f'(burst={nb}, {"noESD" if single_burst else "ESD"}) ...\n')
                        xml = fill_graph(tpl, {
                            'MASTER':      str(ref_dim),
                            'SECONDARY':   str(sec_dim),
                            'OUTPUTFILE':  str(ifg_dim),
                            'EXTERNALDEM': st.ext_dem or '',
                        })
                        tmp_xml = str(graph_tmp / f'ifg_deburst_{pair_name}_{iw}.xml')
                        Path(tmp_xml).write_text(xml)
                        rc = self._run_gpt(
                            tmp_xml,
                            str(log_dir / f'ifg_deburst_{pair_name}_{iw}.log'))
                        if rc != 0:
                            raise RuntimeError(
                                f'ifg_deburst failed for {pair_name} {iw} (rc={rc})')
                    if dimap_product_complete(ifg_dim):
                        per_iw_ifg_dims.append(ifg_dim)

                if len(per_iw_ifg_dims) < 2:
                    raise RuntimeError(
                        f'multi-IW merge requires ≥2 IW ifg products; '
                        f'got {len(per_iw_ifg_dims)}')

                # ── Step 2b: TOPSAR-Merge + TopoPhaseRemoval + Goldstein + ML
                merged_ml_dim = pair_ml_dir / f'{pair_name}_merged_ml.dim'
                if not self.force and dimap_product_complete(merged_ml_dim):
                    self._log(f'[skip] mergeIW {pair_name} (exists)\n')
                else:
                    merge_graph_name = ('snap2mintpy_mergeIW_filter_ml'
                                        + ('_extDEM' if st.ext_dem else '')
                                        + '.xml')
                    merge_gp = graphs / merge_graph_name
                    if not merge_gp.exists():
                        raise RuntimeError(f'Merge graph not found: {merge_gp}')
                    # ProductSet-Reader expects comma-separated absolute paths
                    ifg_files_str = ','.join(str(p) for p in per_iw_ifg_dims)
                    self._log(
                        f'[mergeIW] {pair_name} '
                        f'({len(per_iw_ifg_dims)} IWs: '
                        f'{", ".join(p.stem for p in per_iw_ifg_dims)}) ...\n')
                    xml = fill_graph(merge_gp.read_text(), {
                        'IFGFILES':    ifg_files_str,
                        'NRGLOOKS':    str(st.rg_looks),
                        'NAZLOOKS':    str(st.az_looks),
                        # 雷達座標裁切用加 buffer 的多邊形, 避免斜 swath 裁成矩形時
                        # 邊角缺資料 (TC 最後仍裁精確 AOI, 範圍不變)。
                        'POLYGON':     pad_wkt_bbox(wkt),
                        'OUTPUTFILE':  str(merged_ml_dim),
                        'EXTERNALDEM': st.ext_dem or '',
                    })
                    tmp_xml = str(graph_tmp / f'mergeIW_{pair_name}.xml')
                    Path(tmp_xml).write_text(xml)
                    # mergeIW_filter_ml 同樣含 Goldstein → 記憶體炸彈, 一併降載防 OOM-kill。
                    _fc = _cap_gb(st.cache, _FILTER_ML_CACHE_CEIL_GB)
                    _fq = _cap_int(st.cpu, _FILTER_ML_CPU_CEIL)
                    if _fc != st.cache or _fq != st.cpu:
                        self._log((f'  [mem] mergeIW 降載: -c {st.cache}->{_fc} '
                                  f'-q {st.cpu}->{_fq} (防 OOM-kill)\n' if LANG == 'zh' else f'  [mem] mergeIW downscale: -c {st.cache}->{_fc} -q {st.cpu}->{_fq} (OOM-kill guard)\n'))
                    rc = self._run_gpt(
                        tmp_xml, str(log_dir / f'mergeIW_{pair_name}.log'),
                        cache=_fc, cpu=_fq)
                    if rc != 0:
                        raise RuntimeError(
                            f'mergeIW failed for {pair_name} (rc={rc})')

                # Steps 3-5 run once on the merged product (iw tag = 'merged')
                effective_loop: List[str] = ['merged']
            else:
                effective_loop = list(iw_list)

            for iw in effective_loop:
                if multi_iw:
                    ml_dim = pair_ml_dir / f'{pair_name}_merged_ml.dim'
                    if not dimap_product_complete(ml_dim):
                        self._log(f'[warn] merged ml_dim missing, skip steps 3-5\n')
                        continue
                else:
                    ml_dim = pair_ml_dir / f'{pair_name}_{iw}_ml.dim'
                    if not self.force and dimap_product_complete(ml_dim):
                        self._log(f'[skip] ifg_ml {pair_name} {iw} (exists)\n')
                    else:
                        if ml_dim.exists():
                            self._log(
                                f'[warn] ifg_ml {pair_name} {iw} incomplete output, reprocessing\n')
                        if self._cancel.is_set():
                            raise RuntimeError('cancelled')
                        ref_dim = split_dir / f'{self.ref}_{iw}.dim'
                        sec_dim = split_dir / f'{self.sec}_{iw}.dim'
                        if (not dimap_product_complete(ref_dim)
                                or not dimap_product_complete(sec_dim)):
                            self._log(f'[warn] split dim missing/incomplete for {iw}, skipping\n')
                            continue
                        nb = split_burst_count(str(ref_dim))
                        single_burst = (nb == 1)
                        # ── Stage 1: coreg+ESD+IFG+Deburst → _ifg.dim (heavy;
                        #    written to disk, frees heap). Split from Stage 2 so
                        #    full-res Goldstein never co-resides → no heap bomb. ──
                        ifg_dim = pair_ml_dir / f'{pair_name}_{iw}_ifg.dim'
                        if self.force or not dimap_product_complete(ifg_dim):
                            if ifg_dim.exists():
                                self._log(
                                    f'[warn] ifg_deburst {pair_name} {iw} incomplete, reprocessing\n')
                            gname, dbg_tpl = _ifg_deburst_graph_tpl(single_burst)
                            self._log(f'[ifg_deburst] {pair_name} {iw} '
                                      f'(burst={nb}, {"noESD" if single_burst else "ESD"}) ...\n')
                            xml = fill_graph(dbg_tpl, {
                                'MASTER':      str(ref_dim),
                                'SECONDARY':   str(sec_dim),
                                'OUTPUTFILE':  str(ifg_dim),
                                'EXTERNALDEM': st.ext_dem or '',
                            })
                            tmp_xml = str(graph_tmp / f'ifg_deburst_{pair_name}_{iw}.xml')
                            Path(tmp_xml).write_text(xml)
                            rc = self._run_gpt(
                                tmp_xml,
                                str(log_dir / f'ifg_deburst_{pair_name}_{iw}.log'))
                            if rc != 0:
                                raise RuntimeError(
                                    f'ifg_deburst failed for {pair_name} {iw} (rc={rc})')
                        if not dimap_product_complete(ifg_dim):
                            self._log(f'[warn] ifg dim missing/incomplete for {iw}, skipping\n')
                            continue
                        # ── Stage 2: Multilook→TopoPhase→Goldstein→Subset → _ml.dim
                        #    (Goldstein runs on multilooked data → low memory). ──
                        fname = ('snap2mintpy_filter_ml'
                                 + ('_extDEM' if st.ext_dem else '') + '.xml')
                        fgp = graphs / fname
                        if not fgp.exists():
                            raise RuntimeError(f'Filter graph not found: {fgp}')
                        self._log(f'[filter_ml] {pair_name} {iw} '
                                  f'(ML {st.rg_looks}x{st.az_looks}->TopoPhase->Goldstein) ...\n')
                        xml = fill_graph(fgp.read_text(), {
                            'INPUTFILE':   str(ifg_dim),
                            'NRGLOOKS':    str(st.rg_looks),
                            'NAZLOOKS':    str(st.az_looks),
                            # 雷達座標裁切用加 buffer 的多邊形, 避免斜 swath 裁成矩形時
                            # 邊角缺資料 (TC 最後仍裁精確 AOI, 範圍不變)。
                            'POLYGON':     pad_wkt_bbox(wkt),
                            'OUTPUTFILE':  str(ml_dim),
                            'EXTERNALDEM': st.ext_dem or '',
                        })
                        tmp_xml = str(graph_tmp / f'filter_ml_{pair_name}_{iw}.xml')
                        Path(tmp_xml).write_text(xml)
                        # 記憶體炸彈步驟: 壓低 tileCache + 執行緒, 避免 OS OOM-killer
                        # 砍 worker (硬骨頭對如 20260116-20260128 的根因)。
                        _fc = _cap_gb(st.cache, _FILTER_ML_CACHE_CEIL_GB)
                        _fq = _cap_int(st.cpu, _FILTER_ML_CPU_CEIL)
                        if _fc != st.cache or _fq != st.cpu:
                            self._log((f'  [mem] filter_ml 降載: -c {st.cache}->{_fc} '
                                      f'-q {st.cpu}->{_fq} (防 OOM-kill)\n' if LANG == 'zh' else f'  [mem] filter_ml downscale: -c {st.cache}->{_fc} -q {st.cpu}->{_fq} (OOM-kill guard)\n'))
                        rc = self._run_gpt(tmp_xml,
                                           str(log_dir / f'filter_ml_{pair_name}_{iw}.log'),
                                           cache=_fc, cpu=_fq)
                        if rc != 0:
                            raise RuntimeError(
                                f'filter_ml failed for {pair_name} {iw} (rc={rc})')

                # ── Step 3: Smart Multilook ───────────────────────────────
                sml_dim = pair_ml_dir / f'{pair_name}_{iw}_sml.dim'
                if not self.force and dimap_product_complete(sml_dim):
                    self._log(f'[skip] sml {pair_name} {iw} (exists)\n')
                else:
                    if not dimap_product_complete(ml_dim):
                        self._log(f'[warn] ml_dim missing/incomplete for {iw}, skip SML\n')
                        continue
                    # Smart Multilook = SNAP Multilook (正確降採樣格+metadata，
                    # 幾何波段自動平均) → 再用 n×n 區塊 max-coh 覆寫 i/q/coh。
                    n_sml = st.smart_ml_n
                    ml_cmd = [
                        gpt_exe(st.snap_dir), 'Multilook',
                        f'-Ssource={ml_dim}',
                        f'-PnAzLooks={n_sml}', f'-PnRgLooks={n_sml}',
                        '-PgrSquarePixel=false',
                        '-t', str(sml_dim), '-f', 'BEAM-DIMAP',
                        '-c', st.cache, '-q', st.cpu,
                    ]
                    self._log(f'[sml] {pair_name} {iw} Multilook n={n_sml} ...\n')
                    rc_ml = self._run_cmd(
                        ml_cmd, str(log_dir / f'sml_multilook_{pair_name}_{iw}.log'))
                    ok = (rc_ml == 0
                          and dimap_product_complete(sml_dim)
                          and apply_smart_ml(str(ml_dim), str(sml_dim),
                                             n_sml, log_fn=self._log))
                    if not ok:
                        self._log(f'[warn] smart ML failed for {iw}, using ml_dim\n')
                        sml_dim = ml_dim  # fallback: use ml product

                if not dimap_product_complete(sml_dim):
                    self._log(f'[warn] sml_dim not found/incomplete for {iw}, skipping SNAPHU+TC\n')
                    continue
                if self._cancel.is_set():
                    raise RuntimeError('cancelled')

                # ── Step 4: SNAPHU Export → Run → Import ──────────────────
                snaphu_dir = snaphu_base / pair_name / iw
                snaphu_dir.mkdir(parents=True, exist_ok=True)
                unw_dim = snaphu_dir / f'{pair_name}_{iw}_unw.dim'

                if not self.force and dimap_product_complete(unw_dim):
                    self._log(f'[skip] snaphu {pair_name} {iw} (unw exists)\n')
                else:
                    # 4a: SNAPHU Export
                    # SnaphuExport creates a subdir inside snaphu_dir
                    self._log(f'[snaphu-export] {pair_name} {iw} ...\n')
                    # Single-tile unwrap (1x1). SnaphuExport defaults to 10x10
                    # = 100 tiles, whose "Assembling tile connected components"
                    # stage runs out of memory on snaphu v2.0.6 even with ample
                    # RAM (a tiled-reassembly pathology, not a system-RAM limit).
                    # Our AOIs are small (~18M px ≈ 1.7GB single-tile), so single
                    # tiling skips that stage entirely and yields cleaner output.
                    export_args = [
                        gpt_exe(st.snap_dir),
                        'SnaphuExport',
                        f'-Ssource={sml_dim}',
                        f'-PtargetFolder={snaphu_dir}',
                        '-PstatCostMode=DEFO',
                        '-PinitMethod=MCF',
                        '-PnumberOfTileRows=1',
                        '-PnumberOfTileCols=1',
                        f'-PnumberOfProcessors={st.cpu}',
                        '-c', st.cache, '-q', st.cpu,
                    ]
                    self._log(f'  $ {" ".join(export_args)}\n')
                    exp_log = str(log_dir / f'snaphu_export_{pair_name}_{iw}.log')
                    rc_exp = self._run_cmd(export_args, exp_log)
                    if rc_exp != 0:
                        self._log(f'[warn] snaphu export failed (rc={rc_exp}), skip unwrap\n')
                    else:
                        # SnaphuExport creates a subdirectory; find it
                        snaphu_export_dir = self._find_snaphu_export_dir(snaphu_dir)
                        if snaphu_export_dir is None:
                            self._log('[warn] snaphu export subdir not found, skip unwrap\n')
                        else:
                            _snaphu_bin = snaphu_exe(st.snaphu_path)
                            if not Path(_snaphu_bin).exists() and shutil.which(_snaphu_bin) is None:
                                self._log(
                                    f'[warn] snaphu not found at "{st.snaphu_path}" '
                                    f'(also tried common paths), skipping phase unwrapping\n')
                            else:
                                snaphu_conf = snaphu_export_dir / 'snaphu.conf'
                                # 4b: Parse snaphu.conf for phase file and width
                                phase_file, width = self._parse_snaphu_conf(snaphu_conf)
                                if phase_file is None:
                                    self._log('[warn] cannot parse snaphu.conf, skip unwrap\n')
                                else:
                                    # 4b': unwrap 前 QC + 自動後備內插。
                                    # 依序試 linear→smooth→none，存低 DPI QC 圖。
                                    rc_run = self._unwrap_with_fallback(
                                        snaphu_export_dir, phase_file, width,
                                        _snaphu_bin, pair_name, iw, log_dir)
                                    if rc_run != 0:
                                        self._log((f'[warn] snaphu 全部後備仍失敗 (rc={rc_run})\n' if LANG == 'zh' else f'[warn] snaphu all fallbacks still failed (rc={rc_run})\n'))
                                    else:
                                        # 4c: SNAPHU Import
                                        unw_hdr = self._find_snaphu_unw_hdr(
                                            snaphu_export_dir, phase_file)
                                        if unw_hdr is None:
                                            self._log(
                                                '[warn] unw .hdr not found, skip import\n')
                                        else:
                                            self._log(
                                                f'[snaphu-import] {pair_name} {iw} ...\n')
                                            imp_args = [
                                                gpt_exe(st.snap_dir),
                                                'SnaphuImport',
                                                f'-Ssource={sml_dim}',
                                                f'-Ssource2={unw_hdr}',
                                                '-t', str(unw_dim),
                                                '-f', 'BEAM-DIMAP',
                                                '-c', st.cache, '-q', st.cpu,
                                            ]
                                            self._log(f'  $ {" ".join(imp_args)}\n')
                                            imp_log = str(log_dir /
                                                          f'snaphu_import_{pair_name}_{iw}.log')
                                            rc_imp = self._run_cmd(imp_args, imp_log)
                                            if rc_imp != 0:
                                                self._log(
                                                    f'[warn] snaphu import failed '
                                                    f'(rc={rc_imp})\n')

                if self._cancel.is_set():
                    raise RuntimeError('cancelled')

                # ── Step 5: Terrain Correction ────────────────────────────
                tc_dir = tc_base / pair_name
                tc_dir.mkdir(exist_ok=True)

                tc_graph = graphs / 'snap2mintpy_terrain_correction.xml'
                if not tc_graph.exists():
                    self._log('[warn] terrain correction graph not found, skip TC\n')
                    continue
                tc_graph_tpl = tc_graph.read_text()

                # 5a: TC of SML product (wrapped phase + coh + elevation)
                # TC now outputs BEAM-DIMAP (.dim) to preserve band names
                wrapped_tc_dim = tc_dir / f'{pair_name}_{iw}_wrapped_tc.dim'
                if not self.force and dimap_product_complete(wrapped_tc_dim):
                    self._log(f'[skip] TC wrapped {pair_name} {iw} (exists)\n')
                else:
                    self._log(f'[tc-wrapped] {pair_name} {iw} ...\n')
                    xml_tc = fill_graph(tc_graph_tpl, {
                        'INPUTFILE':    str(sml_dim),
                        'OUTPUTFILE':   str(wrapped_tc_dim),
                        'PIXELSPACING': tc_pixel_spacing,
                        'EXTERNALDEM':  st.ext_dem or '',
                        'POLYGON':      wkt,
                    })
                    tmp_tc_xml = str(graph_tmp / f'tc_wrapped_{pair_name}_{iw}.xml')
                    Path(tmp_tc_xml).write_text(xml_tc)
                    rc_tc = self._run_gpt(tmp_tc_xml,
                                          str(log_dir / f'tc_wrapped_{pair_name}_{iw}.log'))
                    if rc_tc != 0:
                        self._log(f'[warn] TC wrapped failed (rc={rc_tc})\n')

                # 5b: TC of UNW product (if exists)
                unw_tc_dim: Optional[Path] = None
                if dimap_product_complete(unw_dim):
                    unw_tc_dim = tc_dir / f'{pair_name}_{iw}_unw_tc.dim'
                    if not self.force and dimap_product_complete(unw_tc_dim):
                        self._log(f'[skip] TC unw {pair_name} {iw} (exists)\n')
                    else:
                        self._log(f'[tc-unw] {pair_name} {iw} ...\n')
                        xml_tc_u = fill_graph(tc_graph_tpl, {
                            'INPUTFILE':    str(unw_dim),
                            'OUTPUTFILE':   str(unw_tc_dim),
                            'PIXELSPACING': tc_pixel_spacing,
                            'EXTERNALDEM':  st.ext_dem or '',
                            'POLYGON':      wkt,
                        })
                        tmp_tc_u_xml = str(graph_tmp / f'tc_unw_{pair_name}_{iw}.xml')
                        Path(tmp_tc_u_xml).write_text(xml_tc_u)
                        rc_tcu = self._run_gpt(tmp_tc_u_xml,
                                               str(log_dir / f'tc_unw_{pair_name}_{iw}.log'))
                        if rc_tcu != 0:
                            self._log(f'[warn] TC unw failed (rc={rc_tcu})\n')
                            unw_tc_dim = None

                # 5c: Extract named bands from BEAM-DIMAP → GeoTIFFs (QC / QGIS)
                if dimap_product_complete(wrapped_tc_dim):
                    self._split_tc_bands(
                        wrapped_tc_dim, tc_dir, pair_name, iw,
                        unw_tc_dim=unw_tc_dim if (unw_tc_dim and dimap_product_complete(unw_tc_dim)) else None,
                        geo_dir=geo_dir if is_first_pair else None)

                # 5d: MintPy geometry — copy geometry bands from first pair's wrapped_tc
                # CIFS/NTFS filesystems don't support symlinks; use copies instead.
                # MintPy reads: dem_tc.data/elevation.img, localIncidenceAngle.img,
                #               orthorectifiedLat_VV.img, orthorectifiedLon_VV.img
                if is_first_pair and dimap_product_complete(wrapped_tc_dim):
                    dem_tc_data = project / 'dem_tc.data'
                    dem_tc_data.mkdir(exist_ok=True)
                    src_data = wrapped_tc_dim.with_suffix('.data')
                    geo_bands = [
                        'elevation',
                        'localIncidenceAngle',
                        'orthorectifiedLat_VV',
                        'orthorectifiedLon_VV',
                    ]
                    import shutil as _shutil
                    for bname in geo_bands:
                        for ext in ('.img', '.hdr'):
                            src = src_data / f'{bname}{ext}'
                            dst = dem_tc_data / f'{bname}{ext}'
                            # copy if dst missing OR size mismatch (NFS truncation guard)
                            src_size = src.stat().st_size if src.exists() else 0
                            dst_size = dst.stat().st_size if dst.exists() else -1
                            if src_size > 0 and dst_size != src_size:
                                try:
                                    _shutil.copy2(str(src), str(dst))
                                    dst_size_after = dst.stat().st_size
                                    if dst_size_after != src_size:
                                        self._log(f'[warn] dem_tc.data/{bname}{ext}: '
                                                  f'size mismatch after copy '
                                                  f'({dst_size_after} != {src_size})\n')
                                    else:
                                        self._log(f'[MintPy] dem_tc.data/{bname}{ext}\n')
                                except Exception as exc:
                                    self._log(f'[warn] copy {bname}{ext}: {exc}\n')
                    # Also copy dem_tc.dim (MintPy prep_snap.py needs it for metadata)
                    dem_tc_dim = project / 'dem_tc.dim'
                    if not dem_tc_dim.exists():
                        try:
                            _shutil.copy2(str(wrapped_tc_dim), str(dem_tc_dim))
                            self._log('[MintPy] dem_tc.dim copied\n')
                        except Exception as exc:
                            self._log(f'[warn] copy dem_tc.dim: {exc}\n')

                # 5d': MintPy 官方慣例輸出 (separate per-type single-band products)
                #   {pair}_coh_tc / _filt_tc / _unw_tc + dem_tc.data/dem.img
                #   並把 unw 中 coh<coh_min 的像素設為 NaN (Q3 步驟5)。
                if dimap_product_complete(wrapped_tc_dim):
                    normalize_to_mintpy(
                        str(wrapped_tc_dim),
                        str(unw_tc_dim) if (unw_tc_dim
                                            and dimap_product_complete(unw_tc_dim))
                        else None,
                        pair_name, st.smart_ml_coh,
                        make_dem=is_first_pair, project_dir=str(project),
                        log_fn=self._log)

                # 5e: Per-pair diagnostic PNG
                self._make_pair_png(tc_dir, pair_name, iw, geo_dir)

            dur = time.time() - t0
            self._emit('pair_done', duration=dur)

        except Exception as exc:
            dur = time.time() - t0
            self._emit('pair_error', error=str(exc), duration=dur)

    # ── internal helpers ─────────────────────────────────────────────────

    def _run_cmd(self, args: List[str], log_path: str,
                 cwd: Optional[str] = None) -> int:
        """Run an arbitrary subprocess, streaming output to log and GUI."""
        env = dict(os.environ, LD_LIBRARY_PATH='.',
                   JAVA_TOOL_OPTIONS=_GPT_JAVA_OPTS)
        with open(log_path, 'a') as lf:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, text=True, env=env, cwd=cwd)
            self._proc = proc
            for line in proc.stdout:  # type: ignore[union-attr]
                if self._cancel.is_set():
                    proc.terminate()
                    return -2
                self._log(line)
                lf.write(line)
            return proc.wait()

    def _find_snaphu_export_dir(self, snaphu_dir: 'Path') -> 'Optional[Path]':
        """Return the subdir that SnaphuExport created (contains snaphu.conf)."""
        for d in snaphu_dir.iterdir():
            if d.is_dir() and (d / 'snaphu.conf').exists():
                return d
        return None

    def _unwrap_with_fallback(self, export_dir: 'Path', phase_file: 'Path',
                              width: int, snaphu_bin: str, pair_name: str,
                              iw: str, log_dir: 'Path') -> int:
        """unwrap 前 QC + 自動後備：依序試內插模式直到 snaphu 成功。

        順序 (使用者選定): linear → smooth(更平滑) → none(原始 wrap+coh)。
        每對都存低 DPI QC 圖到 out_fig/{pair}_{iw}_preunwrap.png 供檢查。
        回最後一次 snaphu 的 rc (0=成功)。
        """
        st = self.st
        phase_path = export_dir / phase_file.name
        coh_list = list(export_dir.glob('coh*.img'))
        coh_img = str(coh_list[0]) if coh_list else None

        run_args = [snaphu_bin, '-f', 'snaphu.conf',
                    phase_file.name, str(width), '-g', 'conncomp.img']
        run_log = str(log_dir / f'snaphu_run_{pair_name}_{iw}.log')

        # 讀原始 wrap + coh (自動判 endian)，備份供後備與 QC
        dt, phase_orig, coh_arr = '<f4', None, None
        if coh_img:
            dt, phase_orig, coh_arr = _read_phase_coh_auto_endian(
                str(phase_path), coh_img, width)

        # 讀不到 → 退回原行為 (直接 snaphu，不內插不畫圖)
        if phase_orig is None or coh_arr is None:
            self._log(('[nan] 無法讀 phase/coh，直接 snaphu (不內插)\n' if LANG == 'zh' else '[nan] cannot read phase/coh, running snaphu directly (no interpolation)\n'))
            self._log(f'[snaphu-run] {pair_name} {iw} width={width} ...\n')
            return self._run_cmd(run_args, run_log, cwd=str(export_dir))

        modes = ['linear', 'smooth', 'none']
        rc_run, used_mode, phase_used = 1, 'linear', phase_orig
        for mi, mode in enumerate(modes):
            filled, _mask = fill_phase_by_mode(
                phase_orig, coh_arr, st.smart_ml_coh, mode=mode, log_fn=self._log)
            filled.astype(dt).tofile(str(phase_path))   # 覆寫供 snaphu 讀
            phase_used, used_mode = filled, mode
            self._log((f'[snaphu-run] {pair_name} {iw} width={width} '
                      f'mode={mode} (嘗試 {mi + 1}/{len(modes)}) ...\n' if LANG == 'zh' else f'[snaphu-run] {pair_name} {iw} width={width} mode={mode} (attempt {mi + 1}/{len(modes)}) ...\n'))
            rc_run = self._run_cmd(run_args, run_log, cwd=str(export_dir))
            if rc_run == 0:
                if mi > 0:
                    self._log((f'[nan] 後備成功: mode={mode} (前 {mi} 種失敗)\n' if LANG == 'zh' else f'[nan] fallback succeeded: mode={mode} ({mi} earlier modes failed)\n'))
                break
            if self._cancel.is_set():
                break
            self._log((f'[nan] snaphu 失敗 (rc={rc_run}, mode={mode})，試下一後備\n' if LANG == 'zh' else f'[nan] snaphu failed (rc={rc_run}, mode={mode}), trying next fallback\n'))

        # 每對都畫低 DPI QC 圖 (原始 wrap | 送 snaphu wrap | coh)
        try:
            out_png = (Path(st.project_dir) / 'out_fig' /
                       f'{pair_name}_{iw}_preunwrap.png')
            tag = '✓' if rc_run == 0 else ('✗unwrap失敗' if LANG == 'zh' else '✗unwrap failed')
            save_preunwrap_qc(
                phase_orig, phase_used, coh_arr, str(out_png),
                title=f'{pair_name} {iw}  [{tag}]', mode=used_mode)
            self._log(f'[qc] {out_png}\n')
        except Exception as exc:
            self._log((f'[qc] 繪圖略過: {exc}\n' if LANG == 'zh' else f'[qc] plotting skipped: {exc}\n'))
        return rc_run

    def _parse_snaphu_conf(
            self, conf: 'Path') -> 'Tuple[Optional[Path], int]':
        """Parse SNAP-generated snaphu.conf for the phase file and image width.

        SNAP embeds the suggested command as a comment:
          # snaphu -f snaphu.conf <phase_file> <width>
        This method extracts those values and also fixes the CORRFILE path in
        the conf if it is malformed (SNAP sometimes writes just `.snaphu.img`).
        Returns (phase_file_path, width) or (None, 0) on failure.
        """
        phase_file: Optional[Path] = None
        width: int = 0
        try:
            text = conf.read_text()
            conf_dir = conf.parent

            # 1) Extract phase file + width from the suggested command comment
            for line in text.splitlines():
                stripped = line.strip()
                # SNAP comment format: # snaphu -f snaphu.conf <phase.img> <width>
                if stripped.startswith('#') and 'snaphu' in stripped and '-f' in stripped:
                    parts = stripped.lstrip('#').split()
                    try:
                        idx = parts.index('-f')
                        # parts: snaphu -f snaphu.conf <phase_file> <width>
                        if idx + 3 < len(parts):
                            candidate = conf_dir / parts[idx + 2]
                            w_str = parts[idx + 3]
                            if candidate.exists():
                                phase_file = candidate
                                width = int(w_str)
                                break
                    except (ValueError, IndexError):
                        pass

            # 2) Fallback: look for Phase_ifg*.snaphu.img
            if phase_file is None:
                matches = sorted(conf_dir.glob('Phase_ifg*.snaphu.img'))
                if matches:
                    phase_file = matches[0]
                    # get width from .hdr
                    hdr = phase_file.with_suffix('.hdr')
                    if hdr.exists():
                        for hline in hdr.read_text().splitlines():
                            hline = hline.strip()
                            if hline.startswith('samples'):
                                try:
                                    width = int(hline.split('=')[1].strip())
                                except (ValueError, IndexError):
                                    pass

            # 3) Fix malformed CORRFILE (SNAP writes just '.snaphu.img' without prefix)
            coh_files = sorted(conf_dir.glob('coh*.snaphu.img'))
            if coh_files:
                fixed = text.replace(
                    'CORRFILE \t\t.snaphu.img',
                    f'CORRFILE \t\t{coh_files[0].name}')
                if fixed != text:
                    conf.write_text(fixed)
                    self._log(f'[snaphu] Fixed CORRFILE → {coh_files[0].name}\n')

        except Exception as exc:
            self._log(f'[warn] _parse_snaphu_conf: {exc}\n')
        return phase_file, width

    def _find_snaphu_unw_hdr(
            self, snaphu_dir: 'Path',
            phase_file: 'Optional[Path]') -> 'Optional[Path]':
        """Locate the .hdr for SnaphuImport (UnwPhase*.snaphu.hdr)."""
        # SNAP creates UnwPhase_*.snaphu.hdr in the export subdir
        for candidate in snaphu_dir.glob('UnwPhase*.hdr'):
            return candidate
        if phase_file is not None:
            stem = phase_file.stem  # e.g. Phase_ifg_VV_...snaphu
            for candidate in snaphu_dir.iterdir():
                if candidate.suffix == '.hdr' and (
                        'unw' in candidate.name.lower() or stem in candidate.name):
                    return candidate
        hdrs = list(snaphu_dir.glob('*.hdr'))
        return hdrs[0] if hdrs else None

    def _split_tc_bands(self, wrapped_tc_dim: 'Path', tc_dir: 'Path',
                        pair_name: str, iw: str,
                        unw_tc_dim: 'Optional[Path]',
                        geo_dir: 'Optional[Path]') -> None:
        """Read TC BEAM-DIMAP, extract named bands, write MintPy-named GeoTIFFs.

        TC outputs BEAM-DIMAP so band names are preserved in .dim XML.
        Georeferencing is read via GDAL (works for map-projected BEAM-DIMAP).
        """
        import numpy as np
        import xml.etree.ElementTree as ET

        try:
            from osgeo import gdal, osr
        except ImportError:
            self._log('[TC] gdal not available, skipping band split\n')
            return

        def _parse_dim_bands(dim_p: Path) -> Dict[str, dict]:
            """Return {band_name: {h, w, img_path}} from .dim XML."""
            bm: Dict[str, dict] = {}
            try:
                root = ET.parse(str(dim_p)).getroot()
            except Exception:
                return bm
            data_dir = dim_p.with_suffix('.data')
            for bi in root.findall('.//Spectral_Band_Info'):
                name = (bi.findtext('BAND_NAME') or '').strip()
                w    = int(bi.findtext('BAND_RASTER_WIDTH') or 0)
                h    = int(bi.findtext('BAND_RASTER_HEIGHT') or 0)
                if not name or not w or not h:
                    continue
                # find matching .img (exact or glob)
                img = data_dir / f'{name}.img'
                if not img.exists():
                    candidates = list(data_dir.glob(f'{name}*.img'))
                    img = candidates[0] if candidates else None
                if img and img.exists():
                    bm[name] = {'h': h, 'w': w, 'img': img}
            return bm

        def _read_img(info: dict) -> 'Optional[np.ndarray]':
            try:
                return np.fromfile(str(info['img']), dtype='>f4').reshape(
                    info['h'], info['w'])
            except Exception:
                return None

        def _get_georef(dim_p: Path):
            """Return (gt, proj_wkt) from BEAM-DIMAP .dim XML.

            Parses IMAGE_TO_MODEL_TRANSFORM and WKT from the XML, since
            GDAL cannot open SAR-origin BEAM-DIMAP due to ENVI .hdr conflict.
            IMAGE_TO_MODEL_TRANSFORM format:  sx, 0, 0, sy, x0, y0
            GDAL GeoTransform format:         (x0, sx, 0, y0, 0, sy)
            """
            try:
                root2 = ET.parse(str(dim_p)).getroot()
                t_txt = root2.findtext('.//IMAGE_TO_MODEL_TRANSFORM') or ''
                parts2 = [float(x) for x in t_txt.replace(',', ' ').split()]
                # parts: sx, shx, shy, sy, x0, y0
                sx, shx, shy, sy, x0, y0 = parts2
                gt = (x0, sx, shx, y0, shy, sy)
                wkt = (root2.findtext('.//WKT') or '').strip()
                return gt, wkt
            except Exception as exc:
                self._log(f'[TC] georef parse failed: {exc}\n')
                return None, None

        def _write_tif(arr: 'np.ndarray', gt, proj_wkt: str,
                       out_path: Path, nodata: float = 0.0) -> bool:
            arr32 = arr.astype(np.float32)
            drv = gdal.GetDriverByName('GTiff')
            out_ds = drv.Create(str(out_path), arr32.shape[1], arr32.shape[0],
                                1, gdal.GDT_Float32,
                                options=['COMPRESS=DEFLATE', 'TILED=YES'])
            if gt:
                out_ds.SetGeoTransform(gt)
            if proj_wkt:
                out_ds.SetProjection(proj_wkt)
            band = out_ds.GetRasterBand(1)
            band.WriteArray(arr32)
            band.SetNoDataValue(nodata)
            out_ds.FlushCache()
            out_ds = None
            return True

        # ── Wrapped TC ────────────────────────────────────────────────────
        bm = _parse_dim_bands(wrapped_tc_dim)
        self._log(f'[TC] wrapped bands: {list(bm.keys())}\n')
        gt, proj = _get_georef(wrapped_tc_dim)

        # Phase from i_ifg / q_ifg (or Phase_ifg if present)
        phase_out = tc_dir / f'{pair_name}_{iw}_phase_ifg_VV.tif'
        phase_written = False
        for name, info in bm.items():
            if name.startswith('Phase_ifg'):
                arr = _read_img(info)
                if arr is not None:
                    _write_tif(arr, gt, proj, phase_out)
                    self._log(f'[TC] wrote {phase_out.name}\n')
                    phase_written = True
                    break
        if not phase_written:
            i_info = next((v for k, v in bm.items() if k.startswith('i_ifg')), None)
            q_info = next((v for k, v in bm.items() if k.startswith('q_ifg')), None)
            if i_info and q_info:
                i64 = _read_img(i_info).astype(np.float64)
                q64 = _read_img(q_info).astype(np.float64)
                phase = np.arctan2(q64, i64).astype(np.float32)
                _write_tif(phase, gt, proj, phase_out)
                self._log(f'[TC] wrote {phase_out.name} (from I/Q)\n')

        # Coherence
        for name, info in bm.items():
            if name.startswith('coh_'):
                arr = _read_img(info)
                if arr is not None:
                    out = tc_dir / f'{pair_name}_{iw}_coh_VV.tif'
                    _write_tif(arr, gt, proj, out)
                    self._log(f'[TC] wrote {out.name}\n')
                break

        # Geometry: DEM + local incidence angle (first pair only)
        if geo_dir is not None:
            for name, info in bm.items():
                if name.lower() == 'elevation' or name.lower() == 'dem':
                    arr = _read_img(info)
                    if arr is not None:
                        dem_out = geo_dir / 'DEM.tif'
                        _write_tif(arr, gt, proj, dem_out)
                        self._log(f'[TC] wrote geometry/DEM.tif\n')
                    break
            for name, info in bm.items():
                if 'localincidence' in name.lower() or name == 'localIncidenceAngle':
                    arr = _read_img(info)
                    if arr is not None:
                        lia_out = geo_dir / 'local_incidence_angle.tif'
                        _write_tif(arr, gt, proj, lia_out)
                        self._log(f'[TC] wrote geometry/local_incidence_angle.tif\n')
                    break

        # ── Unwrapped TC ──────────────────────────────────────────────────
        if unw_tc_dim is not None and unw_tc_dim.exists():
            bm2 = _parse_dim_bands(unw_tc_dim)
            self._log(f'[TC] unw bands: {list(bm2.keys())}\n')
            gt2, proj2 = _get_georef(unw_tc_dim)

            unw_out = tc_dir / f'{pair_name}_{iw}_Unw_Phase_ifg_VV.tif'
            written = False
            for name, info in bm2.items():
                if 'unw' in name.lower():
                    arr = _read_img(info)
                    if arr is not None:
                        _write_tif(arr, gt2, proj2, unw_out)
                        self._log(f'[TC] wrote {unw_out.name}\n')
                        written = True
                    break
            if not written:
                # fallback: compute from I/Q
                i_info2 = next((v for k, v in bm2.items() if k.startswith('i_ifg')), None)
                q_info2 = next((v for k, v in bm2.items() if k.startswith('q_ifg')), None)
                if i_info2 and q_info2:
                    i64 = _read_img(i_info2).astype(np.float64)
                    q64 = _read_img(q_info2).astype(np.float64)
                    phase = np.arctan2(q64, i64).astype(np.float32)
                    _write_tif(phase, gt2, proj2, unw_out)
                    self._log(f'[TC] wrote {unw_out.name} (from I/Q)\n')

    def _make_pair_png(self, tc_dir: 'Path', pair_name: str, iw: str,
                       geo_dir: 'Path') -> None:
        """Generate QC PNG (1×3: phase | coh | unw) for rapid visual inspection.

        Reads .img files directly from unw_tc.data/ (big-endian float32, ENVI).
        Output: {tc_dir}/{pair_name}_{iw}_qc.png
        """
        import numpy as np
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError as exc:
            self._log(f'[PNG] skip (missing lib: {exc})\n')
            return

        def _load_img(data_dir: 'Path', glob: str):
            hits = sorted(data_dir.glob(glob))
            if not hits:
                return None
            p = hits[0]
            hdr_p = p.with_suffix('.hdr')
            if not hdr_p.exists():
                return None
            hdr = {}
            for line in hdr_p.read_text(errors='replace').splitlines():
                if '=' in line and not line.strip().startswith('{'):
                    k, _, v = line.partition('=')
                    hdr[k.strip().lower().replace(' ', '_')] = v.strip()
            w  = int(hdr.get('samples', 0))
            h  = int(hdr.get('lines',   0))
            bo = int(hdr.get('byte_order', 1))
            if not w or not h:
                return None
            arr = np.fromfile(str(p), dtype='>f4' if bo == 1 else '<f4').reshape(h, w)
            arr = arr.astype(np.float32)
            arr[arr == 0] = np.nan
            return arr

        def _panel(ax, arr, title, cmap, vmin=None, vmax=None):
            if arr is None:
                ax.text(0.5, 0.5, 'N/A', ha='center', va='center',
                        transform=ax.transAxes, fontsize=12, color='grey')
            else:
                v = arr[np.isfinite(arr)]
                lo = vmin if vmin is not None else (float(np.percentile(v, 2)) if len(v) else 0)
                hi = vmax if vmax is not None else (float(np.percentile(v, 98)) if len(v) else 1)
                im = ax.imshow(arr, cmap=cmap, vmin=lo, vmax=hi, aspect='auto',
                               interpolation='nearest')
                plt.colorbar(im, ax=ax, fraction=0.046, pad=0.02)
                stats = (f'med={np.nanmedian(arr):.2f}\n'
                         f'[{np.nanmin(arr):.2f},{np.nanmax(arr):.2f}]\n'
                         f'valid={100*np.isfinite(arr).mean():.0f}%')
                ax.text(0.02, 0.97, stats, transform=ax.transAxes, fontsize=7,
                        va='top', family='monospace', color='white',
                        bbox=dict(boxstyle='round,pad=0.2', fc='black', alpha=0.5))
            ax.set_title(title, fontsize=9, pad=3)
            ax.axis('off')

        unw_data = tc_dir / f'{pair_name}_{iw}_unw_tc.data'
        ph  = _load_img(unw_data, 'Phase_ifg_VV_*.img')
        coh = _load_img(unw_data, 'coh_*.img')
        unw = _load_img(unw_data, 'Unw_Phase_ifg_*.img')

        if all(x is None for x in (ph, coh, unw)):
            self._log(f'[PNG] no .img data in {unw_data.name}, skip\n')
            return

        _first = next(x for x in (ph, coh, unw) if x is not None)
        h, w = _first.shape[0], _first.shape[1]

        fig, axes = plt.subplots(1, 3, figsize=(14, 5))
        fig.suptitle(f'{pair_name}  {iw}   {h}×{w} px', fontsize=11, fontweight='bold')
        _panel(axes[0], ph,  'Wrapped phase [rad]',   'RdBu_r', -np.pi, np.pi)
        _panel(axes[1], coh, 'Coherence',             'plasma',  0,     1)
        _panel(axes[2], unw, 'Unwrapped phase [rad]', 'RdBu_r')
        plt.tight_layout()

        png_out = tc_dir / f'{pair_name}_{iw}_qc.png'
        fig.savefig(str(png_out), dpi=130, bbox_inches='tight')
        plt.close(fig)
        self._log(f'[PNG] qc saved → {png_out.name}\n')


# ─────────────────────────────────────────────────────────────────────────
# MintPy cfg generator
# ─────────────────────────────────────────────────────────────────────────
def default_mintpy_cfg(state: AppState) -> str:
    if state.aoi_mode == 'WKT':
        aoi_note = f'WKT: {state.wkt[:60]}...'
    else:
        aoi_note = (f'BBOX  lon {state.lonmin}~{state.lonmax}  '
                    f'lat {state.latmin}~{state.latmax}')
    iw = ('merged' if len(state.iw_list) > 1
          else (state.iw_list[0] if state.iw_list else 'IW1'))
    pol = state.polarisation
    project = state.project_dir.rstrip('/')
    weight = getattr(state, 'mp_weight_func', 'var') or 'var'
    # cfg is saved to {project}/mintpy/S1_smallbaseline.cfg
    # so ../tc/ → {project}/tc/ and ../geometry/ → {project}/geometry/
    return f"""\
##------- snap2mintpy_gui v2 auto-generated S1_smallbaseline.cfg -------##
## Project  : {project}
## Pairs    : {len(state.pairs)}
## AOI      : {aoi_note}
## Sensor   : Sentinel-1 IW  Polarisation: {pol}  Subswath: {iw}
##
## Directory structure (MintPy SNAP convention):
##   interferograms/{{pair}}/{{pair}}_{iw}_wrapped_tc.dim  ← per-pair TC (outputComplex=true)
##   interferograms/{{pair}}/{{pair}}_{iw}_wrapped_tc.data/
##       i_ifg_VV_*.img / q_ifg_VV_*.img  ← complex interferogram (not used by MintPy)
##       coh_*_{iw}_*.img                 ← coherence (also in unw_tc.data)
##       elevation.img / localIncidenceAngle.img / orthorectifiedLat/Lon_VV.img ← geometry
##   interferograms/{{pair}}/{{pair}}_{iw}_unw_tc.dim      ← per-pair TC after SnaphuImport
##   interferograms/{{pair}}/{{pair}}_{iw}_unw_tc.data/
##       Phase_ifg_VV_*.img     ← wrapped phase (from SnaphuImport)
##       Unw_Phase_ifg_*.img    ← unwrapped phase
##       coh_*_{iw}_*.img       ← coherence
##   dem_tc.dim  →  symlink to first pair wrapped_tc.dim
##   dem_tc.data/
##       elevation.img              ← DEM [m]
##       localIncidenceAngle.img    ← incidence angle [deg]
##       orthorectifiedLat_VV.img   ← lookup latitude
##       orthorectifiedLon_VV.img   ← lookup longitude
##   geometry/  (GeoTIFFs for QGIS + diagnostic PNGs, nodata=0)
##
## Reference: https://github.com/insarlab/MintPy/blob/main/docs/dir_structure.md
## ---------------------------------------------------------------------- ##

########## 1. Load Data (SNAP BEAM-DIMAP .img — official MintPy SNAP format) ##########
mintpy.load.processor        = snap

##--- interferogram stack ---
##   Two wildcards: interferograms/{{pair_dir}}/{{pair}}.data/{{band}}.img
mintpy.load.unwFile          = {project}/interferograms/*/20*_20*_{iw}_unw_tc.data/Unw_*.img
mintpy.load.corFile          = {project}/interferograms/*/20*_20*_{iw}_unw_tc.data/coh_*.img
mintpy.load.connCompFile     = no
mintpy.load.intFile          = {project}/interferograms/*/20*_20*_{iw}_unw_tc.data/Phase_ifg_*.img

##--- geometry (from dem_tc.dim symlink → first pair wrapped_tc) ---
mintpy.load.demFile          = {project}/dem_tc.data/elevation.img
mintpy.load.lookupYFile      = {project}/dem_tc.data/orthorectifiedLat_VV.img
mintpy.load.lookupXFile      = {project}/dem_tc.data/orthorectifiedLon_VV.img
mintpy.load.incAngleFile     = {project}/dem_tc.data/localIncidenceAngle.img
mintpy.load.azAngleFile      = no
mintpy.load.shadowMaskFile   = no
mintpy.load.waterMaskFile    = no

########## 2. Reference Point ##########
mintpy.reference.yx          = auto
mintpy.reference.lalo        = auto

########## 3. Tropospheric Correction ##########
mintpy.troposphericDelay.method      = no

########## 4. Topographic Residual ##########
mintpy.topographicResidual           = yes

########## 5. Deramp (optional) ##########
## Remove phase ramp per epoch: no / linear / quadratic
## no: velocity.h5 = demErr-only, no deramp -> keeps real regional deformation
##     (deramp on a small AOI removes true deformation gradients as a ramp).
##     A deramped version is produced separately by post-processing
##     (velocity_deramp.h5). MintPy order: invert -> demErr -> velocity.
mintpy.deramp          = no
mintpy.deramp.maskFile = maskTempCoh.h5

########## 6. Velocity ##########
mintpy.velocity.excludeDate          = no

########## 7. Network Inversion ##########
## weightFunc: var = weighted LS (good coherence); no = uniform LS (low coherence / vegetation)
## Field-tested: var brings velocity closer to GNSS (N=6: -20.1 -> -21.1);
## set via the GUI "Inversion weight" dropdown
mintpy.networkInversion.weightFunc    = {weight}
mintpy.networkInversion.waterMaskFile = no
## minTempCoh: lower this value for vegetated / low-coherence areas (default MintPy=0.7)
## Recommended: 0.7 (urban/arid), 0.5 (mixed), 0.4 (forest/mountain), 0.3 (worst-case)
mintpy.networkInversion.minTempCoh    = 0.4
"""


# ─────────────────────────────────────────────────────────────────────────
# Tab 1 — Input & Pairs
# ─────────────────────────────────────────────────────────────────────────
class InputPairFrame(ttk.Frame):
    def __init__(self, nb: ttk.Notebook, app: 'Snap2MintPyApp'):
        super().__init__(nb)
        self.app = app
        self._build()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self):
        # scrollable canvas
        canvas = tk.Canvas(self, borderwidth=0)
        vsb = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _resize(event):
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.itemconfig(win_id, width=canvas.winfo_width())
        inner.bind('<Configure>', _resize)

        self._build_inner(inner)

    def _build_inner(self, f: ttk.Frame):
        st = self.app.state

        # ── Project ──────────────────────────────────────────────────────
        pf = ttk.LabelFrame(f, text=_T('lf_project'))
        pf.pack(fill='x', padx=8, pady=4)
        self._project_var = self._row_dir(pf, 0, 'Project folder', st.project_dir)
        ttk.Button(pf, text=_T('btn_check_project'), command=self._check_project_ready).grid(
            row=0, column=5, padx=4)
        ttk.Button(pf, text=_T('btn_mkdir'), command=self._mkdir_project).grid(
            row=0, column=6, padx=(0, 4))
        self._slc_var     = self._row_dir(pf, 1, 'SLC folder', st.slc_dir)
        # Auto-scan whenever SLC folder is set to an existing directory
        self._slc_var.trace_add('write', self._on_slc_dir_change)
        self._extdem_var  = self._row_file(pf, 2, 'External DEM (.tif)',
                                            st.ext_dem,
                                            ftypes=[('GeoTIFF', '*.tif *.tiff'),
                                                    ('All', '*')])
        self._dem_dl_btn = ttk.Button(pf, text=_T('btn_auto_dem'),
                                       command=self._auto_download_dem)
        self._dem_dl_btn.grid(row=2, column=5, padx=4)
        pf.columnconfigure(1, weight=1)

        # ── SNAP ─────────────────────────────────────────────────────────
        sf = ttk.LabelFrame(f, text=_T('lf_snap'))
        sf.pack(fill='x', padx=8, pady=4)
        self._snap_var = self._row_dir(sf, 0, 'SNAP installation', st.snap_dir)
        ttk.Label(sf, text='CPU').grid(row=1, column=0, sticky='w', padx=6, pady=2)
        self._cpu_var = tk.StringVar(value=st.cpu)
        ttk.Spinbox(sf, from_=1, to=128, width=6,
                    textvariable=self._cpu_var).grid(row=1, column=1, sticky='w')
        ttk.Label(sf, text='Cache').grid(row=1, column=2, sticky='e', padx=(16, 4))
        self._cache_var = tk.StringVar(value=st.cache)
        ttk.Entry(sf, textvariable=self._cache_var, width=8).grid(
            row=1, column=3, sticky='w')
        ttk.Label(sf, text='(GPT graphs: snap2stamps/graphs/)',
                  foreground='#888').grid(row=1, column=4, sticky='w', padx=8)
        ttk.Label(sf, text=_T('lbl_xmx')).grid(row=2, column=0, sticky='w', padx=6, pady=2)
        self._xmx_var = tk.StringVar(value=st.xmx)
        ttk.Entry(sf, textvariable=self._xmx_var, width=8).grid(row=2, column=1, sticky='w')

        def _auto_fill_mem():
            xmx, cache = _auto_memory_defaults()
            self._xmx_var.set(xmx)
            self._cache_var.set(cache)
            try:
                total_gb = int(open('/proc/meminfo').read().split('MemTotal:')[1].split()[0]) / 1024 / 1024
                hint = (f'已套用：{total_gb:.0f}GB RAM × 80%  →  Xmx={xmx}  Cache={cache}' if LANG == 'zh' else f'Applied: {total_gb:.0f}GB RAM × 80%  →  Xmx={xmx}  Cache={cache}')
            except Exception:
                hint = (f'已套用：Xmx={xmx}  Cache={cache}' if LANG == 'zh' else f'Applied: Xmx={xmx}  Cache={cache}')
            _mem_hint_var.set(hint)

        ttk.Button(sf, text=('自動' if LANG == 'zh' else 'Auto'), width=5,
                   command=_auto_fill_mem).grid(row=2, column=2, sticky='w', padx=(16, 4))
        _mem_hint_var = tk.StringVar(value=('← 依實際 RAM × 80% 計算' if LANG == 'zh' else '← Calculated from actual RAM × 80%'))
        ttk.Label(sf, textvariable=_mem_hint_var, foreground='#888',
                  font=('TkDefaultFont', 8)).grid(
            row=2, column=3, columnspan=2, sticky='w', padx=4)
        sf.columnconfigure(1, weight=1)

        # ── SSD Swap ──────────────────────────────────────────────────────
        swf = ttk.LabelFrame(f, text=_T('lf_ssd_swap'))
        swf.pack(fill='x', padx=8, pady=4)
        # row 0: path + size
        ttk.Label(swf, text=_T('lbl_swap_path')).grid(
            row=0, column=0, sticky='w', padx=6, pady=3)
        self._swap_path_var = tk.StringVar(value=st.ssd_swap_path)
        ttk.Entry(swf, textvariable=self._swap_path_var, width=32).grid(
            row=0, column=1, sticky='ew', padx=4)
        ttk.Label(swf, text=_T('lbl_swap_size')).grid(
            row=0, column=2, sticky='e', padx=(12, 4))
        self._swap_size_var = tk.StringVar(value=st.ssd_swap_size)
        ttk.Entry(swf, textvariable=self._swap_size_var, width=6).grid(
            row=0, column=3, sticky='w')
        ttk.Label(swf, text='(1T, 500G…)', foreground='#888',
                  font=('TkDefaultFont', 8)).grid(row=0, column=4, sticky='w', padx=4)
        ttk.Label(swf, text=('sudo 密碼:' if LANG == 'zh' else 'sudo password:')).grid(
            row=0, column=5, sticky='e', padx=(16, 4))
        self._swap_sudo_var = tk.StringVar()
        ttk.Entry(swf, textvariable=self._swap_sudo_var, width=14, show='*').grid(
            row=0, column=6, sticky='w')
        # row 1: status
        ttk.Label(swf, text=_T('lbl_swap_status')).grid(
            row=1, column=0, sticky='w', padx=6, pady=2)
        self._swap_status_var = tk.StringVar(
            value='(尚未檢查)' if LANG == 'zh' else '(not checked)')
        ttk.Label(swf, textvariable=self._swap_status_var,
                  foreground='#555', font=('TkDefaultFont', 9, 'italic')).grid(
            row=1, column=1, sticky='w')
        ttk.Button(swf, text='🔄', width=3,
                   command=self._refresh_swap_status).grid(
            row=1, column=2, sticky='w', padx=4)
        # row 2: action buttons + auto-enable checkbox
        _r2 = ttk.Frame(swf)
        _r2.grid(row=2, column=0, columnspan=5, sticky='w', padx=6, pady=4)
        ttk.Button(_r2, text=_T('btn_swap_create'),
                   command=self._create_swapfile_ui).pack(side='left', padx=4)
        ttk.Button(_r2, text=_T('btn_swap_enable'),
                   command=self._enable_swap_ui).pack(side='left', padx=4)
        ttk.Button(_r2, text=_T('btn_swap_disable'),
                   command=self._disable_swap_ui).pack(side='left', padx=4)
        self._swap_auto_var = tk.BooleanVar(value=st.ssd_swap_auto)
        ttk.Checkbutton(_r2, text=_T('chk_swap_auto'),
                        variable=self._swap_auto_var).pack(side='left', padx=(16, 0))
        swf.columnconfigure(1, weight=1)

        # ── AOI ──────────────────────────────────────────────────────────
        af = ttk.LabelFrame(f, text=_T('lf_aoi'))
        af.pack(fill='x', padx=8, pady=4)
        self._aoi_mode = tk.StringVar(value=st.aoi_mode)
        ttk.Radiobutton(af, text='BBOX', variable=self._aoi_mode, value='BBOX',
                        command=self._refresh_aoi).grid(row=0, column=0, padx=6, pady=2)
        ttk.Radiobutton(af, text='WKT', variable=self._aoi_mode, value='WKT',
                        command=self._refresh_aoi).grid(row=0, column=1, padx=6)

        # 地圖按鈕：開啟 boundingbox.klokantech.com 選範圍
        ttk.Button(af, text=('[地圖]' if LANG == 'zh' else '[Map]'),
                   command=lambda: _open_url(
                       'https://boundingbox.klokantech.com/')).grid(
            row=0, column=2, padx=8)

        self._bbox_frame = ttk.Frame(af)
        self._bbox_frame.grid(row=1, column=0, columnspan=6, sticky='ew', padx=6)
        for i, (lbl, key) in enumerate([('lonmin', st.lonmin), ('latmin', st.latmin),
                                         ('lonmax', st.lonmax), ('latmax', st.latmax)]):
            ttk.Label(self._bbox_frame, text=lbl).grid(row=0, column=i * 2, padx=(8, 2))
            var = tk.StringVar(value=key)
            setattr(self, f'_{lbl}_var', var)
            ttk.Entry(self._bbox_frame, textvariable=var, width=14).grid(
                row=0, column=i * 2 + 1, padx=2)

        # CSV 貼入列：貼上 boundingbox.klokantech.com 的 CSV 格式 (lon_min,lat_min,lon_max,lat_max)
        ttk.Label(self._bbox_frame, text=('CSV 貼入:' if LANG == 'zh' else 'CSV paste:'),
                  foreground='#555').grid(row=1, column=0, columnspan=2,
                                          sticky='w', padx=(8, 2), pady=(3, 0))
        self._bbox_csv_var = tk.StringVar()
        csv_entry = ttk.Entry(self._bbox_frame, textvariable=self._bbox_csv_var, width=46)
        csv_entry.grid(row=1, column=2, columnspan=5, sticky='ew', padx=2, pady=(3, 0))
        csv_entry.bind('<Return>', lambda e: self._apply_bbox_csv())
        ttk.Button(self._bbox_frame, text=('套用' if LANG == 'zh' else 'Apply'),
                   command=self._apply_bbox_csv).grid(
            row=1, column=7, padx=(4, 8), pady=(3, 0))
        ttk.Label(self._bbox_frame,
                  text=('（從 boundingbox.klokantech.com 選 Copy&Paste CSV）' if LANG == 'zh' else '(Select on boundingbox.klokantech.com, Copy & Paste CSV)'),
                  foreground='#888').grid(row=2, column=2, columnspan=6,
                                           sticky='w', padx=2)

        self._wkt_frame = ttk.Frame(af)
        self._wkt_frame.grid(row=2, column=0, columnspan=6, sticky='ew', padx=6)
        ttk.Label(self._wkt_frame, text='WKT').grid(row=0, column=0, sticky='w')
        self._wkt_var = tk.StringVar(value=st.wkt)
        wkt_entry = ttk.Entry(self._wkt_frame, textvariable=self._wkt_var, width=72)
        wkt_entry.grid(row=0, column=1, sticky='ew', padx=(0, 4))
        wkt_entry.bind('<FocusOut>', lambda e: self._normalize_wkt())
        ttk.Button(self._wkt_frame, text='[→ WKT]',
                   command=self._normalize_wkt).grid(row=0, column=2, padx=2)
        self._wkt_frame.columnconfigure(1, weight=1)
        af.columnconfigure(5, weight=1)

        # ── Subswath (IW) 選擇：自動偵測 ↔ 手動勾選 ──────────────────────────
        # 手動模式下完全照勾選的 IW 跑 (略過自動偵測覆蓋)，用於某對某子帶退化
        # (如 IW2 ESD 重疊權重 0 → 干涉圖全零 → merge 失敗) 時強制只跑 IW1。
        iw_row = ttk.Frame(af)
        iw_row.grid(row=3, column=0, columnspan=6, sticky='w', padx=6, pady=(4, 2))
        ttk.Label(iw_row, text='Subswaths (IW):').pack(side='left', padx=(2, 6))
        self._iw_auto_var = tk.BooleanVar(value=(st.iw_mode != 'manual'))
        ttk.Checkbutton(iw_row, text=('自動偵測' if LANG == 'zh' else 'Auto-detect'), variable=self._iw_auto_var,
                        command=self._on_iw_auto_toggle).pack(side='left', padx=(0, 10))
        self._iw_vars: Dict[str, tk.BooleanVar] = {}
        self._iw_checks: Dict[str, ttk.Checkbutton] = {}
        for _iw in ALL_IW:
            v = tk.BooleanVar(value=(_iw in (st.iw_list or ALL_IW)))
            cb = ttk.Checkbutton(iw_row, text=_iw, variable=v)
            cb.pack(side='left', padx=2)
            self._iw_vars[_iw] = v
            self._iw_checks[_iw] = cb
        ttk.Label(iw_row, text=('（手動：取消「自動偵測」後勾選要處理的子帶）' if LANG == 'zh' else '(Manual: uncheck "Auto-detect" then select subswaths to process)'),
                  foreground='#888').pack(side='left', padx=(8, 0))
        self._on_iw_auto_toggle()   # 依目前模式啟用/停用勾選框

        self._refresh_aoi()

        # ── Processing options ────────────────────────────────────────────
        of = ttk.LabelFrame(f, text=_T('lf_processing'))
        of.pack(fill='x', padx=8, pady=4)

        # Polarisation is always VV for Sentinel-1 IW
        ttk.Label(of, text='Polarisation').grid(row=0, column=0, sticky='w', padx=6, pady=2)
        self._pol_var = tk.StringVar(value='VV')
        ttk.Label(of, text=('VV（自動）' if LANG == 'zh' else 'VV (auto)'), foreground='#226').grid(
            row=0, column=1, sticky='w')

        # Subswaths: auto-detected from SLC scan, no manual selection needed
        ttk.Label(of, text='Subswaths').grid(row=0, column=2, sticky='w', padx=(16, 4))
        self._iw_detect_lbl = tk.StringVar(value=('⏳ 請先掃描 SLC' if LANG == 'zh' else '⏳ Scan SLC first'))
        ttk.Label(of, textvariable=self._iw_detect_lbl,
                  foreground='#448').grid(row=0, column=3, sticky='w')
        ttk.Label(of, text=('（由 SLC 自動偵測，掃描後更新）' if LANG == 'zh' else '(Auto-detected from SLC, updates after scan)'),
                  foreground='#888').grid(row=0, column=4, sticky='w', padx=4)
        self._detected_iws: List[str] = list(st.iw_list)
        # ESD is handled inside snap2stamps/graphs — no manual toggle needed

        # Row 1: Multilook
        _S1_RG, _S1_AZ = 2.3296, 13.9374  # S1 IW single-look pixel spacings [m]

        ttk.Label(of, text='Multilook').grid(row=1, column=0, sticky='w', padx=6, pady=2)
        ml_frame = ttk.Frame(of)
        ml_frame.grid(row=1, column=1, columnspan=4, sticky='w')
        ttk.Label(ml_frame, text='Rg looks').pack(side='left')
        self._rg_looks_var = tk.IntVar(value=st.rg_looks)
        ttk.Spinbox(ml_frame, from_=1, to=20, width=4,
                    textvariable=self._rg_looks_var).pack(side='left', padx=(2, 8))
        ttk.Label(ml_frame, text='Az looks').pack(side='left')
        self._az_looks_var = tk.IntVar(value=st.az_looks)
        ttk.Spinbox(ml_frame, from_=1, to=20, width=4,
                    textvariable=self._az_looks_var).pack(side='left', padx=(2, 8))

        # Row 2: Smart ML
        ttk.Label(of, text='Smart ML').grid(row=2, column=0, sticky='w', padx=6, pady=2)
        sml_frame = ttk.Frame(of)
        sml_frame.grid(row=2, column=1, columnspan=4, sticky='w')
        ttk.Label(sml_frame, text='n').pack(side='left')
        self._sml_n_var = tk.IntVar(value=st.smart_ml_n)
        ttk.Spinbox(sml_frame, from_=1, to=10, width=4,
                    textvariable=self._sml_n_var).pack(side='left', padx=(2, 8))
        ttk.Label(sml_frame, text='coh_min').pack(side='left')
        self._sml_coh_var = tk.DoubleVar(value=st.smart_ml_coh)
        ttk.Spinbox(sml_frame, from_=0.1, to=0.99, increment=0.05, width=6,
                    textvariable=self._sml_coh_var, format='%.2f').pack(
            side='left', padx=(2, 8))
        ttk.Label(sml_frame, text=('(鄰域 n×n，coh 最大者勝)' if LANG == 'zh' else '(n×n neighborhood, highest coh wins)'),
                  foreground='#666').pack(side='left', padx=4)

        # Live TC pixel spacing display: ML spacing × smart_ml_n
        self._tc_px_lbl = tk.StringVar()

        def _update_tc_px(*_):
            try:
                rg = int(self._rg_looks_var.get())
                az = int(self._az_looks_var.get())
                n  = int(self._sml_n_var.get())
            except (ValueError, tk.TclError):
                return
            ml_rg = rg * _S1_RG
            ml_az = az * _S1_AZ
            tc_px = max(ml_rg, ml_az) * n
            self._tc_px_lbl.set(
                f'ML: {ml_rg:.1f}m(rg)×{ml_az:.1f}m(az)  →  '
                f'SmartML×{n}  →  TC output ≈ {tc_px:.1f}m')

        self._rg_looks_var.trace_add('write', _update_tc_px)
        self._az_looks_var.trace_add('write', _update_tc_px)
        self._sml_n_var.trace_add('write', _update_tc_px)
        _update_tc_px()  # initialise

        # Row 3 (derived, read-only): TC pixel spacing
        ttk.Label(of, text='TC spacing').grid(row=3, column=0, sticky='w', padx=6, pady=2)
        ttk.Label(of, textvariable=self._tc_px_lbl,
                  foreground='#226').grid(row=3, column=1, columnspan=4, sticky='w', padx=6)

        # Row 4: SNAPHU path
        ttk.Label(of, text='SNAPHU').grid(row=4, column=0, sticky='w', padx=6, pady=2)
        snaphu_frame = ttk.Frame(of)
        snaphu_frame.grid(row=4, column=1, columnspan=4, sticky='w')
        # Auto-detect: use which snaphu if saved path doesn't exist
        import shutil as _sh
        _snaphu_init = st.snaphu_path
        if not _sh.which(_snaphu_init):
            _detected = _sh.which('snaphu')
            if _detected:
                _snaphu_init = _detected
        self._snaphu_var = tk.StringVar(value=_snaphu_init)
        ttk.Entry(snaphu_frame, textvariable=self._snaphu_var, width=36).pack(
            side='left', padx=(0, 4))
        ttk.Button(snaphu_frame, text='Check',
                   command=self._check_snaphu).pack(side='left', padx=2)
        ttk.Button(snaphu_frame, text='Browse...',
                   command=self._browse_snaphu).pack(side='left', padx=2)
        self._snaphu_status = tk.StringVar(value='')
        ttk.Label(snaphu_frame, textvariable=self._snaphu_status,
                  foreground='#448').pack(side='left', padx=4)


        # ── ASF Download ─────────────────────────────────────────────────
        # ── SLC Dates ────────────────────────────────────────────────────
        df = ttk.LabelFrame(f, text=_T('lf_slc_dates'))
        df.pack(fill='x', padx=8, pady=4)
        # 第一排：起訖日期 (縮短橫向寬度，把衛星/Scan 移到第二排)
        _drow1 = ttk.Frame(df); _drow1.pack(fill='x')
        ttk.Label(_drow1, text='Start').pack(side='left', padx=(6, 2))
        self._start_var = tk.StringVar(value=st.start_date)
        ttk.Entry(_drow1, textvariable=self._start_var, width=12).pack(side='left')
        ttk.Label(_drow1, text='End').pack(side='left', padx=(8, 2))
        self._end_var = tk.StringVar(value=st.end_date)
        ttk.Entry(_drow1, textvariable=self._end_var, width=12).pack(side='left')
        ttk.Label(_drow1, text='(YYYY-MM-DD)', foreground='#666').pack(side='left', padx=4)
        # 第二排：衛星選擇 + Scan + 結果 (只納入勾選衛星的日期)
        _drow2 = ttk.Frame(df); _drow2.pack(fill='x', pady=(2, 0))
        ttk.Label(_drow2, text=('衛星' if LANG == 'zh' else 'Satellite')).pack(side='left', padx=(6, 2))
        self._sat_vars: Dict[str, tk.BooleanVar] = {}
        for _sat in ('S1A', 'S1B', 'S1C', 'S1D'):
            _v = tk.BooleanVar(value=(_sat in st.satellites))
            self._sat_vars[_sat] = _v
            ttk.Checkbutton(_drow2, text=_sat, variable=_v).pack(side='left')
        ttk.Button(_drow2, text=_T('btn_scan_slc'),
                   command=self._scan_dates).pack(side='left', padx=8)
        self._dates_info = tk.StringVar(value='(not scanned)')
        ttk.Label(_drow2, textvariable=self._dates_info).pack(side='left', padx=6)

        # ── ASF Download ─────────────────────────────────────────────────
        asff = ttk.LabelFrame(f, text=_T('lf_asf'))
        asff.pack(fill='x', padx=8, pady=4)
        ttk.Label(asff, text='Username').grid(row=0, column=0, sticky='w', padx=6, pady=2)
        self._asf_user_var = tk.StringVar(value=st.asf_username)
        ttk.Entry(asff, textvariable=self._asf_user_var, width=24).grid(
            row=0, column=1, sticky='w', padx=2)
        ttk.Label(asff, text='Password').grid(row=0, column=2, sticky='w', padx=(12, 4))
        self._asf_pass_var = tk.StringVar(value=st.asf_password)
        ttk.Entry(asff, textvariable=self._asf_pass_var, show='*', width=24).grid(
            row=0, column=3, sticky='w', padx=2)
        ttk.Label(asff, text='Path No.').grid(row=0, column=4, sticky='w', padx=(12, 4))
        # Auto-detect relative orbit from SLC dir name (e.g. SAR_A69 → 69, SAR_D145 → 145)
        _orbit_default = st.asf_relative_orbit
        if not _orbit_default:
            import re as _re2
            _m = _re2.search(r'[_/]([AD])(\d{1,3})(?:[/_]|$)',
                             self._slc_var.get().replace('\\', '/'))
            if _m:
                _orbit_default = _m.group(2)
        self._asf_orbit_var = tk.StringVar(value=_orbit_default)
        ttk.Entry(asff, textvariable=self._asf_orbit_var, width=6).grid(
            row=0, column=5, sticky='w', padx=2)
        ttk.Label(asff, text='Frame').grid(row=0, column=6, sticky='w', padx=(8, 4))
        self._asf_frame_var = tk.StringVar(value=st.asf_frame)
        ttk.Entry(asff, textvariable=self._asf_frame_var, width=6).grid(
            row=0, column=7, sticky='w', padx=2)

        # Row 1: Download destination path (defaults to SLC folder)
        ttk.Label(asff, text='Path').grid(row=1, column=0, sticky='w', padx=6, pady=2)
        self._asf_path_var = tk.StringVar(value=st.slc_dir)
        ttk.Entry(asff, textvariable=self._asf_path_var, width=52).grid(
            row=1, column=1, columnspan=4, sticky='ew', padx=2)
        ttk.Button(asff, text='…', width=3,
                   command=lambda: self._asf_path_var.set(
                       filedialog.askdirectory() or self._asf_path_var.get())).grid(
            row=1, column=5, padx=4)
        # Keep path in sync when SLC folder changes (unless user overrode it)
        self._slc_var.trace_add('write', lambda *_: self._asf_path_var.set(
            self._slc_var.get()) if self._asf_path_var.get() == self._slc_var.get()
            or not self._asf_path_var.get() else None)

        self._asf_check_btn = ttk.Button(asff, text=_T('btn_check_slc'),
                                          command=self._check_slc_completeness)
        self._asf_check_btn.grid(row=2, column=0, columnspan=3, padx=6, pady=4, sticky='w')
        self._asf_dl_btn = ttk.Button(asff, text=_T('btn_asf_dl'),
                                       command=self._download_missing_slcs)
        self._asf_dl_btn.grid(row=2, column=3, columnspan=3, padx=6, pady=4, sticky='w')
        self._asf_status_var = tk.StringVar(value='')
        ttk.Label(asff, textvariable=self._asf_status_var, wraplength=700,
                  justify='left', foreground='#448').grid(
            row=3, column=0, columnspan=6, sticky='w', padx=6, pady=2)
        asff.columnconfigure(1, weight=1)

        # ── Pair Strategy ────────────────────────────────────────────────
        psf = ttk.LabelFrame(f, text=_T('lf_pair_strategy'))
        psf.pack(fill='both', padx=8, pady=4, expand=True)
        left = ttk.Frame(psf); left.pack(side='left', fill='y', padx=8, pady=4)
        right = ttk.Frame(psf); right.pack(side='right', fill='both', expand=True, padx=8, pady=4)

        self._strategy = tk.StringVar(value=st.pair_strategy)
        for txt, val in [(_T('strategy_nearest_n'), 'nearest_n'),
                          (_T('strategy_grid'), 'grid')]:
            ttk.Radiobutton(left, text=txt, variable=self._strategy, value=val,
                            command=self._refresh_strategy).pack(anchor='w', pady=2)

        ttk.Separator(left, orient='horizontal').pack(fill='x', pady=6)

        nn_f = ttk.Frame(left); nn_f.pack(fill='x')
        ttk.Label(nn_f, text=_T('lbl_n_equals')).pack(side='left')
        self._nn_var = tk.StringVar(value=str(st.nearest_n))
        self._nn_spin = ttk.Spinbox(nn_f, from_=1, to=30, width=5,
                                     textvariable=self._nn_var)
        self._nn_spin.pack(side='left')

        ttk.Label(left, text=_T('lbl_day_intervals')).pack(anchor='w', pady=(6, 2))
        self._day_lb = tk.Listbox(left, selectmode='extended',
                                   height=6, width=10, exportselection=False)
        for d in DAY_INTERVALS_ALL:
            self._day_lb.insert('end', f'{d}d')
        for idx, d in enumerate(DAY_INTERVALS_ALL):
            if d in st.selected_day_intervals:
                self._day_lb.selection_set(idx)
        self._day_lb.pack(fill='y')

        ttk.Button(left, text=_T('btn_compute_pairs'),
                   command=self._compute_pairs).pack(fill='x', pady=(8, 2))
        ttk.Button(left, text=_T('btn_baseline_plot'),
                   command=self._plot_baseline).pack(fill='x', pady=(0, 8))

        ttk.Label(right, text=_T('lbl_pair_preview')).pack(anchor='w')
        self._pair_tree = ttk.Treeview(
            right, columns=('no', 'ref', 'sec', 'dt', 'bperp'), show='headings', height=8)
        for col, hdr, w in [('no', 'No.', 38),
                              ('ref', 'Reference', 95),
                              ('sec', 'Secondary', 95),
                              ('dt', 'Δdays', 52),
                              ('bperp', 'Bperp(m)', 72)]:
            self._pair_tree.heading(col, text=hdr)
            self._pair_tree.column(col, width=w, anchor='center')
        sb = ttk.Scrollbar(right, orient='vertical',
                            command=self._pair_tree.yview)
        self._pair_tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self._pair_tree.pack(fill='both', expand=True)

        self._refresh_strategy()

        # ── Confirm button ───────────────────────────────────────────────
        ttk.Button(f, text=_T('btn_confirm'),
                   command=self._confirm).pack(pady=10)

    # ── helpers ──────────────────────────────────────────────────────────
    def _row_dir(self, parent, row: int, label: str, default: str) -> tk.StringVar:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w',
                                            padx=6, pady=2)
        var = tk.StringVar(value=default)
        ttk.Entry(parent, textvariable=var, width=60).grid(
            row=row, column=1, columnspan=3, sticky='ew', padx=2)
        ttk.Button(parent, text='…', width=3,
                   command=lambda v=var: v.set(
                       filedialog.askdirectory() or v.get())).grid(
            row=row, column=4, padx=4)
        return var

    def _row_file(self, parent, row: int, label: str, default: str,
                  ftypes=None) -> tk.StringVar:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w',
                                            padx=6, pady=2)
        var = tk.StringVar(value=default)
        ttk.Entry(parent, textvariable=var, width=60).grid(
            row=row, column=1, columnspan=3, sticky='ew', padx=2)
        ttk.Button(parent, text='…', width=3,
                   command=lambda v=var: v.set(
                       filedialog.askopenfilename(filetypes=ftypes or []) or v.get())).grid(
            row=row, column=4, padx=4)
        return var

    def _refresh_aoi(self):
        mode = self._aoi_mode.get()
        for w in self._bbox_frame.winfo_children():
            w.configure(state='normal' if mode == 'BBOX' else 'disabled')
        for w in self._wkt_frame.winfo_children():
            w.configure(state='normal' if mode == 'WKT' else 'disabled')

    def _on_iw_auto_toggle(self):
        """自動偵測勾選 → 停用 IW 勾選框並反映偵測結果; 取消 → 開放手動勾選。"""
        auto = self._iw_auto_var.get()
        if auto:
            # 自動模式: 勾選框反映目前偵測到的子帶, 並停用 (僅顯示)。
            # 尚未掃描時 (_detected_iws 未設) 退回已存的 iw_list, 避免覆蓋成全選。
            detected = (getattr(self, '_detected_iws', None)
                        or list(self.app.state.iw_list) or list(ALL_IW))
            for iw, var in self._iw_vars.items():
                var.set(iw in detected)
        for iw, cb in self._iw_checks.items():
            cb.configure(state='disabled' if auto else 'normal')

    def _refresh_strategy(self):
        s = self._strategy.get()
        self._nn_spin.configure(state='normal' if s == 'nearest_n' else 'disabled')
        self._day_lb.configure(state='normal' if s == 'grid' else 'disabled')

    # ── SSD Swap UI helpers ───────────────────────────────────────────────
    def _swap_img(self) -> str:
        return _swap_img_path(self._swap_path_var.get().strip())

    def _sudo_op(self, fn_with_pass) -> Tuple[bool, str]:
        """Try fn_with_pass(sudo_pass). Priority: UI field > env var > popup."""
        ui_pass = self._swap_sudo_var.get().strip()
        sudo_pass = ui_pass or _SUDO_PASS
        ok, msg = fn_with_pass(sudo_pass)
        _need_pw = any(w in msg.lower() for w in
                       ('password', 'sorry', 'a password is required', 'incorrect'))
        if not ok and _need_pw:
            from tkinter import simpledialog
            pwd = simpledialog.askstring(
                ('sudo 密碼' if LANG == 'zh' else 'sudo password'), ('請輸入 sudo 密碼（或在「sudo 密碼」欄預先填寫）：' if LANG == 'zh' else 'Enter sudo password (or pre-fill the "sudo password" field):'),
                show='*', parent=self.app)
            if pwd is None:
                return False, ('[取消] 未輸入密碼' if LANG == 'zh' else '[Cancelled] No password entered')
            ok, msg = fn_with_pass(pwd)
        return ok, msg

    def _refresh_swap_status(self):
        img = self._swap_img()
        active, info = _swap_status(img)
        if active:
            # 比對現有檔大小與設定大小是否相符
            cfg_size = self._swap_size_var.get().strip().upper()
            size_warn = ''
            try:
                actual_gib = Path(img).stat().st_size / (1024 ** 3)
                cfg_val = float(''.join(c for c in cfg_size if c.isdigit() or c == '.'))
                cfg_unit = ''.join(c for c in cfg_size if c.isalpha())
                cfg_gib = cfg_val * {'T': 1024, 'G': 1, 'M': 1/1024}.get(cfg_unit, 1)
                if abs(actual_gib - cfg_gib) > 1:
                    size_warn = (f'  ⚠ 現有 {actual_gib:.0f}G ≠ 設定 {cfg_gib:.0f}G（需重建才能改大小）' if LANG == 'zh' else f'  ⚠ Existing {actual_gib:.0f}G ≠ configured {cfg_gib:.0f}G (rebuild required to resize)')
            except Exception:
                pass
            self._swap_status_var.set((f'✓ 已啟用  {info}{size_warn}  ({img})' if LANG == 'zh' else f'✓ Enabled  {info}{size_warn}  ({img})'))
        elif Path(img).exists():
            self._swap_status_var.set((f'◉ Swapfile 存在但未啟用 ({img})' if LANG == 'zh' else f'◉ Swapfile exists but not enabled ({img})'))
        else:
            self._swap_status_var.set((f'✗ 未建立 ({img})' if LANG == 'zh' else f'✗ Not created ({img})'))

    def _create_swapfile_ui(self):
        img  = self._swap_img()
        size = self._swap_size_var.get().strip() or '100G'
        # 若檔案已存在，詢問是否停用→刪除→重建
        if Path(img).exists():
            active, info = _swap_status(img)
            active_hint = (f'（目前已啟用：{info}）' if LANG == 'zh' else f'(Currently enabled: {info})') if active else ('（目前未啟用）' if LANG == 'zh' else '(Currently not enabled)')
            if not messagebox.askyesno(
                    ('覆蓋確認' if LANG == 'zh' else 'Overwrite confirmation'),
                    (f'Swapfile 已存在{active_hint}：\n  {img}\n\n'
                    f'將執行：停用 → 刪除 → 重建 ({size})\n\n'
                    f'確定覆蓋？' if LANG == 'zh' else f'Swapfile already exists{active_hint}:\n  {img}\n\nWill: disable → delete → rebuild ({size})\n\nConfirm overwrite?'),
                    icon='warning', parent=self.app):
                return
            # 停用（若 active）
            if active:
                self._swap_status_var.set(('停用舊 swap…' if LANG == 'zh' else 'Disabling old swap…'))
                self.update_idletasks()
                ok, msg = self._sudo_op(lambda pw: _disable_swap(img, pw))
                if not ok:
                    self._swap_status_var.set((f'[ERROR] 無法停用：{msg}' if LANG == 'zh' else f'[ERROR] Failed to disable: {msg}'))
                    return
            # 刪除舊檔（root 擁有，需 sudo rm）
            self._swap_status_var.set(('刪除舊 swapfile…' if LANG == 'zh' else 'Deleting old swapfile…'))
            self.update_idletasks()
            def _do_rm(pw, _img=img):
                rc, out = _run_sudo_cmd(['rm', '-f', _img], pw)
                return rc == 0, out
            ok, msg = self._sudo_op(_do_rm)
            if not ok:
                self._swap_status_var.set((f'[ERROR] 無法刪除：{msg}' if LANG == 'zh' else f'[ERROR] Failed to delete: {msg}'))
                return
        self._swap_status_var.set((f'建立 {size} swapfile…' if LANG == 'zh' else f'Creating {size} swapfile…'))
        self.update_idletasks()
        ok, msg = self._sudo_op(lambda pw: _create_swapfile(img, size, pw))
        self._swap_status_var.set(msg)
        if ok:
            self._refresh_swap_status()

    def _enable_swap_ui(self):
        self._swap_status_var.set(('啟用中…' if LANG == 'zh' else 'Enabling…'))
        self.update_idletasks()
        img = self._swap_img()
        ok, msg = self._sudo_op(lambda pw: _enable_swap(img, pw))
        self._swap_status_var.set(msg)
        if ok:
            self._refresh_swap_status()

    def _disable_swap_ui(self):
        self._swap_status_var.set(('停用中…' if LANG == 'zh' else 'Disabling…'))
        self.update_idletasks()
        img = self._swap_img()
        ok, msg = self._sudo_op(lambda pw: _disable_swap(img, pw))
        self._swap_status_var.set(msg)
        if ok:
            self._refresh_swap_status()

    def _err(self, title: str, msg: str):
        """Show error dialog parented to the root window (avoids hidden-behind-main bug)."""
        messagebox.showerror(title, msg, parent=self.app)

    def _warn(self, title: str, msg: str):
        messagebox.showwarning(title, msg, parent=self.app)

    def _info(self, title: str, msg: str):
        messagebox.showinfo(title, msg, parent=self.app)

    def _mkdir_project(self):
        path = self._project_var.get().strip()
        if not path:
            self._warn('Project folder',
                       '請先填入 Project folder 路徑。' if LANG == 'zh'
                       else 'Please enter a Project folder path first.')
            return
        p = Path(path)
        if p.is_dir():
            self._info('Project folder',
                       f'資料夾已存在：\n{p}' if LANG == 'zh'
                       else f'Folder already exists:\n{p}')
            return
        try:
            p.mkdir(parents=True, exist_ok=True)
            self._info('Project folder',
                       f'已建立：\n{p}' if LANG == 'zh'
                       else f'Created:\n{p}')
        except Exception as exc:
            self._err('Project folder',
                      f'建立失敗：\n{exc}' if LANG == 'zh'
                      else f'Failed to create:\n{exc}')

    def _apply_bbox_csv(self):
        """Parse CSV from boundingbox.klokantech.com and fill lonmin/latmin/lonmax/latmax.

        Expected format: lon_min,lat_min,lon_max,lat_max  (4 comma-separated floats)
        Example: 120.9,24.7,122.0,25.5
        """
        raw = self._bbox_csv_var.get().strip()
        if not raw:
            return
        # strip surrounding brackets/quotes if present
        raw = raw.strip('"\'[]')
        parts = [p.strip() for p in raw.split(',')]
        if len(parts) != 4:
            messagebox.showerror(('CSV 格式錯誤' if LANG == 'zh' else 'CSV format error'),
                                 (f'需要 4 個數字（lon_min,lat_min,lon_max,lat_max），'
                                 f'收到 {len(parts)} 個：\n{raw}' if LANG == 'zh' else f'Requires 4 numbers (lon_min,lat_min,lon_max,lat_max), got {len(parts)}:\n{raw}'),
                                 parent=self.app)
            return
        try:
            lon_min, lat_min, lon_max, lat_max = [float(p) for p in parts]
        except ValueError:
            messagebox.showerror(('CSV 格式錯誤' if LANG == 'zh' else 'CSV format error'),
                                 (f'無法解析為數字：\n{raw}' if LANG == 'zh' else f'Cannot parse as numbers:\n{raw}'),
                                 parent=self.app)
            return
        # basic sanity check
        if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
            messagebox.showerror(('座標範圍錯誤' if LANG == 'zh' else 'Coordinate range error'),
                                 (f'數值不合理：lon {lon_min}–{lon_max}, lat {lat_min}–{lat_max}\n'
                                 f'請確認格式為 lon_min,lat_min,lon_max,lat_max' if LANG == 'zh' else f'Invalid values: lon {lon_min}–{lon_max}, lat {lat_min}–{lat_max}\nPlease confirm the format is lon_min,lat_min,lon_max,lat_max'),
                                 parent=self.app)
            return
        self._lonmin_var.set(str(lon_min))
        self._latmin_var.set(str(lat_min))
        self._lonmax_var.set(str(lon_max))
        self._latmax_var.set(str(lat_max))
        # switch to BBOX mode and clear CSV field
        self._aoi_mode.set('BBOX')
        self._refresh_aoi()
        self._bbox_csv_var.set('')

    def _normalize_wkt(self):
        """Rewrite the WKT entry using _coerce_to_wkt(); show error on bad input."""
        raw = self._wkt_var.get().strip()
        if not raw:
            return
        result = _coerce_to_wkt(raw)
        if result == raw and not raw.upper().startswith('POLYGON'):
            # _coerce_to_wkt returned input unchanged → unparseable
            self._warn(
                'WKT',
                '無法解析座標，請確認格式：\n[lon lat], [lon lat], ...\n或 lon lat, lon lat, ...'
                if LANG == 'zh' else
                'Cannot parse coordinates. Expected:\n[lon lat], [lon lat], ...\nor lon lat, lon lat, ...'
            )
            return
        self._wkt_var.set(result)

    def _check_snaphu(self):
        import shutil as _sh
        path = self._snaphu_var.get().strip() or 'snaphu'
        found = _sh.which(path)
        if found:
            self._snaphu_status.set(f'✓ {found}')
        else:
            self._snaphu_status.set((f'✗ 找不到 "{path}"' if LANG == 'zh' else f'✗ Not found "{path}"'))

    def _browse_snaphu(self):
        from tkinter import filedialog
        current = self._snaphu_var.get().strip()
        init_dir = str(Path(current).parent) if current and Path(current).parent.is_dir() else '/'
        chosen = filedialog.askopenfilename(
            title='選擇 snaphu 執行檔' if LANG == 'zh' else 'Select snaphu executable',
            initialdir=init_dir,
            filetypes=[('Executable', '*'), ('All files', '*.*')],
        )
        if chosen:
            self._snaphu_var.set(chosen)
            self._check_snaphu()

    def _auto_download_dem(self):
        """Download GLO-30 DEM as GeoTIFF for SNAP, based on current BBOX AOI."""
        if self._aoi_mode.get() != 'BBOX':
            self._err('DEM', ('自動下載 DEM 目前僅支援 BBOX 模式。\n請切換至 BBOX 並填入座標。' if LANG == 'zh' else 'Auto-download DEM currently only supports BBOX mode.\nPlease switch to BBOX and fill in coordinates.'))
            return
        try:
            lon_min = float(self._lonmin_var.get())
            lat_min = float(self._latmin_var.get())
            lon_max = float(self._lonmax_var.get())
            lat_max = float(self._latmax_var.get())
        except ValueError:
            self._err('AOI', ('請先填入有效的 BBOX 座標再下載 DEM。' if LANG == 'zh' else 'Please fill in valid BBOX coordinates before downloading DEM.'))
            return

        dem_dir = str(Path(__file__).parent / 'DEM')
        self._dem_dl_btn.configure(state='disabled', text=('⏳ 下載中...' if LANG == 'zh' else '⏳ Downloading...'))
        self.app.update_idletasks()

        def _work():
            result = download_snap_dem(lon_min, lat_min, lon_max, lat_max, dem_dir)
            def _done():
                self._dem_dl_btn.configure(state='normal', text=_T('btn_auto_dem'))
                if result:
                    self._extdem_var.set(result)
                    self._info(('DEM 下載完成' if LANG == 'zh' else 'DEM download complete'), (f'DEM 已儲存:\n{result}\n已自動填入 External DEM 欄位。' if LANG == 'zh' else f'DEM saved:\n{result}\nAuto-filled into the External DEM field.'))
                else:
                    self._err(('DEM 下載失敗' if LANG == 'zh' else 'DEM download failed'),
                               ('無法取得 GLO-30 DEM，請確認：\n'
                               '  1. 網路可連線至 AWS S3\n'
                               '  2. GDAL/osgeo 已安裝\n'
                               '  3. AOI 座標正確\n'
                               '或手動選取 DEM 檔案。' if LANG == 'zh' else 'Failed to fetch GLO-30 DEM, please check:\n  1. Network can reach AWS S3\n  2. GDAL/osgeo is installed\n  3. AOI coordinates are correct\nOr select the DEM file manually.'))
            self.app.after(0, _done)

        threading.Thread(target=_work, daemon=True).start()

    def _check_slc_completeness(self):
        if not self._aoi_latlon_valid():
            return
        slc = self._slc_var.get().strip()
        if not slc or not Path(slc).is_dir():
            self._err('SLC', ('請先設定有效的 SLC 資料夾。' if LANG == 'zh' else 'Please set a valid SLC folder first.'))
            return
        dates = self.app.state.available_dates
        if not dates:
            self._err('Dates', ('請先按「Scan SLC dir → dates」取得日期清單。' if LANG == 'zh' else 'Please click "Scan SLC dir → dates" first to get the date list.'))
            return
        self._asf_check_btn.configure(state='disabled', text=('⏳ 檢查中...' if LANG == 'zh' else '⏳ Checking...'))
        self._asf_status_var.set(('⏳ 本地 SLC 完整性檢查中...' if LANG == 'zh' else '⏳ Checking local SLC integrity...'))
        self.app.update_idletasks()

        self.collect_state()    # sync slc/lat widget values → state
        st    = self.app.state
        start = st.start_date
        end   = st.end_date
        wkt   = st.wkt_polygon()
        frame_str = self._asf_frame_var.get().strip()
        frame = int(frame_str) if frame_str.isdigit() else None
        orbit_str = self._asf_orbit_var.get().strip()
        relative_orbit = int(orbit_str) if orbit_str.isdigit() else None
        try:
            _clat_min = float(st.latmin)
            _clat_max = float(st.latmax)
        except (TypeError, ValueError):
            _clat_min = _clat_max = None

        def _set(msg: str):
            self.app.after(0, self._asf_status_var.set, msg)

        def _work():
            # ── Step 1: local completeness (cross-frame-aware) ────────────
            status    = check_slc_completeness(slc, dates,
                                               lat_min=_clat_min,
                                               lat_max=_clat_max)
            ok_local  = [d for d, s in status.items() if s == 'ok']
            offtrack  = [(d, s) for d, s in status.items() if 'off-track' in s]
            xframe    = [(d, s) for d, s in status.items() if 'cross-frame' in s]
            # 「有問題」只計真缺/損壞：排除 ok / off-track / cross-frame
            real_bad  = [(d, s) for d, s in status.items()
                         if s != 'ok' and 'off-track' not in s
                         and 'cross-frame' not in s]

            lines = [(f'[本地] ✓ {len(ok_local)}/{len(dates)} SLC 正常' if LANG == 'zh' else f'[Local] ✓ {len(ok_local)}/{len(dates)} SLC OK')]
            if offtrack:
                lines.append((f'[本地] ℹ {len(offtrack)} 筆影像不覆蓋 AOI（正常跳過，非缺漏）:' if LANG == 'zh' else f'[Local] ℹ {len(offtrack)} scene(s) do not cover AOI (normally skipped, not missing):'))
                for d, reason in offtrack:
                    lines.append(f'  {d}: {reason}')
            if xframe:
                lines.append((f'[本地] ⚠ 跨 frame 不完整 {len(xframe)} 筆:' if LANG == 'zh' else f'[Local] ⚠ Cross-frame incomplete {len(xframe)} scene(s):'))
                for d, reason in xframe:
                    lines.append(f'  {d}: {reason}')
            if real_bad:
                lines.append((f'[本地] ✗ 有問題 {len(real_bad)} 筆:' if LANG == 'zh' else f'[Local] ✗ Problem {len(real_bad)} scene(s):'))
                for d, reason in real_bad:
                    lines.append(f'  {d}: {reason}')

            # ── Step 2: query ASF ─────────────────────────────────────────
            asf_dates: Optional[List[str]] = None
            asf_log: List[str] = []          # 收集 query_asf_dates 內部訊息/例外
            if start and end:
                lines.append(('\n[ASF] ⏳ 正在查詢 ASF 資料庫...' if LANG == 'zh' else '\n[ASF] ⏳ Querying ASF database...'))
                _set('\n'.join(lines))
                asf_dates = query_asf_dates(
                    start, end, wkt, frame=frame,
                    relative_orbit=relative_orbit,
                    log_fn=lambda m: asf_log.append(m.rstrip()))

            asf_missing: List[str] = []
            need_download: List[str] = []
            if asf_dates is not None:
                local_all = set(d for d in status)      # dates found by scan
                candidates = [d for d in asf_dates if d not in local_all]
                lines.append((f'[ASF] 資料庫共 {len(asf_dates)} 筆場景' if LANG == 'zh' else f'[ASF] Database has {len(asf_dates)} scene(s)'))

                if candidates:
                    # ── Step 3: deep-check each "ASF-only" date in SLC dir ─
                    lines.append((f'[ASF] scan 未收錄 {len(candidates)} 筆，逐一確認本地...' if LANG == 'zh' else f'[ASF] scan did not include {len(candidates)} scene(s), verifying locally one by one...'))
                    _set('\n'.join(lines))

                    p_slc = Path(slc)
                    all_items = list(p_slc.iterdir())
                    safe_ok_dates:    List[str] = []
                    zip_ok_dates:     List[str] = []
                    incomplete_dates: List[str] = []   # exists but invalid
                    truly_missing:    List[str] = []

                    for d in sorted(candidates):
                        iw_items = [f for f in all_items
                                    if d in f.name and '_IW_' in f.name
                                    and (f.suffix in ('.zip', '.SAFE') or f.name.endswith('.SAFE'))]
                        if not iw_items:
                            truly_missing.append(d)
                            continue

                        # Validate each item; collect best result
                        best_ok   = False
                        reasons   = []
                        for item in sorted(iw_items):
                            ok, reason = validate_slc(str(item))
                            if ok:
                                best_ok = True
                                if item.suffix == '.SAFE' or item.name.endswith('.SAFE'):
                                    safe_ok_dates.append(d)
                                else:
                                    zip_ok_dates.append(d)
                                break
                            reasons.append(f'{item.name}: {reason}')
                        if not best_ok:
                            incomplete_dates.append(d)
                            lines.append((f'  [不完整] {d}: {" | ".join(reasons)}' if LANG == 'zh' else f'  [Incomplete] {d}: {" | ".join(reasons)}'))

                    # Summary
                    if safe_ok_dates:
                        lines.append((f'[ASF] ✓ SAFE 完整（可直接使用）: {len(safe_ok_dates)} 筆' if LANG == 'zh' else f'[ASF] ✓ SAFE complete (ready to use): {len(safe_ok_dates)} scene(s)'))
                    if zip_ok_dates:
                        lines.append((f'[ASF] ✓ ZIP 完整（可直接使用）: {len(zip_ok_dates)} 筆' if LANG == 'zh' else f'[ASF] ✓ ZIP complete (ready to use): {len(zip_ok_dates)} scene(s)'))
                    if incomplete_dates:
                        lines.append((f'[ASF] ⚠ 檔案不完整需重下: {len(incomplete_dates)} 筆: '
                                     f'{", ".join(incomplete_dates[:5])}' if LANG == 'zh' else f'[ASF] ⚠ File incomplete, needs re-download: {len(incomplete_dates)} scene(s): {", ".join(incomplete_dates[:5])}')
                                     + (f' …' if len(incomplete_dates) > 5 else ''))
                    if truly_missing:
                        missing_str = ', '.join(truly_missing[:6])
                        if len(truly_missing) > 6:
                            missing_str += (f' …（共 {len(truly_missing)} 筆）' if LANG == 'zh' else f' …({len(truly_missing)} total)')
                        lines.append((f'[ASF] ✗ 完全缺漏需下載: {len(truly_missing)} 筆: {missing_str}' if LANG == 'zh' else f'[ASF] ✗ Completely missing, needs download: {len(truly_missing)} scene(s): {missing_str}'))

                    need_download = truly_missing + incomplete_dates
                    if not need_download:
                        lines.append(('[ASF] ✓ 所有場景均已齊全（含 SAFE/ZIP），無需下載' if LANG == 'zh' else '[ASF] ✓ All scenes are complete (SAFE/ZIP included), no download needed'))
                    asf_missing = need_download
                else:
                    lines.append(('[ASF] ✓ 本地與 ASF 一致，無缺漏' if LANG == 'zh' else '[ASF] ✓ Local matches ASF, nothing missing'))
            elif start and end:
                lines.append(('[ASF] ⚠ 查詢失敗（請確認 asf-search 套件或網路連線）' if LANG == 'zh' else '[ASF] ⚠ Query failed (check asf-search package or network connection)'))
                # 顯示 query_asf_dates 內部捕捉到的真正例外 (避免被吞掉、無從診斷)
                detail = next((m for m in reversed(asf_log)
                               if '失敗' in m or 'fail' in m.lower()
                               or 'rror' in m.lower()), '')
                if not detail and asf_log:
                    detail = asf_log[-1]
                if detail:
                    lines.append((f'    ↳ 細節: {detail}' if LANG == 'zh' else f'    ↳ Detail: {detail}'))

            _set('\n'.join(lines))
            self.app.after(0, self._asf_check_btn.configure,
                           {'state': 'normal', 'text': ('[?] 檢查 SLC 完整性' if LANG == 'zh' else '[?] Check SLC integrity')})

            # ── Step 4: prompt only for truly missing / corrupt scenes ────
            if asf_missing:
                self.app.after(0, self._prompt_download_missing, asf_missing)

        threading.Thread(target=_work, daemon=True).start()

    def _prompt_download_missing(self, missing: List[str]):
        """Popup: ask whether to download ASF-missing scenes."""
        preview = ', '.join(missing[:6])
        if len(missing) > 6:
            preview += (f'\n  …（共 {len(missing)} 筆）' if LANG == 'zh' else f'\n  …({len(missing)} total)')
        ans = messagebox.askquestion(
            ('比對 ASF 資料庫' if LANG == 'zh' else 'Compare with ASF database'),
            (f'ASF 資料庫有 {len(missing)} 筆場景在本地缺漏：\n\n'
            f'  {preview}\n\n'
            '是否從 ASF 網站補齊缺漏 SLC？' if LANG == 'zh' else f'ASF database has {len(missing)} scene(s) missing locally:\n\n  {preview}\n\nDownload missing SLC from the ASF website?'),
            icon='question')
        if ans == 'yes':
            self._download_missing_slcs(dates_override=missing)

    def _download_missing_slcs(self, dates_override: Optional[List[str]] = None):
        """Download missing SLCs from ASF.

        dates_override: explicit list of YYYYMMDD to download (from ASF comparison).
        If None, re-checks local completeness and downloads anything not 'ok'.
        """
        slc = self._asf_path_var.get().strip() or self._slc_var.get().strip()
        user = self._asf_user_var.get().strip()
        pwd  = self._asf_pass_var.get().strip()
        if not slc:
            self._err('SLC', ('請先設定 ASF 下載路徑（Path 欄位）。' if LANG == 'zh' else 'Please set the ASF download path (Path field) first.'))
            return
        Path(slc).mkdir(parents=True, exist_ok=True)
        if not user or not pwd:
            self._err('ASF', ('請先填入 Earthdata username / password。' if LANG == 'zh' else 'Please fill in Earthdata username / password first.'))
            return

        frame_str = self._asf_frame_var.get().strip()
        frame = int(frame_str) if frame_str.isdigit() else None
        orbit_str = self._asf_orbit_var.get().strip()
        relative_orbit = int(orbit_str) if orbit_str.isdigit() else None

        self._asf_dl_btn.configure(state='disabled', text=('⏳ 下載中...' if LANG == 'zh' else '⏳ Downloading...'))
        self._asf_status_var.set(('⏳ 準備中...' if LANG == 'zh' else '⏳ Preparing...'))
        self.app.update_idletasks()

        def _restore():
            self.app.after(0, self._asf_dl_btn.configure,
                           {'state': 'normal', 'text': ('補ASF SLC' if LANG == 'zh' else 'Fetch ASF SLC')})

        def _update(msg: str):
            self.app.after(0, self._asf_status_var.set, msg)

        def _work():
            # ── Step 0: ensure asf_search is installed ───────────────────
            try:
                import asf_search  # noqa: F401
            except ImportError:
                _update(('⏳ asf_search 未安裝，正在安裝 pip install asf-search …' if LANG == 'zh' else '⏳ asf_search not installed, installing via pip install asf-search …'))
                ok, out = _pip_install('asf-search')
                if not ok:
                    _update((f'✗ 安裝失敗:\n{out[-300:]}' if LANG == 'zh' else f'✗ Install failed:\n{out[-300:]}'))
                    _restore()
                    return
                _update(('✓ asf_search 安裝完成，繼續下載…' if LANG == 'zh' else '✓ asf_search installed, continuing download…'))

            # ── Step 1: decide which dates to download ────────────────────
            if dates_override is not None:
                missing = list(dates_override)
            else:
                dates = self.app.state.available_dates
                if not dates:
                    _update(('⚠ 尚未取得日期清單，請先 Scan SLC dir。' if LANG == 'zh' else '⚠ Date list not available yet, please Scan SLC dir first.'))
                    _restore()
                    return
                status  = check_slc_completeness(slc, dates)
                missing = [d for d, s in status.items() if s != 'ok']

            if not missing:
                _update(('✓ 所有 SLC 完整，無需下載' if LANG == 'zh' else '✓ All SLC complete, no download needed'))
                _restore()
                return

            # ── Step 2: download each missing date ────────────────────────
            masked_pwd = ('*' * max(0, len(pwd) - 2) + pwd[-2:]) if len(pwd) > 2 else '***'
            logs: List[str] = [
                (f'補下 {len(missing)} 筆: {missing}' if LANG == 'zh' else f'Fetching {len(missing)} missing: {missing}'),
                f'[auth] user={user}  pwd={masked_pwd}',
            ]
            for d in missing:
                path = download_slc_from_asf(
                    d, slc, user, pwd, frame=frame,
                    log_fn=lambda t, _l=logs: _l.append(t.rstrip()))
                logs.append(f'{"✓" if path else "✗"} {d}: {path or "failed"}')
                _update('\n'.join(logs[-6:]))

            _update('\n'.join(logs[-10:]))
            _restore()

        threading.Thread(target=_work, daemon=True).start()

    def _on_slc_dir_change(self, *_):
        """Auto-trigger _scan_dates when SLC folder is set to a valid directory."""
        slc = self._slc_var.get().strip()
        if slc and Path(slc).is_dir():
            # Schedule via after() so the trace completes before scan starts
            self.app.after(100, self._scan_dates)

    def _aoi_latlon_valid(self) -> bool:
        """Validate the BBOX lat/lon entry fields are within physical range.

        Catches typos like latmin=233.33 (extra digit) that otherwise pass
        float() but silently break every downstream step: aoi_lat lands far
        outside any orbit's latitude band → compute_bperp returns None →
        baseline plots flat at 0; find_slcs_covering_lat_range finds no overlap
        → every date reported 'missing' (0/N). Fail loudly here instead.

        Returns True when:
          - AOI mode is not BBOX (WKT/none → lat/lon boxes unused), or
          - any box is blank (existing blank-handling paths apply), or
          - all four values parse and satisfy -90<=lat<=90, -180<=lon<=180,
            latmin<latmax, lonmin<lonmax.
        Otherwise pops an error dialog and returns False.
        """
        if self._aoi_mode.get() != 'BBOX':
            return True
        raw = {'lonmin': self._lonmin_var.get().strip(),
               'latmin': self._latmin_var.get().strip(),
               'lonmax': self._lonmax_var.get().strip(),
               'latmax': self._latmax_var.get().strip()}
        if any(v == '' for v in raw.values()):
            return True  # blank → let existing empty-field handling proceed
        try:
            vals = {k: float(v) for k, v in raw.items()}
        except ValueError:
            self._err('AOI', (f'AOI 經緯度需為數字，目前: {raw}' if LANG == 'zh' else f'AOI lat/lon must be numbers, currently: {raw}'))
            return False
        errs = []
        if not -90.0 <= vals['latmin'] <= 90.0:
            errs.append((f"latmin={vals['latmin']} 超出 [-90, 90]（是否多打一位數？）" if LANG == 'zh' else f"latmin={vals['latmin']} out of range [-90, 90] (extra digit typed?)"))
        if not -90.0 <= vals['latmax'] <= 90.0:
            errs.append((f"latmax={vals['latmax']} 超出 [-90, 90]" if LANG == 'zh' else f"latmax={vals['latmax']} out of range [-90, 90]"))
        if not -180.0 <= vals['lonmin'] <= 180.0:
            errs.append((f"lonmin={vals['lonmin']} 超出 [-180, 180]" if LANG == 'zh' else f"lonmin={vals['lonmin']} out of range [-180, 180]"))
        if not -180.0 <= vals['lonmax'] <= 180.0:
            errs.append((f"lonmax={vals['lonmax']} 超出 [-180, 180]" if LANG == 'zh' else f"lonmax={vals['lonmax']} out of range [-180, 180]"))
        if not errs and vals['latmin'] >= vals['latmax']:
            errs.append((f"latmin({vals['latmin']}) 必須 < latmax({vals['latmax']})" if LANG == 'zh' else f"latmin({vals['latmin']}) must be < latmax({vals['latmax']})"))
        if not errs and vals['lonmin'] >= vals['lonmax']:
            errs.append((f"lonmin({vals['lonmin']}) 必須 < lonmax({vals['lonmax']})" if LANG == 'zh' else f"lonmin({vals['lonmin']}) must be < lonmax({vals['lonmax']})"))
        if errs:
            self._err(('AOI 範圍錯誤' if LANG == 'zh' else 'AOI range error'), ('請修正 AOI 邊界：\n  ' if LANG == 'zh' else 'Please fix the AOI bounds:\n  ') + '\n  '.join(errs))
            return False
        return True

    def _scan_dates(self):
        if not self._aoi_latlon_valid():
            return
        try:
            slc = self._slc_var.get().strip()
            if not slc:
                self._dates_info.set(('⚠ SLC 資料夾未設定' if LANG == 'zh' else '⚠ SLC folder not set'))
                self._err('SLC dir', ('請先在上方設定 SLC folder 路徑。' if LANG == 'zh' else 'Please set the SLC folder path above first.'))
                return
            if not Path(slc).is_dir():
                self._dates_info.set((f'⚠ 路徑不存在: {slc}' if LANG == 'zh' else f'⚠ Path does not exist: {slc}'))
                self._err('SLC dir', (f'找不到資料夾:\n{slc}' if LANG == 'zh' else f'Folder not found:\n{slc}'))
                return

            self._dates_info.set(('⏳ 掃描中（驗證 IW SLC）...' if LANG == 'zh' else '⏳ Scanning (validating IW SLC)...'))
            self.app.update_idletasks()

            # 讀使用者勾選的衛星，存入 state 並用於篩選
            sats = [s for s, v in self._sat_vars.items() if v.get()]
            if not sats:
                sats = ['S1A', 'S1B', 'S1C', 'S1D']
            self.app.state.satellites = sats
            # scan_safe_dates now returns only dates with valid IW SLC
            all_valid = scan_safe_dates(slc, satellites=sats)

            # Also collect raw IW dates (before validity filter) to show excluded
            p_slc = Path(slc)
            raw_iw: set = set()
            for item in p_slc.iterdir():
                if '_IW_' not in item.name:
                    continue
                m2 = SAFE_DATE_RE.search(item.name)
                if m2:
                    raw_iw.add(m2.group(1))
            excluded = sorted(raw_iw - set(all_valid))

            # 依 AOI 排除 off-track 日期：影像緯度範圍完全不覆蓋 AOI 的日期
            # 不納入配對清單（檔案仍保留於 SLC DATABASE，供其他 AOI/軌道專案使用）。
            # off-track ≠ 無效檔，故在 excluded 計算之後才剔除；
            # cross-frame 部分覆蓋的日期 find_slcs_covering_lat_range 會回非空 → 保留。
            offtrack_excluded: List[str] = []
            try:
                _amin = float(self._latmin_var.get())
                _amax = float(self._latmax_var.get())
            except (TypeError, ValueError):
                _amin = _amax = None
            if _amin is not None and _amax is not None and _amax > _amin:
                _kept = []
                for _d in all_valid:
                    if find_slcs_covering_lat_range(slc, _d, _amin, _amax):
                        _kept.append(_d)
                    else:
                        offtrack_excluded.append(_d)
                all_valid = _kept

            if not all_valid:
                msg = ('⚠ 找不到有效的 IW SLC' if LANG == 'zh' else '⚠ No valid IW SLC found')
                if excluded:
                    msg += (f'（找到 IW 檔但無效: {excluded}）' if LANG == 'zh' else f'(Found IW file(s) but invalid: {excluded})')
                self._dates_info.set(msg)
                self._warn('Scan', (f'在 {slc} 中找不到可用的 IW SLC。\n'
                           '請確認 .SAFE 或 .zip 資料完整。' if LANG == 'zh' else f'No usable IW SLC found in {slc}.\nPlease confirm .SAFE or .zip data is complete.'))
                return

            try:
                dates = filter_dates(all_valid,
                                     self._start_var.get(), self._end_var.get())
            except ValueError as e:
                self._dates_info.set((f'⚠ 日期格式錯誤: {e}' if LANG == 'zh' else f'⚠ Date format error: {e}'))
                self._err('Date format', str(e))
                return

            self.app.state.available_dates = dates
            excl_note = (f'  已排除無效: {excluded}' if LANG == 'zh' else f'  Excluded invalid: {excluded}') if excluded else ''
            if offtrack_excluded:
                excl_note += ((f'  已排除 off-track(不覆蓋AOI,非缺漏,'
                              f'檔案保留): {offtrack_excluded}' if LANG == 'zh' else f'  Excluded off-track (does not cover AOI, not missing, file kept): {offtrack_excluded}'))
            if dates:
                self._dates_info.set(
                    (f'✓ {len(dates)} 筆有效 IW 日期  '
                    f'({dates[0]} … {dates[-1]}){excl_note}' if LANG == 'zh' else f'✓ {len(dates)} valid IW date(s)  ({dates[0]} … {dates[-1]}){excl_note}'))
            else:
                self._dates_info.set(
                    (f'⚠ 篩選後無日期 (原始 {len(all_valid)} 筆){excl_note}' if LANG == 'zh' else f'⚠ No dates after filtering (original {len(all_valid)}){excl_note}'))

            # Auto-detect subswaths using the same AOI that will be used for
            # processing (WKT extent when aoi_mode == 'WKT', bbox otherwise)
            try:
                lon_min, lat_min, lon_max, lat_max = self.app.state.aoi_bbox()
                detected = detect_iws_from_slc(slc, lon_min, lat_min, lon_max, lat_max)
            except (ValueError, AttributeError):
                detected = detect_iws_from_slc(slc)
            self._detected_iws = detected
            self._iw_detect_lbl.set(f'✓ {", ".join(detected)}')
            # 自動模式下，讓 AOI 區的 IW 勾選框反映最新偵測結果 (手動模式不動)
            if getattr(self, '_iw_auto_var', None) is not None \
                    and self._iw_auto_var.get():
                self._on_iw_auto_toggle()

        except Exception as exc:
            self._dates_info.set((f'⚠ 錯誤: {exc}' if LANG == 'zh' else f'⚠ Error: {exc}'))
            self._err('Scan error', str(exc))

    def _auto_detect_on_load(self):
        """On opening a project that already has processing: show the in-range
        IW scene count (checked satellites) and tick the day-intervals already
        processed — union with the saved selection (computed off the UI thread)."""
        st = self.app.state
        slc, start, end = st.slc_dir, st.start_date, st.end_date
        sats = list(st.satellites) if st.satellites else None
        proj = st.project_dir

        def work():
            try:
                valid = scan_safe_dates(slc, sats) if slc else []
                dates = filter_dates(valid, start, end) if valid else []
            except Exception:
                dates = []
            proc_ivs = processed_day_intervals(scan_processed_pairs(proj))

            def apply():
                if dates:
                    self._dates_info.set(
                        (f'✓ {len(dates)} 筆有效 IW 日期 '
                        f'({dates[0]} … {dates[-1]})' if LANG == 'zh' else f'✓ {len(dates)} valid IW date(s) ({dates[0]} … {dates[-1]})'))
                want = set(self.app.state.selected_day_intervals) | proc_ivs
                for idx, d in enumerate(DAY_INTERVALS_ALL):
                    if d in want:
                        self._day_lb.selection_set(idx)
            self.app.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _compute_pairs(self):
        try:
            dates = self.app.state.available_dates
            if not dates:
                self._warn('Dates', ('請先按「Scan SLC dir → dates」取得日期清單。' if LANG == 'zh' else 'Please click "Scan SLC dir → dates" first to get the date list.'))
                return

            s = self._strategy.get()
            if s == 'nearest_n':
                try:
                    n = int(self._nn_var.get())
                except ValueError:
                    n = 3
                pairs = pairs_nearest_n(dates, n)
            else:
                sel = [DAY_INTERVALS_ALL[i]
                       for i in self._day_lb.curselection()]
                if not sel:
                    self._warn('Day grid', ('請在 Day intervals 清單中選至少一個間隔。' if LANG == 'zh' else 'Please select at least one interval in the Day intervals list.'))
                    return
                pairs = pairs_grid(dates, sel)

            # 把專案資料夾內「已完成且兩端都在目前日期範圍」的干涉對成果納入網路
            # → 時間/空間範圍與上次一致時沿用既有成果 (計入完成、不重做)。
            #   時間一致: 兩端日期都在目前 available_dates; 空間一致: 同一 project_dir
            #   (產物在本專案 interferograms/ 內); 並尊重使用者的排除清單。
            st = self.app.state
            excl_d = set(st.excluded_dates)
            excl_p = set(st.excluded_pairs)
            in_dates = set(dates)
            try:
                proc = scan_processed_pairs(st.project_dir)
            except Exception:
                proc = []
            existing = set(pairs)
            added_done = 0
            for r, s2 in proc:
                if (r, s2) in existing:
                    continue
                if (r in excl_d or s2 in excl_d or f'{r}_{s2}' in excl_p):
                    continue
                if r in in_dates and s2 in in_dates:
                    pairs.append((r, s2))
                    existing.add((r, s2))
                    added_done += 1
            pairs = sorted(pairs, key=lambda p: (p[0], p[1]))

            self.app.state.pairs = pairs
            for row in self._pair_tree.get_children():
                self._pair_tree.delete(row)
            iid_list = []
            for i, (ref, sec) in enumerate(pairs, 1):
                iid = self._pair_tree.insert('', 'end',
                                              values=(i, ref, sec,
                                                      delta_days(ref, sec), '...'))
                iid_list.append((iid, ref, sec))

            # Fill Bperp in background so UI stays responsive
            self._fill_bperp_async(iid_list)

            if pairs:
                extra = ((f'\n其中已納入專案內既有完成成果 {added_done} 對'
                         f'（範圍一致，沿用不重做）。' if LANG == 'zh' else f'\nOf which {added_done} pair(s) reuse existing completed results in the project (same range, not redone).') if added_done else '')
                self._info('Pairs', (f'計算完成：{len(pairs)} 組 pair。{extra}\n'
                           f'請確認右側 pair 清單後點「✓ 確認 → Tab 2」。' if LANG == 'zh' else f'Calculation complete: {len(pairs)} pair(s).{extra}\nPlease confirm the pair list on the right, then click "✓ Confirm → Tab 2".'))
            else:
                self._warn('Pairs', ('產生 0 組 pair。\n'
                           '日期數量不足或選取的 day-interval 無符合。' if LANG == 'zh' else 'Generated 0 pairs.\nNot enough dates, or no day-interval selection matched.'))

        except Exception as exc:
            self._err('Compute error', str(exc))

    def _rebuild_pair_tree(self):
        """Rebuild _pair_tree from st.pairs with sequential No., then async Bperp."""
        for row in self._pair_tree.get_children():
            self._pair_tree.delete(row)
        iid_list = []
        for i, (ref, sec) in enumerate(self.app.state.pairs, 1):
            iid = self._pair_tree.insert('', 'end',
                                          values=(i, ref, sec,
                                                  delta_days(ref, sec), '...'))
            iid_list.append((iid, ref, sec))
        self._fill_bperp_async(iid_list)

    def _fill_bperp_async(self, iid_list):
        """Compute Bperp for each pair in a background thread, updating tree rows."""
        self.collect_state()          # sync widget values → state before reading slc_dir/lat
        st = self.app.state
        try:
            aoi_lat = (float(st.latmin) + float(st.latmax)) / 2.0
        except (TypeError, ValueError):
            aoi_lat = 23.83

        slc_dir = st.slc_dir
        tree = self._pair_tree

        def worker():
            slc_cache: dict = {}

            def get_slc(date):
                if date not in slc_cache:
                    slc_cache[date] = find_slc_for_date(slc_dir, date, aoi_lat)
                return slc_cache[date]

            all_dates = sorted(set(d for _, r, s in iid_list for d in (r, s)))
            for d in all_dates:
                get_slc(d)

            # Determine reference date (most connected)
            degree: dict = {}
            for _, ref, sec in iid_list:
                degree[ref] = degree.get(ref, 0) + 1
                degree[sec] = degree.get(sec, 0) + 1
            ref_date = max(degree, key=degree.get) if degree else None
            ref_slc = get_slc(ref_date) if ref_date else None

            for iid, ref, sec in iid_list:
                try:
                    r_slc = get_slc(ref)
                    s_slc = get_slc(sec)
                    if r_slc and s_slc:
                        bp = compute_bperp(r_slc, s_slc, aoi_lat=aoi_lat)
                        val = f'{bp:.0f}' if bp is not None else 'N/A'
                    else:
                        val = 'N/A'
                except Exception:
                    val = 'N/A'
                try:
                    tree.set(iid, 'bperp', val)
                except Exception:
                    pass

        import threading
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def _baseline_cache_paths(self):
        """Return (png_path, json_path) for the baseline cache files."""
        proj = Path(self.app.state.project_dir)
        name = proj.name
        return proj / f'{name}_baseline_network.png', proj / f'{name}_baseline_network_cache.json'

    def _baseline_cache_valid(self) -> bool:
        """Return True if the cached PNG matches the current settings fingerprint."""
        png, jsn = self._baseline_cache_paths()
        if not png.exists() or not jsn.exists():
            return False
        try:
            saved = json.loads(jsn.read_text(encoding='utf-8'))
            return saved == self.app.state.baseline_fingerprint()
        except Exception:
            return False

    def _plot_baseline(self, _from_cache_check: bool = False):
        if not _from_cache_check and not self._aoi_latlon_valid():
            return
        if not _from_cache_check:
            self.collect_state()      # sync widget values → state before reading slc_dir/lat
        st = self.app.state
        if not st.pairs:
            if not _from_cache_check:
                self._warn(('基線圖' if LANG == 'zh' else 'Baseline plot'), ('請先按「↻ Compute pairs」產生 pair 清單。' if LANG == 'zh' else 'Please click "↻ Compute pairs" first to generate the pair list.'))
            return
        try:
            aoi_lat = None
            try:
                aoi_lat = (float(st.latmin) + float(st.latmax)) / 2.0
            except (TypeError, ValueError):
                pass

            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

            # ── Fast path: load from cache if settings unchanged ──────────
            use_cache = self._baseline_cache_valid()
            png_path, _ = self._baseline_cache_paths()

            win = tk.Toplevel(self)
            win.title('InSAR Baseline Network')
            win.geometry('1100x740')

            # ── figure area ──────────────────────────────────────────────
            canvas_frame = tk.Frame(win)
            canvas_frame.pack(fill='both', expand=True)

            _state = {'fig': None}

            def _redraw(augment=True):
                """Compute baseline network in background, embed figure on main thread.

                augment=True (初次開圖): 自動橋接斷裂網路 + 納入磁碟上新到的影像。
                augment=False (手動新增/移除/篩選後): 只顯示 st.pairs 原樣，
                  不可再自動擴充，否則會把剛移除/篩掉的日期當「新影像」加回來。
                """
                for w in canvas_frame.winfo_children():
                    w.destroy()

                # Recompute cache validity each call (pairs may have changed)
                _use_cache = self._baseline_cache_valid()
                _png_c, _jsn_c = self._baseline_cache_paths()

                def _show_err(msg):
                    for w in canvas_frame.winfo_children():
                        w.destroy()
                    tk.Label(canvas_frame,
                             text=(f'⚠ 繪圖失敗：\n{msg}' if LANG == 'zh' else f'⚠ Plot failed:\n{msg}'),
                             fg='red', wraplength=700,
                             font=('TkDefaultFont', 10)).pack(expand=True, padx=20)

                def _embed(new_fig, new_edges=None, bridge_new=None):
                    # Clear old figure BEFORE destroying widgets so that
                    # fig.clf() can still reach the live toolbar buttons.
                    # (Destroying first → toolbar buttons are gone →
                    #  clf() raises TclError when trying to update them.)
                    if _state['fig'] is not None:
                        try:
                            _state['fig'].clf()
                        except Exception:
                            pass
                    # 註銷舊登記 (重畫前)
                    if _state.get('bl_token') is not None:
                        self.app.unregister_baseline(_state['bl_token'])
                        _state['bl_token'] = None
                    for w in canvas_frame.winfo_children():
                        w.destroy()
                    _state['fig'] = new_fig
                    cv = FigureCanvasTkAgg(new_fig, master=canvas_frame)
                    cv.draw()
                    tb = NavigationToolbar2Tk(cv, canvas_frame)
                    tb.update()
                    cv.get_tk_widget().pack(fill='both', expand=True)
                    # 登記此彈窗基線圖 → 叢集完成一對時即時改色; 並套用目前完成狀態
                    if new_edges:
                        _state['bl_token'] = self.app.register_baseline(cv, new_edges)
                        try:
                            for r, s in scan_processed_pairs(st.project_dir):
                                self.app.notify_pair_state(r, s, 'done')
                        except Exception:
                            pass

                    # ── auto-bridge: sync new pairs to state + Tab 1 + Tab 2 ──────
                    if bridge_new:
                        existing_keys = set(f'{r}_{s}' for r, s in st.pairs)
                        actually_new  = [(r, s) for r, s in bridge_new
                                         if f'{r}_{s}' not in existing_keys]
                        if actually_new:
                            st.pairs.extend(actually_new)
                            self._rebuild_pair_tree()
                            self.app.tab2.load_pairs()
                        # notification bar (bridge pairs + new-image pairs)
                        msg = ((f'🔗 自動納入 {len(bridge_new)} 對（橋接／新影像）：  ' if LANG == 'zh' else f'🔗 Auto-included {len(bridge_new)} pair(s) (bridge / new image):  ')
                               + '   '.join(f'{r} ↔ {s}'
                                            for r, s in bridge_new[:6]))
                        if len(bridge_new) > 6:
                            msg += (f'  … 等共 {len(bridge_new)} 對' if LANG == 'zh' else f'  … {len(bridge_new)} pair(s) total')
                        bridge_var.set(msg)
                        if not _state.get('bridge_bar_visible'):
                            bridge_bar.pack(fill='x', before=ctrl)
                            _state['bridge_bar_visible'] = True
                    else:
                        bridge_var.set('')
                        if _state.get('bridge_bar_visible'):
                            bridge_bar.pack_forget()
                            _state['bridge_bar_visible'] = False

                    # Auto-save cache after fresh computation
                    if not _use_cache:
                        try:
                            new_fig.savefig(str(_png_c), dpi=150, bbox_inches='tight')
                            _jsn_c.write_text(
                                json.dumps(self.app.state.baseline_fingerprint(), indent=2),
                                encoding='utf-8')
                        except Exception:
                            pass

                # Always render a live Figure (FigureCanvasTkAgg) so the window
                # can be freely resized to any width/height ratio without the
                # distortion a stretched cached bitmap would introduce. The
                # cached PNG is still kept on disk as a saved artifact, but is
                # no longer used for on-screen display.
                # Compute in background thread
                tk.Label(canvas_frame,
                         text=('計算中，請稍候…' if LANG == 'zh' else 'Calculating, please wait…'),
                         font=('TkDefaultFont', 12)).pack(expand=True)

                pairs_snap = list(st.pairs)   # snapshot
                proj_snap  = st.project_dir   # for processed-pair lookup

                def _compute():
                    try:
                        # 自動擴充只在初次開圖 (augment=True) 進行；手動編輯後
                        # 必須顯示 st.pairs 原樣，否則剛移除/篩掉的日期會被當
                        # 「新影像」重新配對加回來 (使用者回報的 bug)。
                        if augment:
                            # ── auto-bridge: 重連斷裂的子網路 ──
                            bridge_new = find_bridge_pairs(pairs_snap)
                            # ── 納入磁碟上新到的影像 (範圍內、勾選的衛星) ──
                            try:
                                _valid = scan_safe_dates(st.slc_dir,
                                                         st.satellites or None)
                                _avail = (filter_dates(_valid, st.start_date, st.end_date)
                                          if _valid else [])
                            except Exception:
                                _avail = []
                            newimg = compute_new_image_pairs(
                                pairs_snap, _avail, st.pair_strategy,
                                st.selected_day_intervals, st.nearest_n)
                            # 排除使用者手動移除的日期/對 → 不被當「新影像」加回
                            _exd = set(st.excluded_dates)
                            _exp = set(st.excluded_pairs)

                            def _not_excluded(p):
                                return (p[0] not in _exd and p[1] not in _exd
                                        and f'{p[0]}_{p[1]}' not in _exp)
                            # merge auto-added pairs (bridge + new images), de-duped
                            added = []
                            _seen = set()
                            for p in list(bridge_new) + list(newimg):
                                if p not in _seen and _not_excluded(p):
                                    _seen.add(p); added.append(p)
                        else:
                            added = []
                        display_pairs = pairs_snap + added
                        # Already-processed pairs are coloured distinctly (green)
                        processed = scan_processed_pairs(proj_snap)
                        new_fig, new_edges = plot_baseline_network(
                            display_pairs, st.slc_dir, aoi_lat=aoi_lat,
                            title=f'{Path(st.project_dir).name} InSAR Network '
                                  f'({len(display_pairs)} pairs)',
                            processed_pairs=processed, return_edges=True)
                        canvas_frame.after(
                            0, lambda: _embed(new_fig, new_edges, added))
                    except Exception as exc:
                        err_msg = str(exc)
                        canvas_frame.after(0, lambda: _show_err(err_msg))

                import threading
                threading.Thread(target=_compute, daemon=True).start()

            # ── bridge notification bar (hidden until auto-bridge fires) ──
            bridge_var = tk.StringVar(value='')
            bridge_bar = tk.Label(win, textvariable=bridge_var,
                                  fg='#1a6b1a', bg='#e8f5e9',
                                  anchor='w', padx=8,
                                  font=('TkFixedFont', 9))
            # Not packed yet; _embed will pack it before ctrl when needed.

            # ── bottom control bar ───────────────────────────────────────
            ctrl = tk.Frame(win, bd=1, relief='groove')
            ctrl.pack(fill='x', padx=6, pady=4)

            # --- 增加干涉對 ---
            add_frm = ttk.LabelFrame(ctrl, text=('增加干涉對' if LANG == 'zh' else 'Add interferogram pair'))
            add_frm.pack(side='left', padx=(4, 8), pady=4)

            tk.Label(add_frm,
                     text=('格式: 20250101-20250113  20250201-20250213' if LANG == 'zh' else 'Format: 20250101-20250113  20250201-20250213'),
                     font=('TkFixedFont', 8)).pack(anchor='w', padx=4)
            add_entry = ttk.Entry(add_frm, width=48)
            add_entry.pack(side='left', padx=4, pady=(0, 4))

            def _add_pairs():
                raw = add_entry.get().strip()
                if not raw:
                    return
                new_pairs = []
                invalid_toks = []
                for tok in raw.replace(',', ' ').split():
                    parts = tok.split('-')
                    if len(parts) == 2:
                        try:
                            datetime.strptime(parts[0], '%Y%m%d')
                            datetime.strptime(parts[1], '%Y%m%d')
                            new_pairs.append((parts[0], parts[1]))
                        except ValueError:
                            invalid_toks.append(tok)
                    else:
                        invalid_toks.append(tok)
                if invalid_toks:
                    messagebox.showwarning(('日期格式錯誤' if LANG == 'zh' else 'Date format error'),
                        ('以下 token 不是合法日期（需 yyyymmdd-yyyymmdd）：\n' if LANG == 'zh' else 'The following tokens are not valid dates (need yyyymmdd-yyyymmdd):\n')
                        + '  ' + '\n  '.join(invalid_toks),
                        parent=win)
                    return
                if not new_pairs:
                    messagebox.showwarning(('格式錯誤' if LANG == 'zh' else 'Format error'),
                        ('請用「yyyymmdd-yyyymmdd」格式，多對以空格分隔。\n'
                        '例如: 20250101-20250113 20250201-20250213' if LANG == 'zh' else 'Use the "yyyymmdd-yyyymmdd" format, separate multiple pairs with spaces.\nExample: 20250101-20250113 20250201-20250213'),
                        parent=win)
                    return
                existing = set(f'{r}_{s}' for r, s in st.pairs)
                # Deduplicate within the input batch AND against existing pairs
                _batch_seen: set = set()
                added = []
                for r, s in new_pairs:
                    k = f'{r}_{s}'
                    if k not in existing and k not in _batch_seen:
                        _batch_seen.add(k)
                        added.append((r, s))
                if not added:
                    messagebox.showinfo(('提示' if LANG == 'zh' else 'Note'), ('所有輸入的對都已存在清單中。' if LANG == 'zh' else 'All entered pairs already exist in the list.'),
                                        parent=win)
                    return
                st.pairs.extend(added)
                # 手動加回 → 解除這些對/日期的排除 (否則下次擴充又被濾掉)
                _akeys = {f'{r}_{s}' for r, s in added}
                _adates = {d for r, s in added for d in (r, s)}
                st.excluded_pairs = [k for k in st.excluded_pairs if k not in _akeys]
                st.excluded_dates = [d for d in st.excluded_dates if d not in _adates]
                self.app.save_prefs()   # 立即存檔 → 新增/解除排除即刻持久
                # update pair preview tree (renumber all)
                self._rebuild_pair_tree()
                self.app.tab2.load_pairs()
                add_entry.delete(0, 'end')
                _redraw(augment=False)   # 手動編輯後不可自動擴充

            ttk.Button(add_frm, text=('確認新增' if LANG == 'zh' else 'Confirm add'), command=_add_pairs).pack(
                side='left', padx=4, pady=(0, 4))

            # --- 移除某幾天 ---
            rm_frm = ttk.LabelFrame(ctrl, text=('移除某幾天' if LANG == 'zh' else 'Remove specific days'))
            rm_frm.pack(side='left', padx=(0, 8), pady=4)

            tk.Label(rm_frm, text=('日期 (yyyymmdd，空格分隔):' if LANG == 'zh' else 'Dates (yyyymmdd, space-separated):'),
                     font=('TkFixedFont', 8)).pack(anchor='w', padx=4)
            rm_entry = ttk.Entry(rm_frm, width=30)
            rm_entry.pack(side='left', padx=4, pady=(0, 4))

            def _remove_dates():
                tokens = rm_entry.get().split()
                bad = [t for t in tokens if len(t) != 8 or not t.isdigit()]
                if not tokens:
                    return
                if bad:
                    messagebox.showwarning(('格式錯誤' if LANG == 'zh' else 'Format error'),
                        (f'以下日期格式不正確（需 8 位數字）：\n{", ".join(bad)}' if LANG == 'zh' else f'The following dates are invalid (need 8 digits):\n{", ".join(bad)}'),
                        parent=win)
                    return
                dates_set = set(tokens)
                # 記錄排除 → 跨重畫/重開 GUI 都不再被自動擴充當新影像加回
                for d in dates_set:
                    if d not in st.excluded_dates:
                        st.excluded_dates.append(d)
                before = len(st.pairs)
                st.pairs = [(r, s) for r, s in st.pairs
                            if r not in dates_set and s not in dates_set]
                removed = before - len(st.pairs)
                self.app.save_prefs()   # 立即存檔 → 排除不怕 GUI 被 kill 而遺失
                if removed == 0:
                    messagebox.showinfo(('提示' if LANG == 'zh' else 'Note'),
                        (f'這些日期已排除（清單中原本就沒有相關干涉對）。' if LANG == 'zh' else f'These dates were already excluded (no related pairs existed in the list).'),
                        parent=win)
                    return
                self._rebuild_pair_tree()
                self.app.tab2.load_pairs()
                rm_entry.delete(0, 'end')
                messagebox.showinfo(('完成' if LANG == 'zh' else 'Done'),
                    (f'已移除 {removed} 對（含 {", ".join(sorted(dates_set))}）。' if LANG == 'zh' else f'Removed {removed} pair(s) (including {", ".join(sorted(dates_set))}).'),
                    parent=win)
                _redraw(augment=False)   # 手動移除後不可自動把該日期當新影像加回

            ttk.Button(rm_frm, text=('確認移除' if LANG == 'zh' else 'Confirm remove'), command=_remove_dates).pack(
                side='left', padx=4, pady=(0, 4))

            # --- Bperp 門檻篩選 ---
            thr_frm = ttk.LabelFrame(ctrl, text=('Bperp 門檻篩選' if LANG == 'zh' else 'Bperp threshold filter'))
            thr_frm.pack(side='left', padx=(0, 8), pady=4)

            tk.Label(thr_frm, text=('最大 Bperp (m) — 預設=現行最大值:' if LANG == 'zh' else 'Max Bperp (m) — default = current max:'),
                     font=('TkFixedFont', 8)).pack(anchor='w', padx=4)
            thr_entry = ttk.Entry(thr_frm, width=8)
            thr_entry.insert(0, '...')   # placeholder until current max is computed
            thr_entry.pack(side='left', padx=4, pady=(0, 4))

            # Default threshold = current network's max |Bperp|, so nothing is
            # removed until the user tightens it (then 確認篩選 filters).
            def _set_thr(val):
                if thr_entry.get().strip() in ('...', ''):
                    thr_entry.delete(0, 'end')
                    thr_entry.insert(0, val)

            def _max_from_tree():
                """Max |Bperp| already computed in the Tab-1 pair tree, or None
                if the tree is not fully populated yet. Main-thread only
                (tkinter is not thread-safe)."""
                tree = self._pair_tree
                children = tree.get_children()
                vals, pending = [], 0
                for iid in children:
                    raw = str(tree.set(iid, 'bperp')).strip()
                    if raw in ('...', ''):
                        pending += 1
                        continue
                    try:
                        vals.append(abs(float(raw)))   # skips 'N/A'
                    except ValueError:
                        pass
                if children and pending == 0 and vals:
                    return max(vals)
                return None

            try:
                _mx_tree = _max_from_tree()
            except Exception:
                _mx_tree = None

            if _mx_tree is not None:
                # Reuse the already-computed Bperp values — instant.
                _set_thr(f'{_mx_tree:.0f}')
            else:
                # Tree not ready — recompute from orbit state vectors in a
                # background thread to keep the dialog responsive.
                def _init_thr_default():
                    _aoi = aoi_lat if aoi_lat is not None else 23.83
                    cache: dict = {}

                    def _g(d):
                        if d not in cache:
                            cache[d] = find_slc_for_date(st.slc_dir, d, _aoi)
                        return cache[d]

                    mx = 0.0
                    for ref, sec in list(st.pairs):
                        r_slc, s_slc = _g(ref), _g(sec)
                        if r_slc and s_slc:
                            bp = compute_bperp(r_slc, s_slc, aoi_lat=_aoi)
                            if bp is not None:
                                mx = max(mx, abs(bp))
                    val = f'{mx:.0f}' if mx > 0 else '200'
                    try:
                        win.after(0, lambda: _set_thr(val))
                    except Exception:
                        pass

                threading.Thread(target=_init_thr_default, daemon=True).start()

            def _filter_bperp():
                try:
                    threshold = float(thr_entry.get().strip())
                except ValueError:
                    messagebox.showwarning(('格式錯誤' if LANG == 'zh' else 'Format error'),
                        ('請輸入數字，例如: 200' if LANG == 'zh' else 'Please enter a number, e.g. 200'), parent=win)
                    return
                if threshold <= 0:
                    messagebox.showwarning(('數值錯誤' if LANG == 'zh' else 'Value error'),
                        ('門檻值必須大於 0。' if LANG == 'zh' else 'Threshold must be greater than 0.'), parent=win)
                    return

                # Compute Bperp for every pair (ref→sec, not relative to network ref)
                _aoi = aoi_lat if aoi_lat is not None else 23.83
                slc_cache: dict = {}

                def _get_slc(d):
                    if d not in slc_cache:
                        slc_cache[d] = find_slc_for_date(st.slc_dir, d, _aoi)
                    return slc_cache[d]

                kept, removed = [], []
                for ref, sec in st.pairs:
                    r_slc = _get_slc(ref)
                    s_slc = _get_slc(sec)
                    bp = compute_bperp(r_slc, s_slc, aoi_lat=_aoi) if (r_slc and s_slc) else None
                    if bp is not None and abs(bp) > threshold:
                        removed.append((ref, sec, bp))
                    else:
                        kept.append((ref, sec))

                if not removed:
                    messagebox.showinfo(('篩選結果' if LANG == 'zh' else 'Filter result'),
                        (f'所有 {len(st.pairs)} 對的 |Bperp| ≤ {threshold:.0f} m，\n無需移除。' if LANG == 'zh' else f'All {len(st.pairs)} pair(s) have |Bperp| ≤ {threshold:.0f} m,\nno removal needed.'),
                        parent=win)
                    return

                msg = ((f'將移除 {len(removed)} 對（|Bperp| > {threshold:.0f} m）：\n\n' if LANG == 'zh' else f'Will remove {len(removed)} pair(s) (|Bperp| > {threshold:.0f} m):\n\n')
                       + '\n'.join(f'  {r}_{s}  {bp:+.0f} m' for r, s, bp in removed[:20])
                       + ((f'\n  …共 {len(removed)} 對' if LANG == 'zh' else f'\n  …and {len(removed)} more pairs') if len(removed) > 20 else '')
                       + (f'\n\n保留 {len(kept)} 對。確定篩選？' if LANG == 'zh' else f'\n\nKeep {len(kept)} pairs. Confirm filter?'))
                if not messagebox.askyesno(('確認篩選' if LANG == 'zh' else 'Confirm Filter'), msg, parent=win):
                    return

                # 記錄被篩掉的對 → 跨重畫/重開都不再被自動擴充重新生成
                for r, s, _bp in removed:
                    key = f'{r}_{s}'
                    if key not in st.excluded_pairs:
                        st.excluded_pairs.append(key)
                st.pairs = kept
                self.app.save_prefs()   # 立即存檔 → 篩選結果即刻持久
                self._rebuild_pair_tree()
                self.app.tab2.load_pairs()
                _redraw(augment=False)   # 篩選後不可自動把被篩掉的對重新生成
                messagebox.showinfo(('完成' if LANG == 'zh' else 'Done'),
                    (f'已移除 {len(removed)} 對，保留 {len(kept)} 對。' if LANG == 'zh' else f'Removed {len(removed)} pairs, kept {len(kept)} pairs.'), parent=win)

            ttk.Button(thr_frm, text=('確認篩選' if LANG == 'zh' else 'Confirm Filter'), command=_filter_bperp).pack(
                side='left', padx=4, pady=(0, 4))

            # --- 儲存 ---
            def _save():
                if _state['fig'] is None:
                    return
                from tkinter import filedialog
                fp = filedialog.asksaveasfilename(
                    defaultextension='.png',
                    filetypes=[('PNG', '*.png'), ('PDF', '*.pdf'), ('SVG', '*.svg')],
                    initialfile=f'{Path(st.project_dir).name}_baseline_network.png',
                    parent=win)
                if fp:
                    _state['fig'].savefig(fp, dpi=150, bbox_inches='tight')
                    messagebox.showinfo(('儲存' if LANG == 'zh' else 'Saved'), (f'已儲存：{fp}' if LANG == 'zh' else f'Saved: {fp}'), parent=win)

            ttk.Button(ctrl, text=('[S] 儲存圖片' if LANG == 'zh' else '[S] Save Image'), command=_save).pack(
                side='right', padx=8, pady=4)

            def _on_win_close():
                # 註銷基線圖登記 (關窗後不再被完成事件廣播)
                if _state.get('bl_token') is not None:
                    self.app.unregister_baseline(_state['bl_token'])
                    _state['bl_token'] = None
                if _state['fig'] is not None:
                    try:
                        _state['fig'].clf()
                    except Exception:
                        pass
                win.destroy()

            win.protocol('WM_DELETE_WINDOW', _on_win_close)

            _redraw()

        except Exception as exc:
            self._err(('基線圖錯誤' if LANG == 'zh' else 'Baseline Plot Error'), str(exc))

    def collect_state(self):
        """Sync all Tab 1 widget values → app.state (called on Confirm and on close)."""
        st = self.app.state
        st.project_dir    = self._project_var.get().strip()
        st.slc_dir        = self._slc_var.get().strip()
        st.ext_dem        = self._extdem_var.get().strip()
        st.snap_dir       = self._snap_var.get().strip()
        st.cpu            = self._cpu_var.get().strip()
        st.cache          = self._cache_var.get().strip()
        st.xmx            = self._xmx_var.get().strip()
        st.ssd_swap_path  = self._swap_path_var.get().strip()
        st.ssd_swap_size  = self._swap_size_var.get().strip()
        st.ssd_swap_auto  = self._swap_auto_var.get()
        st.aoi_mode       = self._aoi_mode.get()
        st.lonmin         = self._lonmin_var.get()
        st.latmin         = self._latmin_var.get()
        st.lonmax         = self._lonmax_var.get()
        st.latmax         = self._latmax_var.get()
        st.wkt            = self._wkt_var.get()
        st.polarisation   = 'VV'
        st.do_esd         = True
        # IW: 自動 → 用偵測結果; 手動 → 用使用者勾選 (至少一個, 否則退回 ALL_IW)
        if self._iw_auto_var.get():
            st.iw_mode = 'auto'
            st.iw_list = self._detected_iws or list(ALL_IW)
        else:
            st.iw_mode = 'manual'
            sel = [iw for iw in ALL_IW if self._iw_vars[iw].get()]
            st.iw_list = sel or list(ALL_IW)
        st.start_date     = self._start_var.get()
        st.end_date       = self._end_var.get()
        st.pair_strategy  = self._strategy.get()
        # Persist the user's day-interval listbox selection so it is restored
        # next session (only when non-empty — e.g. grid mode active).
        sel_days = [DAY_INTERVALS_ALL[i] for i in self._day_lb.curselection()]
        if sel_days:
            st.selected_day_intervals = sel_days
        # Persist the checked satellites (only the ticked ones are used).
        sats = [s for s, v in self._sat_vars.items() if v.get()]
        if sats:
            st.satellites = sats
        try:
            st.nearest_n  = int(self._nn_var.get())
        except (ValueError, tk.TclError):
            pass
        try:
            st.rg_looks   = int(self._rg_looks_var.get())
        except (ValueError, tk.TclError):
            st.rg_looks   = 6
        try:
            st.az_looks   = int(self._az_looks_var.get())
        except (ValueError, tk.TclError):
            st.az_looks   = 1
        try:
            st.smart_ml_n = int(self._sml_n_var.get())
        except (ValueError, tk.TclError):
            st.smart_ml_n = 2
        try:
            st.smart_ml_coh = float(self._sml_coh_var.get())
        except (ValueError, tk.TclError):
            st.smart_ml_coh = 0.6
        st.snaphu_path    = self._snaphu_var.get().strip() or 'snaphu'
        st.asf_username        = self._asf_user_var.get().strip()
        st.asf_password        = self._asf_pass_var.get().strip()
        st.asf_frame           = self._asf_frame_var.get().strip()
        st.asf_relative_orbit  = self._asf_orbit_var.get().strip()

    def _confirm(self):
        st = self.app.state
        if not st.pairs:
            self._err('Pairs', ('請先按「↻ Compute pairs」產生 pair 清單。' if LANG == 'zh' else 'Please click "↻ Compute pairs" first to generate the pair list.'))
            return
        _proj_path = Path(self._project_var.get())
        if not _proj_path.is_dir():
            try:
                _proj_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._err('Project', (f'無法建立 Project 資料夾:\n{_proj_path}\n{exc}' if LANG == 'zh' else f'Failed to create Project folder:\n{_proj_path}\n{exc}'))
                return
        if not Path(self._snap_var.get()).is_dir():
            self._err('SNAP', (f'SNAP 安裝目錄找不到:\n{self._snap_var.get()}' if LANG == 'zh' else f'SNAP installation directory not found:\n{self._snap_var.get()}'))
            return
        if not Path(_GRAPHS_DIR).is_dir():
            self._err('Graphs', (f'Graphs 資料夾不存在:\n{_GRAPHS_DIR}\n'
                      '請確認 snap2stamps/graphs/ 在 GUI 腳本旁邊。' if LANG == 'zh' else f'Graphs folder does not exist:\n{_GRAPHS_DIR}\nPlease make sure snap2stamps/graphs/ is next to the GUI script.'))
            return

        self.collect_state()
        self.app.save_prefs()
        self.app.notebook.tab(1, state='normal')
        self.app.notebook.select(1)
        self.app.tab2.load_pairs(check_resume=True)

    def _check_project_ready(self):
        """🔍 按鈕：檢查 Project folder 是否已有完整輸出，詢問下一步。"""
        proj = self._project_var.get().strip()
        if not proj:
            messagebox.showwarning('Project folder', ('請先填入 Project folder 路徑。' if LANG == 'zh' else 'Please enter the Project folder path first.'))
            return
        p = Path(proj)

        # Count finished pairs (has *_unw_tc.dim)
        ifg_dir = p / 'interferograms'
        done_pairs = []
        if ifg_dir.is_dir():
            for d in sorted(ifg_dir.iterdir()):
                if d.is_dir() and list(d.glob('*_unw_tc.dim')):
                    done_pairs.append(d.name)

        mp_cfg = p / 'mintpy' / 'S1_smallbaseline.cfg'

        if not done_pairs:
            messagebox.showinfo(
                ('[?] 檢查結果' if LANG == 'zh' else '[?] Check Result'),
                (f'路徑：{proj}\n\n'
                '尚未找到完整干涉對輸出（*_unw_tc.dim）。\n'
                '請從 Tab 1 設定參數後執行 SNAP pipeline。' if LANG == 'zh' else f'Path: {proj}\n\nNo complete interferogram pair output found yet (*_unw_tc.dim).\nPlease set parameters in Tab 1 and run the SNAP pipeline.'))
            return

        has_cfg = mp_cfg.exists()
        msg = ((f'路徑：{proj}\n\n'
               f'✅  已完成干涉對：{len(done_pairs)} 對\n'
               f'    第一對：{done_pairs[0]}\n'
               f'    最後一對：{done_pairs[-1]}\n\n'
               f'MintPy cfg：{"✅ 存在" if has_cfg else "⚠️ 尚未產生"}\n\n'
               '要直接進入 MintPy 流程嗎？\n'
               '（選「否」從 Tab 1 重新跑 SNAP pipeline）' if LANG == 'zh' else f'Path: {proj}\n\n✅  Completed pairs: {len(done_pairs)}\n    First pair: {done_pairs[0]}\n    Last pair: {done_pairs[-1]}\n\nMintPy cfg: {"✅ exists" if has_cfg else "⚠️ not generated yet"}\n\nGo straight to the MintPy workflow?\n(Choose "No" to rerun the SNAP pipeline from Tab 1)'))

        ans = messagebox.askquestion(('[?] 檢查結果' if LANG == 'zh' else '[?] Check Result'), msg, icon='question')
        if ans == 'yes':
            # 把 project_dir 同步到 state，解鎖 Tab 3，跳過去
            self.app.state.project_dir = proj
            self.app.notebook.tab(2, state='normal')
            self.app.tab3.init_cfg()
            self.app.notebook.select(2)


# ─────────────────────────────────────────────────────────────────────────
# Tab 2 — Run (SNAP/GPT)
# ─────────────────────────────────────────────────────────────────────────
def _read_ssh_hosts() -> List[str]:
    """Parse ~/.ssh/config and return Host aliases (skipping wildcards)."""
    cfg = Path.home() / '.ssh' / 'config'
    if not cfg.exists():
        return []
    hosts: List[str] = []
    for line in cfg.read_text(errors='replace').splitlines():
        stripped = line.strip()
        if stripped.lower().startswith('host ') and not stripped.startswith('#'):
            for tok in stripped[5:].split():
                if '*' not in tok and '?' not in tok and tok not in hosts:
                    hosts.append(tok)
    return hosts


# 基線圖線段狀態配色 (所有基線視窗共用): 綠完成/紅失敗/橘處理中/灰未做
_BASELINE_EDGE_COLOR = {'done': '#00cc44', 'error': '#ff4444',
                        'running': '#ff8c00', 'cluster': '#cccccc',
                        'pending': '#cccccc'}


class RunFrame(ttk.Frame):
    def __init__(self, nb: ttk.Notebook, app: 'Snap2MintPyApp'):
        super().__init__(nb)
        self.app = app
        self._workers: List[SnapPairWorker] = []
        self._stop_ev = threading.Event()
        self._current_idx = 0
        # Cluster mode state
        self._cluster_mode: bool = False
        self._cluster_chunks: Dict[str, list] = {}   # label → pairs list
        self._cluster_done: int = 0
        self._cluster_total: int = 0
        self._machine_logs: Dict[str, tk.Text] = {}
        self._cluster_resume_dismissed: bool = False  # once dismissed, skip for rest of session
        self._force: bool = False  # 「重跑全部」時忽略既有完整輸出
        self._run_cutoff_ts: float = 0.0  # force 重跑開跑時間; 進度只算此後重做完成的對
        self._host_procs: list = []          # 追蹤本機/ssh client 程序 (關閉時終止)
        self._remote_hosts_running: set = set()  # 正在跑的遠端主機 (關閉時 pkill)
        # Work-stealing: store dispatch context so idle hosts can claim leftover pairs
        self._cluster_host_ssh: Dict[str, Optional[str]] = {}
        self._cluster_config_path: str = ''
        self._cluster_worker_script: str = ''
        self._cluster_finished_hosts: set = set()   # hosts that have called _apply_host_marks
        self._steal_counts: Dict[str, int] = {}     # pair_key → steal attempt count
        # 基線網路即時分頁 (執行時每完成一對就改該線段顏色)
        self._baseline_frame: Optional[ttk.Frame] = None
        self._baseline_canvas = None
        self._baseline_edges: Dict[Tuple[str, str], object] = {}
        self._baseline_token = None   # app._live_baselines 登記憑證
        self._build()

    def _build(self):
        # summary
        sf = ttk.LabelFrame(self, text=_T('lf_summary'))
        sf.pack(fill='x', padx=8, pady=6)
        self._summary_var = tk.StringVar(value='—')
        ttk.Label(sf, textvariable=self._summary_var, justify='left').pack(
            anchor='w', padx=6, pady=4)

        # buttons
        btn = ttk.Frame(self); btn.pack(fill='x', padx=8, pady=4)
        self._start_btn = ttk.Button(btn, text=_T('btn_start'), command=self._start)
        self._start_btn.pack(side='left', padx=4)
        self._stop_btn = ttk.Button(btn, text=_T('btn_stop'), command=self._stop,
                                     state='disabled')
        self._stop_btn.pack(side='left', padx=4)
        self._progress_lbl = tk.StringVar(value='')
        ttk.Label(btn, textvariable=self._progress_lbl).pack(side='left', padx=12)

        # progress bar
        self._pbar = ttk.Progressbar(self, mode='determinate')
        self._pbar.pack(fill='x', padx=8, pady=2)

        # pair table
        tf = ttk.LabelFrame(self, text=_T('lf_pairs'))
        tf.pack(fill='both', padx=8, pady=4, expand=False)
        self._tree = ttk.Treeview(
            tf, columns=('ref', 'sec', 'dt', 'status', 'dur'),
            show='headings', height=5)
        for col, hdr, w in [('ref', 'Reference', 110), ('sec', 'Secondary', 110),
                              ('dt', 'Δdays', 60), ('status', 'Status', 80),
                              ('dur', 'Duration', 90)]:
            self._tree.heading(col, text=hdr)
            self._tree.column(col, width=w, anchor='center')
        sb = ttk.Scrollbar(tf, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self._tree.pack(fill='both', expand=True)
        self._tree.tag_configure('running', foreground='#aaff55')
        self._tree.tag_configure('done',    foreground='#00cc44')
        self._tree.tag_configure('error',   foreground='#ff4444')
        self._tree.tag_configure('cluster', foreground='#88ccff')

        # ── Cluster section ───────────────────────────────────────────────
        self._build_cluster_section()

        # ── Live log — tabbed (one tab per machine in cluster mode) ───────
        lf = ttk.LabelFrame(self, text=_T('lf_live_log'))
        lf.pack(fill='both', expand=True, padx=8, pady=6)
        self._log_nb = ttk.Notebook(lf)
        self._log_nb.pack(fill='both', expand=True)
        _local_tab = ttk.Frame(self._log_nb)
        _local_label = '本機' if LANG == 'zh' else 'Local'
        self._log_nb.add(_local_tab, text=_local_label)
        self._log = _make_log(_local_tab, height=6)
        self._machine_logs[_local_label] = self._log

    # ── cluster helpers ──────────────────────────────────────────────────
    def _build_cluster_section(self):
        """Build the cluster-mode control section inside RunFrame."""
        lbl_cluster = '叢集運算' if LANG == 'zh' else 'Cluster'
        clf = ttk.LabelFrame(self, text=lbl_cluster)
        clf.pack(fill='x', padx=8, pady=2)

        row0 = ttk.Frame(clf)
        row0.pack(fill='x', padx=4, pady=2)

        self._cluster_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row0,
            text=('啟用叢集' if LANG == 'zh' else 'Enable cluster'),
            variable=self._cluster_var,
            command=self._refresh_cluster_preview,
        ).pack(side='left', padx=(0, 10))

        # Refresh hosts from ~/.ssh/config
        ttk.Button(
            row0,
            text='↻' + (' 更新主機' if LANG == 'zh' else ' Refresh hosts'),
            command=self._reload_cluster_hosts,
        ).pack(side='left', padx=(0, 10))

        # Start cluster button — disabled until "啟用叢集" is checked
        self._cluster_start_btn = ttk.Button(
            row0,
            text=('▶ 執行叢集' if LANG == 'zh' else '▶ Run Cluster'),
            command=self._run_cluster_button,
            state='disabled',
        )
        self._cluster_start_btn.pack(side='left', padx=(0, 10))

        # 失敗分析 + 重跑失敗對 (掃 logs 分類失敗原因；重跑會 skip 已完成)
        ttk.Button(
            row0,
            text=('🔍 分析失敗' if LANG == 'zh' else '🔍 Analyze fails'),
            command=self._analyze_failures,
        ).pack(side='left', padx=(0, 6))
        ttk.Button(
            row0,
            text=('↻ 重跑未完成' if LANG == 'zh' else '↻ Rerun incomplete'),
            command=self._rerun_failed,
        ).pack(side='left', padx=(0, 6))

        self._cluster_host_frame = ttk.Frame(clf)
        self._cluster_host_frame.pack(fill='x', padx=4, pady=2)
        self._cluster_host_vars: Dict[str, tk.BooleanVar] = {}

        self._cluster_preview_var = tk.StringVar(value='')
        ttk.Label(clf, textvariable=self._cluster_preview_var,
                  foreground='gray', justify='left').pack(anchor='w', padx=8, pady=(0, 4))

        # Populate initially
        self._reload_cluster_hosts()

    def _reload_cluster_hosts(self):
        """Re-read ~/.ssh/config and rebuild the host checkbox list."""
        for w in self._cluster_host_frame.winfo_children():
            w.destroy()
        self._cluster_host_vars.clear()

        # 本機 is always first
        local_label = '本機' if LANG == 'zh' else 'Local'
        v = tk.BooleanVar(value=True)
        self._cluster_host_vars[local_label] = v
        ttk.Checkbutton(self._cluster_host_frame, text=local_label,
                        variable=v, command=self._refresh_cluster_preview).pack(
            side='left', padx=(0, 8))

        for host in _read_ssh_hosts():
            v2 = tk.BooleanVar(value=True)
            self._cluster_host_vars[host] = v2
            ttk.Checkbutton(self._cluster_host_frame, text=host,
                            variable=v2, command=self._refresh_cluster_preview).pack(
                side='left', padx=(0, 8))

        self._refresh_cluster_preview()

    def _refresh_cluster_preview(self):
        """Update the pair-distribution preview label and cluster button state."""
        if not self._cluster_var.get():
            self._cluster_preview_var.set('')
            self._cluster_start_btn.configure(state='disabled')
            return
        pairs = getattr(self.app, 'state', None)
        pairs = getattr(pairs, 'pairs', []) if pairs else []
        active = [lbl for lbl, v in self._cluster_host_vars.items() if v.get()]
        if not active:
            self._cluster_preview_var.set(
                '請至少選擇一台主機' if LANG == 'zh' else 'Select ≥ 1 host')
            self._cluster_start_btn.configure(state='disabled')
            return
        n = len(active)
        size, rem = divmod(len(pairs), n)
        parts = [(f'{lbl}: {size + (1 if i < rem else 0)} 對' if LANG == 'zh' else f'{lbl}: {size + (1 if i < rem else 0)} pairs')
                 for i, lbl in enumerate(active)]
        self._cluster_preview_var.set(
            ('分配: ' if LANG == 'zh' else 'Split: ') + '  |  '.join(parts))
        # Enable the button only when not currently running
        if self._stop_btn.instate(['disabled']):
            self._cluster_start_btn.configure(state='normal')

    def _run_cluster_button(self):
        """『執行叢集』鈕: 語意=只跑未完成 → 先重置 _force=False, 避免沿用上次
        『開始→重跑全部』殘留的 _force 而誤重跑全部。"""
        self._force = False
        self._start_cluster()

    def _shutdown_cluster(self):
        """關 GUI 時乾淨停止: 通知停止旗標 + 終止本機/ssh client 程序 +
        遠端 pkill 本專案的 worker/gpt (ssh 斷線不保證殺得掉安靜運算中的遠端)。"""
        self._stop_ev.set()
        for proc in list(self._host_procs):
            try:
                proc.terminate()
            except Exception:
                pass
        proj = self.app.state.project_dir
        for host in list(self._remote_hosts_running):
            try:
                # 以 project_dir 過濾 → 只殺本專案的 worker/gpt/snaphu, 不誤殺他人
                subprocess.run(
                    ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                     host, f'pkill -f {shlex.quote(proj)}'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
            except Exception:
                pass

    # ── start routing ────────────────────────────────────────────────────
    def _start(self):
        st = self.app.state
        if not st.pairs:
            messagebox.showwarning('', ('請先在 Tab 1 計算干涉對。' if LANG == 'zh' else 'Please compute pairs in Tab 1 first.'))
            return

        # 輸入與干涉對已就緒 → 先檢查專案是否已有完整 MintPy 輸出，
        # 詢問要重跑全部、只補缺漏、還是直接進入 MintPy。
        done, missing = self._count_done_pairs()
        pairs_to_run = None
        if done:
            choice = self._ask_existing_outputs(len(done), len(st.pairs))
            if choice == 'cancel':
                return
            if choice == 'mintpy':
                self._goto_mintpy()
                return
            self._force = (choice == 'rerun')
            if choice == 'missing':
                pairs_to_run = missing      # 只補缺漏 → 只派未完成
        else:
            self._force = False

        if self._cluster_var.get():
            self._start_cluster(pairs_to_run=pairs_to_run)
        else:
            self._start_local()

    def _count_done_pairs(self, since_ts: float = 0.0):
        """回傳 (done, missing) 兩個 pair 清單。

        完整判定 (非只看資料夾名)：該對的三個 MintPy 最終產物
        coh_tc/filt_tc/unw_tc 都存在且 dimap_product_complete (波段.img
        存在、size>0、抽樣非全零)。任一缺/不完整 → 列入 missing 需重跑。

        since_ts>0 (force 重跑進行中): 僅把『最終產物 mtime >= since_ts』者算完成,
        排除本次開跑前的舊產物 → 進度從 0 起算 (見 pair_done_after)。
        """
        st = self.app.state
        ifg_dir = Path(st.project_dir) / 'interferograms'
        done, missing = [], []
        for ref, sec in st.pairs:
            try:
                pdir = ifg_dir / f'{ref}_{sec}'
                if pdir.is_dir() and pair_done_after(pdir, f'{ref}_{sec}', since_ts):
                    done.append((ref, sec))
                else:
                    missing.append((ref, sec))
            except OSError:
                missing.append((ref, sec))  # NFS error → treat as incomplete
        return done, missing

    def _run_cutoff(self) -> float:
        """force 重跑進行中才回傳開跑時間戳 (進度套 mtime cutoff); 其餘情況
        (檢視 / 增量補跑) 回 0.0 = 純 disk-truth。"""
        if getattr(self, '_force', False) and getattr(self, '_cluster_mode', False):
            return getattr(self, '_run_cutoff_ts', 0.0)
        return 0.0

    def _refresh_done_from_disk(self):
        """背景掃磁碟真實產物 → 已完成的對標綠、更新進度標籤/進度條。
        重開 GUI 或進入 Tab2 時呼叫，讓干涉對清單立即反映真實完成度。"""
        st = self.app.state
        if not st.pairs:
            return
        total = len(st.pairs)

        cutoff = self._run_cutoff()
        def _work():
            done, missing = self._count_done_pairs(cutoff)

            def _apply():
                for ref, sec in done:
                    if self._tree.exists(f'{ref}_{sec}'):
                        self._update_row_host(ref, sec, 'done', ('已完成' if LANG == 'zh' else 'Done'))
                self._pbar['maximum'] = total
                self._pbar['value'] = len(done)
                self._progress_lbl.set(
                    (f'已完成 {len(done)} / 未完成 {len(missing)} / 共 {total}' if LANG == 'zh' else f'Done {len(done)} / Incomplete {len(missing)} / Total {total}'))
                # 重開 GUI 偵測到已跑過的對 → 自動建基線網路圖 (僅建一次)
                if done and self._baseline_canvas is None:
                    self._ensure_baseline_view()
            self.app.after(0, _apply)

        threading.Thread(target=_work, daemon=True).start()

    def _ask_existing_outputs(self, n_done: int, n_total: int) -> str:
        """三選一對話框。回傳 'rerun' | 'missing' | 'mintpy' | 'cancel'。"""
        result = {'val': 'cancel'}
        dlg = tk.Toplevel(self.app)
        dlg.title(_T('dlg_project_ready_title'))
        dlg.transient(self.app)
        dlg.resizable(False, False)
        msg = ((f'專案資料夾：\n  {self.app.state.project_dir}\n\n'
               f'已偵測到 {n_done}/{n_total} 對已有完整 MintPy 輸出'
               f'（*_unw_tc.dim）。\n\n請選擇下一步：' if LANG == 'zh' else f'Project folder:\n  {self.app.state.project_dir}\n\nDetected {n_done}/{n_total} pairs with complete MintPy output (*_unw_tc.dim).\n\nPlease choose the next step:'))
        ttk.Label(dlg, text=msg, justify='left').pack(
            padx=16, pady=(14, 8), anchor='w')
        bf = ttk.Frame(dlg)
        bf.pack(padx=16, pady=(0, 14), fill='x')

        def _choose(v: str):
            result['val'] = v
            dlg.destroy()

        ttk.Button(bf, text=('重跑全部' if LANG == 'zh' else 'Rerun all'),
                   command=lambda: _choose('rerun')).pack(side='left', padx=4)
        if n_done < n_total:
            ttk.Button(bf, text=(f'只補缺漏 ({n_total - n_done})' if LANG == 'zh' else f'Fill missing only ({n_total - n_done})'),
                       command=lambda: _choose('missing')).pack(side='left', padx=4)
        ttk.Button(bf, text=('直接進入 MintPy' if LANG == 'zh' else 'Go straight to MintPy'),
                   command=lambda: _choose('mintpy')).pack(side='left', padx=4)
        ttk.Button(bf, text=('取消' if LANG == 'zh' else 'Cancel'),
                   command=lambda: _choose('cancel')).pack(side='left', padx=4)
        dlg.bind('<Escape>', lambda e: _choose('cancel'))
        dlg.grab_set()
        self.app.wait_window(dlg)
        return result['val']

    def _goto_mintpy(self):
        """解鎖並跳到 Tab 3 MintPy（與 Tab1 🔍、Run 完成後流程一致）。"""
        self.app.notebook.tab(2, state='normal')
        self.app.tab3.init_cfg()
        self.app.notebook.select(2)

    # ── public ──────────────────────────────────────────────────────────
    def load_pairs(self, check_resume: bool = False):
        st = self.app.state
        # 緊湊摘要 (2 行, 去掉長路徑→只顯示名稱, 縮短橫向寬度)
        self._summary_var.set(
            f'{Path(st.project_dir).name}  |  Pairs {len(st.pairs)}  |  '
            f'IW {", ".join(st.iw_list)}  |  ESD {"ON" if st.do_esd else "OFF"}  |  '
            f'CPU {st.cpu} Cache {st.cache}\n'
            f'ML rg={st.rg_looks} az={st.az_looks}  |  '
            f'SmartML n={st.smart_ml_n} coh={st.smart_ml_coh}  |  '
            f'SNAPHU {Path(st.snaphu_path).name}')
        for row in self._tree.get_children():
            self._tree.delete(row)
        _inserted_iids: set = set()
        for ref, sec in st.pairs:
            iid = f'{ref}_{sec}'
            if iid in _inserted_iids:   # guard against duplicate pairs in state
                continue
            try:
                days = delta_days(ref, sec)
            except ValueError:
                print(f'[RunFrame] WARNING: skipping pair with bad dates: {ref}-{sec}')
                continue
            self._tree.insert('', 'end',
                               iid=iid,
                               values=(ref, sec, days, 'pending', ''),
                               tags=('pending',))
            _inserted_iids.add(iid)
        self._pbar['maximum'] = len(st.pairs)
        self._pbar['value'] = 0
        # 以磁碟真實產物標記已完成(綠) → 重開 GUI/進入 Tab2 即看到已完成/未完成
        self._refresh_done_from_disk()
        # Only check for cluster resume when explicitly coming from Tab 1 setup
        if check_resume and not self._cluster_resume_dismissed:
            self.app.after(300, self._check_cluster_resume)

    # ── controls ────────────────────────────────────────────────────────
    def _start_local(self):
        """Sequential (single-machine) processing — original logic."""
        st = self.app.state
        if not st.pairs:
            return
        # 同步存 prefs → 重開還原的網路 = 正在處理的網路
        try:
            self.app.save_prefs()
        except Exception:
            pass

        # Pre-check: validate that all SLCs are present and readable
        dates_needed: set = set()
        for ref, sec in st.pairs:
            dates_needed.add(ref)
            dates_needed.add(sec)
        bad = {d: r for d, r in check_slc_completeness(
                   st.slc_dir, sorted(dates_needed)).items() if r != 'ok'}
        if bad:
            detail = '\n'.join(f'  {d}: {r}' for d, r in sorted(bad.items()))
            proceed = messagebox.askyesno(
                ('SLC 資料不完整' if LANG == 'zh' else 'Incomplete SLC data'),
                (f'以下 {len(bad)} 個日期的 SLC 有問題：\n{detail}\n\n'
                '繼續執行可能導致這些日期的 pair 失敗。\n'
                '建議先到 Tab 1 → ASF Download 補下缺漏檔案。\n\n'
                '確定仍要繼續？' if LANG == 'zh' else f'The SLC for the following {len(bad)} date(s) has issues:\n{detail}\n\nProceeding may cause pairs on these dates to fail.\nRecommended: go to Tab 1 → ASF Download to fetch the missing files first.\n\nContinue anyway?'),
                parent=self.app)
            if not proceed:
                return

        self._cluster_mode = False
        self._stop_ev.clear()
        self._start_btn.configure(state='disabled')
        self._cluster_start_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        self._current_idx = 0
        self._log.delete('1.0', 'end')
        if bad:
            self._log.insert('end',
                (f'[warn] {len(bad)} SLC 有問題: {list(bad.keys())}\n' if LANG == 'zh' else f'[warn] {len(bad)} SLC file(s) have issues: {list(bad.keys())}\n'))
        self._log.insert('end', f'[start] {len(st.pairs)} pairs\n')
        self._run_next()

    # ── 失敗分析 / 重跑 ───────────────────────────────────────────────────
    def _show_text_dialog(self, title: str, text: str):
        top = tk.Toplevel(self.app)
        top.title(title)
        top.geometry('760x520')
        frame = ttk.Frame(top)
        frame.pack(fill='both', expand=True)
        sb = ttk.Scrollbar(frame)
        sb.pack(side='right', fill='y')
        txt = tk.Text(frame, wrap='word', yscrollcommand=sb.set,
                      exportselection=False)
        txt.pack(side='left', fill='both', expand=True)
        sb.config(command=txt.yview)
        txt.insert('1.0', text)
        txt.configure(state='disabled')
        ttk.Button(top, text=('關閉' if LANG == 'zh' else 'Close'), command=top.destroy).pack(pady=4)

    def _analyze_failures(self):
        """掃描 logs 分析失敗原因 → 彈窗顯示分類/明細/建議修正 + 完成統計。"""
        import importlib
        import glob
        from collections import Counter
        proj = self.app.state.project_dir
        if not proj or not Path(proj).is_dir():
            messagebox.showwarning('', ('專案資料夾未設定。' if LANG == 'zh' else 'Project folder not set.'))
            return
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import analyze_failures as af
            importlib.reload(af)
            res = af.analyze_project(proj)
        except Exception as exc:
            messagebox.showerror(('分析失敗' if LANG == 'zh' else 'Analysis failed'), f'{exc}')
            return
        logs_dir = Path(proj) / 'logs'
        done: set = set()
        for wf in glob.glob(str(logs_dir / 'worker_*.json')):
            try:
                done |= set(json.loads(Path(wf).read_text()).get('done', []))
            except Exception:
                pass
        total = len(self.app.state.pairs)
        lines = [(f'專案: {proj}' if LANG == 'zh' else f'Project: {proj}'),
                 (f'完成 {len(done)}  失敗 {len(res)}  總 {total}' if LANG == 'zh' else f'Done {len(done)}  Failed {len(res)}  Total {total}'), '']
        if res:
            lines.append(('失敗原因分類:' if LANG == 'zh' else 'Failure category breakdown:'))
            for cat, n in Counter(v['category'] for v in res.values()).most_common():
                lines.append(f'  {n:3d}  {cat}')
            lines.append(('\n每對明細:' if LANG == 'zh' else '\nPer-pair details:'))
            for pair, v in sorted(res.items()):
                lines.append(f'  {pair}  [{v["host"]}] {v["step"]} → {v["category"]}')
            lines.append(('\n建議修正:' if LANG == 'zh' else '\nSuggested fixes:'))
            seen: set = set()
            for v in res.values():
                if v['category'] in seen:
                    continue
                seen.add(v['category'])
                lines.append(f'  • [{v["category"]}] {v["fix"]}')
        else:
            lines.append(('沒有失敗的干涉對 ✓' if LANG == 'zh' else 'No failed pairs ✓'))
        self._show_text_dialog(('失敗分析' if LANG == 'zh' else 'Failure Analysis'), '\n'.join(lines))

    def _rerun_failed(self):
        """只把『磁碟上未完成』的干涉對重新分配給目前勾選的工作站。
        已完成的(磁碟有完整 *_unw_tc.dim)不再處理；未完成的重新派工。"""
        if not self.app.state.pairs:
            messagebox.showwarning('', ('請先計算干涉對 (Tab 1)。' if LANG == 'zh' else 'Please compute pairs first (Tab 1).'))
            return
        done, missing = self._count_done_pairs()
        if not missing:
            messagebox.showinfo('', (f'全部 {len(done)} 對都已完成 ✓ 無需重跑。' if LANG == 'zh' else f'All {len(done)} pairs are already done ✓ No rerun needed.'))
            return
        if messagebox.askyesno(
                ('重跑未完成' if LANG == 'zh' else 'Rerun incomplete'),
                (f'磁碟確認：已完成 {len(done)}、未完成 {len(missing)}。\n\n'
                f'將把這 {len(missing)} 對未完成的重新分配給目前勾選的工作站\n'
                f'（已完成的不再處理；失敗/中斷的接續重做）。\n\n'
                f'提示：請先取消勾選已知故障的工作站。要開始嗎?' if LANG == 'zh' else f'Disk check: {len(done)} done, {len(missing)} incomplete.\n\nThe {len(missing)} incomplete pairs will be redistributed to the currently checked hosts\n(completed pairs are skipped; failed/interrupted ones are redone).\n\nTip: uncheck any known faulty hosts first. Start now?')):
            self._start_cluster(pairs_to_run=missing)

    # ── cluster execution ────────────────────────────────────────────────
    def _preflight_swap(self, active: list) -> bool:
        """啟動前確認所有機器的 Swap 狀態；回傳 False 代表使用者取消。

        規則：
        - ssd_swap_auto=False → 略過所有檢查
        - 每台機器（本機 + 遠端）逐一確認 swap 是否啟用
        - 全部都有啟用 → 靜默通過，不彈任何視窗
        - 遠端沒有啟用 → 僅列出未啟用的機器名稱做提醒
        - 本機未啟用 → 嘗試自動啟用；失敗才詢問是否繼續
        """
        st = self.app.state
        if not getattr(st, 'ssd_swap_auto', False):
            return True
        _sp = getattr(st, 'ssd_swap_path', '').strip()
        if not _sp:
            return True
        _img = _swap_img_path(_sp)

        # ── 本機 swap 檢查 ─────────────────────────────────────────────────
        _active, _info = _swap_status(_img)
        if not _active:
            # 嘗試自動啟用
            _ui_pass = ''
            try:
                _ui_pass = self.app.tab1._swap_sudo_var.get().strip()
            except Exception:
                pass
            sudo_pass = _ui_pass or _SUDO_PASS
            ok, msg = _enable_swap(_img, sudo_pass)
            if not ok:
                from tkinter import simpledialog
                pw = simpledialog.askstring(
                    ('SSD Swap 未啟用' if LANG == 'zh' else 'SSD Swap Not Enabled'),
                    (f'本機 Swapfile 尚未啟用，需要 sudo 密碼：\n  {_img}\n\n'
                    f'（或先在 Tab1 的「sudo 密碼」欄填好；取消 → 不啟用繼續跑可能 OOM）' if LANG == 'zh' else f'Local swapfile is not enabled yet, sudo password required:\n  {_img}\n\n(Or fill in the "sudo password" field in Tab 1 first; Cancel → continuing without swap may cause OOM)'),
                    show='*', parent=self.app)
                if pw:
                    ok, msg = _enable_swap(_img, pw)
            if not ok:
                return messagebox.askyesno(
                    ('⚠ 本機 Swap 未啟用' if LANG == 'zh' else '⚠ Local Swap Not Enabled'),
                    (f'無法啟用本機 Swap：{msg}\n\n'
                    f'未啟用 Swap 在記憶體不足時可能導致 OOM Killed。\n\n'
                    f'確定要繼續（無 Swap 保護）？' if LANG == 'zh' else f'Failed to enable local Swap: {msg}\n\nWithout Swap enabled, low memory may cause OOM Killed.\n\nContinue anyway (without Swap protection)?'),
                    icon='warning', parent=self.app)

        # ── 遠端機器 swap 狀態：SSH 快速確認（timeout 5s/台）────────────────
        _remote_no_swap = []
        for lbl, ssh_host in active:
            if ssh_host is None:
                continue  # 本機已檢查過
            try:
                result = subprocess.run(
                    ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                     ssh_host,
                     f'swapon --show=NAME --noheadings 2>/dev/null | grep -q . && echo yes || echo no'],
                    capture_output=True, text=True, timeout=8)
                if result.stdout.strip() != 'yes':
                    _remote_no_swap.append(lbl)
            except Exception:
                _remote_no_swap.append(f'{lbl}(?)')  # SSH 失敗，保守列為未知

        # ── 有遠端未啟用 swap → 提醒（不阻擋，使用者自行決定）──────────────
        if _remote_no_swap:
            messagebox.showwarning(
                ('⚠ 部分機器 Swap 未啟用' if LANG == 'zh' else '⚠ Some Hosts Have Swap Not Enabled'),
                (f'以下遠端機器的 Swap 尚未啟用，記憶體不足時可能 OOM Killed：\n\n'
                f'  {", ".join(_remote_no_swap)}\n\n'
                f'── 若尚未建立 swap（首次設定）──\n'
                f'  sudo fallocate -l 100G ~/snap_swap.img\n'
                f'  sudo chmod 600 ~/snap_swap.img\n'
                f'  sudo mkswap ~/snap_swap.img\n'
                f'  sudo swapon ~/snap_swap.img\n'
                f'  SWAP_ABS=$(realpath ~/snap_swap.img)\n'
                f'  echo "$SWAP_ABS none swap sw 0 0" | sudo tee -a /etc/fstab\n\n'
                f'── 若已建立但尚未啟用 ──\n'
                f'  sudo swapon ~/snap_swap.img\n\n'
                f'（按確認繼續；或先登入各機設好 Swap 後再按「叢集開始」）' if LANG == 'zh' else f'The following remote hosts do not have Swap enabled; low memory may cause OOM Killed:\n\n  {", ".join(_remote_no_swap)}\n\n── If swap has not been created yet (first-time setup) ──\n  sudo fallocate -l 100G ~/snap_swap.img\n  sudo chmod 600 ~/snap_swap.img\n  sudo mkswap ~/snap_swap.img\n  sudo swapon ~/snap_swap.img\n  SWAP_ABS=$(realpath ~/snap_swap.img)\n  echo "$SWAP_ABS none swap sw 0 0" | sudo tee -a /etc/fstab\n\n── If already created but not enabled ──\n  sudo swapon ~/snap_swap.img\n\n(Click OK to continue; or log into each host to set up Swap first, then click "Cluster Start")'),
                parent=self.app)
        # 全部都有 swap（或使用者確認繼續）→ 靜默通過
        return True

    def _start_cluster(self, pairs_to_run=None):
        """Distribute pairs across machines listed in ~/.ssh/config.

        pairs_to_run: 只派這些對 (如「重跑未完成」只派未完成的)；None=全部 st.pairs。
        """
        st = self.app.state
        if not st.pairs:
            messagebox.showwarning('', ('請先在 Tab 1 計算干涉對。' if LANG == 'zh' else 'Please compute pairs in Tab 1 first.'))
            return

        active = [(lbl, None if lbl in ('本機', 'Local') else lbl)
                  for lbl, v in self._cluster_host_vars.items() if v.get()]
        if not active:
            messagebox.showwarning('', ('請至少選擇一台主機。' if LANG == 'zh' else 'Please select at least one host.'))
            return

        if not self._preflight_swap(active):
            return

        # 決定要派工哪些對 (以磁碟真實產物判定已完成):
        #   pairs_to_run 明確給定 → 用它
        #   self._force (重跑全部)  → 全部 st.pairs
        #   預設 (執行叢集)        → 只派『未完成』的, 已完成的跳過不重跑
        done_pairs, missing = self._count_done_pairs()
        if pairs_to_run is not None:
            base_pairs = list(pairs_to_run)
            _run_set = set(base_pairs)
            done_pairs = [p for p in done_pairs if p not in _run_set]
        elif getattr(self, '_force', False):
            base_pairs = list(st.pairs)
            done_pairs = []            # 重跑全部 → 不標既有完成
        else:
            base_pairs = list(missing)
        # force 重跑: 記錄開跑時間; 進度/完成數只算此後才重做完成的對 (排除舊產物),
        # 進度條從 0 起算。非 force → 0 = 沿用 disk-truth (顯示既有完成)。
        self._run_cutoff_ts = time.time() if getattr(self, '_force', False) else 0.0
        if not base_pairs:
            # 沒有未完成 → 把已完成標綠, 不派工
            for ref, sec in done_pairs:
                self._update_row_host(ref, sec, 'done', ('已完成' if LANG == 'zh' else 'Done'))
            messagebox.showinfo(
                '', (f'全部 {len(st.pairs)} 對都已完成 ✓ 無需處理。\n'
                    f'(如要強制重跑請用 ▶ 開始 → 重跑全部)' if LANG == 'zh' else f'All {len(st.pairs)} pairs are already done ✓ Nothing to process.\n(To force a rerun, use ▶ Start → Rerun all)'))
            return
        # Sort pairs by ref date to reduce simultaneous split-file writes
        sorted_pairs = sorted(base_pairs, key=lambda p: (p[0], p[1]))
        n = len(active)
        size, rem = divmod(len(sorted_pairs), n)
        self._cluster_chunks.clear()
        self._cluster_finished_hosts.clear()   # 新輪派工清除上次結束 host 紀錄
        self._steal_counts.clear()             # 新輪派工清除上次搶工計數
        start = 0
        for i, (lbl, _) in enumerate(active):
            end = start + size + (1 if i < rem else 0)
            self._cluster_chunks[lbl] = sorted_pairs[start:end]
            start = end

        # 確保 GUI 欄位值（cache/xmx/AOI 等）已同步進 state，再寫 dist_config
        try:
            self.app.tab1.collect_state()
        except Exception:
            pass

        # Save config + assignment record to shared storage
        logs_dir = Path(st.project_dir) / 'logs'
        logs_dir.mkdir(parents=True, exist_ok=True)
        config_path = str(logs_dir / 'dist_config.json')
        with open(config_path, 'w', encoding='utf-8') as fh:
            json.dump(st.to_dict(), fh, indent=2, ensure_ascii=False)

        # dist_state.json: records which pairs each machine was assigned
        dist_state = {
            'version': 1,
            'ts':      datetime.now().isoformat(timespec='seconds'),
            'config':  config_path,
            'assignments': {
                lbl: [f'{r}-{s}' for r, s in chunk]
                for lbl, chunk in self._cluster_chunks.items()
            },
        }
        with open(str(logs_dir / 'dist_state.json'), 'w', encoding='utf-8') as fh:
            json.dump(dist_state, fh, indent=2, ensure_ascii=False)

        # 派工時同步存 prefs → 重開 GUI 還原的網路 = 正在處理的網路 (避免
        # prefs 停在舊日期範圍, 重開後完成度/網路對不上 dist_config)。
        try:
            self.app.save_prefs()
        except Exception:
            pass

        # Reset log notebook: remove extra tabs, create one per machine
        for tab in self._log_nb.tabs()[1:]:
            self._log_nb.forget(tab)
        # clear first tab and reuse it for first machine
        first_lbl = active[0][0]
        self._log_nb.tab(0, text=first_lbl)
        first_frame = self._log_nb.nametowidget(self._log_nb.tabs()[0])
        for w in first_frame.winfo_children():
            w.destroy()
        self._machine_logs.clear()
        self._machine_logs[first_lbl] = _make_log(first_frame, height=6)
        self._log = self._machine_logs[first_lbl]

        for lbl, _ in active[1:]:
            frame = ttk.Frame(self._log_nb)
            self._log_nb.add(frame, text=lbl)
            self._machine_logs[lbl] = _make_log(frame, height=6)

        # 重建「基線網路」分頁 (notebook 剛被重置 → 重新加回並算圖)
        self._ensure_baseline_view()

        # 已完成的標綠(不重跑, 基線圖也轉綠); 派工的標 cluster(排隊, 灰)
        for ref, sec in done_pairs:
            self._update_row_host(ref, sec, 'done', ('已完成' if LANG == 'zh' else 'Done'))
        for lbl, chunk in self._cluster_chunks.items():
            for ref, sec in chunk:
                self._update_row_host(ref, sec, 'cluster', lbl)

        # UI state
        self._cluster_mode = True
        self.app.after(2000, self._poll_cluster_live)  # 即時顯示哪台處理哪一幅
        self._stop_ev.clear()
        self._start_btn.configure(state='disabled')
        self._cluster_start_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        self._pbar['maximum'] = len(st.pairs)
        self._pbar['value'] = 0
        self._cluster_done = 0
        self._cluster_total = sum(1 for lbl, _ in active
                                  if self._cluster_chunks.get(lbl))

        if self._cluster_total == 0:
            self._finish()
            return

        worker_script = str(Path(__file__).parent / 'snap2mintpy_worker.py')
        # Store dispatch context for work-stealing (idle host claims leftover pairs)
        self._cluster_host_ssh = {lbl: ssh_host for lbl, ssh_host in active}
        self._cluster_config_path = config_path
        self._cluster_worker_script = worker_script
        for lbl, ssh_host in active:
            chunk = self._cluster_chunks.get(lbl, [])
            if not chunk:
                continue
            pairs_arg = ','.join(f'{r}-{s}' for r, s in chunk)
            threading.Thread(
                target=self._run_on_host,
                args=(lbl, ssh_host, worker_script, config_path, pairs_arg),
                daemon=True,
            ).start()

    def _run_on_host(self, label: str, ssh_host: Optional[str],
                     worker_script: str, config_path: str, pairs_arg: str,
                     make_dem: bool = True):
        """Daemon thread: SSH (or local subprocess) → stream output to log tab.

        make_dem: 本機是否負責產 dem_tc (僅指派給第一台; 其他傳 --no-make-dem)。
        """
        if ssh_host is None:
            # Local — run in same Python environment
            cmd = [sys.executable, worker_script,
                   '--config', config_path,
                   '--pairs',  pairs_arg,
                   '--label',  label]
            if self._force:
                cmd.append('--force')
            if not make_dem:
                cmd.append('--no-make-dem')
            env = dict(os.environ)
        else:
            # Remote via SSH — activate conda/FastISCE.config then run worker.
            # Prepend conda libgfortran to LD_LIBRARY_PATH so SNAP's bundled
            # jblas finds a compatible libgfortran-5.so on older glibc systems
            # (e.g. Ubuntu 18.04 with GLIBC 2.27 requires conda's version).
            remote_cmd = (
                'source ~/FastISCE.config 2>/dev/null; '
                # jblas.skipArchiveExtraction: prevents SNAP from extracting
                # its bundled libgfortran-5.so (which requires GLIBC_2.29),
                # forcing jblas to use system BLAS or its pure-Java fallback.
                # Fixes SNAP 13.0.0 / jblas crash on Ubuntu 18.04 (GLIBC 2.27).
                'export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }'
                r'-Djava.awt.headless=true -Djblas.skipArchiveExtraction=true"; '
                f'python3 /mnt/SARDB/snap2mintpy/snap2mintpy_worker.py '
                f'--config {shlex.quote(config_path)} '
                f'--pairs  {shlex.quote(pairs_arg)} '
                f'--label  {shlex.quote(label)}'
                + (' --force' if self._force else '')
                + ('' if make_dem else ' --no-make-dem')
            )
            cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30',
                   ssh_host, remote_cmd]
            env = None

        n = len(self._cluster_chunks.get(label, []))
        self._cluster_log(label, f'[{label}] starting {n} pairs\n')
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env)
            self._host_procs.append(proc)              # 追蹤 (關閉時終止)
            if ssh_host is not None:
                self._remote_hosts_running.add(ssh_host)
            for line in proc.stdout:
                if self._stop_ev.is_set():
                    proc.terminate()
                    break
                self._cluster_log(label, line)
            rc = proc.wait()
        except Exception as exc:
            self._cluster_log(label, f'[ERROR] {exc}\n')
            rc = -1
        finally:
            if proc in self._host_procs:
                self._host_procs.remove(proc)
            if ssh_host is not None:
                self._remote_hosts_running.discard(ssh_host)

        ok = (rc == 0)
        self._cluster_log(
            label,
            f'[{label}] {"DONE ✓" if ok else f"FAILED ✗ (rc={rc})"}\n')
        self.app.after(0, self._on_host_done, label, ok)

    # Worker progress-line patterns (see snap2mintpy_worker.py):
    #   [label] (idx/total) {ref}-{sec} ...  → running
    #   [label] OK  {ref}-{sec}              → done
    #   [label] FAILED  {ref}-{sec}          → error
    # Anchored on the "]" after [label] so the start:/done summary lines
    # (which contain ok=/failed=) never match.
    _CLUSTER_RUN_RE  = re.compile(r'\]\s+\(\d+/\d+\)\s+(\d{8})-(\d{8})')
    _CLUSTER_OK_RE   = re.compile(r'\]\s+OK\s+(\d{8})-(\d{8})')
    _CLUSTER_FAIL_RE = re.compile(r'\]\s+FAILED\s+(\d{8})-(\d{8})')

    def _cluster_log(self, label: str, text: str):
        """Thread-safe: append text to a machine's log tab, and reflect each
        pair's live status on the tree by parsing the worker's progress lines."""
        widget = self._machine_logs.get(label)
        if widget:
            self.app.after(0, lambda w=widget, t=text: (
                w.insert('end', t), w.see('end')))
        self._reflect_pair_status(label, text)

    def _reflect_pair_status(self, label: str, text: str):
        """Parse one worker progress line and update that pair's tree row live
        (running/done/error). Called from the host daemon thread, so the actual
        tree update is marshalled onto the main thread via after()."""
        for rx, status in ((self._CLUSTER_OK_RE,   'done'),
                           (self._CLUSTER_FAIL_RE, 'error'),
                           (self._CLUSTER_RUN_RE,  'running')):
            m = rx.search(text)
            if m:
                ref, sec = m.group(1), m.group(2)
                self.app.after(0, lambda r=ref, s=sec, stt=status, lb=label:
                               (self._update_row_host(r, s, stt, lb),
                                self._sync_cluster_pbar()))
                return

    def _row_status(self, ref: str, sec: str) -> str:
        """Current status tag of a pair row ('pending'/'cluster'/'running'/
        'done'/'error'), or '' if the row is missing."""
        iid = f'{ref}_{sec}'
        if self._tree.exists(iid):
            tags = self._tree.item(iid, 'tags')
            if tags:
                return tags[0]
        return ''

    def _sync_cluster_pbar(self):
        """Set the progress bar to the number of finished pairs (done/error) on
        the tree. Idempotent — safe to call after every per-pair update without
        double counting."""
        n = sum(1 for iid in self._tree.get_children()
                if (self._tree.item(iid, 'tags') or ('',))[0] in ('done', 'error'))
        self._pbar['value'] = n

    def _on_host_done(self, label: str, ok: bool):
        """Called on main thread when one remote host finishes.

        以 worker_{label}.json 的 done/failed 為準標記 (worker 真實結果),
        不因 host 程序非零退出(被停/斷線/被 kill)就把整批未完成標成 error。
        未被處理到的對 → 保持 'cluster'(待處理), 不誤標失敗 → 修「host 斷線
        導致該機整批顯示紅 ✗」。
        """
        chunk = list(self._cluster_chunks.get(label, []))
        proj = self.app.state.project_dir
        # worker 明確回報失敗的對 (僅作為「真失敗」依據, 完成則一律以磁碟為準)
        failed_set: set = set()
        try:
            wf = Path(proj) / 'logs' / f'worker_{label}.json'
            failed_set = set(json.loads(wf.read_text(encoding='utf-8')).get('failed', []))
        except Exception:
            pass
        ifg = Path(proj) / 'interferograms'
        cutoff = self._run_cutoff()   # force 重跑: 只認本次重做完成的對 (排除舊產物)

        # 背景以「磁碟實體檔案」判定 (pair_mintpy_complete: 三產物 .dim/.data/.img
        # 都在且非全零) → 避免逐對 SMB I/O 凍住 UI; 完成再回主執行緒標記。
        def _work():
            marks = []
            for ref, sec in chunk:
                d = ifg / f'{ref}_{sec}'
                if d.is_dir() and pair_done_after(d, f'{ref}_{sec}', cutoff):
                    marks.append((ref, sec, 'done'))     # 磁碟確認完整 → 完成
                elif f'{ref}-{sec}' in failed_set:
                    marks.append((ref, sec, 'error'))    # worker 真實回報失敗
                else:
                    marks.append((ref, sec, 'cluster'))  # 未完成/未處理 → 待處理
            self.app.after(0, lambda: self._apply_host_marks(label, marks))

        threading.Thread(target=_work, daemon=True).start()

    def _apply_host_marks(self, label: str, marks: list):
        """主執行緒套用 _on_host_done 的磁碟判定結果。"""
        for ref, sec, state in marks:
            if self._row_status(ref, sec) == 'done':
                continue
            host = ('已完成' if LANG == 'zh' else 'Done') if state == 'done' else label
            self._update_row_host(ref, sec, state, host)
        self._sync_cluster_pbar()

        # 記錄此 host 已完成（work-stealing 只搶「已結束 host」的剩餘 pair）
        self._cluster_finished_hosts.add(label)

        # Work-stealing：只從「已完成的 host」之 chunk 中找仍是 cluster 的對。
        # 這樣可避免搶走「仍在跑的 host」正在處理中的對（防止競態寫入）。
        # 每對最多嘗試 2 次（_steal_counts 計數）避免無限重試失敗對。
        if not self._stop_ev.is_set() and self._cluster_config_path:
            stolen = []
            for fin_lbl in list(self._cluster_finished_hosts):
                for ref, sec in list(self._cluster_chunks.get(fin_lbl, [])):
                    key = f'{ref}-{sec}'
                    if self._steal_counts.get(key, 0) >= 2:
                        continue  # 已重試過，不再搶
                    iid = f'{ref}_{sec}'
                    if self._tree.exists(iid) and \
                            (self._tree.item(iid, 'tags') or ('',))[0] == 'cluster':
                        stolen.append((ref, sec))
            # 去重
            seen: set = set()
            stolen = [(r, s) for r, s in stolen
                      if (r, s) not in seen and not seen.add((r, s))]  # type: ignore[func-returns-value]
            if stolen:
                for r, s in stolen:
                    self._steal_counts[f'{r}-{s}'] = \
                        self._steal_counts.get(f'{r}-{s}', 0) + 1
                    self._update_row_host(r, s, 'cluster', label)
                self._cluster_chunks[label] = stolen
                pairs_arg = ','.join(f'{r}-{s}' for r, s in stolen)
                ssh_host = self._cluster_host_ssh.get(label)
                self._cluster_log(
                    label,
                    (f'[{label}] ↩ 搶工: 接手 {len(stolen)} 對未完成 → 繼續跑\n' if LANG == 'zh' else f'[{label}] ↩ Work-steal: took over {len(stolen)} incomplete pairs → continuing\n'))
                threading.Thread(
                    target=self._run_on_host,
                    args=(label, ssh_host, self._cluster_worker_script,
                          self._cluster_config_path, pairs_arg, False),
                    daemon=True,
                ).start()
                return  # wait for steal to finish; don't increment _cluster_done yet

        self._cluster_done += 1
        if self._cluster_done >= self._cluster_total:
            self._finish()

    # ── cluster resume ───────────────────────────────────────────────────
    def _check_cluster_resume(self):
        """On Tab-2 entry: if dist_state.json exists, offer to resume."""
        st = self.app.state
        if not st.project_dir:
            return
        state_file = Path(st.project_dir) / 'logs' / 'dist_state.json'
        if not state_file.exists():
            return

        try:
            dist = json.loads(state_file.read_text(encoding='utf-8'))
        except Exception:
            return

        assignments: Dict[str, list] = dist.get('assignments', {})
        if not assignments:
            return
        ts_str = dist.get('ts', '')[:16].replace('T', ' ')

        # 以「磁碟真實產物」判定完成度 (不信 worker JSON — 它低估、且不含前次
        # 跑完被 skip 的對)。背景掃描避免凍住 UI，再回主執行緒彈窗。
        def _work():
            done, missing = self._count_done_pairs()
            self.app.after(0, lambda: self._resume_prompt(ts_str, done, missing))

        threading.Thread(target=_work, daemon=True).start()

    def _resume_prompt(self, ts_str: str, done: list, missing: list):
        """重開 GUI 偵測結果 → 標綠已完成、並依真實未完成度詢問是否繼續處理。"""
        total = len(self.app.state.pairs)
        for ref, sec in done:
            if self._tree.exists(f'{ref}_{sec}'):
                self._update_row_host(ref, sec, 'done', ('已完成' if LANG == 'zh' else 'Done'))
        self._pbar['maximum'] = total
        self._pbar['value'] = len(done)
        self._progress_lbl.set(
            (f'已完成 {len(done)} / 未完成 {len(missing)} / 共 {total}' if LANG == 'zh' else f'Done {len(done)} / Incomplete {len(missing)} / Total {total}'))

        if not missing:
            # 磁碟確認全部完成 → 才解鎖 MintPy
            self._log.insert('end', ('[resume] 磁碟確認所有干涉對已完成，解鎖 Tab 3。\n' if LANG == 'zh' else '[resume] Disk check confirms all pairs are done, unlocking Tab 3.\n'))
            self._log.see('end')
            self.app.notebook.tab(2, state='normal')
            self.app.tab3.init_cfg()
            return

        # 尚有未完成 → 詢問「是否繼續處理未完成」(不是問跑 MintPy)
        msg = (
            (f'偵測到先前叢集記錄（{ts_str}）。\n\n'
            f'磁碟實際完成度：\n'
            f'  ✓ 已完成 {len(done)} 對\n'
            f'  ⬜ 未完成 {len(missing)} 對\n'
            f'  共 {total} 對\n\n'
            f'是否現在繼續處理未完成的 {len(missing)} 對？\n'
            f'（會分配給「目前勾選」的工作站；建議先取消勾選已知故障機）\n\n'
            f'選「是」立即開始；選「否」稍後手動調整機器再按「↻ 重跑未完成」。' if LANG == 'zh' else f'Detected a previous cluster record ({ts_str}).\n\nActual completion on disk:\n  ✓ Done {len(done)} pairs\n  ⬜ Incomplete {len(missing)} pairs\n  Total {total} pairs\n\nContinue processing the {len(missing)} incomplete pairs now?\n(Will be assigned to the "currently checked" hosts; recommended to uncheck known faulty hosts first)\n\nChoose "Yes" to start immediately; choose "No" to adjust hosts manually later and click "↻ Rerun incomplete".')
        )
        if messagebox.askyesno(('繼續處理未完成' if LANG == 'zh' else 'Continue Processing Incomplete'), msg, parent=self.app):
            self._start_cluster(pairs_to_run=missing)
        else:
            self._cluster_resume_dismissed = True

    def _resume_cluster(self, dist_state: dict, incomplete: Dict[str, list]):
        """Re-dispatch only the unfinished pairs to their assigned machines."""
        st = self.app.state
        logs_dir = Path(st.project_dir) / 'logs'

        # Re-use saved config or regenerate
        config_path = dist_state.get('config', '')
        if not config_path or not Path(config_path).exists():
            config_path = str(logs_dir / 'dist_config.json')
            with open(config_path, 'w', encoding='utf-8') as fh:
                json.dump(st.to_dict(), fh, indent=2, ensure_ascii=False)

        # Rebuild cluster_chunks from incomplete dict (str → tuple pairs)
        self._cluster_chunks = {
            lbl: [tuple(p.split('-', 1)) for p in pairs_strs]
            for lbl, pairs_strs in incomplete.items()
        }

        # Reset log notebook
        for tab in self._log_nb.tabs()[1:]:
            self._log_nb.forget(tab)
        active_labels = list(incomplete.keys())
        first_lbl = active_labels[0]
        self._log_nb.tab(0, text=first_lbl)
        first_frame = self._log_nb.nametowidget(self._log_nb.tabs()[0])
        for w in first_frame.winfo_children():
            w.destroy()
        self._machine_logs.clear()
        self._machine_logs[first_lbl] = _make_log(first_frame, height=6)
        self._log = self._machine_logs[first_lbl]
        for lbl in active_labels[1:]:
            frame = ttk.Frame(self._log_nb)
            self._log_nb.add(frame, text=lbl)
            self._machine_logs[lbl] = _make_log(frame, height=6)

        # 重建「基線網路」分頁
        self._ensure_baseline_view()

        # Treeview: mark already-done pairs green, remaining blue
        all_assignments: dict = dist_state.get('assignments', {})
        incomplete_sets = {lbl: set(v) for lbl, v in incomplete.items()}
        for lbl, pairs_strs in all_assignments.items():
            for p in pairs_strs:
                parts = p.split('-', 1)
                if len(parts) != 2:
                    continue
                ref, sec = parts
                tag = 'cluster' if p in incomplete_sets.get(lbl, set()) else 'done'
                self._update_row_host(ref, sec, tag, lbl)

        # UI state
        self._cluster_mode = True
        self.app.after(2000, self._poll_cluster_live)  # 即時顯示哪台處理哪一幅
        self._stop_ev.clear()
        self._start_btn.configure(state='disabled')
        self._cluster_start_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')
        total_incomplete = sum(len(v) for v in self._cluster_chunks.values())
        self._pbar['maximum'] = total_incomplete
        self._pbar['value'] = 0
        self._cluster_done = 0
        self._cluster_total = sum(1 for v in self._cluster_chunks.values() if v)

        if self._cluster_total == 0:
            self._finish()
            return

        # 派工前先清掉各機殘留的舊 worker/gpt/snaphu (本專案) → 避免上次 GUI 異常
        # 退出(X server 斷線/OOM 砍 GUI/崩潰)沒走 _shutdown_cluster 留下的孤兒程序
        # 累積, 造成兩個 gpt 並存 OOM。
        self._cleanup_host_leftovers([(lbl, None if lbl in ('本機', 'Local') else lbl)
                                      for lbl in self._cluster_chunks])

        # DEM 機器 = 第一台有分到對的被勾選機器 (本機沒勾就是剩下的第一台)
        # → 只它產 dem_tc, 避免多台競態。
        dem_machine = next((lbl for lbl, c in self._cluster_chunks.items() if c), None)

        worker_script = str(Path(__file__).parent / 'snap2mintpy_worker.py')
        for lbl, chunk in self._cluster_chunks.items():
            if not chunk:
                continue
            ssh_host = None if lbl in ('本機', 'Local') else lbl
            pairs_arg = ','.join(f'{r}-{s}' for r, s in chunk)
            threading.Thread(
                target=self._run_on_host,
                args=(lbl, ssh_host, worker_script, config_path, pairs_arg,
                      lbl == dem_machine),
                daemon=True,
            ).start()

    def _cleanup_host_leftovers(self, hosts):
        """派工前清掉各機殘留的本專案 worker/gpt/snaphu (孤兒程序)。

        以 project_dir 過濾 → 只殺本專案的 worker(--config .../proj/...)、
        gpt(.../proj/graphs/...xml)、snaphu(cwd 在 proj 內)。GUI 自身 cmdline
        不含 project_dir → 不會誤殺 GUI。
        """
        proj = self.app.state.project_dir
        if not proj:
            return
        for lbl, ssh_host in hosts:
            try:
                if ssh_host is None:
                    subprocess.run(['pkill', '-f', proj],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10)
                else:
                    subprocess.run(
                        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                         ssh_host, f'pkill -f {shlex.quote(proj)}'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=12)
                self._cluster_log(lbl, (f'[{lbl}] 清除殘留舊程序 (本專案)\n' if LANG == 'zh' else f'[{lbl}] Cleared leftover old processes (this project)\n'))
            except Exception:
                pass

    def _stop(self):
        self._stop_ev.set()
        self._stop_btn.configure(state='disabled')
        self._log.insert('end', '[stopping — waiting for current pair...]\n')
        self._log.see('end')

    def _run_next(self):
        st = self.app.state
        if self._current_idx >= len(st.pairs) or self._stop_ev.is_set():
            self._finish()
            return
        ref, sec = st.pairs[self._current_idx]
        self._update_row(ref, sec, 'running', '')
        self._progress_lbl.set(
            f'{self._current_idx + 1}/{len(st.pairs)}  {ref}→{sec}')

        w = SnapPairWorker(ref, sec, st, self._on_event, self._stop_ev,
                           force=self._force)
        self._workers.append(w)
        w.start()

    def _on_event(self, kind: str, payload: dict):
        self.app.after(0, self._handle_event, kind, payload)

    def _handle_event(self, kind: str, p: dict):
        ref, sec = p.get('ref', ''), p.get('sec', '')
        if kind == 'log':
            self._log.insert('end', p['text'])
            self._log.see('end')
        elif kind == 'pair_start':
            self._update_row(ref, sec, 'running', '')
        elif kind == 'pair_done':
            dur = p.get('duration', 0)
            self._update_row(ref, sec, 'done', f'{dur:.0f}s')
            self._pbar['value'] = self._current_idx + 1
            self._current_idx += 1
            self._run_next()
        elif kind == 'pair_error':
            dur = p.get('duration', 0)
            self._update_row(ref, sec, 'error', f'{dur:.0f}s')
            self._pbar['value'] = self._current_idx + 1
            self._current_idx += 1
            self._run_next()

    def _update_row(self, ref: str, sec: str, status: str, dur: str):
        iid = f'{ref}_{sec}'
        if self._tree.exists(iid):
            self._tree.item(iid,
                             values=(ref, sec, delta_days(ref, sec), status, dur),
                             tags=(status,))
        self._mark_edge(ref, sec, status)

    def _update_row_host(self, ref: str, sec: str, state_tag: str, host: str,
                         detail: str = ''):
        """更新某對狀態欄為「圖示+主機(+步驟)」，用 state_tag 著色 (即時叢集監看)。

        例: '▶ worker01 · ifg_ml' (處理中) / '✓ worker02' (完成) / '✗ local' (失敗)。
        """
        # 基線圖改色獨立於 tree 列是否存在 → 先標邊, 再更新 tree
        # (修: 3 台在跑卻只有 1 條橘線 — 因 tree 列不存在就 early-return 跳過改色)
        self._mark_edge(ref, sec, state_tag)
        iid = f'{ref}_{sec}'
        if not self._tree.exists(iid):
            return
        icon = {'running': '▶ ', 'done': '✓ ', 'error': '✗ ',
                'cluster': '⋯ '}.get(state_tag, '')
        label = f'{icon}{host}' + (f' · {detail}' if detail else '')
        self._tree.item(iid,
                        values=(ref, sec, delta_days(ref, sec), label, ''),
                        tags=(state_tag,))

    # ── 基線網路即時分頁 (每完成一對就改該線段顏色) ──────────────────────
    def _mark_edge(self, ref: str, sec: str, state: str):
        """廣播給所有開著的基線圖 (Tab1 彈窗 + Tab2 內嵌) 改該線段顏色。"""
        self.app.notify_pair_state(ref, sec, state)

    def _ensure_baseline_view(self):
        """(重)建「基線網路」分頁：背景算 bperp、主執行緒嵌圖，並套用目前各對狀態。"""
        st = self.app.state
        if not st.pairs:
            return
        if self._baseline_frame is None or \
                str(self._baseline_frame) not in self._log_nb.tabs():
            if self._baseline_frame is not None:
                try:
                    self._baseline_frame.destroy()
                except Exception:
                    pass
            self._baseline_frame = ttk.Frame(self._log_nb)
            self._log_nb.add(self._baseline_frame, text=('基線網路' if LANG == 'zh' else 'Baseline Network'))
        for w in self._baseline_frame.winfo_children():
            w.destroy()
        tk.Label(self._baseline_frame, text=('基線圖計算中…' if LANG == 'zh' else 'Computing baseline plot…'),
                 font=('TkDefaultFont', 11)).pack(expand=True)
        self._baseline_canvas = None
        self._baseline_edges = {}

        pairs_snap = list(st.pairs)
        slc_dir = st.slc_dir
        try:
            aoi_lat = (float(st.latmin) + float(st.latmax)) / 2.0
        except (TypeError, ValueError):
            aoi_lat = None
        title = (f'{Path(st.project_dir).name} 即時進度' if LANG == 'zh' else f'{Path(st.project_dir).name} Live Progress')
        proj = st.project_dir

        def _compute():
            try:
                processed = scan_processed_pairs(proj)   # 磁碟已完成 → 圖例/綠色
                fig, edges = plot_baseline_network(
                    pairs_snap, slc_dir, aoi_lat=aoi_lat, title=title,
                    processed_pairs=processed, return_edges=True)
            except Exception as exc:
                self.app.after(0, lambda e=str(exc): self._baseline_err(e))
                return
            self.app.after(0, lambda: self._embed_baseline(fig, edges))

        threading.Thread(target=_compute, daemon=True).start()

    def _baseline_err(self, msg: str):
        if self._baseline_frame is None:
            return
        for w in self._baseline_frame.winfo_children():
            w.destroy()
        tk.Label(self._baseline_frame, text=(f'基線圖失敗：\n{msg}' if LANG == 'zh' else f'Baseline plot failed:\n{msg}'),
                 fg='red', wraplength=600, justify='left').pack(expand=True, padx=10)

    def _embed_baseline(self, fig, edges):
        from matplotlib.backends.backend_tkagg import (
            FigureCanvasTkAgg, NavigationToolbar2Tk)
        frame = self._baseline_frame
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        cv = FigureCanvasTkAgg(fig, master=frame)
        cv.draw()
        NavigationToolbar2Tk(cv, frame).update()
        cv.get_tk_widget().pack(fill='both', expand=True)
        self._baseline_canvas = cv
        self._baseline_edges = edges
        # 註銷舊登記、登記這個內嵌基線圖 → 完成事件廣播時會更新它
        if getattr(self, '_baseline_token', None) is not None:
            self.app.unregister_baseline(self._baseline_token)
        self._baseline_token = self.app.register_baseline(cv, edges)
        # 套用目前 tree 上每對的狀態 (done/error/running/cluster/pending)
        for iid in self._tree.get_children():
            tags = self._tree.item(iid, 'tags')
            if tags and '_' in iid:
                r, s = iid.split('_', 1)
                self.app.notify_pair_state(r, s, tags[0])

    def _poll_cluster_live(self):
        """每 3 秒輪詢 worker_<host>.json，即時顯示哪台正在處理哪一幅干涉。"""
        if not getattr(self, '_cluster_mode', False):
            return
        try:
            logs_dir = Path(self.app.state.project_dir) / 'logs'
            for lbl in list(getattr(self, '_cluster_chunks', {}).keys()):
                wf = logs_dir / f'worker_{lbl}.json'
                if not wf.exists():
                    continue
                try:
                    w = json.loads(wf.read_text(encoding='utf-8'))
                except Exception:
                    continue
                for p in w.get('done', []):
                    if '-' in p:
                        r, s = p.split('-', 1)
                        self._update_row_host(r, s, 'done', lbl)
                for p in w.get('failed', []):
                    if '-' in p:
                        r, s = p.split('-', 1)
                        self._update_row_host(r, s, 'error', lbl)
                cur = w.get('current', '')
                if cur and '-' in cur and cur not in w.get('done', []) \
                        and cur not in w.get('failed', []):
                    r, s = cur.split('-', 1)
                    self._update_row_host(r, s, 'running', lbl,
                                          detail=w.get('current_step', ''))
        except Exception:
            pass
        # 依目前線段顏色更新基線圖圖例計數 (Done/Pending/Running/Failed) → 數字跟線條同步
        self.app.refresh_baseline_legends()
        # 每 ~30 秒(每10輪) 重掃磁碟真值 → 「已完成數 + 綠標」跟磁碟同步,
        # 不依賴 worker JSON 的 done(每次 worker 重啟會歸零) → 修「跑很久顯示停滯」。
        self._poll_count = getattr(self, '_poll_count', 0) + 1
        if self._poll_count % 10 == 1:
            self._refresh_done_from_disk()
        # 持續輪詢直到叢集結束
        if getattr(self, '_cluster_mode', False):
            self.app.after(3000, self._poll_cluster_live)

    def _finish(self):
        # 先抓 cutoff (force 重跑時 = 開跑時間), 再關 _cluster_mode → 完成度只算
        # 本次重做完成的對; 重跑失敗者 (舊產物 mtime 未更新) 正確列為未完成、
        # MintPy 維持鎖定。非 force → cutoff 0 = 沿用 disk-truth。
        cutoff = self._run_cutoff()
        self._start_btn.configure(state='normal')
        # Re-enable cluster button only when cluster mode is still active
        cluster_enabled = getattr(self, '_cluster_var', None) and self._cluster_var.get()
        self._cluster_start_btn.configure(
            state='normal' if cluster_enabled else 'disabled')
        self._stop_btn.configure(state='disabled')
        self._cluster_mode = False   # 停止即時輪詢

        # 完成度以「磁碟上真實產物」判定，不是 host 程序有沒有結束。
        done_pairs, missing = self._count_done_pairs(cutoff)
        done, total = len(done_pairs), len(self.app.state.pairs)
        # 把磁碟確認完成的標綠 (host 程序崩潰早退也能反映真實狀態)
        for ref, sec in done_pairs:
            if self._tree.exists(f'{ref}_{sec}'):
                self._update_row_host(ref, sec, 'done', ('已完成' if LANG == 'zh' else 'Done'))
        self._pbar['maximum'] = total
        self._pbar['value'] = done
        self._progress_lbl.set((f'已完成 {done} / 未完成 {len(missing)} / 共 {total}' if LANG == 'zh' else f'Done {done} / Incomplete {len(missing)} / Total {total}'))
        self.app.refresh_baseline_legends()  # 讓基線圖圖例計數與最終狀態同步
        self._log.insert(
            'end', (f'\n[done] 磁碟確認完成 {done}/{total}，未完成 {len(missing)}。\n' if LANG == 'zh' else f'\n[done] Disk check: {done}/{total} done, {len(missing)} incomplete.\n'))
        self._log.see('end')

        if missing:
            # 尚未全部完成 → 不解鎖 MintPy，提示用「重跑未完成」接續
            self._log.insert(
                'end', (f'[!] 還有 {len(missing)} 對未完成；請取消勾選故障機後按'
                       f'「↻ 重跑未完成」繼續 (只會處理未完成的)。\n' if LANG == 'zh' else f'[!] {len(missing)} pairs still incomplete; please uncheck faulty hosts then click "↻ Rerun incomplete" to continue (only incomplete pairs will be processed).\n'))
            self._log.see('end')
            messagebox.showwarning(
                ('尚未全部完成' if LANG == 'zh' else 'Not All Done Yet'),
                (f'已完成 {done}/{total}，還有 {len(missing)} 對未完成。\n'
                f'MintPy 尚未解鎖。\n\n'
                f'請取消勾選故障工作站後按「↻ 重跑未完成」，\n'
                f'只會把未完成的重新分配給健康機器。' if LANG == 'zh' else f'Done {done}/{total}, {len(missing)} pairs still incomplete.\nMintPy not unlocked yet.\n\nPlease uncheck faulty hosts then click "↻ Rerun incomplete";\nonly the incomplete pairs will be reassigned to healthy hosts.'))
            return

        # 全部完成 → 檢查網路連通性 (無斷點) 才解鎖 MintPy
        bridges = find_bridge_pairs(list(self.app.state.pairs))
        if bridges:
            messagebox.showwarning(
                ('InSAR 網路有斷點' if LANG == 'zh' else 'InSAR Network Has Gaps'),
                (f'{total} 對全部完成, 但網路不連續 — 偵測到 {len(bridges)} 處'
                f'斷點/孤立子網路。\nMintPy SBAS 需要連通網路。\n\n'
                f'請在基線圖用「增加干涉對」補上橋接後再進入 MintPy。' if LANG == 'zh' else f'All {total} pairs are done, but the network is not connected — detected {len(bridges)} gap(s)/isolated sub-networks.\nMintPy SBAS requires a connected network.\n\nPlease use "Add pairs" in the baseline plot to add bridging pairs before entering MintPy.'))
            return
        self.app.notebook.tab(2, state='normal')
        self.app.tab3.init_cfg()
        messagebox.showinfo(('全部完成' if LANG == 'zh' else 'All Done'),
                            (f'{total}/{total} 對全部完成 ✓ 網路連續無斷點。\n'
                            f'Tab 3 MintPy 已解鎖。' if LANG == 'zh' else f'{total}/{total} pairs all done ✓ Network is connected with no gaps.\nTab 3 MintPy is now unlocked.'))


# ─────────────────────────────────────────────────────────────────────────
# MintPy env helpers
# ─────────────────────────────────────────────────────────────────────────
_MINTPY_CONDA_ENVS = ('FastISCE2', 'isce2')

def _find_mintpy_python() -> tuple:
    """Return (env_name, python_path, bin_dir) for first conda env with mintpy."""
    base = Path(os.path.expanduser('~/miniconda3/envs'))
    for env in _MINTPY_CONDA_ENVS:
        py  = base / env / 'bin' / 'python'
        app = base / env / 'bin' / 'smallbaselineApp.py'
        if py.exists() and app.exists():
            return env, str(py), str(base / env / 'bin')
    return '', sys.executable, ''

def _project_has_snap_output(project_dir: str) -> bool:
    """True if project has ≥1 pair with *_unw_tc.dim (SNAP pipeline completed)."""
    ifg = Path(project_dir) / 'interferograms'
    if not ifg.is_dir():
        return False
    for pair_dir in ifg.iterdir():
        if pair_dir.is_dir() and list(pair_dir.glob('*_unw_tc.dim')):
            return True
    return False


_MINTPY_TOOLS_HELP = """\
═══════════════════════════════════════════════════════════════════════
 MintPy 後處理工具參考  (https://github.com/insarlab/MintPy)
 在下方終端輸入指令，或複製後貼上執行
═══════════════════════════════════════════════════════════════════════

━━ 1. 資料資訊 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
info.py inputs/ifgramStack.h5              # 查看所有資料集與屬性
info.py inputs/ifgramStack.h5 --date       # 列出所有日期
info.py timeseries.h5 --date               # 時間序列日期清單
info.py velocity.h5                        # 速度檔屬性

━━ 2. 影像顯示 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
view.py inputs/ifgramStack.h5 unwrapPhase  # 顯示解纏相位（所有對）
view.py inputs/ifgramStack.h5 coherence    # 顯示相干係數
view.py timeseries.h5                      # 形變時間序列（每張日期）
view.py velocity.h5                        # 視線向速度圖（mm/yr）
view.py temporalCoherence.h5               # 時間相干圖（0~1）
view.py velocity.h5 --dem inputs/geometryGeo.h5  # 疊加 DEM 陰影

━━ 3. 互動式時間序列 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tsview.py timeseries.h5                    # 點選像素顯示時間序列
tsview.py timeseries_demErr.h5             # 含 DEM 誤差校正後結果
tsview.py timeseries_demErr_ramp.h5        # 含相位趨勢移除

━━ 4. 遮罩 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
mask.py temporalCoherence.h5 -m 0.7       # 時間相干 ≥ 0.7 產生遮罩
mask.py timeseries.h5 -m maskTempCoh.h5   # 套用遮罩到時間序列

━━ 5. 地理編碼（雷達坐標 → 地理坐標）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
geocode.py timeseries_demErr.h5 -l inputs/geometryGeo.h5
geocode.py velocity.h5           -l inputs/geometryGeo.h5
geocode.py temporalCoherence.h5  -l inputs/geometryGeo.h5
# 輸出加 geo_ 前綴，例如 geo_velocity.h5

━━ 6. 匯出格式 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ── GeoTIFF (GDAL) ──────────────────────────────────────────────────
save_gdal.py geo_velocity.h5                        # → geo_velocity.tif
save_gdal.py geo_timeseries_demErr.h5 --date 20251030  # 指定日期
save_gdal.py geo_timeseries_demErr.h5 --date-list dates.txt

# ── GMT .grd (NetCDF-3) ─────────────────────────────────────────────
save_gmt.py geo_velocity.h5                         # → geo_velocity.grd

# ── HDF-EOS5 (NASA 標準格式) ────────────────────────────────────────
save_hdfeos5.py timeseries_demErr.h5                # → S1*.he5

# ── ROIPAC .unw (StaMPS 輸入) ───────────────────────────────────────
save_roipac.py timeseries_demErr.h5

━━ 7. 常用工作流程 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 完整跑 smallbaselineApp（使用上方 ▶ 按鈕或）：
smallbaselineApp.py smallbaselineApp.cfg

# 只跑特定步驟：
smallbaselineApp.py smallbaselineApp.cfg --dostep load_data
smallbaselineApp.py smallbaselineApp.cfg --dostep reference_point
smallbaselineApp.py smallbaselineApp.cfg --dostep network_inversion
smallbaselineApp.py smallbaselineApp.cfg --dostep velocity

# 從指定步驟重跑：
smallbaselineApp.py smallbaselineApp.cfg --start network_inversion

━━ 8. 參考資料 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GitHub  : https://github.com/insarlab/MintPy
教學文件: https://mintpy.readthedocs.io
範例    : https://github.com/insarlab/MintPy-tutorial
"""

_MINTPY_TOOLS_HELP_EN = """\
═══════════════════════════════════════════════════════════════════════
 MintPy Post-processing Tools Reference  (https://github.com/insarlab/MintPy)
 Type commands below in the terminal, or copy and paste to run
═══════════════════════════════════════════════════════════════════════

━━ 1. Data Info ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
info.py inputs/ifgramStack.h5              # show all datasets and attributes
info.py inputs/ifgramStack.h5 --date       # list all dates
info.py timeseries.h5 --date               # time series date list
info.py velocity.h5                        # velocity file attributes

━━ 2. Image Display ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
view.py inputs/ifgramStack.h5 unwrapPhase  # show unwrapped phase (all pairs)
view.py inputs/ifgramStack.h5 coherence    # show coherence
view.py timeseries.h5                      # deformation time series (each date)
view.py velocity.h5                        # line-of-sight velocity map (mm/yr)
view.py temporalCoherence.h5               # temporal coherence map (0~1)
view.py velocity.h5 --dem inputs/geometryGeo.h5  # overlay DEM shading

━━ 3. Interactive Time Series ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
tsview.py timeseries.h5                    # click a pixel to show its time series
tsview.py timeseries_demErr.h5             # with DEM error correction applied
tsview.py timeseries_demErr_ramp.h5        # with phase ramp removed

━━ 4. Masking ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
mask.py temporalCoherence.h5 -m 0.7       # generate mask for temporal coherence ≥ 0.7
mask.py timeseries.h5 -m maskTempCoh.h5   # apply mask to time series

━━ 5. Geocoding (radar coordinates → geo coordinates) ━━━━━━━━━━━━━━━━━━━━━━━━
geocode.py timeseries_demErr.h5 -l inputs/geometryGeo.h5
geocode.py velocity.h5           -l inputs/geometryGeo.h5
geocode.py temporalCoherence.h5  -l inputs/geometryGeo.h5
# output gets geo_ prefix, e.g. geo_velocity.h5

━━ 6. Export Formats ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ── GeoTIFF (GDAL) ────────────────────────────────────────────────────────────
save_gdal.py geo_velocity.h5                        # → geo_velocity.tif
save_gdal.py geo_timeseries_demErr.h5 --date 20251030  # specific date
save_gdal.py geo_timeseries_demErr.h5 --date-list dates.txt

# ── GMT .grd (NetCDF-3) ──────────────────────────────────────────────────
save_gmt.py geo_velocity.h5                         # → geo_velocity.grd

# ── HDF-EOS5 (NASA standard format) ──────────────────────────────
save_hdfeos5.py timeseries_demErr.h5                # → S1*.he5

# ── ROIPAC .unw (StaMPS input) ──────────────────────────────
save_roipac.py timeseries_demErr.h5

━━ 7. Common Workflows ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Run the full smallbaselineApp (use the ▶ button above, or):
smallbaselineApp.py smallbaselineApp.cfg

# Run a specific step only:
smallbaselineApp.py smallbaselineApp.cfg --dostep load_data
smallbaselineApp.py smallbaselineApp.cfg --dostep reference_point
smallbaselineApp.py smallbaselineApp.cfg --dostep network_inversion
smallbaselineApp.py smallbaselineApp.cfg --dostep velocity

# Rerun from a specific step:
smallbaselineApp.py smallbaselineApp.cfg --start network_inversion

━━ 8. References ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GitHub  : https://github.com/insarlab/MintPy
Docs    : https://mintpy.readthedocs.io
Examples: https://github.com/insarlab/MintPy-tutorial
"""


# ─────────────────────────────────────────────────────────────────────────
# Embedded terminal widget
# ─────────────────────────────────────────────────────────────────────────
class _TerminalWidget(ttk.Frame):
    """Simple bash terminal widget — runs commands in a conda env."""

    def __init__(self, parent, cwd: str = '.', env_name: str = '',
                 bin_dir: str = ''):
        super().__init__(parent)
        self._cwd     = Path(cwd).expanduser().resolve()
        self._env_name = env_name
        self._bin_dir  = bin_dir
        self._proc: Optional[subprocess.Popen] = None
        self._history: List[str] = []
        self._hist_idx: int = -1
        self._build()

    def _build(self):
        # output area (dark theme)
        out_frame = ttk.Frame(self)
        out_frame.pack(fill='both', expand=True)
        self._out = scrolledtext.ScrolledText(
            out_frame, height=10, font=('Consolas', 9),
            bg='#1e1e2e', fg='#cdd6f4', insertbackground='white',
            selectbackground='#45475a', wrap='char', exportselection=False)
        self._out.pack(fill='both', expand=True)
        self._out.tag_config('prompt', foreground='#89b4fa', font=('Consolas', 9, 'bold'))
        self._out.tag_config('cmd',    foreground='#a6e3a1')
        self._out.tag_config('err',    foreground='#f38ba8')
        self._out.tag_config('ok',     foreground='#89b4fa')
        self._out.tag_config('info',   foreground='#6c7086')

        # input row
        inp = ttk.Frame(self)
        inp.pack(fill='x', pady=(2, 0))
        self._prompt_lbl = ttk.Label(inp, text=self._short_cwd(),
                                     foreground='#89b4fa',
                                     font=('Consolas', 9, 'bold'))
        self._prompt_lbl.pack(side='left', padx=(4, 2))
        ttk.Label(inp, text='$', foreground='#a6e3a1',
                  font=('Consolas', 10, 'bold')).pack(side='left')
        self._entry = ttk.Entry(inp, font=('Consolas', 10))
        self._entry.pack(side='left', fill='x', expand=True, padx=4)
        self._entry.bind('<Return>',  lambda _e: self._run_cmd())
        self._entry.bind('<Up>',      lambda _e: self._hist_up())
        self._entry.bind('<Down>',    lambda _e: self._hist_down())
        self._entry.bind('<Tab>',     lambda _e: 'break')  # prevent focus steal
        ttk.Button(inp, text='▶', width=3,
                   command=self._run_cmd).pack(side='left')
        ttk.Button(inp, text='⏹', width=3,
                   command=self._kill_cmd).pack(side='left', padx=(2, 4))
        ttk.Button(inp, text='✕ Clear', width=8,
                   command=self._clear).pack(side='right', padx=4)

        self._append(f'# conda env: {self._env_name or "system"}  '
                     f'cwd: {self._cwd}\n', 'info')
        self._append(('# 支援 cd 切換目錄；上下鍵瀏覽歷史；Tab 不補全\n' if LANG == 'zh' else '# Supports cd to change directory; use up/down arrows to browse history; Tab does not autocomplete\n'), 'info')

    # ── public API ───────────────────────────────────────────────────────
    def set_cwd(self, cwd: str):
        self._cwd = Path(cwd).expanduser().resolve()
        self._prompt_lbl.config(text=self._short_cwd())

    # ── internal ─────────────────────────────────────────────────────────
    def _short_cwd(self) -> str:
        home = Path.home()
        try:
            return '~/' + str(self._cwd.relative_to(home))
        except ValueError:
            return str(self._cwd)

    def _run_cmd(self):
        raw = self._entry.get().strip()
        if not raw:
            return
        self._history.append(raw)
        self._hist_idx = -1
        self._entry.delete(0, 'end')

        self._append(f'\n{self._short_cwd()} $ ', 'prompt')
        self._append(f'{raw}\n', 'cmd')

        # handle 'cd' locally
        if raw.startswith('cd'):
            parts = raw.split(None, 1)
            target = parts[1] if len(parts) > 1 else str(Path.home())
            if target.startswith('~'):
                target = str(Path.home()) + target[1:]
            new_cwd = (self._cwd / target).resolve()
            if new_cwd.is_dir():
                self._cwd = new_cwd
                self._prompt_lbl.config(text=self._short_cwd())
                self._append(f'[cwd] {self._cwd}\n', 'ok')
            else:
                self._append(f'bash: cd: {target}: No such file or directory\n', 'err')
            return

        def _work():
            try:
                env = dict(os.environ)
                if self._bin_dir:
                    env['PATH'] = self._bin_dir + ':' + env.get('PATH', '')
                env['PYTHONUNBUFFERED'] = '1'
                # Activate conda env inside bash so conda-installed tools work
                if self._env_name:
                    conda_sh = os.path.expanduser(
                        '~/miniconda3/etc/profile.d/conda.sh')
                    prefix = (f'source {conda_sh} 2>/dev/null && '
                              f'conda activate {self._env_name} 2>/dev/null && ')
                else:
                    prefix = ''
                self._proc = subprocess.Popen(
                    ['bash', '-c', prefix + raw],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    bufsize=1, text=True, cwd=str(self._cwd), env=env)
                for line in self._proc.stdout:  # type: ignore[union-attr]
                    self.after(0, self._append, line)
                rc = self._proc.wait()
                tag = 'ok' if rc == 0 else 'err'
                self.after(0, self._append, f'\n[exit {rc}]\n', tag)
            except Exception as exc:
                self.after(0, self._append, f'[ERROR] {exc}\n', 'err')
            finally:
                self._proc = None

        threading.Thread(target=_work, daemon=True).start()

    def _kill_cmd(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._append('[terminated]\n', 'err')

    def _clear(self):
        self._out.delete('1.0', 'end')

    def _hist_up(self):
        if not self._history:
            return
        self._hist_idx = (len(self._history) - 1
                          if self._hist_idx == -1
                          else max(0, self._hist_idx - 1))
        self._entry.delete(0, 'end')
        self._entry.insert(0, self._history[self._hist_idx])

    def _hist_down(self):
        if self._hist_idx == -1:
            return
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self._entry.delete(0, 'end')
            self._entry.insert(0, self._history[self._hist_idx])
        else:
            self._hist_idx = -1
            self._entry.delete(0, 'end')

    def _append(self, text: str, tag: str = ''):
        self._out.insert('end', text, tag)
        self._out.see('end')


def _deramp_velocity(data: np.ndarray) -> np.ndarray:
    """Fit and remove a linear ramp (plane: z = a·row + b·col + c) from velocity data.

    Only valid (finite, non-zero) pixels are used for fitting.
    Returns a copy with the ramp subtracted; NaN pixels remain NaN.
    """
    ny, nx = data.shape
    valid = np.isfinite(data)
    if valid.sum() < 10:
        return data
    yy, xx = np.mgrid[0:ny, 0:nx]
    y_v = yy[valid].astype('float64')
    x_v = xx[valid].astype('float64')
    z_v = data[valid].astype('float64')
    A = np.column_stack([y_v, x_v, np.ones_like(y_v)])
    coef, _, _, _ = np.linalg.lstsq(A, z_v, rcond=None)
    ramp = (coef[0] * yy + coef[1] * xx + coef[2]).astype('float32')
    out = data.copy()
    out[valid] -= ramp[valid]
    return out


# ─────────────────────────────────────────────────────────────────────────
# Tab 3 — MintPy
# ─────────────────────────────────────────────────────────────────────────
class MintPyFrame(ttk.Frame):
    def __init__(self, nb: ttk.Notebook, app: 'Snap2MintPyApp'):
        super().__init__(nb)
        self.app  = app
        self._proc: Optional[subprocess.Popen] = None
        self._env_name, self._py_exe, self._bin_dir = _find_mintpy_python()
        self._build()

    def _build(self):
        # ── top: workdir row ──────────────────────────────────────────────
        top = ttk.Frame(self); top.pack(fill='x', padx=8, pady=4)
        ttk.Label(top, text=_T('lbl_workdir')).pack(side='left')
        _mp_default = str(Path(self.app.state.project_dir) / 'mintpy') \
                      if self.app.state.project_dir else ''
        self._mp_var = tk.StringVar(value=_mp_default)
        ttk.Entry(top, textvariable=self._mp_var, width=55).pack(
            side='left', fill='x', expand=True, padx=4)
        ttk.Button(top, text='…', width=3,
                   command=lambda: (
                       self._mp_var.set(filedialog.askdirectory()
                                        or self._mp_var.get()),
                       self._reload_cfg(),
                       self._sync_terminal_cwd()
                   )).pack(side='left')
        ttk.Button(top, text=_T('btn_reload'),
                   command=self._reload_cfg).pack(side='left', padx=4)
        env_lbl = (f'env: {self._env_name}' if self._env_name
                   else 'env: system')
        ttk.Label(top, text=env_lbl, foreground='#666',
                  font=('TkDefaultFont', 8)).pack(side='right', padx=6)

        # ── vertical PanedWindow: cfg area | bottom notebook ─────────────
        paned = tk.PanedWindow(self, orient='vertical',
                               sashwidth=5, sashrelief='flat',
                               bg='#cccccc')
        paned.pack(fill='both', expand=True, padx=4, pady=2)

        # upper pane: cfg editor + run buttons + log
        upper = ttk.Frame(paned)
        paned.add(upper, minsize=200)

        cf = ttk.LabelFrame(upper, text=_T('lf_cfg'))
        cf.pack(fill='both', expand=True, padx=4, pady=2)
        self._cfg_text = scrolledtext.ScrolledText(
            cf, height=10, font=('Consolas', 9), exportselection=False)
        self._cfg_text.pack(fill='both', expand=True, padx=2, pady=2)

        btns = ttk.Frame(upper); btns.pack(fill='x', padx=4, pady=2)
        ttk.Button(btns, text=_T('btn_save_cfg'),
                   command=self._save_cfg).pack(side='left', padx=4)
        # 反演加權 (weightFunc): var=同調加權(近GNSS)/no=均權(低同調植被)/coh/fim
        ttk.Label(btns, text=('反演加權:' if LANG == 'zh' else 'Inversion weighting:')).pack(side='left', padx=(12, 2))
        self._weight_var = tk.StringVar(
            value=getattr(self.app.state, 'mp_weight_func', 'var'))
        _wcb = ttk.Combobox(btns, textvariable=self._weight_var, width=5,
                            state='readonly', values=['var', 'no', 'coh', 'fim'])
        _wcb.pack(side='left')

        def _on_weight_change(*_):
            import re as _re
            self.app.state.mp_weight_func = self._weight_var.get()
            txt = self._cfg_text.get('1.0', 'end')
            new = _re.sub(r'(mintpy\.networkInversion\.weightFunc\s*=\s*)\S+',
                          r'\g<1>' + self._weight_var.get(), txt)
            if new != txt:
                self._cfg_text.delete('1.0', 'end')
                self._cfg_text.insert('end', new)
        _wcb.bind('<<ComboboxSelected>>', _on_weight_change)
        self._run_btn = ttk.Button(
            btns, text=_T('btn_run_mintpy'),
            command=self._run_mintpy)
        self._run_btn.pack(side='left', padx=4)
        self._stop_btn = ttk.Button(
            btns, text=_T('btn_stop_mintpy'), command=self._stop_mintpy, state='disabled')
        self._stop_btn.pack(side='left', padx=4)
        self._status_var = tk.StringVar(value='')
        ttk.Label(btns, textvariable=self._status_var,
                  font=('TkDefaultFont', 10, 'bold'),
                  foreground='#0a8').pack(side='left', padx=10)

        # ── basemap view buttons ───────────────────────────────────────────
        vbtns = ttk.Frame(upper); vbtns.pack(fill='x', padx=4, pady=(0, 2))
        ttk.Label(vbtns, text=_T('lbl_basemap')).pack(side='left', padx=(4, 6))
        self._basemap_var = tk.StringVar(value='satellite')
        for bm_key, val in [('bm_satellite', 'satellite'),
                              ('bm_google',    'google'),
                              ('bm_osm',       'osm'),
                              ('bm_topo',      'topo'),
                              ('bm_cartodb',   'cartodb')]:
            ttk.Radiobutton(vbtns, text=_T(bm_key), variable=self._basemap_var,
                            value=val).pack(side='left', padx=3)
        # Velocity coherence mask threshold (temporalCoherence.h5); blank = no coh mask
        ttk.Label(vbtns, text=_T('lbl_vel_coh')).pack(side='left', padx=(10, 2))
        self._vel_coh_thresh_var = tk.StringVar(value='0.4')
        ttk.Combobox(vbtns, textvariable=self._vel_coh_thresh_var, width=5,
                     values=['', '0.3', '0.4', '0.5', '0.6', '0.7']
                     ).pack(side='left', padx=2)
        ttk.Button(vbtns, text=_T('btn_view_vel'),
                   command=self._view_with_basemap).pack(side='left', padx=(12, 4))
        ttk.Button(vbtns, text=_T('btn_save_png'),
                   command=lambda: self._view_with_basemap(save=True)
                   ).pack(side='left', padx=2)

        lf = ttk.LabelFrame(upper, text=_T('lf_mintpy_out'))
        lf.pack(fill='both', expand=True, padx=4, pady=2)
        self._log = _make_log(lf, height=8)

        # lower pane: tools reference + terminal (sub-notebook)
        lower = ttk.Frame(paned)
        paned.add(lower, minsize=160)

        sub_nb = ttk.Notebook(lower)
        sub_nb.pack(fill='both', expand=True)

        # ── sub-tab A: post-processing tools reference ────────────────────
        tools_frame = ttk.Frame(sub_nb)
        sub_nb.add(tools_frame, text=('後處理工具說明' if LANG == 'zh' else 'Post-processing Tools Reference'))
        tools_txt = scrolledtext.ScrolledText(
            tools_frame, height=10, font=('Consolas', 9),
            wrap='none', bg='#f8f8f8', exportselection=False)
        tools_txt.pack(fill='both', expand=True, padx=2, pady=2)
        tools_txt.insert('1.0', _MINTPY_TOOLS_HELP if LANG == 'zh' else _MINTPY_TOOLS_HELP_EN)
        tools_txt.config(state='disabled')

        # ── sub-tab B: export ────────────────────────────────────────────
        export_frame = ttk.Frame(sub_nb)
        sub_nb.add(export_frame, text=_T('tab_export'))
        self._build_export_tab(export_frame)

        # ── sub-tab C: terminal ───────────────────────────────────────────
        term_frame = ttk.Frame(sub_nb)
        sub_nb.add(term_frame, text=('終端 Terminal' if LANG == 'zh' else 'Terminal'))
        mp_cwd = (Path(self.app.state.project_dir) / 'mintpy'
                  if hasattr(self.app, 'state') else Path.home())
        self._terminal = _TerminalWidget(
            term_frame,
            cwd=str(mp_cwd),
            env_name=self._env_name,
            bin_dir=self._bin_dir)
        self._terminal.pack(fill='both', expand=True)

    # ── export tab ───────────────────────────────────────────────────────
    # Source table: (display_name, h5_filename, dataset_key, out_unit, scale)
    _GTIFF_SOURCES = [
        ('velocity.h5  (velocity)',          'velocity.h5',          'velocity',           'mm/yr', 1000.0),
        ('avgSpatialCoh.h5  (coherence)',    'avgSpatialCoh.h5',     'coherence',          '0~1',   1.0),
        ('temporalCoherence.h5',             'temporalCoherence.h5', 'temporalCoherence',  '0~1',   1.0),
    ]
    _TS_SOURCES = [
        'timeseries_ramp_demErr.h5',
        'timeseries_demErr.h5',
        'timeseries_ramp.h5',
        'timeseries.h5',
    ]

    def _build_export_tab(self, frame: ttk.Frame):
        # ── GeoTIFF section ───────────────────────────────────────────────
        gf = ttk.LabelFrame(frame, text=_T('lf_geotiff'))
        gf.pack(fill='x', padx=8, pady=6)

        r0 = ttk.Frame(gf); r0.pack(fill='x', padx=6, pady=3)
        ttk.Label(r0, text=_T('lbl_source')).pack(side='left')
        self._gt_src_var = tk.StringVar()
        gt_names = [s[0] for s in self._GTIFF_SOURCES]
        self._gt_src_cb = ttk.Combobox(r0, textvariable=self._gt_src_var,
                                        values=gt_names, state='readonly', width=42)
        self._gt_src_cb.current(0)
        self._gt_src_cb.pack(side='left', padx=6)
        ttk.Label(r0, text=_T('lbl_unit_detect')).pack(side='left', padx=(12, 2))
        self._gt_unit_lbl = tk.StringVar(value=self._GTIFF_SOURCES[0][3])
        ttk.Label(r0, textvariable=self._gt_unit_lbl, foreground='#226',
                  font=('TkDefaultFont', 9, 'bold')).pack(side='left')
        self._gt_src_cb.bind('<<ComboboxSelected>>', self._on_gt_src_change)

        r1 = ttk.Frame(gf); r1.pack(fill='x', padx=6, pady=2)
        self._gt_mask_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text=_T('lbl_mask'), variable=self._gt_mask_var).pack(side='left')
        self._gt_deramp_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text=_T('lbl_deramp'),
                        variable=self._gt_deramp_var).pack(side='left', padx=(16, 0))

        r1c = ttk.Frame(gf); r1c.pack(fill='x', padx=6, pady=2)
        ttk.Label(r1c, text=_T('lbl_coh_thresh')).pack(side='left')
        self._gt_coh_thresh_var = tk.StringVar(value='')
        ttk.Combobox(r1c, textvariable=self._gt_coh_thresh_var,
                     values=['', '0.3', '0.4', '0.5', '0.6'],
                     width=6).pack(side='left', padx=4)
        ttk.Label(r1c, text=_T('lbl_coh_hint'),
                  foreground='#888', font=('TkDefaultFont', 8)).pack(side='left', padx=4)

        r2 = ttk.Frame(gf); r2.pack(fill='x', padx=6, pady=3)
        ttk.Label(r2, text=_T('lbl_out_path')).pack(side='left')
        self._gt_out_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self._gt_out_var, width=50).pack(
            side='left', fill='x', expand=True, padx=4)
        ttk.Button(r2, text='…', width=3,
                   command=self._browse_gt_out).pack(side='left')

        r3 = ttk.Frame(gf); r3.pack(fill='x', padx=6, pady=4)
        ttk.Button(r3, text=_T('btn_export_gtiff'),
                   command=self._export_geotiff).pack(side='left', padx=4)
        self._gt_status = tk.StringVar(value='')
        ttk.Label(r3, textvariable=self._gt_status, foreground='#0a8',
                  font=('TkDefaultFont', 9, 'bold')).pack(side='left', padx=8)

        # ── Time-series CSV section ───────────────────────────────────────
        tf = ttk.LabelFrame(frame, text=_T('lf_ts_csv'))
        tf.pack(fill='x', padx=8, pady=6)

        t0 = ttk.Frame(tf); t0.pack(fill='x', padx=6, pady=3)
        ttk.Label(t0, text=_T('lbl_source')).pack(side='left')
        self._ts_src_var = tk.StringVar()
        self._ts_src_cb = ttk.Combobox(t0, textvariable=self._ts_src_var,
                                        values=self._TS_SOURCES, state='readonly', width=38)
        self._ts_src_cb.current(0)
        self._ts_src_cb.pack(side='left', padx=6)
        ttk.Label(t0, text=_T('lbl_unit_detect')).pack(side='left', padx=(12, 2))
        ttk.Label(t0, text='mm', foreground='#226',
                  font=('TkDefaultFont', 9, 'bold')).pack(side='left')
        self._ts_info_lbl = tk.StringVar(value='')
        ttk.Label(t0, textvariable=self._ts_info_lbl,
                  foreground='#666', font=('TkDefaultFont', 8)).pack(side='left', padx=8)
        self._ts_src_cb.bind('<<ComboboxSelected>>', self._on_ts_src_change)
        self._on_ts_src_change()  # init epoch count

        t1 = ttk.Frame(tf); t1.pack(fill='x', padx=6, pady=2)
        self._ts_mask_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(t1, text=_T('lbl_mask'), variable=self._ts_mask_var).pack(side='left')

        t2 = ttk.Frame(tf); t2.pack(fill='x', padx=6, pady=3)
        ttk.Label(t2, text=_T('lbl_out_path')).pack(side='left')
        self._ts_out_var = tk.StringVar()
        ttk.Entry(t2, textvariable=self._ts_out_var, width=50).pack(
            side='left', fill='x', expand=True, padx=4)
        ttk.Button(t2, text='…', width=3,
                   command=self._browse_ts_out).pack(side='left')

        t3 = ttk.Frame(tf); t3.pack(fill='x', padx=6, pady=4)
        ttk.Button(t3, text=_T('btn_export_csv'),
                   command=self._export_ts_csv).pack(side='left', padx=4)
        self._ts_status = tk.StringVar(value='')
        ttk.Label(t3, textvariable=self._ts_status, foreground='#0a8',
                  font=('TkDefaultFont', 9, 'bold')).pack(side='left', padx=8)

        self._init_export_defaults()

    def _init_export_defaults(self):
        mp = self._mp_var.get().strip()
        if mp:
            self._gt_out_var.set(str(Path(mp) / 'velocity_mm.tif'))
            self._ts_out_var.set(str(Path(mp) / 'timeseries_mm.csv'))

    def _on_gt_src_change(self, *_):
        idx = self._gt_src_cb.current()
        if 0 <= idx < len(self._GTIFF_SOURCES):
            self._gt_unit_lbl.set(self._GTIFF_SOURCES[idx][3])
            # Update default output filename
            h5name = self._GTIFF_SOURCES[idx][1]
            mp = self._mp_var.get().strip()
            stem = h5name.replace('.h5', '')
            unit = self._GTIFF_SOURCES[idx][3].replace('/', '_').replace('~', '-')
            if mp:
                self._gt_out_var.set(str(Path(mp) / f'{stem}_{unit}.tif'))

    def _on_ts_src_change(self, *_):
        mp = self._mp_var.get().strip()
        h5name = self._ts_src_var.get() or self._TS_SOURCES[0]
        h5path = Path(mp) / h5name if mp else None
        if h5path and h5path.exists():
            try:
                import h5py as _h5
                with _h5.File(str(h5path), 'r') as h:
                    n = h['timeseries'].shape[0]
                self._ts_info_lbl.set(f'({n} epochs)')
            except Exception:
                self._ts_info_lbl.set('')
        else:
            self._ts_info_lbl.set('(檔案不存在)' if LANG == 'zh' else '(file not found)')

    def _browse_gt_out(self):
        p = filedialog.asksaveasfilename(
            defaultextension='.tif',
            filetypes=[('GeoTIFF', '*.tif *.tiff'), ('All', '*')],
            initialdir=self._mp_var.get().strip() or '.')
        if p:
            self._gt_out_var.set(p)

    def _browse_ts_out(self):
        p = filedialog.asksaveasfilename(
            defaultextension='.csv',
            filetypes=[('CSV', '*.csv'), ('All', '*')],
            initialdir=self._mp_var.get().strip() or '.')
        if p:
            self._ts_out_var.set(p)

    def _export_geotiff(self):
        import h5py as _h5
        try:
            import rasterio
            from rasterio.transform import from_origin
        except ImportError:
            messagebox.showerror('rasterio', ('rasterio 未安裝：pip install rasterio' if LANG == 'zh' else 'rasterio not installed: pip install rasterio'))
            return

        mp      = Path(self._mp_var.get().strip())
        idx     = self._gt_src_cb.current()
        _, h5fn, ds_key, unit_label, scale = self._GTIFF_SOURCES[idx]
        h5path  = mp / h5fn
        outpath = self._gt_out_var.get().strip()

        if not h5path.exists():
            messagebox.showerror('Export', (f'找不到檔案：{h5path}' if LANG == 'zh' else f'File not found: {h5path}'))
            return
        if not outpath:
            messagebox.showerror('Export', ('請指定輸出路徑。' if LANG == 'zh' else 'Please specify an output path.'))
            return

        self._gt_status.set('處理中…' if LANG == 'zh' else 'Processing…')
        self.update_idletasks()
        try:
            with _h5.File(str(h5path), 'r') as h:
                data = h[ds_key][:].astype('float32')
                atr  = dict(h.attrs)

            if data.ndim == 3:
                data = data[-1]

            data = data * scale
            data[data == 0] = float('nan')

            # Apply maskTempCoh.h5
            mask_path = mp / 'maskTempCoh.h5'
            if self._gt_mask_var.get() and mask_path.exists():
                with _h5.File(str(mask_path), 'r') as hm:
                    mask = hm['mask'][:]
                data[mask == 0] = float('nan')

            # DeRamp (remove linear plane from velocity)
            if self._gt_deramp_var.get() and 'velocity' in h5fn:
                data = _deramp_velocity(data)

            # Coh threshold mask via temporalCoherence.h5 (真實同調性; avgSpatialCoh
            # 被 smart-ML max-coh 灌水, 遮不掉水域 → 改用 temporalCoherence)
            _thresh_str = self._gt_coh_thresh_var.get().strip()
            if _thresh_str:
                try:
                    _thresh = float(_thresh_str)
                    _coh_path = mp / 'temporalCoherence.h5'
                    if _coh_path.exists():
                        with _h5.File(str(_coh_path), 'r') as _hc:
                            _coh = _hc['temporalCoherence'][:].astype('float32')
                        data[_coh < _thresh] = float('nan')
                    else:
                        self._gt_status.set(
                            '[warn] temporalCoherence.h5 不存在'
                            if LANG == 'zh' else '[warn] temporalCoherence.h5 not found')
                except ValueError:
                    pass  # invalid threshold string, skip

            x0 = float(atr['X_FIRST'])
            y0 = float(atr['Y_FIRST'])
            dx = float(atr['X_STEP'])
            dy = float(atr['Y_STEP'])   # negative
            ny, nx = data.shape

            # upper-left corner of top-left pixel
            west  = x0 - dx / 2.0
            north = y0 - dy / 2.0       # dy < 0, so -dy/2 > 0 → moves north

            transform = from_origin(west, north, dx, abs(dy))

            Path(outpath).parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(
                outpath, 'w',
                driver='GTiff',
                height=ny, width=nx,
                count=1, dtype='float32',
                crs='EPSG:4326',
                transform=transform,
                nodata=float('nan'),
            ) as dst:
                dst.write(data, 1)
                dst.update_tags(
                    UNIT=unit_label,
                    SOURCE=h5fn,
                    DATASET=ds_key,
                    MASK='maskTempCoh.h5' if self._gt_mask_var.get() else 'none',
                    DERAMP='plane' if (self._gt_deramp_var.get() and 'velocity' in h5fn) else 'none',
                    COH_THRESH=self._gt_coh_thresh_var.get().strip() or 'none',
                )

            self._gt_status.set(f'[OK] {Path(outpath).name}')
        except Exception as exc:
            self._gt_status.set(f'[ERROR] {exc}')
            messagebox.showerror('GeoTIFF export', str(exc))

    def _export_ts_csv(self):
        import h5py as _h5

        mp      = Path(self._mp_var.get().strip())
        h5fn    = self._ts_src_var.get() or self._TS_SOURCES[0]
        h5path  = mp / h5fn
        outpath = self._ts_out_var.get().strip()

        if not h5path.exists():
            messagebox.showerror('Export', (f'找不到檔案：{h5path}' if LANG == 'zh' else f'File not found: {h5path}'))
            return
        if not outpath:
            messagebox.showerror('Export', ('請指定輸出路徑。' if LANG == 'zh' else 'Please specify an output path.'))
            return

        self._ts_status.set('處理中…' if LANG == 'zh' else 'Processing…')
        self.update_idletasks()
        try:
            with _h5.File(str(h5path), 'r') as h:
                ts   = h['timeseries'][:].astype('float64')   # (ndate, ny, nx)  unit: m
                dates = list(h['date'][:].astype(str))
                atr   = dict(h.attrs)

            ts = ts * 1000.0   # m → mm

            # Build lon / lat arrays from metadata
            x0 = float(atr['X_FIRST'])
            y0 = float(atr['Y_FIRST'])
            dx = float(atr['X_STEP'])
            dy = float(atr['Y_STEP'])
            ndate, ny, nx = ts.shape
            lon_arr = x0 + np.arange(nx) * dx   # shape (nx,)
            lat_arr = y0 + np.arange(ny) * dy   # shape (ny,)

            # Apply mask
            mask_path = mp / 'maskTempCoh.h5'
            if self._ts_mask_var.get() and mask_path.exists():
                with _h5.File(str(mask_path), 'r') as hm:
                    mask = hm['mask'][:]     # (ny, nx)
            else:
                mask = np.ones((ny, nx), dtype=bool)

            valid_rows, valid_cols = np.where(mask.astype(bool))

            Path(outpath).parent.mkdir(parents=True, exist_ok=True)
            date_cols = ','.join(dates)
            with open(outpath, 'w', encoding='utf-8') as fo:
                fo.write(f'#LOS timeseries  unit: mm\n')
                fo.write(f'lon,lat,{date_cols}\n')
                for row, col in zip(valid_rows, valid_cols):
                    lon = x0 + col * dx
                    lat = y0 + row * dy
                    vals = ts[:, row, col]
                    # skip pixels where all values are 0 (unprocessed)
                    if not np.any(vals != 0):
                        continue
                    vals_str = ','.join(
                        f'{v:.4f}' if np.isfinite(v) else 'nan'
                        for v in vals
                    )
                    fo.write(f'{lon:.8f},{lat:.8f},{vals_str}\n')

            self._ts_status.set(f'[OK] {Path(outpath).name}  ({len(valid_rows)} pixels)')
        except Exception as exc:
            self._ts_status.set(f'[ERROR] {exc}')
            messagebox.showerror('CSV export', str(exc))

    # ── public ───────────────────────────────────────────────────────────
    def init_cfg(self):
        st = self.app.state
        mp = Path(st.project_dir) / 'mintpy'
        mp.mkdir(parents=True, exist_ok=True)
        cfg_path = mp / 'S1_smallbaseline.cfg'
        if not cfg_path.exists():
            cfg_path.write_text(default_mintpy_cfg(st))
        self._mp_var.set(str(mp))
        self._reload_cfg()
        self._sync_terminal_cwd()

    def _sync_terminal_cwd(self):
        mp = self._mp_var.get().strip()
        if mp and hasattr(self, '_terminal'):
            self._terminal.set_cwd(mp)

    # ── cfg actions ──────────────────────────────────────────────────────
    def _reload_cfg(self):
        mp  = Path(self._mp_var.get().strip())
        cfg = mp / 'S1_smallbaseline.cfg'
        self._cfg_text.delete('1.0', 'end')
        if cfg.exists():
            self._cfg_text.insert('end', cfg.read_text())
        else:
            self._cfg_text.insert('end', ('(cfg 不存在；請先完成 SNAP 處理)' if LANG == 'zh' else '(cfg does not exist; please complete SNAP processing first)'))

    def _save_cfg(self):
        mp = Path(self._mp_var.get().strip())
        if not mp.name:
            messagebox.showerror('Path', ('請先設定 MintPy workdir。' if LANG == 'zh' else 'Please set the MintPy workdir first.'))
            return
        mp.mkdir(parents=True, exist_ok=True)
        cfg = mp / 'S1_smallbaseline.cfg'
        cfg.write_text(self._cfg_text.get('1.0', 'end'))
        # keep smallbaselineApp.cfg alias
        alias = mp / 'smallbaselineApp.cfg'
        if not alias.exists():
            import shutil as _sh
            _sh.copy2(str(cfg), str(alias))
        self._append_log(f'[saved] {cfg}\n')

    # ── MintPy execution ─────────────────────────────────────────────────
    def _run_mintpy(self):
        self._save_cfg()
        mp  = Path(self._mp_var.get().strip())
        cfg = mp / 'S1_smallbaseline.cfg'
        if not cfg.exists():
            messagebox.showerror('cfg', ('S1_smallbaseline.cfg 不存在，請先儲存。' if LANG == 'zh' else 'S1_smallbaseline.cfg does not exist, please save first.'))
            return
        self._status_var.set('')
        self._log.delete('1.0', 'end')

        # Find smallbaselineApp.py in detected env
        sba = (Path(self._bin_dir) / 'smallbaselineApp.py'
               if self._bin_dir else None)
        if sba and sba.exists():
            cmd = [self._py_exe, str(sba), '--dir', str(mp), str(cfg)]
            lbl = f'[env:{self._env_name}] smallbaselineApp.py\n'
        else:
            cmd = ['smallbaselineApp.py', '--dir', str(mp), str(cfg)]
            lbl = '[run] smallbaselineApp.py (system PATH)\n'

        self._append_log(lbl)
        self._run_btn.configure(state='disabled')
        self._stop_btn.configure(state='normal')

        # accumulate log lines for error analysis
        _log_lines: List[str] = []

        def _work():
            try:
                env = dict(os.environ)
                if self._bin_dir:
                    env['PATH'] = self._bin_dir + ':' + env.get('PATH', '')
                env['PYTHONUNBUFFERED'] = '1'
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    bufsize=1, text=True, cwd=str(mp), env=env)
                self._proc = proc
                for line in proc.stdout:  # type: ignore[union-attr]
                    _log_lines.append(line)
                    self.app.after(0, self._append_log, line)
                rc = proc.wait()
                if rc == 0:
                    self.app.after(0, lambda: self._status_var.set('✓ MintPy Done'))
                    # cfg deramp=no → velocity.h5 = demErr-only (保留真實形變);
                    # 自動另產 deramp 對照版 velocity_deramp.h5。
                    self._make_velocity_deramp(mp, env)
                else:
                    self.app.after(0, lambda: self._status_var.set(f'✗ exit rc={rc}'))
                    full_log = ''.join(_log_lines)
                    if 'Not enough reliable pixels' in full_log:
                        self.app.after(100, self._suggest_mintempCoh_fix, mp / 'S1_smallbaseline.cfg')
            except Exception as exc:
                self.app.after(0, self._append_log, f'ERROR: {exc}\n')
            finally:
                self.app.after(0, self._run_btn.configure,  {'state': 'normal'})
                self.app.after(0, self._stop_btn.configure, {'state': 'disabled'})

        threading.Thread(target=_work, daemon=True).start()

    def _make_velocity_deramp(self, mp: Path, env: dict):
        """cfg deramp=no 時 velocity.h5 = 只 demErr; 對 timeseries_demErr.h5 另做
        linear deramp → velocity_deramp.h5 供對照 (檢視被移除的線性趨勢多大)。

        在 MintPy 執行緒內同步呼叫 (MintPy 已成功結束)。缺 timeseries_demErr.h5
        (例如 cfg 仍 deramp=linear) → 直接略過, 不影響主流程。
        """
        if not (mp / 'timeseries_demErr.h5').exists():
            return

        def _bin(name: str) -> list:
            p = Path(self._bin_dir) / name if self._bin_dir else None
            return [self._py_exe, str(p)] if (p and p.exists()) else [name]

        steps = [
            (_bin('remove_ramp.py') + ['timeseries_demErr.h5', '-s', 'linear',
              '-m', 'maskTempCoh.h5', '-o', 'timeseries_demErr_ramp.h5'], 'deramp'),
            (_bin('timeseries2velocity.py') + ['timeseries_demErr_ramp.h5',
              '-o', 'velocity_deramp.h5'], 'velocity_deramp'),
        ]
        self.app.after(0, self._append_log,
                       ('[deramp對照] 產 velocity_deramp.h5 ...\n' if LANG == 'zh' else '[deramp-check] Generating velocity_deramp.h5 ...\n'))
        for cmd, tag in steps:
            try:
                r = subprocess.run(cmd, cwd=str(mp), env=env,
                                   capture_output=True, text=True)
            except Exception as exc:
                self.app.after(0, self._append_log,
                               (f'[deramp對照] {tag} 錯誤: {exc}\n' if LANG == 'zh' else f'[deramp-check] {tag} error: {exc}\n'))
                return
            if r.returncode != 0:
                self.app.after(0, self._append_log,
                               (f'[deramp對照] {tag} 失敗: {r.stderr[-200:]}\n' if LANG == 'zh' else f'[deramp-check] {tag} failed: {r.stderr[-200:]}\n'))
                return
        self.app.after(0, self._append_log,
                       ('[deramp對照] ✓ velocity_deramp.h5 完成 '
                       '(velocity.h5 = demErr-only 為主要輸出)\n' if LANG == 'zh' else '[deramp-check] ✓ velocity_deramp.h5 done (velocity.h5 = demErr-only is the primary output)\n'))

    def _suggest_mintempCoh_fix(self, cfg_path: Path):
        """Offer to lower minTempCoh when 'Not enough reliable pixels' is detected.

        Reads temporalCoherence.h5 (if exists) to find a threshold that gives
        ≥200 reliable pixels. Cleans stale downstream outputs so MintPy re-runs
        network_inversion instead of skipping it.
        """
        mp = cfg_path.parent

        # ── Analyse temporal coherence to find a safe threshold ───────────
        suggested = 0.4
        tc_stats  = ''
        tc_path   = mp / 'temporalCoherence.h5'
        if tc_path.exists():
            try:
                import numpy as _np, h5py as _h5
                with _h5.File(str(tc_path), 'r') as _f:
                    _tc = _f['temporalCoherence'][:]
                total = _tc.size
                counts = {t: int(_np.sum(_tc > t))
                          for t in [0.7, 0.5, 0.4, 0.3, 0.2]}
                tc_stats = '\n'.join(
                    f'  > {t}: {n} px' for t, n in counts.items())
                # pick the highest threshold that still gives ≥ 200 pixels
                for t in [0.5, 0.4, 0.3, 0.2]:
                    if counts[t] >= 200:
                        suggested = t
                        break
                else:
                    suggested = 0.2
            except Exception:
                pass

        hint = (
            ('偵測到錯誤：Not enough reliable pixels\n\n'
            '時序相干度（temporal coherence）在目前閾值下，\n'
            '有效像素不足 100 個（MintPy 最低需求）。\n\n' if LANG == 'zh' else 'Detected error: Not enough reliable pixels\n\nAt the current temporal coherence threshold,\nthere are fewer than 100 valid pixels (the minimum required by MintPy).\n\n')
            + ((f'【相干度分佈】\n{tc_stats}\n\n' if LANG == 'zh' else f'[Coherence Distribution]\n{tc_stats}\n\n') if tc_stats else '')
            + (f'建議將 minTempCoh 改為 {suggested}\n\n'
            '同時刪除舊的 network_inversion 輸出，強制重算。\n\n'
            f'是否套用修復（minTempCoh → {suggested}）並重新執行？' if LANG == 'zh' else f'Suggest changing minTempCoh to {suggested}\n\nAlso delete the old network_inversion output and force a recompute.\n\nApply the fix (minTempCoh → {suggested}) and rerun?')
        )
        ans = messagebox.askquestion(('MintPy 錯誤修復建議' if LANG == 'zh' else 'MintPy Error Fix Suggestion'), hint, icon='warning')
        if ans != 'yes':
            return
        try:
            # ── 1. Update minTempCoh in cfg ───────────────────────────────
            text = cfg_path.read_text(encoding='utf-8')
            if 'mintpy.networkInversion.minTempCoh' in text:
                text = re.sub(
                    r'(mintpy\.networkInversion\.minTempCoh\s*=\s*)\S+',
                    fr'\g<1>{suggested}', text)
            else:
                text += f'\nmintpy.networkInversion.minTempCoh    = {suggested}\n'
            cfg_path.write_text(text, encoding='utf-8')
            # also update smallbaselineApp.cfg alias if present
            alias = mp / 'smallbaselineApp.cfg'
            if alias.exists():
                atxt = alias.read_text(encoding='utf-8')
                atxt = re.sub(
                    r'(mintpy\.networkInversion\.minTempCoh\s*=\s*)\S+',
                    fr'\g<1>{suggested}', atxt)
                alias.write_text(atxt, encoding='utf-8')

            # ── 2. Delete stale downstream h5 outputs ────────────────────
            stale = [
                'timeseries.h5', 'temporalCoherence.h5', 'maskTempCoh.h5',
                'numInvIfgram.h5', 'numTriNonzeroIntAmbiguity.h5',
                'avgPhaseVelocity.h5',
                'demErr.h5', 'timeseries_demErr.h5',
                'timeseriesResidual.h5', 'timeseriesResidual_ramp.h5',
                'velocity.h5',
            ]
            removed = [f for f in stale if (mp / f).exists()
                       and (mp / f).unlink() or True]

            # ── 3. Reload cfg editor and re-run ──────────────────────────
            self._cfg_text.delete('1.0', 'end')
            self._cfg_text.insert('1.0', text)
            self._append_log(
                (f'\n[修復] minTempCoh → {suggested}，'
                f'已清除 {len(removed)} 個舊輸出，重新執行 MintPy…\n' if LANG == 'zh' else f'\n[fix] minTempCoh → {suggested}, cleared {len(removed)} old output(s), rerunning MintPy…\n'))
            self.app.after(300, self._run_mintpy)
        except Exception as exc:
            messagebox.showerror(('修復失敗' if LANG == 'zh' else 'Fix failed'), str(exc))

    def _view_with_basemap(self, save: bool = False):
        """Launch view_basemap.py for velocity.h5 in a background thread."""
        mp = Path(self._mp_var.get().strip())
        vel = mp / 'velocity.h5'
        if not vel.exists():
            messagebox.showerror('View', (f'找不到 {vel}\n請先完成 MintPy 流程。' if LANG == 'zh' else f'{vel} not found\nPlease complete the MintPy workflow first.'))
            return
        basemap   = self._basemap_var.get()
        mask_file = str(mp / 'maskTempCoh.h5')
        script    = str(Path(__file__).resolve().parent / 'view_basemap.py')

        cmd = [self._py_exe, script, str(vel),
               '--basemap', basemap, '--mask', mask_file]

        # temporalCoherence.h5 threshold mask (blank = skip). 用 temporalCoherence
        # 而非 avgSpatialCoh: 後者由 smart-ML 的 n×n max-coh 覆寫 coh 波段 → 被灌水
        # (水域也假高 0.45), 遮不掉水; temporalCoherence 是 SBAS 反演的網路一致性,
        # 水域真實偏低(~0.3)、穩定散射體(道路/橋)真實偏高(0.9+) → 去水留路。
        coh_suffix = ''
        _coh = self._vel_coh_thresh_var.get().strip()
        coh_file = mp / 'temporalCoherence.h5'
        if _coh:
            try:
                _cohv = float(_coh)
            except ValueError:
                _cohv = None
            if _cohv is not None and coh_file.exists():
                cmd += ['--coh-mask', str(coh_file), '--coh-thresh', str(_cohv)]
                coh_suffix = f'_coh{_cohv:g}'
            elif _cohv is not None:
                self._append_log((f'[basemap] 找不到 {coh_file}，略過同調遮罩\n' if LANG == 'zh' else f'[basemap] {coh_file} not found, skipping coherence mask\n'))
        if save:
            out_png = str(mp / 'pic' / f'velocity_{basemap}{coh_suffix}.png')
            (mp / 'pic').mkdir(exist_ok=True)
            cmd += ['--save', out_png]

        self._append_log(f'[basemap] {" ".join(cmd)}\n')

        def _run():
            import subprocess as _sp
            env = dict(os.environ)
            if self._bin_dir:
                env['PATH'] = self._bin_dir + ':' + env.get('PATH', '')
            r = _sp.run(cmd, capture_output=True, text=True, env=env)
            if r.returncode != 0:
                self.app.after(0, self._append_log,
                               f'[basemap error]\n{r.stderr[-400:]}\n')
            elif save:
                self.app.after(0, self._append_log,
                               (f'[basemap] 已儲存: {out_png}\n' if LANG == 'zh' else f'[basemap] Saved: {out_png}\n'))

        threading.Thread(target=_run, daemon=True).start()

    def _stop_mintpy(self):
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            self._append_log('[stop signal sent]\n')
        self._stop_btn.configure(state='disabled')

    def _append_log(self, text: str):
        self._log.insert('end', text)
        self._log.see('end')


# ─────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────
def _setup_cjk_font(root):
    """Set tkinter default fonts to a CJK-capable font if available.

    On Linux servers without proper locale/font config, the default font
    (DejaVu Sans) lacks CJK glyphs, causing garbled Chinese text.
    Noto Sans CJK TC / Noto Sans CJK SC covers Traditional Chinese.
    """
    import tkinter.font as tkfont
    candidates = [
        'Noto Sans CJK TC', 'Noto Sans CJK SC',
        'Noto Sans TC', 'Noto Sans SC',
        'WenQuanYi Micro Hei', 'WenQuanYi Zen Hei',
        'AR PL UMing TW', 'AR PL UKai TW',
    ]
    available = set(tkfont.families(root))
    chosen = next((c for c in candidates if c in available), None)
    if not chosen:
        return  # no CJK font found, leave default
    for name in ('TkDefaultFont', 'TkTextFont', 'TkFixedFont',
                 'TkMenuFont', 'TkHeadingFont', 'TkCaptionFont',
                 'TkSmallCaptionFont', 'TkIconFont', 'TkTooltipFont'):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=chosen)
        except Exception:
            pass


class GNSSFrame(ttk.Frame):
    """分頁4: GNSS ↔ InSAR 比對 (核心邏輯在 gnss_compare.py)。"""
    def __init__(self, nb: ttk.Notebook, app: 'Snap2MintPyApp'):
        super().__init__(nb)
        self.app = app
        self._stations: Dict[str, str] = {}
        self._build()

    def _mp_default(self) -> str:
        try:
            return self.app.tab3._mp_var.get().strip()
        except Exception:
            pd = self.app.state.project_dir
            return str(Path(pd) / 'mintpy') if pd else ''

    def _build(self):
        top = ttk.LabelFrame(self, text=('GNSS ↔ InSAR 比對' if LANG == 'zh' else 'GNSS ↔ InSAR Comparison'))
        top.pack(fill='x', padx=8, pady=6)
        # MintPy 目錄
        r = 0
        ttk.Label(top, text=('MintPy 目錄:' if LANG == 'zh' else 'MintPy directory:')).grid(row=r, column=0, sticky='e', padx=4, pady=3)
        self._mp_var = tk.StringVar(value=self._mp_default())
        ttk.Entry(top, textvariable=self._mp_var, width=48).grid(row=r, column=1, columnspan=2, sticky='we', padx=4)
        ttk.Button(top, text='...', width=3, command=self._browse_mp
                   ).grid(row=r, column=3, padx=2)
        # GNSS 資料夾 (優先由 MintPy 目錄的上層推 GNSS/, 再退回專案/GNSS)
        r += 1
        ttk.Label(top, text=('GNSS 資料夾:' if LANG == 'zh' else 'GNSS folder:')).grid(row=r, column=0, sticky='e', padx=4, pady=3)
        self._gnss_var = tk.StringVar(value=self._derive_gnss())
        ttk.Entry(top, textvariable=self._gnss_var, width=48).grid(row=r, column=1, columnspan=2, sticky='we', padx=4)
        ttk.Button(top, text='...', width=3, command=self._browse_gnss
                   ).grid(row=r, column=3, padx=2)
        # 座標系統
        r += 1
        ttk.Label(top, text=('座標系統:' if LANG == 'zh' else 'Coordinate system:')).grid(row=r, column=0, sticky='e', padx=4, pady=3)
        import gnss_compare as _gc
        self._epsg_names = [n for n, _ in _gc.EPSG_OPTIONS]
        self._epsg_map = {n: e for n, e in _gc.EPSG_OPTIONS}
        self._epsg_var = tk.StringVar(value=self._epsg_names[0])
        ttk.Combobox(top, textvariable=self._epsg_var, values=self._epsg_names,
                     state='readonly', width=30).grid(row=r, column=1, sticky='w', padx=4)
        ttk.Button(top, text=('掃描測站' if LANG == 'zh' else 'Scan stations'), command=self._scan).grid(row=r, column=2, sticky='w', padx=4)
        # 參考 / 觀測 測站
        r += 1
        ttk.Label(top, text=('參考測站:' if LANG == 'zh' else 'Reference station:')).grid(row=r, column=0, sticky='e', padx=4, pady=3)
        self._ref_var = tk.StringVar()
        self._ref_cb = ttk.Combobox(top, textvariable=self._ref_var, values=[], state='readonly', width=16)
        self._ref_cb.grid(row=r, column=1, sticky='w', padx=4)
        ttk.Label(top, text=('觀測測站(匯出時序):' if LANG == 'zh' else 'Observation station (export time series):')).grid(row=r, column=2, sticky='e', padx=4)
        self._obs_var = tk.StringVar()
        self._obs_cb = ttk.Combobox(top, textvariable=self._obs_var, values=[], state='readonly', width=16)
        self._obs_cb.grid(row=r, column=3, sticky='w', padx=4)
        # 圖下方判讀註解 (預設帶泥炭說明; 可清空或改)
        r += 1
        ttk.Label(top, text=('圖註解:' if LANG == 'zh' else 'Plot annotation:')).grid(row=r, column=0, sticky='ne', padx=4, pady=3)
        import gnss_compare as _gc2
        self._note_txt = tk.Text(top, height=2, width=60, wrap='word', exportselection=False)
        self._note_txt.insert('1.0', _gc2.DEFAULT_NOTE)
        self._note_txt.grid(row=r, column=1, columnspan=3, sticky='we', padx=4)
        # 執行
        r += 1
        ttk.Button(top, text=('▶ 執行比對 (出圖+表)' if LANG == 'zh' else '▶ Run Comparison (plot + table)'), command=self._run
                   ).grid(row=r, column=1, sticky='w', padx=4, pady=6)
        self._status = tk.StringVar(value=('選 MintPy/GNSS 目錄 → 掃描測站 → 選參考/觀測 → 執行' if LANG == 'zh' else 'Select MintPy/GNSS directory → Scan stations → Select reference/observation → Run'))
        ttk.Label(top, textvariable=self._status, foreground='#0a0').grid(
            row=r, column=2, columnspan=2, sticky='w')
        top.columnconfigure(1, weight=1)
        # log
        self._log = _make_log(self, height=14)
        # 進分頁時: MintPy 目錄可能已改 → 連動 GNSS 並自動掃描
        self.bind('<Visibility>', lambda e: self._on_show())
        if Path(self._gnss_var.get().strip() or '.').is_dir():
            self._scan()

    def _derive_gnss(self) -> str:
        """GNSS 資料夾: 優先 <MintPy目錄>/../GNSS, 再退回 <專案>/GNSS。"""
        mp = ''
        try:
            mp = self._mp_var.get().strip()
        except Exception:
            mp = self._mp_default()
        for cand in ([str(Path(mp).parent / 'GNSS')] if mp else []) + \
                    ([str(Path(self.app.state.project_dir) / 'GNSS')]
                     if self.app.state.project_dir else []):
            if Path(cand).is_dir():
                return cand
        return mp and str(Path(mp).parent / 'GNSS') or ''

    def _browse_mp(self):
        d = filedialog.askdirectory()
        if not d:
            return
        self._mp_var.set(d)
        nd = str(Path(d).parent / 'GNSS')       # MintPy 上層推 GNSS
        if Path(nd).is_dir():
            self._gnss_var.set(nd)
            self._stations = {}
            self._scan()

    def _browse_gnss(self):
        d = filedialog.askdirectory()
        if d:
            self._gnss_var.set(d)
            self._stations = {}
            self._scan()

    def _on_show(self):
        """分頁顯示時: 若 GNSS 欄空/失效, 由目前 MintPy 目錄重新推導並掃描。"""
        gd = self._gnss_var.get().strip()
        if not gd or not Path(gd).is_dir():
            nd = self._derive_gnss()
            if nd:
                self._gnss_var.set(nd)
        if Path(self._gnss_var.get().strip() or '.').is_dir() and not self._stations:
            self._scan()

    def _scan(self):
        import gnss_compare as _gc
        gd = self._gnss_var.get().strip()
        if not gd or not Path(gd).is_dir():
            self._status.set((f'⚠ GNSS 資料夾無效: {gd or "(空)"}' if LANG == 'zh' else f'⚠ Invalid GNSS folder: {gd or "(empty)"}'))
            return
        self._stations = _gc.scan_gnss_dir(gd)
        names = list(self._stations)
        self._ref_cb['values'] = names
        self._obs_cb['values'] = names
        if names:
            self._ref_var.set(names[0])
            self._obs_var.set(names[-1])
            self._status.set((f'找到 {len(names)} 站: {", ".join(names)}' if LANG == 'zh' else f'Found {len(names)} station(s): {", ".join(names)}'))
        else:
            self._status.set((f'⚠ 該資料夾無 *.xlsx 測站檔: {gd}' if LANG == 'zh' else f'⚠ No *.xlsx station files in this folder: {gd}'))

    def _run(self):
        mp = self._mp_var.get().strip()
        gd = self._gnss_var.get().strip()
        ref = self._ref_var.get().strip()
        obs = self._obs_var.get().strip()
        if not (mp and gd and ref and obs):
            self._status.set(('⚠ 請先設定目錄並選參考/觀測測站' if LANG == 'zh' else '⚠ Please set the directories and select reference/observation stations first'))
            return
        epsg = self._epsg_map.get(self._epsg_var.get(), 3826)
        note = self._note_txt.get('1.0', 'end-1c').strip()
        self._status.set(('⏳ 比對中...' if LANG == 'zh' else '⏳ Comparing...'))

        def _work():
            import gnss_compare as _gc
            def _log(m):
                self.app.after(0, self._log.insert, 'end', m + '\n')
                self.app.after(0, self._log.see, 'end')
            try:
                r = _gc.compare_station(mp, gd, ref, obs, epsg=epsg, note=note, log=_log)
                _gc.refcorrected_velocity_map(mp, gd, ref, epsg=epsg, log=_log)
                self.app.after(0, self._status.set, ('✓ 完成 (圖/表在 mintpy/pic)' if LANG == 'zh' else '✓ Done (plots/tables in mintpy/pic)'))
            except Exception as exc:
                _log((f'[錯誤] {exc}' if LANG == 'zh' else f'[error] {exc}'))
                self.app.after(0, self._status.set, f'✗ {exc}')
        threading.Thread(target=_work, daemon=True).start()


class CumDeformFrame(ttk.Frame):
    """分頁5: 累積變形量地圖 (4×N 網格 + GIF; 核心在 gnss_compare.py)。"""
    def __init__(self, nb: ttk.Notebook, app: 'Snap2MintPyApp'):
        super().__init__(nb)
        self.app = app
        self._build()

    def _build(self):
        top = ttk.LabelFrame(self, text=('累積變形量地圖 (timeseries 各期相對首期)' if LANG == 'zh' else 'Cumulative Deformation Map (timeseries, each epoch relative to the first)'))
        top.pack(fill='x', padx=8, pady=6)
        r = 0
        ttk.Label(top, text=('MintPy 目錄:' if LANG == 'zh' else 'MintPy directory:')).grid(row=r, column=0, sticky='e', padx=4, pady=3)
        try:
            _mp = self.app.tab3._mp_var.get().strip()
        except Exception:
            _mp = str(Path(self.app.state.project_dir) / 'mintpy') if self.app.state.project_dir else ''
        self._mp_var = tk.StringVar(value=_mp)
        ttk.Entry(top, textvariable=self._mp_var, width=48).grid(row=r, column=1, columnspan=2, sticky='we', padx=4)
        ttk.Button(top, text='...', width=3,
                   command=lambda: self._mp_var.set(filedialog.askdirectory() or self._mp_var.get())
                   ).grid(row=r, column=3, padx=2)
        r += 1
        ttk.Label(top, text=('Coh 遮罩門檻(temporalCoherence):' if LANG == 'zh' else 'Coh mask threshold (temporalCoherence):')).grid(row=r, column=0, sticky='e', padx=4)
        self._coh_var = tk.StringVar(value='0.5')
        ttk.Entry(top, textvariable=self._coh_var, width=6).grid(row=r, column=1, sticky='w', padx=4)
        ttk.Label(top, text=('每列欄數:' if LANG == 'zh' else 'Columns per row:')).grid(row=r, column=1, sticky='e', padx=4)
        self._ncol_var = tk.StringVar(value='4')
        ttk.Entry(top, textvariable=self._ncol_var, width=4).grid(row=r, column=2, sticky='w', padx=4)
        r += 1
        ttk.Button(top, text=('▶ 產生 4×N 網格圖 + GIF' if LANG == 'zh' else '▶ Generate 4×N Grid Plot + GIF'), command=self._run
                   ).grid(row=r, column=1, sticky='w', padx=4, pady=6)
        self._status = tk.StringVar(value=('設定 MintPy 目錄 → 產生' if LANG == 'zh' else 'Set MintPy directory → Generate'))
        ttk.Label(top, textvariable=self._status, foreground='#0a0').grid(
            row=r, column=2, columnspan=2, sticky='w')
        top.columnconfigure(1, weight=1)
        self._log = _make_log(self, height=14)

    def _run(self):
        mp = self._mp_var.get().strip()
        if not (mp and Path(mp).is_dir()):
            self._status.set(('⚠ MintPy 目錄無效' if LANG == 'zh' else '⚠ Invalid MintPy directory'))
            return
        try:
            coh = float(self._coh_var.get()); ncol = int(self._ncol_var.get())
        except ValueError:
            self._status.set(('⚠ 門檻/欄數需為數字' if LANG == 'zh' else '⚠ Threshold/column count must be numeric')); return
        self._status.set(('⏳ 產生中 (含 GIF, 稍候)...' if LANG == 'zh' else '⏳ Generating (including GIF, please wait)...'))

        def _work():
            import gnss_compare as _gc
            def _log(m):
                self.app.after(0, self._log.insert, 'end', m + '\n')
                self.app.after(0, self._log.see, 'end')
            try:
                out = _gc.cumulative_deformation(mp, coh_thresh=coh, ncol=ncol, log=_log)
                self.app.after(0, self._status.set,
                               (f'✓ 完成: {Path(out["grid"]).name} + {Path(out["gif"]).name}' if LANG == 'zh' else f'✓ Done: {Path(out["grid"]).name} + {Path(out["gif"]).name}'))
            except Exception as exc:
                _log((f'[錯誤] {exc}' if LANG == 'zh' else f'[error] {exc}'))
                self.app.after(0, self._status.set, f'✗ {exc}')
        threading.Thread(target=_work, daemon=True).start()


class Snap2MintPyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:
            self.tk.call('encoding', 'system', 'utf-8')
        except Exception:
            pass
        _setup_cjk_font(self)
        _install_clipboard_guard(self)
        self.title('snap2mintpy GUI v2  —  SNAP/GPT → MintPy SBAS')
        self.geometry('1200x900')
        self.state_obj = AppState()
        self.state = self.state_obj
        self._loaded_prefs_path = None
        # 所有「開著的基線網路圖」登記表 (Tab1 彈窗 + Tab2 內嵌)；完成一對時
        # 廣播給全部即時改色，這樣不論在哪個視窗看都會更新。
        self._live_baselines: list = []
        self._load_prefs()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True, padx=4, pady=4)

        self.tab1 = InputPairFrame(self.notebook, self)
        self.tab2 = RunFrame(self.notebook, self)
        self.tab3 = MintPyFrame(self.notebook, self)
        self.tab4 = GNSSFrame(self.notebook, self)
        self.tab5 = CumDeformFrame(self.notebook, self)

        self.notebook.add(self.tab1, text=_T('tab1'))
        self.notebook.add(self.tab2, text=_T('tab2'))
        self.notebook.add(self.tab3, text=_T('tab3'))
        self.notebook.add(self.tab4, text=('[4] GNSS 比對' if LANG == 'zh' else '[4] GNSS Comparison'))
        self.notebook.add(self.tab5, text=('[5] 累積變形' if LANG == 'zh' else '[5] Cumulative Deformation'))

        # Tab 2 & 3 locked until ready; restore if prefs had saved pairs
        self.notebook.tab(1, state='disabled')
        self.notebook.tab(2, state='disabled')

        if self.state.pairs:
            self.notebook.tab(1, state='normal')
            self.tab1._rebuild_pair_tree()   # restore Tab-1 pair preview list
            self.tab2.load_pairs()
            # Resume an already-processed project: show in-range IW scene count
            # and tick processed day-intervals (union with saved selection).
            self.after(300, self.tab1._auto_detect_on_load)

        self.protocol('WM_DELETE_WINDOW', self._on_close)

        # Deferred project-ready check and prefs notification (after mainloop starts)
        self.after(200, self._notify_loaded_prefs)
        # Auto-show cached baseline figure if settings unchanged from last session
        if self.state.pairs and self.tab1._baseline_cache_valid():
            self.after(600, lambda: self.tab1._plot_baseline(_from_cache_check=True))
        self.after(400, self._check_project_ready)

    def _notify_loaded_prefs(self):
        """Update title bar to show which prefs file was auto-loaded."""
        if not self._loaded_prefs_path:
            return
        fname = Path(self._loaded_prefs_path).name
        self.title(_T('win_title') + '  │  ' + _T('loaded_prefs', f=fname))

    def _check_project_ready(self):
        """只有在『所有干涉對都完成』且『網路連續無斷點』時, 才問是否進入 MintPy。

        背景掃磁碟真實產物 (三產物齊全) + 用 find_bridge_pairs 檢查連通性,
        再回主執行緒彈窗。避免「有一對完成就問 MintPy」的誤觸。
        """
        st = self.state
        if not st.pairs:
            return
        pairs = list(st.pairs)
        ifg = Path(st.project_dir) / 'interferograms'

        def _work():
            done = set((r, s) for (r, s) in pairs
                       if (ifg / f'{r}_{s}').is_dir()
                       and pair_mintpy_complete(ifg / f'{r}_{s}', f'{r}_{s}'))
            n_missing = sum(1 for p in pairs if p not in done)
            # 只有全完成才檢查連通性 (find_bridge_pairs 非空 = 有斷點/孤立子網路)
            bridges = find_bridge_pairs(pairs) if n_missing == 0 else None
            self.after(0, lambda: self._project_ready_prompt(
                len(pairs), n_missing, bridges))

        threading.Thread(target=_work, daemon=True).start()

    def _project_ready_prompt(self, total: int, n_missing: int, bridges):
        if n_missing > 0:
            return   # 尚有未完成 → 不問 MintPy (Tab2 進度會顯示)
        if bridges:
            messagebox.showwarning(
                ('InSAR 網路有斷點' if LANG == 'zh' else 'InSAR Network Has Gaps'),
                (f'所有 {total} 對干涉已完成, 但網路不連續 — 偵測到 '
                f'{len(bridges)} 處斷點/孤立子網路。\n\n'
                f'MintPy SBAS 需要連通的網路。請在基線圖用「增加干涉對」補上'
                f'橋接 (連接斷開的時段) 後再進入 MintPy。' if LANG == 'zh' else f'All {total} pairs are done, but the network is not connected — detected {len(bridges)} gap(s)/isolated sub-networks.\n\nMintPy SBAS requires a connected network. Please use "Add pairs" in the baseline plot to add bridging pairs (connecting the disconnected periods) before entering MintPy.'))
            return
        ans = messagebox.askquestion(
            ('InSAR 全部完成' if LANG == 'zh' else 'InSAR All Done'),
            (f'所有 {total} 對干涉已完成, 且網路連續無斷點 ✓\n\n要進入 MintPy 嗎?' if LANG == 'zh' else f'All {total} pairs are done, and the network is connected with no gaps ✓\n\nProceed to MintPy?'),
            icon='question')
        if ans == 'yes':
            self.notebook.tab(2, state='normal')
            self.tab3.init_cfg()
            self.notebook.select(2)

    # ── prefs ────────────────────────────────────────────────────────────
    def _load_prefs(self):
        """Load the most-recent snap2mintpy_gui_para_{datetime}.json, or legacy txt."""
        latest = _latest_prefs_file()
        # Migration fallback: when the launch dir has no prefs yet, fall back to
        # the newest prefs stored next to the script (the pre-CWD behaviour) so
        # existing settings carry forward on first launch in a new work dir.
        _script_files = sorted(_SCRIPT_DIR.glob(_PREFS_PATTERN))
        script_latest = str(_script_files[-1]) if _script_files else None
        candidates = [p for p in (latest, PREFS_PATH,
                                   script_latest,
                                   str(_SCRIPT_DIR / 'snap2mintpy_gui_para.txt'),
                                   os.path.expanduser('~/.snap2mintpy_gui_prefs.json'))
                      if p]
        for path in candidates:
            try:
                with open(path, encoding='utf-8') as fh:
                    content = ''.join(ln for ln in fh if not ln.startswith('#'))
                self.state.from_dict(json.loads(content))
                self._loaded_prefs_path = path   # remember for status bar
                # If prefs came from outside the launch CWD (fallback from
                # script dir or legacy path), reset project_dir to CWD — the
                # user launched here intentionally, so CWD is the project root.
                if Path(path).parent != _PREFS_DIR:
                    self.state.project_dir = str(_PREFS_DIR)
                break
            except Exception:
                continue
        else:
            self._loaded_prefs_path = None

        # Always prefer ~/.netrc credentials over saved prefs
        # (prefs may contain a stale password from a previous session)
        user, pwd = _read_netrc_asf()
        if user:
            self.state.asf_username = user
        if pwd:
            self.state.asf_password = pwd

    # ── 基線圖即時更新登記表 (Tab1 彈窗 + Tab2 內嵌共用) ──────────────────
    def register_baseline(self, canvas, edges: dict):
        """登記一個開著的基線圖 (canvas + {(ref,sec):Line2D})；回傳 token 供註銷。"""
        token = {'canvas': canvas, 'edges': edges}
        self._live_baselines.append(token)
        return token

    def unregister_baseline(self, token):
        try:
            self._live_baselines.remove(token)
        except (ValueError, AttributeError):
            pass

    def notify_pair_state(self, ref: str, sec: str, state: str):
        """完成/失敗/處理中時呼叫 → 廣播給所有開著的基線圖, 改該線段顏色。
        綠=完成 紅=失敗 橘=處理中 灰=未做/排隊。死掉的視窗自動剔除。"""
        col = _BASELINE_EDGE_COLOR.get(state)
        if not col:
            return
        dead = []
        for tok in list(self._live_baselines):
            line = tok['edges'].get((ref, sec))
            cv = tok['canvas']
            if line is None or cv is None:
                continue
            try:
                done = state in ('done', 'error')
                line.set_color(col)
                line.set_alpha(0.95 if state in ('done', 'error', 'running') else 0.45)
                line.set_linewidth(2.0 if done else 1.2)
                line.set_zorder(4 if done else 2)
                cv.draw_idle()
            except Exception:
                dead.append(tok)
        for tok in dead:
            self.unregister_baseline(tok)

    def refresh_baseline_legends(self):
        """依目前線段顏色重算所有基線圖的圖例計數 → 圖例數字與線條同步更新
        (修: edges 即時改色但 Done/Pending 數字不變)。"""
        order = [('#00cc44', ('已完成 Done' if LANG == 'zh' else 'Done')), ('#cccccc', ('未處理 Pending' if LANG == 'zh' else 'Pending')),
                 ('#ff8c00', ('處理中 Running' if LANG == 'zh' else 'Running')), ('#ff4444', ('失敗 Failed' if LANG == 'zh' else 'Failed'))]
        for tok in list(self._live_baselines):
            try:
                cv = tok['canvas']
                leg = getattr(cv.figure, '_status_legend', None)
                if leg is None:
                    continue
                counts = {}
                for ln in tok['edges'].values():
                    c = ln.get_color()
                    counts[c] = counts.get(c, 0) + 1
                texts = leg.get_texts()
                for i, (col, lab) in enumerate(order):
                    if i < len(texts):
                        texts[i].set_text(f'{lab} ({counts.get(col, 0)})')
                cv.draw_idle()
            except Exception:
                pass

    def save_prefs(self):
        """Save current parameters to snap2mintpy_gui_para_{YYYYMMDD_HHMMSS}.json."""
        try:
            self.tab1.collect_state()
        except Exception:
            pass
        try:
            data = self.state.to_dict()
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            fname = f'snap2mintpy_gui_para_{ts}.json'
            path  = str(_PREFS_DIR / fname)
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _on_close(self):
        # Sync all widget values → state before saving
        try:
            self.tab1.collect_state()
        except Exception:
            pass
        self.state.language = LANG
        self.save_prefs()
        # 乾淨停止叢集 (選項A): 通知停止 + 終止本機/ssh 程序 + 遠端 pkill 本專案
        # worker/gpt, 再退出 → 關 GUI 就全停, 不留孤兒程序。
        try:
            self.tab2._shutdown_cluster()
        except Exception:
            pass
        self.destroy()
        import os as _os
        _os._exit(0)


# ─────────────────────────────────────────────────────────────────────────
def main():
    global LANG
    # Check saved language from the most-recent prefs file
    latest = _latest_prefs_file()
    saved_lang = None
    if latest:
        try:
            with open(latest, encoding='utf-8') as fh:
                saved_lang = json.load(fh).get('language')
        except Exception:
            pass

    # Always show language dialog; pass saved preference as default selection.
    LANG = _ask_language(default=saved_lang if saved_lang in ('zh', 'en') else 'zh')

    app = Snap2MintPyApp()
    app.state.language = LANG   # persist language in prefs
    app.mainloop()


if __name__ == '__main__':
    main()
