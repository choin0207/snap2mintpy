#!/usr/bin/env python3
"""GNSS ↔ InSAR 比對與累積變形量地圖的核心邏輯。

被 snap2mintpy_gui.py 的「GNSS 比對」「累積變形量地圖」分頁呼叫；也可獨立執行:

    python3 gnss_compare.py <mintpy_dir> <gnss_dir> --ref TSBS --obs TSB2

GNSS xlsx 欄位: Date, DOY, Year, N, E, h  (N/E 為投影座標, h 為橢球高=垂直 U)。
座標系統預設 TWD97/TM2 (EPSG:3826); InSAR timeseries.h5 為 LOS 位移(公尺)。

比對方法(兩種都出):
  U 比對  : InSAR LOS 投影成垂直 U = d_LOS / cos(入射角); 與 GNSS h 位移比。
  LOS 比對: GNSS 的 (dE,dN,dU) 用 heading+入射角投影成 LOS; 與 InSAR LOS 比。
LOS 單位向量(地面→衛星): φ=heading-90°(右視), [sinθsinφ, sinθcosφ, cosθ]·[E,N,U]。
參考點校正: 整個 InSAR 場減去參考站像素值 (空間參考); GNSS 亦以參考站為基準。
"""
import os
import glob
import numpy as np


# ─────────────────────────────────────────────────────────────────────────
# GNSS 讀取 / 座標
# ─────────────────────────────────────────────────────────────────────────
def read_gnss_xlsx(path):
    """讀單站 GNSS xlsx → dict(name, dates[datetime64], N, E, h)。"""
    import pandas as pd
    df = pd.read_excel(path, sheet_name=0)
    cols = {c.lower(): c for c in df.columns}
    need = ['date', 'n', 'e', 'h']
    for k in need:
        if k not in cols:
            raise ValueError(f'{os.path.basename(path)} 缺欄位 {k} (現有 {list(df.columns)})')
    dates = pd.to_datetime(df[cols['date']]).values.astype('datetime64[D]')
    name = os.path.basename(path).split('_')[0].split('.')[0]
    return {
        'name': name,
        'dates': dates,
        'N': df[cols['n']].to_numpy(float),
        'E': df[cols['e']].to_numpy(float),
        'h': df[cols['h']].to_numpy(float),
    }


