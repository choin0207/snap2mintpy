#!/usr/bin/env python3
"""snap2mintpy headless worker — process a set of SNAP pairs without GUI.

Called remotely via SSH by snap2mintpy_gui.py's cluster mode:

    source ~/FastISCE.config
    python3 ~/tools/InSAR-SNAP-MintPy/snap2mintpy_worker.py \\
        --config /mnt/SARDB/.../logs/dist_config.json \\
        --pairs  20200101-20200113,20200113-20200125,... \\
        --label  worker01

Prerequisites on each worker node:
- ESA SNAP installed, gpt in PATH (via ~/FastISCE.config or similar)
- /mnt/SARDB (shared NFS) accessible at the same path as on master
- Same Python environment as master (snap2mintpy_gui.py importable)

After each pair the worker atomically updates
  {project_dir}/logs/worker_{label}.json
so that the master GUI can track progress and resume after a crash.

Exit codes: 0 = all OK, 1 = one or more pairs failed.
"""
import argparse
import json
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from snap2mintpy_gui import AppState, SnapPairWorker


# ── per-pair result tracking ──────────────────────────────────────────────────

import re as _re
_STEP_RE = _re.compile(
    r'\[(split|ifg_deburst|filter_ml|ifg_ml|sml|snaphu-export|snaphu-run|'
    r'snaphu-import|tc-wrapped|tc-unw|mintpy)\]')
_STEP_NAME = {
    'split': 'split',
    # run() 拆成兩階段 (Stage1 ifg_deburst / Stage2 filter_ml); 舊 ifg_ml 保留相容
    'ifg_deburst': 'ifg/deburst', 'filter_ml': 'filter/ML', 'ifg_ml': 'ifg_ml',
    'sml': 'smart_ml',
    'snaphu-export': 'snaphu', 'snaphu-run': 'unwrap',
    'snaphu-import': 'unwrap', 'tc-wrapped': 'geocode',
    'tc-unw': 'geocode', 'mintpy': 'MintPy',
}


def _run_pair(ref: str, sec: str, state: AppState,
              stop_ev: threading.Event, force: bool = False,
              on_step=None, make_dem: bool = True) -> bool:
    """Run one interferometric pair synchronously; return True on success.

    on_step(step_name): 每進入一個處理步驟時回呼 (供 master GUI 即時顯示
    「哪台正在做哪一步」)。
    """
    result: dict = {'ok': None}

    def on_event(kind: str, data: dict) -> None:
        if kind == 'log':
            text = data.get('text', '')
            sys.stdout.write(text)
            sys.stdout.flush()
            if on_step:
                m = _STEP_RE.search(text)
                if m:
                    on_step(_STEP_NAME.get(m.group(1), m.group(1)))
        elif kind in ('pair_done', 'pair_error'):
            result['ok'] = (kind == 'pair_done')

    w = SnapPairWorker(ref, sec, state, on_event, stop_ev, force=force,
                       make_dem=make_dem)
    w.start()
    w.join()
    return bool(result.get('ok', True))  # default ok if event missed


def _auto_cache_gb(fraction: float = 0.12, floor: int = 4, ceil: int = 24):
    """偵測本機 RAM(GB)，回傳安全的 gpt tile cache 大小。

    SNAP gpt 的 -Xmx(JVM heap) 預設約 RAM 70% (如 98G→-Xmx68G)。若 cache 再取
    30%, heap+cache≈100% RAM → 無餘裕 → OS OOM-killer 砍掉 gpt(java) / 狂 swap
    變超慢。故 cache 只取 12%(floor 4/ceil 24G): 98G→~12G, 加 68G heap=80G,
    留 ~18G 給 OS, 單一 gpt 不再 OOM。讀不到回 None(沿用 config)。
    """
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    gb = int(line.split()[1]) / (1024 * 1024)
                    return max(floor, min(ceil, int(gb * fraction)))
    except Exception:
        pass
    return None


