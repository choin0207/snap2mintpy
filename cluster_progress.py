#!/usr/bin/env python3
"""叢集進度監控 (CLI) — 每分鐘更新。

顯示：總進度條、處理數/總數、各機正在跑的對 (yyyymmdd-yyyymmdd)、步驟、
完成/進行中/剩餘/失敗、速率與預估剩餘時間。

用法:
    python3 cluster_progress.py                 # 預設 60 秒更新
    python3 cluster_progress.py --interval 30   # 自訂秒數
    python3 cluster_progress.py --once          # 只印一次 (給 cron/腳本)
    python3 cluster_progress.py --config <dist_config.json 路徑>

可從本機或任一 worker 節點執行。
"""
import argparse
import glob
import json
import os
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = '/mnt/SARDB/snap2mintpy/Changhua_Yunlin/logs/dist_config.json'
LOCAL_LABELS = ('本機', 'Local')


def _pair_done(ifg_dir: Path, pair: str) -> bool:
    """一對是否完整完成：三個 *_tc.dim 最終地理編碼產物 + 對應 .data 有 .img。"""
    d = ifg_dir / pair
    if not d.is_dir():
        return False
    for kind in ('coh', 'filt', 'unw'):
        dim = d / f'{pair}_{kind}_tc.dim'
        data = d / f'{pair}_{kind}_tc.data'
        if not dim.exists() or not data.is_dir():
            return False
        imgs = list(data.glob('*.img'))
        if not imgs or not any(p.stat().st_size > 0 for p in imgs):
            return False
    return True


def count_done(project_dir: str, pairs: list) -> int:
    ifg = Path(project_dir) / 'interferograms'
    if not ifg.is_dir():
        return 0
    return sum(1 for r, s in pairs if _pair_done(ifg, f'{r}_{s}'))


def worker_alive(label: str) -> bool:
    """該機 worker 程序是否還在跑 (本機用本地 pgrep, 遠端用 ssh)。"""
    cmd_pgrep = 'pgrep -fc "[s]nap2mintpy_worker"'
    try:
        if label in LOCAL_LABELS:
            out = subprocess.run(['bash', '-c', cmd_pgrep],
                                 capture_output=True, text=True, timeout=8).stdout
        else:
            out = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=6', '-o', 'BatchMode=yes',
                 label, cmd_pgrep],
                capture_output=True, text=True, timeout=10).stdout
        return int(out.strip() or 0) > 0
    except Exception:
        return False


def read_workers(logs_dir: Path) -> list:
    rows = []
    for jf in sorted(glob.glob(str(logs_dir / 'worker_*.json'))):
        try:
            d = json.load(open(jf, encoding='utf-8'))
        except Exception:
            continue
        rows.append({
            'label':   d.get('label', Path(jf).stem.replace('worker_', '')),
            'done':    len(d.get('done', [])),
            'failed':  len(d.get('failed', [])),
            'current': d.get('current') or '-',
            'step':    d.get('current_step') or '-',
            'ts':      d.get('ts', ''),
        })
    return rows


def bar(frac: float, width: int = 30) -> str:
    n = int(round(frac * width))
    return '█' * n + '░' * (width - n)


def render(config_path: str, t0: float, done0: int, clear: bool = True) -> int:
    cfg = json.load(open(config_path, encoding='utf-8'))
    project = cfg['project_dir']
    pairs = [(r, s) for r, s in cfg['pairs']]
    total = len(pairs)
    logs = Path(project) / 'logs'

    done = count_done(project, pairs)
    workers = read_workers(logs)
    for w in workers:                      # 每台只查一次 aliveness (省 SSH)
        w['alive'] = worker_alive(w['label'])

    # 速率 / ETA (以本監控啟動後的觀測窗計算)
    elapsed = time.time() - t0
    rate_txt, eta_txt = '計算中', '計算中'
    if elapsed > 120 and done > done0:
        rate = (done - done0) / (elapsed / 3600.0)  # 對/時
        if rate > 0:
            rate_txt = f'{rate:.1f} 對/時'
            remain = total - done
            eta_h = remain / rate
            eta_txt = f'~{eta_h:.1f} 小時' if eta_h < 48 else f'~{eta_h/24:.1f} 天'

    running = sum(1 for w in workers
                  if w['alive'] and w['current'] not in ('-', None))
    # 只計運行中機器的失敗 (已停機器的舊失敗會被其他機重跑, 不顯示避免誤導)
    failed_total = sum(w['failed'] for w in workers if w['alive'])
    remain = total - done

    name = Path(project).name
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    W = 64
    lines = []
    lines.append('═' * W)
    lines.append(f'  {name} InSAR 叢集進度{" " * (W - len(name) - 28)}{now}')
    lines.append('═' * W)
    frac = done / total if total else 0
    lines.append(f'  總進度  [{bar(frac)}]  {done}/{total}  ({frac*100:.1f}%)')
    lines.append('─' * W)
    lines.append(f'  {"機器":<8} {"狀態":<5} {"執行中的對":<22} {"步驟":<10} 本輪')
    for w in workers:
        alive = w['alive']
        st = '● 跑' if alive else '○ 停'
        cur = w['current'] if alive else '-'
        step = w['step'] if alive else '-'
        lbl = w['label']
        lines.append(f'  {lbl:<8} {st:<5} {cur:<22} {step:<10} {w["done"]}')
    lines.append('─' * W)
    lines.append(f'  完成 {done} | 進行中 {running} | 剩餘 {remain} | 失敗 {failed_total}')
    lines.append(f'  速率 {rate_txt}  |  預估剩餘 {eta_txt}')
    lines.append('═' * W)
    lines.append('  (每分鐘更新 · Ctrl+C 結束)')

    if clear:
        os.system('clear' if os.name != 'nt' else 'cls')
    print('\n'.join(lines), flush=True)
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--interval', type=int, default=60)
    ap.add_argument('--once', action='store_true')
    args = ap.parse_args()

    t0 = time.time()
    done0 = None
    try:
        while True:
            d = render(args.config, t0, done0 if done0 is not None else 0,
                       clear=not args.once)
            if done0 is None:
                done0 = d
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n[結束]')


if __name__ == '__main__':
    main()