def scan_gnss_dir(gnss_dir):
    """回傳 {station_name: xlsx_path} (掃 *.xlsx)。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(gnss_dir, '*.xlsx'))):
        if os.path.basename(p).startswith('~$'):
            continue
        name = os.path.basename(p).split('_')[0].split('.')[0]
        out[name] = p
    return out


def station_lonlat(E, N, epsg=3826):
    """投影座標 (E,N) → (lon,lat) WGS84。epsg 預設 TWD97/TM2。"""
    from pyproj import Transformer
    t = Transformer.from_crs(int(epsg), 4326, always_xy=True)
    lon, lat = t.transform(np.mean(E), np.mean(N))
    return float(lon), float(lat)


# 常用座標系統 (GUI 下拉)
EPSG_OPTIONS = [
    ('TWD97 / TM2 (EPSG:3826)', 3826),
    ('WGS84 UTM 51N (EPSG:32651)', 32651),
    ('WGS84 經緯度 (EPSG:4326)', 4326),
    ('TWD67 / TM2 (EPSG:3828)', 3828),
]


# ─────────────────────────────────────────────────────────────────────────
# InSAR timeseries / geometry 讀取
# ─────────────────────────────────────────────────────────────────────────
def _geo(attrs):
    def g(k):
        v = attrs.get(k)
        return float(v.decode() if isinstance(v, bytes) else v)
    return g


def lonlat_to_rc(attrs, lon, lat):
    """geocoded h5 屬性 + lon/lat → (row, col); 超界回 None。"""
    g = _geo(attrs)
    xf, xs, yf, ys = g('X_FIRST'), g('X_STEP'), g('Y_FIRST'), g('Y_STEP')
    W, L = int(g('WIDTH')), int(g('LENGTH'))
    col = int(round((lon - xf) / xs))
    row = int(round((lat - yf) / ys))
    if 0 <= row < L and 0 <= col < W:
        return row, col
    return None


def read_insar_ts(mintpy_dir):
    """讀 timeseries + geometryGeo(incidenceAngle) + heading。
    優先用 timeseries_demErr.h5 (與 velocity.h5=demErr-only 一致, 且去 DEM 誤差雜訊),
    無則退回 timeseries.h5。回傳 dict(dates, ts[N,H,W], attrs, inc[H,W], heading, ...)。"""
    import h5py
    ts_p = os.path.join(mintpy_dir, 'timeseries.h5')
    for cand in ('timeseries_demErr.h5',):
        cp = os.path.join(mintpy_dir, cand)
        if os.path.exists(cp):
            ts_p = cp
            break
    with h5py.File(ts_p, 'r') as f:
        dates = np.array([d.decode() for d in f['date'][:]])
        ts = f['timeseries'][:]        # (N, H, W) 公尺
        attrs = dict(f.attrs)
    dates = np.array([np.datetime64(f'{d[:4]}-{d[4:6]}-{d[6:8]}') for d in dates])
    heading = float(attrs.get('HEADING', 349.44)
                    if not isinstance(attrs.get('HEADING'), bytes)
                    else attrs.get('HEADING').decode())
    gp = os.path.join(mintpy_dir, 'inputs', 'geometryGeo.h5')
    with h5py.File(gp, 'r') as f:
        inc = f['incidenceAngle'][:]
        gattrs = dict(f.attrs)
    return {'dates': dates, 'ts': ts, 'attrs': attrs,
            'inc': inc, 'heading': heading, 'geom_attrs': gattrs}


# ─────────────────────────────────────────────────────────────────────────
# LOS ↔ U 投影
# ─────────────────────────────────────────────────────────────────────────
def los_unit_enu(inc_deg, heading_deg):
    """LOS 單位向量 (地面→衛星) 的 ENU 分量。右視: φ=heading-90°。"""
    inc = np.radians(inc_deg)
    phi = np.radians(heading_deg - 90.0)
    return (np.sin(inc) * np.sin(phi),   # E
            np.sin(inc) * np.cos(phi),   # N
            np.cos(inc))                 # U


def los_to_u(los, inc_deg):
    """LOS 位移 → 垂直 U (假設純垂直運動): U = LOS / cos(入射角)。"""
    return los / np.cos(np.radians(inc_deg))


def enu_to_los(dE, dN, dU, inc_deg, heading_deg):
    """GNSS (dE,dN,dU) → LOS 位移。"""
    e, n, u = los_unit_enu(inc_deg, heading_deg)
    return e * dE + n * dN + u * dU


# ─────────────────────────────────────────────────────────────────────────
# 時序對齊 / 位移化
# ─────────────────────────────────────────────────────────────────────────
def gnss_disp(station, ref_epoch=None):
    """GNSS 位置 → 位移(相對 ref_epoch 或首期), 回 (dE,dN,dU) 公尺。"""
    i0 = 0 if ref_epoch is None else int(np.argmin(np.abs(station['dates'] - ref_epoch)))
    return (station['E'] - station['E'][i0],
            station['N'] - station['N'][i0],
            station['h'] - station['h'][i0])


def sample_insar_series(ins, lon, lat):
    """InSAR 某點時序: 回 (dates, los_series[m], inc_deg) 或 None。"""
    rc = lonlat_to_rc(ins['attrs'], lon, lat)
    if rc is None:
        return None
    r, c = rc
    los = ins['ts'][:, r, c].astype(float)        # 相對首期 (timeseries 本就是位移)
    los = los - los[0]
    return ins['dates'], los, float(ins['inc'][r, c])


# ─────────────────────────────────────────────────────────────────────────
# 輸出 1: GNSS vs InSAR 時序比對圖 (U + LOS) + CSV
# ─────────────────────────────────────────────────────────────────────────
# GNSS↔InSAR 比對圖的預設判讀註解 (可由 GUI 覆寫)。頭社為泥炭盆地:
# GNSS 深標量深層穩定, InSAR 量地表 0-8m 泥炭壓縮 → 差距為物理真實, 非誤差。
DEFAULT_NOTE = ('註: GNSS 墩標錨於地底深處(量深層穩定), InSAR 反映地表變形'
                '(頭社泥炭盆地=0–8m 泥炭土壓縮沉陷) → 兩者差距為物理真實, 非處理誤差。')


def compare_station(mintpy_dir, gnss_dir, ref_name, obs_name,
                    epsg=3826, out_dir=None, note=DEFAULT_NOTE, log=print):
    """對 obs 站做 GNSS↔InSAR 比對 (U + LOS 兩種), 出圖+CSV。
    參考站 ref_name 用於空間/基準校正。note=圖下方判讀註解(空字串=不加)。
    回傳輸出檔清單。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import pandas as pd
    out_dir = out_dir or os.path.join(mintpy_dir, 'pic')
    os.makedirs(out_dir, exist_ok=True)
    stations = scan_gnss_dir(gnss_dir)
    if ref_name not in stations or obs_name not in stations:
        raise ValueError(f'找不到測站 ref={ref_name} / obs={obs_name} (有 {list(stations)})')
    ins = read_insar_ts(mintpy_dir)
    ref = read_gnss_xlsx(stations[ref_name])
    obs = read_gnss_xlsx(stations[obs_name])
    ref_lon, ref_lat = station_lonlat(ref['E'], ref['N'], epsg)
    obs_lon, obs_lat = station_lonlat(obs['E'], obs['N'], epsg)
    log(f'[GNSS] 參考站 {ref_name} @ ({ref_lon:.4f},{ref_lat:.4f}); 觀測站 {obs_name} @ ({obs_lon:.4f},{obs_lat:.4f})')

    # InSAR 時序 (obs、ref 像素); 以首期為基準
    si_obs = sample_insar_series(ins, obs_lon, obs_lat)
    si_ref = sample_insar_series(ins, ref_lon, ref_lat)
    if si_obs is None:
        raise ValueError(f'觀測站 {obs_name} 不在 InSAR 範圍內')
    dts, los_obs, inc_obs = si_obs
    los_ref = si_ref[1] if si_ref is not None else np.zeros_like(los_obs)
    los_obs_c = los_obs - los_ref                       # 空間參考校正
    u_insar = los_to_u(los_obs_c, inc_obs) * 1000.0     # mm

    # InSAR 起點歸零 (t0 = 0)
    t0, t1 = dts[0], dts[-1]
    u_insar = u_insar - u_insar[0]
    los_insar = los_obs_c * 1000.0
    los_insar = los_insar - los_insar[0]

    # GNSS: obs 相對 ref (空間基準); 裁到 InSAR 期間; 起點歸零 (t0 = 0)
    dE_o, dN_o, dU_o = gnss_disp(obs, t0)
    dE_r, dN_r, dU_r = gnss_disp(ref, t0)
    def _interp(src_dates, src_val, tgt_dates):
        x = src_dates.astype('datetime64[D]').astype(float)
        return np.interp(np.asarray(tgt_dates).astype('datetime64[D]').astype(float), x, src_val)
    gdates = obs['dates'].astype('datetime64[D]')
    dU_ref_i = _interp(ref['dates'], dU_r, gdates)
    dE_ref_i = _interp(ref['dates'], dE_r, gdates)
    dN_ref_i = _interp(ref['dates'], dN_r, gdates)
    u_gnss = (dU_o - dU_ref_i) * 1000.0                 # mm, 相對參考站
    los_gnss = enu_to_los(dE_o - dE_ref_i, dN_o - dN_ref_i, dU_o - dU_ref_i,
                          inc_obs, ins['heading']) * 1000.0
    # 裁到 InSAR 期間 [t0, t1]
    win = (gdates >= t0) & (gdates <= t1)
    gdates, u_gnss, los_gnss = gdates[win], u_gnss[win], los_gnss[win]
    # 起點歸零: 減去 t0(或窗內最早)處的值 → GNSS 起點也在 0
    if len(gdates):
        gx = gdates.astype(float)
        u_gnss = u_gnss - np.interp(t0.astype('datetime64[D]').astype(float), gx, u_gnss)
        los_gnss = los_gnss - np.interp(t0.astype('datetime64[D]').astype(float), gx, los_gnss)

    # ── 圖: 上 U 比對 / 下 LOS 比對 (皆裁 InSAR 期, 起點在0) ──
    fig, ax = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
    ax[0].axhline(0, color='k', lw=0.6, alpha=0.4)
    ax[0].plot(gdates, u_gnss, '.-', ms=4, lw=0.6, color='gray', alpha=0.7, label=f'GNSS U ({obs_name}-{ref_name})')
    ax[0].plot(dts, u_insar, 'o-', color='crimson', ms=5, label='InSAR U (LOS/cosθ)')
    ax[0].set_ylabel('垂直位移 U (mm)'); ax[0].legend(fontsize=9); ax[0].grid(alpha=0.3)
    ax[0].set_title(f'GNSS vs InSAR 垂直 U  |  obs={obs_name} ref={ref_name} (起點=0, InSAR期)')
    ax[1].axhline(0, color='k', lw=0.6, alpha=0.4)
    ax[1].plot(gdates, los_gnss, '.-', ms=4, lw=0.6, color='gray', alpha=0.7, label='GNSS→LOS')
    ax[1].plot(dts, los_insar, 's-', color='navy', ms=5, label='InSAR LOS')
    ax[1].set_ylabel('視線向 LOS (mm)'); ax[1].legend(fontsize=9); ax[1].grid(alpha=0.3)
    ax[1].set_title('GNSS(投影) vs InSAR 視線向 LOS')
    ax[1].set_xlim(t0.astype('datetime64[D]').astype('O'),
                   t1.astype('datetime64[D]').astype('O'))
    ax[1].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    fig.autofmt_xdate()
    if note:
        fig.tight_layout(rect=(0, 0.055, 1, 1))
        fig.text(0.5, 0.012, note, ha='center', va='bottom', fontsize=8.5,
                 color='#333', wrap=True,
                 bbox=dict(fc='#fffbe6', ec='#ccaa33', alpha=0.9, pad=3))
    else:
        fig.tight_layout()
    png = os.path.join(out_dir, f'GNSS_InSAR_{obs_name}_ref{ref_name}.png')
    fig.savefig(png, dpi=130); plt.close(fig)

    # ── CSV: InSAR 期的 U/LOS + 內插的 GNSS (皆起點=0) ──
    csv = os.path.join(out_dir, f'GNSS_InSAR_{obs_name}_ref{ref_name}.csv')
    g_u_on_ins = _interp(gdates, u_gnss, dts) if len(gdates) else np.full(len(dts), np.nan)
    g_los_on_ins = _interp(gdates, los_gnss, dts) if len(gdates) else np.full(len(dts), np.nan)
    pd.DataFrame({
        'date': dts.astype(str),
        'InSAR_U_mm': np.round(u_insar, 2), 'GNSS_U_mm': np.round(g_u_on_ins, 2),
        'InSAR_LOS_mm': np.round(los_insar, 2), 'GNSS_LOS_mm': np.round(g_los_on_ins, 2),
    }).to_csv(csv, index=False)
    log(f'[GNSS] 出圖 {os.path.basename(png)} + 表 {os.path.basename(csv)}')
    return {'png': png, 'csv': csv, 'ref_lonlat': (ref_lon, ref_lat),
            'obs_lonlat': (obs_lon, obs_lat)}


