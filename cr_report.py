#!/usr/bin/env python3
"""角反射器(CR) 設置前後 效益佐證圖 — 供簡報。

證明: 頭社盆地中央為 InSAR 資料相對缺乏區(田地/低穩定散射), 於 GNSS TSB2 樁位
共站裝設角反射器後, 提供強而穩定的點反射 → 該處才有可信地表變形資料。

以「原始 SLC」全解析度振幅實證 CR 於 INSTALL 日開啟 (峰值/中值比由 ~3 跳到 ~24)。

兩項關鍵修正:
1) 高度校正定位 (取代舊 tie-point nearest-node 版):
   SAR 側視幾何下地物高程 h 於距離向偏移 Δ≈h/tan(入射角)。TSB2 高程 663m、
   入射角 ~39° → 偏 ~580m ≈ 250 距離向像素。地理格點反算若只用 (lat,lon) 做 2D
   仿射(格點高度混 0~2254m)殘差達 ~375 像素; 正解用全格點擬合
   line,pixel=f(lat,lon,height) 代入 TSB2 實際高程 (殘差降至 ~117 像素)。
2) 影像共登記 (coregistration):
   原始 SLC 各期未共登記 → 同一像素在不同影像是不同地物; 且 TSB2 落在相鄰 frame
   重疊區, 部分日期以不同 frame 切分, TSB2 位置整批位移 ~50 像素。以振幅互相關
   (SLC 共登記標準第一步) 將各期對齊到主影像, CR 固定於同一像素 → 時序乾淨。

輸出 (out_dir):
  cr_TSB2_evidence.png    時序(峰值/中值,標安裝日) + 前後振幅裁切 + 距離/方位剖面
  cr_beforeafter_mean.png 安裝前平均 / 安裝後平均 / 增益(after/before) 三聯圖
  cr_before_after_coh.png 安裝前對 vs 安裝後對 CR 鄰域複同調 (共登記後)
"""
import os
import re
import glob
import numpy as np
import xml.etree.ElementTree as ET

# ---- 預設參數 (頭社 TSB2, 與 GNSS 共站) ----
TSB2_LON, TSB2_LAT, TSB2_H = 120.9016, 23.8293, 663.0   # WGS84 經緯 + 高程(m)
INSTALL = '20250708'                                    # 資料實證的 CR 開啟日
SLC_BASE = '/mnt/SARDB/SARIMAGE/ST1/SAR_A69'            # 原始 SLC 影像庫 (rel-orbit 69)
DATE0, DATE1 = '20250101', '20260630'                   # 分析日期範圍
CR_DL, CR_DP = -3, 57                                   # CR 相對高度校正中心的粗略像素偏移
HW = 30                                                 # 分析視窗半寬 (px)
MARGIN = 70                                             # 共登記互相關的搜尋餘裕 (px)


def _grid(safe_dir):
    """讀取 IW2/VV 標註的地理定位格點 → (tif_path, G[N,5])。找不到回 None。

    G 各欄: [line, pixel, latitude, longitude, height]。
    """
    ann = glob.glob(f'{safe_dir}/annotation/s1?-iw2-slc-vv-*.xml')
    tif = glob.glob(f'{safe_dir}/measurement/s1?-iw2-slc-vv-*.tiff')
    if not ann or not tif:
        return None
    root = ET.parse(ann[0]).getroot()
    rows = [[float(g.find(k).text)
             for k in ('line', 'pixel', 'latitude', 'longitude', 'height')]
            for g in root.findall('.//geolocationGridPoint')]
    return tif[0], np.array(rows)


def _locate(safe_dir, lat, lon, hgt):
    """高度校正定位: 全格點擬合 line,pixel = f(lat,lon,height) 代入目標。

    輸出 (tif_path, line, pixel); 此 SAFE 不覆蓋目標則 None。
    """
    r = _grid(safe_dir)
    if r is None:
        return None
    tif, G = r
    L, P, LA, LO, H = G.T
    if LA.min() > lat or LA.max() < lat or LO.min() > lon or LO.max() < lon:
        return None
    A = np.c_[np.ones(L.size), LA, LO, H]            # 含高度項
    cL = np.linalg.lstsq(A, L, rcond=None)[0]
    cP = np.linalg.lstsq(A, P, rcond=None)[0]
    return tif, float(cL @ [1, lat, lon, hgt]), float(cP @ [1, lat, lon, hgt])


def _slc_dates(base, d0, d1):
    """影像庫中 [d0, d1] 範圍內日期字串 (YYYYMMDD, 排序去重)。"""
    ds = sorted(set(re.search(r'_(\d{8})T', os.path.basename(x)).group(1)
                    for x in glob.glob(f'{base}/*.SAFE')))
    return [d for d in ds if d0 <= d <= d1]


