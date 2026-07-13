#!/usr/bin/env python3
"""
hyp3_burst_to_mintpy.py
========================
自動化 Sentinel-1 burst InSAR 時序前處理:
  AOI + 軌道(path 69 升 / 105 降) + 時間範圍
    → asf_search 找 burst SLC
    → 配對 (nearest_n / SBAS)
    → HyP3 雲端算 burst InSAR (繞過本地記憶體 + ISCE2 burst 誤差)
    → smart multilook (n×n 取 coherence 最大像素)
    → 轉 MintPy 格式 → smallbaselineApp 時序

設計理念:把「最吃記憶體又最易出 burst 問題」的 ifg 生成外包給 ASF 雲端
(固定 burst 網格 → 無 gap、配準天生對齊),本地只做 smart ML + MintPy 時序。

依賴:
  pip install hyp3_sdk asf_search numpy
  conda install -c conda-forge gdal mintpy
帳號:Earthdata (~/.netrc 寫 machine urs.earthdata.nasa.gov login <u> password <p>)

用法範例 (彰雲 A69 升軌):
  python3 hyp3_burst_to_mintpy.py \\
    --aoi 120.1058 23.4505 120.8262 24.2801 \\
    --orbit 69 --direction ASCENDING \\
    --start 2025-04-01 --end 2026-06-15 \\
    --spacing 20 --smart-ml 8 \\
    --pair-strategy nearest_n --nearest-n 5 \\
    --workdir ./changhua_a69_hyp3

  台北 D105 降軌:--orbit 105 --direction DESCENDING --aoi 121.345 24.871 121.676 25.171
"""
import argparse
import sys
import json
from pathlib import Path
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────
# 1. 參數
# ─────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--aoi', nargs=4, type=float, required=True,
                   metavar=('LONMIN', 'LATMIN', 'LONMAX', 'LATMAX'),
                   help='AOI bbox: lonmin latmin lonmax latmax')
    p.add_argument('--orbit', type=int, required=True,
                   help='relative orbit / path number (如 69=A69升軌, 105=D105降軌)')
    p.add_argument('--direction', choices=['ASCENDING', 'DESCENDING'], required=True)
    p.add_argument('--start', required=True, help='YYYY-MM-DD')
    p.add_argument('--end', required=True, help='YYYY-MM-DD')
    p.add_argument('--spacing', type=int, default=20, choices=[20, 40, 80],
                   help='HyP3 burst InSAR 像素間距 m (預設 20)')
    p.add_argument('--smart-ml', type=int, default=1,
                   help='額外 smart multilook 因子 n (n×n 取 coh 最大;1=不做)')
    p.add_argument('--pair-strategy', choices=['nearest_n', 'sequential'],
                   default='nearest_n')
    p.add_argument('--nearest-n', type=int, default=5,
                   help='每個影像跟最近 N 個配對')
    p.add_argument('--pol', default='VV', help='極化 (預設 VV)')
    p.add_argument('--workdir', required=True, help='工作目錄')
    p.add_argument('--max-jobs', type=int, default=0,
                   help='本次最多提交幾對 (0=不限;省 credits 用)')
    p.add_argument('--skip-submit', action='store_true',
                   help='只下載已完成的 jobs,不提交新的')
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────
# 2. 找 burst SLC (asf_search)
# ─────────────────────────────────────────────────────────────────────────
def find_bursts(aoi, orbit, direction, start, end, pol):
    """回傳 {date: [burst_id,...]} —— 每個日期涵蓋 AOI 的所有 burst。

    burst InSAR multi-burst:把同一日期涵蓋 AOI 的多個 burst 當一個 scene。
    """
    import asf_search as asf
    lonmin, latmin, lonmax, latmax = aoi
    wkt = (f'POLYGON(({lonmin} {latmin},{lonmax} {latmin},'
           f'{lonmax} {latmax},{lonmin} {latmax},{lonmin} {latmin}))')
    print(f'[asf] 搜尋 burst: orbit={orbit} {direction} {start}~{end}')
    results = asf.search(
        dataset=asf.DATASET.SLC_BURST,        # burst 資料集
        intersectsWith=wkt,
        relativeOrbit=[orbit],
        flightDirection=direction,
        polarization=[pol],
        start=start, end=end,
    )
    by_date = {}
    for r in results:
        p = r.properties
        fid = p.get('fileID', '')
        burst = p.get('burst', {})
        bid = burst.get('fullBurstID') or fid           # burst 識別
        # 日期 YYYYMMDD
        import re
        m = re.search(r'_(\d{8})T\d{6}_', fid)
        if not m:
            continue
        d = m.group(1)
        by_date.setdefault(d, []).append(bid)
    for d in by_date:
        by_date[d] = sorted(set(by_date[d]))
    print(f'[asf] 找到 {len(by_date)} 個日期, 每日期 ~{ (sum(len(v) for v in by_date.values())//max(1,len(by_date))) } bursts')
    return by_date


