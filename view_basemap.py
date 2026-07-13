#!/usr/bin/env python3
"""view_basemap.py — Plot MintPy velocity/timeseries HDF5 on web map tiles.

Usage
-----
    python3 view_basemap.py velocity.h5
    python3 view_basemap.py velocity.h5 --basemap satellite
    python3 view_basemap.py velocity.h5 --basemap osm --vmax 30
    python3 view_basemap.py velocity.h5 --basemap topo --mask maskTempCoh.h5
    python3 view_basemap.py velocity.h5 --save output.png

Basemap options (no API key required)
--------------------------------------
    satellite   Esri WorldImagery (衛星影像)
    google      Google Satellite (Google 衛星影像)
    osm         OpenStreetMap Mapnik (預設)
    topo        OpenTopoMap
    cartodb     CartoDB Positron (淺色簡潔)
    esri_topo   Esri WorldTopoMap

Coherence masking
-----------------
    --coh-mask avgSpatialCoh.h5 --coh-thresh 0.4
    → mask pixels where spatial coherence < 0.4 (adjustable).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── 修正 PROJ 資料庫路徑 (必須在 import pyproj 之前) ─────────────────────
# 全域環境(.bashrc/FastISCE.config)常把 PROJ_DATA/PROJ_LIB 設成別的 conda env
# (如 isce2)的 proj 目錄; 但本腳本跑在 FastISCE2 env → pyproj 讀到不相容/缺失
# 的 proj.db → CRSError "no database context specified"。改指向「當前 env」的
# share/proj (sys.prefix)。
_PROJ_DIR = os.path.join(sys.prefix, 'share', 'proj')
if os.path.exists(os.path.join(_PROJ_DIR, 'proj.db')):
    os.environ['PROJ_DATA'] = _PROJ_DIR
    os.environ['PROJ_LIB'] = _PROJ_DIR

import h5py
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from mpl_toolkits.axes_grid1 import make_axes_locatable

try:
    import contextily as ctx
    import pyproj
    from pyproj import Transformer
    # 雙保險: 強制 pyproj 用當前 env 的 proj.db (即使它已快取錯誤路徑)
    if os.path.exists(os.path.join(_PROJ_DIR, 'proj.db')):
        try:
            pyproj.datadir.set_data_dir(_PROJ_DIR)
        except Exception:
            pass
    _HAS_CTX = True
except ImportError:
    _HAS_CTX = False

# ── tile provider map ──────────────────────────────────────────────────────
# Google Satellite is served as a raw XYZ tile URL (not a contextily provider
# object). contextily.add_basemap() accepts a URL template string directly.
_GOOGLE_SAT_URL = 'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}'

_PROVIDERS = {
    'satellite': 'Esri.WorldImagery',
    'google':    _GOOGLE_SAT_URL,
    'osm':       'OpenStreetMap.Mapnik',
    'topo':      'OpenTopoMap',
    'cartodb':   'CartoDB.Positron',
    'esri_topo': 'Esri.WorldTopoMap',
}

# Human-readable names for figure titles (URLs are ugly).
_PROVIDER_LABELS = {
    'satellite': 'Esri WorldImagery',
    'google':    'Google Satellite',
    'osm':       'OpenStreetMap',
    'topo':      'OpenTopoMap',
    'cartodb':   'CartoDB Positron',
    'esri_topo': 'Esri WorldTopoMap',
}


def _get_provider(name: str):
    val = _PROVIDERS.get(name, name)
    # Raw XYZ tile URL (e.g. Google Satellite) → pass straight to contextily.
    if isinstance(val, str) and val.startswith('http'):
        return val
    parts = val.split('.')
    p = ctx.providers
    for part in parts:
        p = p[part]
    return p


def read_h5_geo(h5_file: str, dataset: str = 'velocity') -> tuple:
    """Return (data_mm, atr, lon_arr, lat_arr).

    For velocity files: converts m/yr → mm/yr.
    For timeseries files: returns the last epoch in mm.
    """
    with h5py.File(h5_file, 'r') as f:
        keys = list(f.keys())
        if dataset not in keys:
            dataset = keys[0]
        data = f[dataset][:]
        atr  = {k: v for k, v in f.attrs.items()}

    # Squeeze timeseries (ndate, ny, nx) → last epoch
    if data.ndim == 3:
        data = data[-1]

    x0 = float(atr['X_FIRST'])
    y0 = float(atr['Y_FIRST'])
    dx = float(atr['X_STEP'])
    dy = float(atr['Y_STEP'])
    ny, nx = data.shape

    lon_arr = x0 + np.arange(nx) * dx
    lat_arr = y0 + np.arange(ny) * dy

    # Convert to mm
    unit = str(atr.get('UNIT', '')).lower()
    if 'm/year' in unit or 'm/yr' in unit or unit == 'm/year':
        data = data * 1000.0   # m/yr → mm/yr
    elif unit in ('m', 'meter'):
        data = data * 1000.0   # m → mm

    data[data == 0] = np.nan
    return data, atr, lon_arr, lat_arr


def apply_mask(data: np.ndarray, mask_file: str) -> np.ndarray:
    with h5py.File(mask_file, 'r') as f:
        mask = f['mask'][:]
    data = data.copy()
    data[mask == 0] = np.nan
    return data


def apply_coh_mask(data: np.ndarray, coh_file: str, thresh: float) -> np.ndarray:
    """Mask pixels whose spatial coherence < thresh.

    coh_file: avgSpatialCoh.h5 (dataset 'coherence', 0–1). Equivalent to
    MintPy 'view.py velocity.h5 -m avgSpatialCoh.h5 --mask-vmin <thresh>'.
    """
    with h5py.File(coh_file, 'r') as f:
        key = 'coherence' if 'coherence' in f else list(f.keys())[0]
        coh = f[key][:]
    if coh.shape != data.shape:
        raise ValueError(
            f'coherence shape {coh.shape} != data shape {data.shape}')
    data = data.copy()
    data[coh < thresh] = np.nan
    return data


def wgs84_to_webmercator(lon_min, lat_min, lon_max, lat_max):
    t = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
    west, south = t.transform(lon_min, lat_min)
    east, north = t.transform(lon_max, lat_max)
    return west, south, east, north


def _make_format_coord_latlon(data, lon_arr, lat_arr):
    """Return format_coord for plain lat/lon axes."""
    dx = lon_arr[1] - lon_arr[0] if len(lon_arr) > 1 else 1
    dy = lat_arr[1] - lat_arr[0] if len(lat_arr) > 1 else 1

    def _fmt(x, y):
        col = int(round((x - lon_arr[0]) / dx))
        row = int(round((y - lat_arr[0]) / dy))
        ny, nx = data.shape
        if 0 <= row < ny and 0 <= col < nx:
            val = data[row, col]
            v_str = f'{val:+.2f} mm/yr' if np.isfinite(val) else 'masked'
        else:
            v_str = '—'
        return f'Lon={x:.6f}°  Lat={y:.6f}°  |  {v_str}'
    return _fmt


def _make_format_coord_mercator(data, lon_arr, lat_arr):
    """Return format_coord for Web Mercator axes (EPSG:3857 → WGS84)."""
    if not _HAS_CTX:
        return None
    t_inv = Transformer.from_crs('EPSG:3857', 'EPSG:4326', always_xy=True)
    dx = lon_arr[1] - lon_arr[0] if len(lon_arr) > 1 else 1
    dy = lat_arr[1] - lat_arr[0] if len(lat_arr) > 1 else 1

    def _fmt(x, y):
        lon, lat = t_inv.transform(x, y)
        col = int(round((lon - lon_arr[0]) / dx))
        row = int(round((lat - lat_arr[0]) / dy))
        ny, nx = data.shape
        if 0 <= row < ny and 0 <= col < nx:
            val = data[row, col]
            v_str = f'{val:+.2f} mm/yr' if np.isfinite(val) else 'masked'
        else:
            v_str = '—'
        return f'Lon={lon:.6f}°  Lat={lat:.6f}°  |  {v_str}'
    return _fmt


def plot(h5_file: str,
         basemap:    str  = 'satellite',
         dataset:    str  = 'velocity',
         mask_file:  str  = '',
         coh_mask:   str  = '',
         coh_thresh: float | None = None,
         vmax:       float | None = None,
         alpha:      float = 0.65,
         cmap:       str  = 'RdYlBu_r',
         save:       str  = '',
         title:      str  = ''):

    data, atr, lon_arr, lat_arr = read_h5_geo(h5_file, dataset)

    if mask_file and Path(mask_file).exists():
        data = apply_mask(data, mask_file)
    if coh_mask and coh_thresh is not None and Path(coh_mask).exists():
        data = apply_coh_mask(data, coh_mask, coh_thresh)

    # Auto vmax
    if vmax is None:
        finite = data[np.isfinite(data)]
        vmax   = float(np.percentile(np.abs(finite), 98)) if len(finite) else 30.0
    vmin = -vmax

    lon_min, lon_max = lon_arr[0],  lon_arr[-1] + float(atr['X_STEP'])
    lat_max, lat_min = lat_arr[0],  lat_arr[-1] + float(atr['Y_STEP'])

    if not _HAS_CTX:
        # Fallback: plain lat/lon plot
        fig, ax = plt.subplots(figsize=(9, 7))
        im = ax.imshow(data, extent=[lon_min, lon_max, lat_min, lat_max],
                       origin='upper', cmap=cmap, vmin=vmin, vmax=vmax,
                       aspect='equal')
        ax.set_xlabel('Longitude'); ax.set_ylabel('Latitude')
        ax.format_coord = _make_format_coord_latlon(data, lon_arr, lat_arr)
        _plot_refpoint(ax, atr, mercator=False)
        _add_colorbar(fig, ax, im, atr)
        ax.set_title(title or Path(h5_file).name)
        _show_or_save(fig, save)
        return

    # ── basemap branch ─────────────────────────────────────────────────────
    west, south, east, north = wgs84_to_webmercator(lon_min, lat_min, lon_max, lat_max)

    fig, ax = plt.subplots(figsize=(10, 8))

    # Plot velocity in Web Mercator extent
    im = ax.imshow(
        np.flipud(data),
        extent=[west, east, south, north],
        origin='lower',
        cmap=cmap,
        vmin=vmin, vmax=vmax,
        alpha=alpha,
        zorder=2,
        interpolation='nearest',
    )

    # Add web map tiles
    try:
        provider = _get_provider(basemap)
        ctx.add_basemap(ax, source=provider, zoom='auto', reset_extent=False)
    except Exception as exc:
        ax.set_facecolor('#e0e0e0')
        ax.text(0.5, 0.01, f'[basemap load failed: {exc}]',
                transform=ax.transAxes, ha='center', fontsize=7, color='red')

    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    ax.set_axis_off()

    # ── mouse coordinate display (Web Mercator → lat/lon + velocity) ──────
    ax.format_coord = _make_format_coord_mercator(data, lon_arr, lat_arr)

    # ── reference point marker ────────────────────────────────────────────
    _plot_refpoint(ax, atr, mercator=True)

    _add_colorbar(fig, ax, im, atr)

    pname = _PROVIDER_LABELS.get(basemap, basemap)
    ax.set_title(title or f'{Path(h5_file).name}  ({pname})', fontsize=11)

    plt.tight_layout()
    _show_or_save(fig, save)


def _plot_refpoint(ax, atr, mercator: bool = True):
    """Mark the MintPy reference point on the axes."""
    ref_lat = atr.get('REF_LAT')
    ref_lon = atr.get('REF_LON')
    if ref_lat is None or ref_lon is None:
        return
    try:
        rlat, rlon = float(ref_lat), float(ref_lon)
    except (TypeError, ValueError):
        return

    if mercator and _HAS_CTX:
        t = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
        rx, ry = t.transform(rlon, rlat)
    else:
        rx, ry = rlon, rlat

    ax.plot(rx, ry,
            marker='*', markersize=14, color='white',
            markeredgecolor='black', markeredgewidth=0.8,
            zorder=5, label=f'Ref  ({rlat:.4f}°N, {rlon:.4f}°E)')
    ax.annotate(f'REF\n{rlat:.4f}°N\n{rlon:.4f}°E',
                xy=(rx, ry), xytext=(8, 8), textcoords='offset points',
                fontsize=7, color='white',
                bbox=dict(boxstyle='round,pad=0.2', fc='black', alpha=0.55),
                zorder=6)


def _add_colorbar(fig, ax, im, atr):
    unit = str(atr.get('UNIT', 'm/year')).lower()
    label = 'LOS velocity (mm/yr)' if 'year' in unit else f'LOS displacement (mm)'
    divider = make_axes_locatable(ax)
    cax = divider.append_axes('right', size='3%', pad=0.05)
    fig.colorbar(im, cax=cax, label=label)


def _show_or_save(fig, save: str):
    if save:
        fig.savefig(save, dpi=150, bbox_inches='tight')
        print(f'[saved] {save}')
    else:
        plt.show()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('h5_file', help='MintPy HDF5 file (velocity.h5 etc.)')
    ap.add_argument('--basemap', default='satellite',
                    choices=list(_PROVIDERS.keys()),
                    help='Basemap type (default: satellite = Esri WorldImagery)')
    ap.add_argument('--dataset', default='velocity',
                    help='HDF5 dataset name (default: velocity)')
    ap.add_argument('--mask', default='', dest='mask_file',
                    help='Binary mask HDF5 (e.g. maskTempCoh.h5)')
    ap.add_argument('--coh-mask', default='', dest='coh_mask',
                    help='Spatial coherence HDF5 (e.g. avgSpatialCoh.h5) for '
                         'threshold masking')
    ap.add_argument('--coh-thresh', type=float, default=None, dest='coh_thresh',
                    help='Mask pixels where coherence < this value (e.g. 0.4)')
    ap.add_argument('--vmax', type=float, default=None,
                    help='Colour scale ±vmax mm/yr (auto if omitted)')
    ap.add_argument('--alpha', type=float, default=0.65,
                    help='Overlay transparency 0–1 (default 0.65)')
    ap.add_argument('--cmap', default='RdYlBu_r',
                    help='Matplotlib colormap (default RdYlBu_r)')
    ap.add_argument('--save', default='',
                    help='Save figure to file instead of displaying')
    ap.add_argument('--title', default='', help='Figure title')
    args = ap.parse_args()

    if not Path(args.h5_file).exists():
        sys.exit(f'File not found: {args.h5_file}')

    plot(args.h5_file,
         basemap=args.basemap,
         dataset=args.dataset,
         mask_file=args.mask_file,
         coh_mask=args.coh_mask,
         coh_thresh=args.coh_thresh,
         vmax=args.vmax,
         alpha=args.alpha,
         cmap=args.cmap,
         save=args.save,
         title=args.title)


if __name__ == '__main__':
    main()