def _read_window(base, date, lat, lon, hgt, hw, dL=CR_DL, dP=CR_DP, complex_out=False):
    """讀取以 (高度校正 TSB2 + CR偏移) 為中心的 2hw×2hw 視窗 (振幅或複數)。

    因原始 SLC 未共登記, 此僅為粗略置中; 精確對齊由後續互相關共登記完成。越界回 None。
    """
    from osgeo import gdal
    for safe in sorted(glob.glob(f'{base}/*{date}T*.SAFE')):
        loc = _locate(safe, lat, lon, hgt)
        if loc is None:
            continue
        tif, line, pix = loc
        ds = gdal.Open(tif)
        r, c = int(round(line)) + dL, int(round(pix)) + dP
        H, W = ds.RasterYSize, ds.RasterXSize
        if not (hw <= r < H - hw and hw <= c < W - hw):
            return None
        z = ds.GetRasterBand(1).ReadAsArray(int(c - hw), int(r - hw), int(2 * hw), int(2 * hw))
        z = z.astype(np.complex64)
        return z if complex_out else np.abs(z)
    return None


def _xcorr_shift(a, ref):
    """振幅互相關求 a 相對 ref 的整數位移 (dy, dx)。"""
    A = np.fft.fft2(a - a.mean())
    B = np.fft.fft2(ref - ref.mean())
    c = np.fft.fftshift(np.fft.ifft2(A * np.conj(B)).real)
    py, px = np.unravel_index(np.argmax(c), c.shape)
    return py - a.shape[0] // 2, px - a.shape[1] // 2


def _coreg_crop(win, dy, dx, hw):
    """依互相關位移 (dy,dx) 對齊 win 後, 裁切中心 2hw×2hw (CR 落於正中心)。"""
    m = win.shape[0] // 2
    r, c = m + dy, m + dx                              # 對齊後 CR 在此
    return win[r - hw:r + hw, c - hw:c + hw]


def build_coreg_stack(base=SLC_BASE, lat=TSB2_LAT, lon=TSB2_LON, hgt=TSB2_H,
                      d0=DATE0, d1=DATE1, install=INSTALL, hw=HW, margin=MARGIN,
                      log=print):
    """建立共登記後的振幅堆疊 (CR 固定於視窗正中心)。

    流程: 各期讀大視窗(高度校正粗略置中) → 取安裝後訊號最強者為主影像 →
    振幅互相關求整數位移 → 對齊裁切。回傳 dict(dates, crops[T,2hw,2hw], master)。
    """
    big = hw + margin
    reads = []
    for d in _slc_dates(base, d0, d1):
        a = _read_window(base, d, lat, lon, hgt, big)
        if a is not None:
            reads.append((d, a))
    # 主影像: 安裝後 峰值/中值比 最大者 (CR 最亮, 互相關最穩)
    post = [(d, a) for d, a in reads if d >= install]
    master = max(post, key=lambda t: t[1].max() / np.median(t[1]))
    ref = master[1]
    dates, crops = [], []
    for d, a in reads:
        dy, dx = _xcorr_shift(a, ref)
        dates.append(np.datetime64(f'{d[:4]}-{d[4:6]}-{d[6:8]}'))
        crops.append(_coreg_crop(a, dy, dx, hw))
    log(f'[CR] 共登記堆疊 {len(dates)} 期, 主影像 {master[0]}, 視窗 {2*hw}×{2*hw}')
    return {'dates': np.array(dates), 'crops': np.array(crops), 'master': master[0]}


def cr_timeseries(stack, hw=HW):
    """共登記堆疊 → CR 峰值/中值比時序 (CR 固定於中心, 取 ±2px 最大)。"""
    C = stack['crops']
    peak = C[:, hw - 2:hw + 3, hw - 2:hw + 3].reshape(len(C), -1).max(1)
    med = np.median(C, axis=(1, 2))
    return peak / med


def _coh_box(z1, z2, win=5):
    """兩複數視窗的箱型窗同調性大小。"""
    from scipy.ndimage import uniform_filter
    x = z1 * np.conj(z2)
    num = np.abs(uniform_filter(x.real, win) + 1j * uniform_filter(x.imag, win))
    den = np.sqrt(uniform_filter(np.abs(z1) ** 2, win) * uniform_filter(np.abs(z2) ** 2, win))
    return num / (den + 1e-6)