# ─────────────────────────────────────────────────────────────────────────
# 輸出 2: 參考點校正後 velocity 圖 (標參考站座標+名)
# ─────────────────────────────────────────────────────────────────────────
def gnss_vu(station, lo=None, hi=None):
    """GNSS 垂直速度 Vu (mm/yr) = h 對時間的線性斜率。

    lo/hi (np.datetime64): 只用此日期窗內的資料擬合。**必給 InSAR 期間**,
    否則各站不同時間跨度的斜率不可比 (且非線性測站在不同期間斜率甚至變號),
    參考校正會出錯。回 NaN 表資料不足。
    """
    d = station['dates'].astype('datetime64[D]')
    t = d.astype(float) / 365.25                  # 年 (相對, 斜率不受基準影響)
    h = station['h'] * 1000.0                      # mm
    ok = np.isfinite(t) & np.isfinite(h)
    if lo is not None:
        ok &= (d >= np.datetime64(lo, 'D'))
    if hi is not None:
        ok &= (d <= np.datetime64(hi, 'D'))
    if ok.sum() < 2:
        return np.nan
    return float(np.polyfit(t[ok], h[ok], 1)[0])


def refcorrected_velocity_map(mintpy_dir, gnss_dir, ref_name, epsg=3826,
                              vel_file='velocity.h5', coh_thresh=0.5,
                              osm=True, out_dir=None, log=print):
    """參考點校正後的『垂直 Vu 速度場』(InSAR LOS/cosθ), 疊全部 GNSS 站:
    圓形填色=該站位置 InSAR Vu (底); 其上疊較小三角形填色=GNSS Vu。同色階可比。
    GNSS Vu 統一以 InSAR 期間擬合 (各站不同跨度不可比, 非線性測站甚至變號)。
    參考站另加金色外框。osm=True 加 OpenStreetMap 底圖。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import h5py
    out_dir = out_dir or os.path.join(mintpy_dir, 'pic')
    os.makedirs(out_dir, exist_ok=True)
    stations = scan_gnss_dir(gnss_dir)
    if ref_name not in stations:
        raise ValueError(f'找不到參考站 {ref_name} (有 {list(stations)})')
    # InSAR 期間 → GNSS Vu 統一擬合窗
    with h5py.File(os.path.join(mintpy_dir, 'timeseries.h5'), 'r') as f:
        _d = [x.decode() for x in f['date'][:]]
    t0 = np.datetime64(f'{_d[0][:4]}-{_d[0][4:6]}-{_d[0][6:8]}')
    t1 = np.datetime64(f'{_d[-1][:4]}-{_d[-1][4:6]}-{_d[-1][6:8]}')
    # 全部站: lon/lat + GNSS Vu (InSAR 期)
    st_ll, st_vu = {}, {}
    for nm, p in stations.items():
        s = read_gnss_xlsx(p)
        st_ll[nm] = station_lonlat(s['E'], s['N'], epsg)
        st_vu[nm] = gnss_vu(s, t0, t1)
    log(f'[GNSS] Vu 擬合期間 {t0}~{t1}; 各站 Vu(絕對): '
        + ', '.join(f'{k}={v:+.1f}' for k, v in st_vu.items()))
    ref_lon, ref_lat = st_ll[ref_name]
    ref_gvu = st_vu[ref_name]
    # GNSS Vu 以參考站為基準
    for nm in st_vu:
        st_vu[nm] = st_vu[nm] - ref_gvu

    with h5py.File(os.path.join(mintpy_dir, vel_file), 'r') as f:
        vel = f['velocity'][:] * 1000.0     # LOS mm/yr
        attrs = dict(f.attrs)
    with h5py.File(os.path.join(mintpy_dir, 'inputs', 'geometryGeo.h5'), 'r') as f:
        inc = f['incidenceAngle'][:]
    tcp = os.path.join(mintpy_dir, 'temporalCoherence.h5')
    if os.path.exists(tcp):
        with h5py.File(tcp, 'r') as f:
            tc = f['temporalCoherence'][:]
        vel = np.where(tc >= coh_thresh, vel, np.nan)
    rc = lonlat_to_rc(attrs, ref_lon, ref_lat)
    if rc is None:
        raise ValueError(f'參考站 {ref_name} 不在 velocity 範圍')
    vel_c = vel - vel[rc[0], rc[1]]                 # LOS 參考校正
    vu_field = vel_c / np.cos(np.radians(inc))      # → 垂直 Vu (mm/yr)
    g = _geo(attrs)
    xf, xs, W = g('X_FIRST'), g('X_STEP'), int(g('WIDTH'))
    yf, ys, L = g('Y_FIRST'), g('Y_STEP'), int(g('LENGTH'))

    def insar_vu_at(lon, lat):
        r = lonlat_to_rc(attrs, lon, lat)
        return None if r is None else float(vu_field[r[0], r[1]])

    # 色階涵蓋場 + 站點 Vu
    allv = [np.nanpercentile(np.abs(vu_field), 95)]
    allv += [abs(v) for v in st_vu.values() if np.isfinite(v)]
    allv += [abs(insar_vu_at(*ll)) for ll in st_ll.values() if insar_vu_at(*ll) is not None]
    vmax = max(allv) if allv else 30.0
    cmap = plt.get_cmap('RdYlBu')
    norm = plt.Normalize(-vmax, vmax)
    fig, ax = plt.subplots(figsize=(9.8, 8.6))

    used_osm = False
    if osm:
        try:
            import contextily as ctx
            from pyproj import Transformer
            tm = Transformer.from_crs(4326, 3857, always_xy=True)
            x0, y0 = tm.transform(xf, yf + ys * L)
            x1, y1 = tm.transform(xf + xs * W, yf)
            im = ax.imshow(vu_field, extent=[x0, x1, y0, y1], origin='upper',
                           cmap=cmap, norm=norm, alpha=0.6, zorder=3)
            ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik,
                            zoom='auto', reset_extent=False, attribution_size=6)
            def to_xy(lon, lat):
                return tm.transform(lon, lat)
            yspan = abs(y1 - y0); used_osm = True
        except Exception as exc:
            log(f'[GNSS] OSM 底圖失敗({exc}), 改純色圖')
    if not used_osm:
        im = ax.imshow(vu_field, extent=[xf, xf + xs * W, yf + ys * L, yf],
                       origin='upper', cmap=cmap, norm=norm, zorder=3)
        def to_xy(lon, lat):
            return lon, lat
        ax.set_xlabel('經度'); ax.set_ylabel('緯度')

    # 疊每站(同位置): 圓形=InSAR Vu(底) + 其上較小三角形=GNSS Vu; 符號縮小 1/3
    station_info = []
    for nm, (lo, la) in st_ll.items():
        x, y = to_xy(lo, la)
        is_ref = (nm == ref_name)
        gvu = st_vu[nm]                       # 已在下方做參考校正
        ivu = insar_vu_at(lo, la)
        # 圓形 = InSAR Vu (底層大圓)
        if ivu is not None:
            ax.scatter([x], [y], marker='o', s=350 if is_ref else 250,
                       c=[ivu], cmap=cmap, norm=norm,
                       edgecolors='gold' if is_ref else 'k',
                       linewidths=2.0 if is_ref else 1.0, zorder=6)
        # 三角形 = GNSS Vu (疊在圓上, 較小)
        ax.scatter([x], [y], marker='^', s=150 if is_ref else 105,
                   c=[gvu], cmap=cmap, norm=norm,
                   edgecolors='gold' if is_ref else 'k',
                   linewidths=1.6 if is_ref else 0.9, zorder=7)
        gtxt = f'{gvu:+.1f}' if np.isfinite(gvu) else 'NA'
        itxt = f'{ivu:+.1f}' if ivu is not None else 'NA'
        station_info.append((nm, is_ref, x, y, gtxt, itxt))
    if used_osm:
        ax.set_xticks([]); ax.set_yticks([])
    # 站點說明框移到資料範圍外(下方), 引線指向站點 (避免遮住速度場)
    ns = max(1, len(station_info))
    for i, (nm, is_ref, x, y, gtxt, itxt) in enumerate(station_info):
        fx = (i + 0.5) / ns                            # 沿底部均勻分布
        ax.annotate(f'{"REF " if is_ref else ""}{nm}\nGNSS Vu {gtxt}\nInSAR Vu {itxt} (mm/yr)',
                    xy=(x, y), xycoords='data',
                    xytext=(fx, -0.14), textcoords='axes fraction',
                    fontsize=8.5, color='k', ha='center', va='top', zorder=9,
                    annotation_clip=False,
                    bbox=dict(boxstyle='round', fc='white', alpha=0.92,
                              ec='gold' if is_ref else 'steelblue'),
                    arrowprops=dict(arrowstyle='->', color='k', lw=0.9,
                                    connectionstyle='arc3,rad=0.15'))
    # 圖例: 三角=GNSS, 圓=InSAR (標籤不含符號字元, 避免與 marker 重複畫成兩次)
    from matplotlib.lines import Line2D
    leg = [Line2D([0], [0], marker='^', color='w', mfc='gray', mec='k', ms=12, label='GNSS Vu'),
           Line2D([0], [0], marker='o', color='w', mfc='gray', mec='k', ms=13, label='InSAR Vu')]
    ax.legend(handles=leg, loc='upper right', fontsize=9, framealpha=0.9)
    ax.set_title(f'垂直 Vu 速度場 (參考點 {ref_name} 校正) + GNSS 站\n'
                 f'▲=GNSS Vu ●=InSAR Vu  紅=下沉 藍=上升')
    cb = plt.colorbar(im, ax=ax, shrink=0.8); cb.set_label('垂直速度 Vu (mm/yr)')
    fig.tight_layout()
    png = os.path.join(out_dir, f'velocity_Vu_refcorr_{ref_name}_GNSS.png')
    fig.savefig(png, dpi=140, bbox_inches='tight'); plt.close(fig)
    log(f'[GNSS] Vu 速度場圖(▲GNSS ●InSAR, {len(st_ll)}站) {os.path.basename(png)}')
    return png


# ─────────────────────────────────────────────────────────────────────────
# 輸出 3: 累積變形量 4×N 網格 + GIF
# ─────────────────────────────────────────────────────────────────────────
def cumulative_deformation(mintpy_dir, coh_thresh=0.5, ncol=4,
                           out_dir=None, ref_lonlat=None, log=print):
    """timeseries 各期(相對首期)累積變形: 4×N 子圖 + GIF。
    只左上標 Y(緯度)、左下標 X(經度)(共用軸); 每格標題=該期日期。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import imageio.v2 as imageio
    import h5py
    out_dir = out_dir or os.path.join(mintpy_dir, 'pic')
    os.makedirs(out_dir, exist_ok=True)
    ins = read_insar_ts(mintpy_dir)
    ts = ins['ts'] * 1000.0                          # mm, (N,H,W)
    # MintPy timeseries 以 REF_DATE 為時間基準(該日=0), 不一定是首期。
    # 明確減去首期 → 真正「相對首期(dates[0])」的累積變形, 首期歸零。
    ts = ts - ts[0][None, :, :]
    dates = ins['dates']
    a = ins['attrs']; g = _geo(a)
    xf, xs, W = g('X_FIRST'), g('X_STEP'), int(g('WIDTH'))
    yf, ys, L = g('Y_FIRST'), g('Y_STEP'), int(g('LENGTH'))
    ext = [xf, xf + xs * W, yf + ys * L, yf]
    tcp = os.path.join(mintpy_dir, 'temporalCoherence.h5')
    if os.path.exists(tcp):
        with h5py.File(tcp, 'r') as f:
            mask = f['temporalCoherence'][:] >= coh_thresh
        ts = np.where(mask[None], ts, np.nan)
    N = ts.shape[0]
    vmax = np.nanpercentile(np.abs(ts), 98)
    nrow = int(np.ceil(N / ncol))

    # ── 4×N 網格圖 ──
    fig, axes = plt.subplots(nrow, ncol, figsize=(ncol * 2.6, nrow * 2.6),
                             sharex=True, sharey=True)
    axes = np.atleast_2d(axes)
    for i in range(nrow * ncol):
        r, c = divmod(i, ncol)
        ax = axes[r, c]
        if i < N:
            im = ax.imshow(ts[i], extent=ext, origin='upper', cmap='RdYlBu',
                           vmin=-vmax, vmax=vmax)
            ax.set_title(str(dates[i]), fontsize=8)
        else:
            ax.set_visible(False)
        # 只左上標 Y座標、只左下(最底列左欄)標 X座標
        if not (c == 0 and r == 0):
            ax.set_ylabel('')
        if c == 0 and r == 0:
            ax.set_ylabel('緯度', fontsize=9)
        if c == 0 and r == nrow - 1:
            ax.set_xlabel('經度', fontsize=9)
        ax.tick_params(labelsize=6)
    fig.suptitle('累積變形量 (相對 %s, mm) 紅=下沉 藍=上升' % str(dates[0]), fontsize=12)
    cb = fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.6, pad=0.02)
    cb.set_label('累積變形 (mm)')
    grid_png = os.path.join(out_dir, 'cumulative_deformation_grid.png')
    fig.savefig(grid_png, dpi=120); plt.close(fig)

    # ── GIF (每期一張, 疊參考點) ──
    frames = []
    tmp = os.path.join(out_dir, '_gif_frames'); os.makedirs(tmp, exist_ok=True)
    for i in range(N):
        f2, ax2 = plt.subplots(figsize=(6, 5.4))
        im2 = ax2.imshow(ts[i], extent=ext, origin='upper', cmap='RdYlBu',
                         vmin=-vmax, vmax=vmax)
        if ref_lonlat:
            ax2.plot(*ref_lonlat, marker='^', ms=11, mfc='lime', mec='k', zorder=5)
        ax2.set_title(f'累積變形 {dates[i]} (相對 {dates[0]})', fontsize=11)
        ax2.set_xlabel('經度'); ax2.set_ylabel('緯度')
        cb2 = plt.colorbar(im2, ax=ax2, shrink=0.85); cb2.set_label('mm')
        f2.tight_layout()
        fp = os.path.join(tmp, f'f{i:03d}.png')
        f2.savefig(fp, dpi=95); plt.close(f2)
        frames.append(imageio.imread(fp))
    gif = os.path.join(out_dir, 'cumulative_deformation.gif')
    imageio.mimsave(gif, frames, duration=0.6, loop=0)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    log(f'[累積變形] 網格圖 {os.path.basename(grid_png)} + GIF {os.path.basename(gif)} ({N} 期)')
    return {'grid': grid_png, 'gif': gif}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('mintpy_dir')
    ap.add_argument('gnss_dir')
    ap.add_argument('--ref', required=True)
    ap.add_argument('--obs', required=True)
    ap.add_argument('--epsg', type=int, default=3826)
    ap.add_argument('--coh', type=float, default=0.5)
    a = ap.parse_args()
    r = compare_station(a.mintpy_dir, a.gnss_dir, a.ref, a.obs, a.epsg)
    refcorrected_velocity_map(a.mintpy_dir, a.gnss_dir, a.ref, a.epsg, coh_thresh=a.coh)
    cumulative_deformation(a.mintpy_dir, coh_thresh=a.coh, ref_lonlat=r['ref_lonlat'])
    print('完成')