def _save_status(state_file: Path, label: str,
                 done: list, failed: list, current: str = '',
                 current_step: str = '') -> None:
    """Atomically write worker status JSON (write-tmp-then-rename).

    current:      目前正在處理的干涉對字串 (供 master GUI 顯示哪台處理哪一幅)。
    current_step: 該對目前進行到的步驟名 (split/ifg_ml/smart_ml/unwrap/geocode/
                  MintPy格式)；供 GUI 顯示「哪台正在做哪一步」。
    """
    data = {
        'label':        label,
        'ts':           datetime.now().isoformat(timespec='seconds'),
        'done':         done,
        'failed':       failed,
        'current':      current,
        'current_step': current_step,
    }
    try:
        tmp = state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
        tmp.replace(state_file)  # atomic on POSIX / same filesystem
    except OSError:
        try:
            state_file.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except OSError:
            pass  # status write failed (NFS issue); worker continues


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', required=True,
                    help='Shared dist_config.json path on /mnt/SARDB')
    ap.add_argument('--pairs', required=True,
                    help='Comma-separated ref-sec pairs: 20200101-20200113,...')
    ap.add_argument('--label', default='',
                    help='Machine label used for the status file name '
                         '(defaults to hostname)')
    ap.add_argument('--force', action='store_true',
                    help='Ignore existing complete outputs and reprocess '
                         'every step (matches GUI "rerun all" choice)')
    ap.add_argument('--no-make-dem', action='store_true',
                    help='Local machine does not produce dem_tc (only the first host produces it in a cluster, to avoid race conditions)')
    args = ap.parse_args()

    state = AppState()
    with open(args.config, encoding='utf-8') as fh:
        state.from_dict(json.load(fh))

    # 自動依本機 RAM 設定 gpt tile cache (-c)，覆寫 config 的共用值。
    # 留充足餘裕給 OS page cache (加速 NAS 讀取)，避免記憶體 thrashing。
    # 各台 worker 各自偵測 → 不同 RAM 的機器自動分配到合適的 cache。
    auto_cache = _auto_cache_gb()
    if auto_cache:
        state.cache = f'{auto_cache}G'

    pairs = []
    for token in args.pairs.split(','):
        token = token.strip()
        if not token:
            continue
        parts = token.split('-', 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))

    hostname = socket.gethostname()
    label    = args.label or hostname
    total    = len(pairs)

    # Status file lives on shared storage so the master can poll it
    logs_dir    = Path(state.project_dir) / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    status_file = logs_dir / f'worker_{label}.json'

    print(f'[{label}] start: {total} pairs  status→{status_file}', flush=True)

    stop_ev = threading.Event()
    done_list:   list = []
    failed_list: list = []

    for idx, (ref, sec) in enumerate(pairs, 1):
        if stop_ev.is_set():
            break
        pair_str = f'{ref}-{sec}'
        print(f'[{label}] ({idx}/{total}) {pair_str} ...', flush=True)

        # 先標記「目前正在處理」，master GUI 即時顯示哪台在跑哪一幅
        _save_status(status_file, label, done_list, failed_list, current=pair_str)

        # 每進入一個步驟就更新狀態檔 (GUI 顯示「哪台正在做哪一步」)
        def _on_step(step, _p=pair_str):
            _save_status(status_file, label, done_list, failed_list,
                         current=_p, current_step=step)

        ok = _run_pair(ref, sec, state, stop_ev, force=args.force,
                       on_step=_on_step, make_dem=not args.no_make_dem)

        if ok:
            print(f'[{label}] OK  {pair_str}', flush=True)
            done_list.append(pair_str)
        else:
            print(f'[{label}] FAILED  {pair_str}', flush=True)
            failed_list.append(pair_str)

        # Persist progress after every pair so master can detect partial completion
        _save_status(status_file, label, done_list, failed_list)

    print(
        f'[{label}] done  ok={len(done_list)}/{total}  failed={failed_list}',
        flush=True)
    sys.exit(1 if failed_list else 0)


if __name__ == '__main__':
    main()