def before_after_coh(out_dir, base=SLC_BASE, lat=TSB2_LAT, lon=TSB2_LON, hgt=TSB2_H,
                     before=('20250614', '20250626'), after=('20250825', '20250906'),
                     hw=HW, margin=MARGIN, log=print):
    """安裝前對 vs 安裝後對 CR 鄰域複同調 (共登記後; 資料橫跨安裝日)。回傳圖路徑。

    因 InSAR 堆疊自安裝日起跳(無安裝前干涉對), 改由原始 SLC 直接算複同調;
    對內兩期先以振幅互相關共登記, 再算箱型窗同調。
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    big = hw + margin
    fig, ax = plt.subplots(1, 2, figsize=(11, 5.2))
    for k, (pair, tt) in enumerate([(before, '安裝前對'), (after, '安裝後對')]):
        z1 = _read_window(base, pair[0], lat, lon, hgt, big, complex_out=True)
        z2 = _read_window(base, pair[1], lat, lon, hgt, big, complex_out=True)
        dy, dx = _xcorr_shift(np.abs(z2), np.abs(z1))          # z2 對齊 z1
        m = big
        z1c = z1[m - hw:m + hw, m - hw:m + hw]
        z2c = z2[m + dy - hw:m + dy + hw, m + dx - hw:m + dx + hw]
        g = _coh_box(z1c, z2c)
        cr = float(g[hw - 2:hw + 2, hw - 2:hw + 2].max())
        bg = float(np.median(g))
        im = ax[k].imshow(g, cmap='viridis', vmin=0, vmax=1, extent=[-hw, hw, hw, -hw])
        ax[k].plot(0, 0, 'r+', ms=16, mew=2)
        ax[k].set_title(f'{tt} {pair[0]}_{pair[1]}\nCR點同調={cr:.2f}  盆地背景中位={bg:.2f}')
        ax[k].set_xticks([]); ax[k].set_yticks([])
        plt.colorbar(im, ax=ax[k], shrink=0.8)
        log(f'[CR] {tt} CR同調={cr:.2f} 背景={bg:.2f}')
    fig.suptitle('TSB2 角反射器 鄰域同調性 (原始SLC複同調, 已共登記) — 安裝後 CR 造出高同調測點\n'
                 '周邊田地盆地同調低(資料缺乏); CR 提供穩定相位 → 可測地表變形', fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.85])
    p = os.path.join(out_dir, 'cr_before_after_coh.png')
    fig.savefig(p, dpi=130)
    plt.close(fig)
    log(f'[CR] 前後同調性圖 {os.path.basename(p)}')
    return p


def make_figures(out_dir, base=SLC_BASE, lat=TSB2_LAT, lon=TSB2_LON, hgt=TSB2_H,
                 install=INSTALL, d0=DATE0, d1=DATE1, hw=HW, log=print):
    """產出簡報用 CR 佐證圖 (主圖 + 前後平均三聯圖 + 前後同調性)。回傳主圖路徑。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    os.makedirs(out_dir, exist_ok=True)
    inst = np.datetime64(f'{install[:4]}-{install[4:6]}-{install[6:8]}')

    stack = build_coreg_stack(base, lat, lon, hgt, d0, d1, install, hw=hw, log=log)
    dd = stack['dates']
    C = stack['crops']
    pre, post = dd < inst, dd >= inst

    # --- 前後同調性圖 (共登記複同調) ---
    try:
        before_after_coh(out_dir, base, lat, lon, hgt, hw=hw, log=log)
    except Exception as e:
        log(f'[CR] 同調性圖略過: {e}')

    # --- 前後平均三聯圖 ---
    bmean, amean = C[pre].mean(0), C[post].mean(0)
    gain = amean / (bmean + 1.0)
    ext = [-hw, hw, hw, -hw]
    fig, ax = plt.subplots(1, 3, figsize=(16, 5.6))
    for a, (img, tt, kw) in zip(ax, [
            (np.log10(bmean + 1), f'安裝前平均振幅 (<{install}, {pre.sum()}期)',
             dict(cmap='inferno', vmin=1.4, vmax=3.2)),
            (np.log10(amean + 1), f'安裝後平均振幅 (>={install}, {post.sum()}期)',
             dict(cmap='inferno', vmin=1.4, vmax=3.2)),
            (gain, '增益 after/before (CR=中心亮點)',
             dict(cmap='hot', vmin=1, vmax=8))]):
        im = a.imshow(img, aspect='auto', extent=ext, **kw)
        a.plot(0, 0, 'c+', ms=16, mew=1.8)               # CR = 中心
        a.set_xlabel('距離向 (px)'); a.set_ylabel('方位向 (px)')
        a.set_title(tt)
        plt.colorbar(im, ax=a, shrink=0.75)
    fig.suptitle(f'共登記後 TSB2/CR(青+, {hgt:.0f}m) 周邊振幅 安裝前/後 — 主影像 {stack["master"]}',
                 fontsize=13)
    fig.tight_layout()
    p2 = os.path.join(out_dir, 'cr_beforeafter_mean.png')
    fig.savefig(p2, dpi=130)
    plt.close(fig)
    log(f'[CR] 前後平均圖 {os.path.basename(p2)}')

    # --- 主佐證圖: 時序 + 前後裁切 + 雙剖面 ---
    rt = cr_timeseries(stack, hw)
    r_pre, r_post = np.median(rt[pre]), np.median(rt[post])
    log(f'[CR] 峰值/中值比: 安裝前中位 {r_pre:.1f} → 安裝後中位 {r_post:.1f} '
        f'({r_post / r_pre:.1f} 倍)')
    i_bef = np.where(pre)[0][-1]
    i_aft = np.where(post)[0][np.argmax(rt[post])]

    fig = plt.figure(figsize=(15, 9))
    ax0 = fig.add_subplot(2, 1, 1)
    ax0.axvspan(inst, dd[-1] + np.timedelta64(12, 'D'), color='#e8f5e9', zorder=0)
    ax0.axvline(inst, color='green', ls='--', lw=1.5)
    ax0.plot(dd, rt, 'o-', color='navy')
    ax0.annotate(f'角反射器 {install[:4]}/{install[4:6]}/{install[6:]} 生效',
                 (inst, rt.max()), color='green', fontsize=11, ha='left', va='top',
                 xytext=(8, -2), textcoords='offset points')
    ax0.set_ylabel('峰值/中值比 (點反射強度)')
    ax0.grid(alpha=0.3)
    ax0.set_title(f'TSB2 角反射器 訊號強度時序 — 共登記後 (原始SLC {d0[:6]}~{d1[:6]}, '
                  f'共{len(dd)}期)')
    ax0.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    axb = fig.add_subplot(2, 4, 5)
    axa = fig.add_subplot(2, 4, 6)
    vmax = np.log10(C[i_aft].max() + 1)
    for ax, i, tt in [(axb, i_bef, '安裝前'), (axa, i_aft, '安裝後')]:
        ax.imshow(np.log10(C[i] + 1), cmap='gray', vmin=1.4, vmax=vmax)
        ax.plot(hw, hw, 'r+', ms=14, mew=1.8)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(f'{tt} {str(dd[i])[:10]}\n峰值/中值={rt[i]:.0f}')

    x = np.arange(-hw, hw)
    axh = fig.add_subplot(2, 4, 7)
    axv = fig.add_subplot(2, 4, 8)
    axh.plot(x, C[i_bef][hw, :], color='gray', label='安裝前')
    axh.plot(x, C[i_aft][hw, :], color='crimson', label='安裝後')
    axh.set_title('水平剖面(距離向) 過CR'); axh.set_xlabel('距離向 (px)')
    axh.legend(fontsize=8); axh.grid(alpha=0.3)
    axv.plot(x, C[i_bef][:, hw], color='gray', label='安裝前')
    axv.plot(x, C[i_aft][:, hw], color='crimson', label='安裝後')
    axv.set_title('垂直剖面(方位向) 過CR'); axv.set_xlabel('方位向 (px)')
    axv.legend(fontsize=8); axv.grid(alpha=0.3)

    fig.suptitle(f'頭社 TSB2 角反射器 佐證 — CR 與 GNSS TSB2 共站'
                 f'({lon:.4f}°E, {lat:.4f}°N, {hgt:.0f}m); 高度校正定位 + 影像共登記',
                 fontsize=12)
    fig.tight_layout()
    p1 = os.path.join(out_dir, 'cr_TSB2_evidence.png')
    fig.savefig(p1, dpi=135)
    plt.close(fig)
    log(f'[CR] 主佐證圖 {os.path.basename(p1)}')
    return p1


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='角反射器設置前後佐證圖 (高度校正 + 共登記)')
    ap.add_argument('--out', default=None, help='輸出資料夾 (預設 <slc-base父>/CR_report)')
    ap.add_argument('--slc-base', default=SLC_BASE, help='原始 SLC 影像庫目錄')
    ap.add_argument('--lat', type=float, default=TSB2_LAT)
    ap.add_argument('--lon', type=float, default=TSB2_LON)
    ap.add_argument('--height', type=float, default=TSB2_H, help='目標橢球高(m), 距離向校正關鍵')
    ap.add_argument('--install', default=INSTALL, help='CR 開啟日 YYYYMMDD')
    ap.add_argument('--d0', default=DATE0)
    ap.add_argument('--d1', default=DATE1)
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(a.slc_base.rstrip('/')), 'CR_report')
    p = make_figures(out, a.slc_base, a.lat, a.lon, a.height, a.install, a.d0, a.d1)
    print('CR 佐證圖完成 →', p)
