#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析叢集失敗的干涉對：掃描 logs/ 比對已知錯誤樣式，分類失敗原因。

可被 GUI「分析失敗原因」按鈕呼叫 (analyze_project 回傳結構化結果)，
也可 CLI 執行印出報告。

用法: python3 analyze_failures.py <project_dir>
"""
import sys
import re
import json
import glob
from pathlib import Path

# (正規式, 步驟, 原因分類, 建議修正)  — 依優先序由上而下比對
PATTERNS = [
    (r'A fatal error has been detected|oopDesc::size_given_klass|SIGSEGV.*libjvm',
     'JVM', 'JVM 致命崩潰 (heap 損毀)',
     '該機硬體/JDK 問題 (壞 RAM 機率高)；換機處理或 memtest 檢查 RAM'),
    (r'No valid orbit file found',
     'split', '缺精密軌道檔',
     '從 ASF s1qc 下載該日期 POEORB 放入 ~/.snap/.../POEORB；或退回 RESORB'),
    (r'does not overlap any burst|wktAOI does not overlap',
     'split', 'AOI 不在該衛星 swath (覆蓋缺失)',
     '該衛星該日期未覆蓋 AOI (如 S1C/S1D commissioning)；改用有覆蓋的衛星或略過'),
    (r'NaN or infinity found in input',
     'unwrap', 'snaphu 輸入含 NaN/inf',
     'gap-fill endian 或內插問題；確認 snaphu Phase 以 little-endian 寫回'),
    (r'Out of memory',
     'unwrap', 'snaphu 記憶體不足 (tile 重組)',
     '改單 tile (NTILEROW/COL=1)；已內建'),
    (r'incomplete output, reprocessing',
     'ifg_ml', '產物全零/不完整 (多半上游崩潰所致)',
     '通常隨上游崩潰；修好崩潰機後零偵測會自動重做'),
    (r'GraphException|OperatorException',
     'SNAP', 'SNAP 圖執行例外',
     '看該步驟 log 細節'),
    # 放最後 (catch-all)：只對 failed 對成立 → 代表步驟有在跑卻被中止、無錯誤訊息
    (r'Executing processing graph|\.\.\d+%|Estimating azimuth offset',
     '?', '中斷/被殺 (執行中被中止，無錯誤 → 可重跑)',
     '通常是叢集被停/被殺所致；直接重跑，skip 機制會接續，多半成功'),
]


def _scan_pair_logs(logs_dir: Path, pair: str, host: str = ''):
    """掃描某對相關的 logs，回傳 (step, category, fix, evidence) 或 None。"""
    # 候選 log：host log + 該對各步驟 log (依失敗多在後段，倒序掃)
    candidates = []
    if host:
        candidates.append(logs_dir / f'host_{host}.log')
    for pat in (f'tc_unw_{pair}*', f'tc_wrapped_{pair}*', f'snaphu_run_{pair}*',
                f'snaphu_export_{pair}*', f'sml_multilook_{pair}*',
                f'ifg_ml_{pair}*', f'split_*{pair[:8]}*'):
        candidates += [Path(p) for p in glob.glob(str(logs_dir / pat))]

    for log in candidates:
        if not log.exists():
            continue
        try:
            txt = log.read_text(errors='ignore')
        except Exception:
            continue
        # host log 太大 → 只取該對 FAILED 前的區段
        if log.name.startswith('host_') and pair.replace('_', '-') in txt:
            key = pair.replace('_', '-')
            idx = txt.rfind(f'FAILED  {key}')
            if idx > 0:
                start = txt.rfind(f') {key} ...', 0, idx)
                txt = txt[max(0, start):idx + 40]
        for rx, step, cat, fix in PATTERNS:
            m = re.search(rx, txt)
            if m:
                line = txt[max(0, m.start() - 0):m.start() + 80].splitlines()[0]
                return step, cat, fix, line.strip()[:120]
    return None


def analyze_project(project: str):
    """回傳 {pair: {host, step, category, fix, evidence}} 給 GUI/CLI 用。"""
    proj = Path(project)
    logs_dir = proj / 'logs'
    result = {}
    for wf in sorted(glob.glob(str(logs_dir / 'worker_*.json'))):
        host = Path(wf).stem.replace('worker_', '')
        try:
            d = json.loads(Path(wf).read_text())
        except Exception:
            continue
        for pair_dash in d.get('failed', []):
            pair = pair_dash.replace('-', '_')
            info = _scan_pair_logs(logs_dir, pair, host)
            if info:
                step, cat, fix, ev = info
            else:
                step, cat, fix, ev = '?', '未知 (log 無已知樣式)', '人工檢視該對 log', ''
            result[pair_dash] = {'host': host, 'step': step,
                                 'category': cat, 'fix': fix, 'evidence': ev}
    return result


def main():
    proj = sys.argv[1] if len(sys.argv) > 1 else '/mnt/SARDB/CW_test'
    res = analyze_project(proj)
    if not res:
        print('沒有失敗的干涉對 (或無 worker 狀態檔)')
        return
    # 依分類彙總
    from collections import Counter, defaultdict
    by_cat = Counter(v['category'] for v in res.values())
    by_host = defaultdict(Counter)
    for v in res.values():
        by_host[v['host']][v['category']] += 1

    print(f'=== 失敗干涉對分析: {len(res)} 對 ({proj}) ===\n')
    print('依原因分類:')
    for cat, n in by_cat.most_common():
        print(f'  {n:3d}  {cat}')
    print('\n依主機:')
    for host, cnt in by_host.items():
        print(f'  {host}: {sum(cnt.values())} 對 → {dict(cnt)}')
    print('\n建議修正 (每類一例):')
    seen = set()
    for pair, v in res.items():
        if v['category'] in seen:
            continue
        seen.add(v['category'])
        print(f'  [{v["category"]}] step={v["step"]}')
        print(f'     修正: {v["fix"]}')
        if v['evidence']:
            print(f'     證據: {v["evidence"]}')


if __name__ == '__main__':
    main()