# ─────────────────────────────────────────────────────────────────────────
# 3. 配對
# ─────────────────────────────────────────────────────────────────────────
def make_pairs(dates, strategy, n):
    """回傳 [(ref_date, sec_date), ...]"""
    ds = sorted(dates)
    pairs = []
    if strategy == 'sequential':
        pairs = list(zip(ds[:-1], ds[1:]))
    else:  # nearest_n
        for i, ref in enumerate(ds):
            for sec in ds[i + 1:i + 1 + n]:
                pairs.append((ref, sec))
    print(f'[pair] {strategy}: {len(pairs)} 對')
    return pairs


# ─────────────────────────────────────────────────────────────────────────
# 4. HyP3 提交 + 下載
# ─────────────────────────────────────────────────────────────────────────
def submit_and_download(pairs, by_date, spacing, workdir, max_jobs, skip_submit):
    """提交 multi-burst InSAR jobs,等完成,下載到 workdir/hyp3/。"""
    import hyp3_sdk as sdk
    hyp3 = sdk.HyP3()                                    # 讀 ~/.netrc
    out = Path(workdir) / 'hyp3'
    out.mkdir(parents=True, exist_ok=True)

    looks = {20: '20x4', 40: '10x2', 80: '5x1'}[spacing]  # HyP3 looks 對應

    if not skip_submit:
        batch = sdk.Batch()
        todo = pairs[:max_jobs] if max_jobs else pairs
        for ref, sec in todo:
            name = f'{ref}_{sec}'
            ref_ids, sec_ids = by_date.get(ref, []), by_date.get(sec, [])
            if not ref_ids or not sec_ids:
                print(f'[skip] {name}: 缺 burst'); continue
            print(f'[submit] {name} ({len(ref_ids)} bursts, {looks})')
            job = hyp3.submit_insar_isce_multi_burst_job(
                reference=ref_ids, secondary=sec_ids,
                name=name, looks=looks, apply_water_mask=True,
            )
            batch += job
        print(f'[hyp3] 已提交 {len(batch)} jobs, 等待雲端處理...')
        batch = hyp3.watch(batch)                        # 阻塞等完成
    else:
        batch = hyp3.find_jobs()

    print(f'[hyp3] 下載到 {out}')
    for job in batch:
        if job.succeeded():
            job.download_files(out)
    # 解壓 .zip
    import zipfile
    for z in out.glob('*.zip'):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(out)
    return out


# ─────────────────────────────────────────────────────────────────────────
# 5. smart multilook (對 geocoded unw/coh GeoTIFF)
# ─────────────────────────────────────────────────────────────────────────
def block_max_coh_decimate_real(val_arr, coh_arr, n):
    """n×n 區塊取 coh 最大像素的 (val, coh)。val 可為 unwrapped phase(實數)。

    與彰雲 SNAP 版同理:保留高同調性點的真實相位,不被平均稀釋。
    """
    import numpy as np
    H, W = coh_arr.shape
    oh, ow = H // n, W // n
    Hc, Wc = oh * n, ow * n

    def _blocks(a):
        return (a[:Hc, :Wc].reshape(oh, n, ow, n)
                .transpose(0, 2, 1, 3).reshape(oh, ow, n * n))

    cb = _blocks(coh_arr); vb = _blocks(val_arr)
    idx = np.argmax(np.where(np.isnan(cb), -1.0, cb), axis=2)
    ii, jj = np.indices((oh, ow))
    return vb[ii, jj, idx].astype(np.float32), cb[ii, jj, idx].astype(np.float32)


def smart_multilook(hyp3_dir, n):
    """對每個 pair 的 unw_phase.tif + corr.tif 做 smart multilook,覆寫輸出。"""
    if n <= 1:
        print('[sml] smart_ml=1, 跳過'); return
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    pair_dirs = [d for d in Path(hyp3_dir).iterdir() if d.is_dir()]
    print(f'[sml] 對 {len(pair_dirs)} 對做 smart multilook n={n}')
    for pd in pair_dirs:
        unw = next(pd.glob('*unw_phase.tif'), None)
        cor = next(pd.glob('*corr.tif'), None)
        if not unw or not cor:
            continue
        u = gdal.Open(str(unw)); c = gdal.Open(str(cor))
        uarr = u.ReadAsArray().astype('float32')
        carr = c.ReadAsArray().astype('float32')
        new_u, new_c = block_max_coh_decimate_real(uarr, carr, n)
        # 寫出 (geotransform 像素間距 × n)
        gt = list(u.GetGeoTransform()); gt[1] *= n; gt[5] *= n
        for arr, src, suffix in [(new_u, unw, '_sml'), (new_c, cor, '_sml')]:
            outp = src.with_name(src.stem + '_sml.tif')
            drv = gdal.GetDriverByName('GTiff')
            ds = drv.Create(str(outp), new_u.shape[1], new_u.shape[0], 1, gdal.GDT_Float32)
            ds.SetGeoTransform(gt); ds.SetProjection(u.GetProjection())
            ds.GetRasterBand(1).WriteArray(arr); ds = None
    print('[sml] 完成 (*_sml.tif)')


# ─────────────────────────────────────────────────────────────────────────
# 6. MintPy 格式
# ─────────────────────────────────────────────────────────────────────────
def prep_mintpy(hyp3_dir, workdir):
    """產 smallbaselineApp.cfg + 用 MintPy prep_hyp3 載入。"""
    mp = Path(workdir) / 'mintpy'
    mp.mkdir(parents=True, exist_ok=True)
    cfg = mp / 'smallbaselineApp.cfg'
    cfg.write_text(
        f"""mintpy.load.processor       = hyp3
mintpy.load.unwFile         = {hyp3_dir}/*/*unw_phase.tif
mintpy.load.corFile         = {hyp3_dir}/*/*corr.tif
mintpy.load.demFile         = {hyp3_dir}/*/*dem.tif
mintpy.load.incAngleFile    = {hyp3_dir}/*/*lv_theta.tif
mintpy.load.azAngleFile     = {hyp3_dir}/*/*lv_phi.tif
mintpy.reference.yx         = auto
mintpy.troposphericDelay.method = no
""")
    print(f'[mintpy] cfg 寫好: {cfg}')
    print('[mintpy] 接著執行:')
    print(f'    cd {mp} && smallbaselineApp.py smallbaselineApp.cfg')
    print('  (若要用 smart ML 結果,把 cfg 的 unwFile/corFile 改成 *_sml.tif)')
    return cfg


# ─────────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    print(f'=== HyP3 burst InSAR → MintPy | orbit {args.orbit} {args.direction} ===')

    by_date = find_bursts(args.aoi, args.orbit, args.direction,
                          args.start, args.end, args.pol)
    if not by_date:
        print('[ERROR] 找不到 burst,檢查 AOI/orbit/direction/時間'); sys.exit(1)
    pairs = make_pairs(list(by_date.keys()), args.pair_strategy, args.nearest_n)

    # 存配對紀錄
    rec = Path(args.workdir) / 'pairs.json'
    rec.write_text(json.dumps({'pairs': pairs, 'dates': sorted(by_date)},
                              ensure_ascii=False, indent=2))

    hyp3_dir = submit_and_download(pairs, by_date, args.spacing,
                                   args.workdir, args.max_jobs, args.skip_submit)
    smart_multilook(hyp3_dir, args.smart_ml)
    prep_mintpy(hyp3_dir, args.workdir)
    print('=== 完成 ===')


if __name__ == '__main__':
    main()
