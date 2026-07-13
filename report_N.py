#!/usr/bin/env python3
"""每個 Nearest-N 的三張圖: 基線網路圖 + 參考校正Vu速度場(標TSBS/TSB2) + TSB2時序(refTSBS)。
用法: python3 report_N.py <N> [out_dir]"""
import os,sys,h5py,numpy as np
import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
import matplotlib.dates as mdates
N=int(sys.argv[1])
ROOT='/mnt/SARDB/snap2mintpy/TSBS_test2/N_compare'
WD=f'{ROOT}/mintpy_N{N}'
OUT=sys.argv[2] if len(sys.argv)>2 else f'{ROOT}/figs'
os.makedirs(OUT,exist_ok=True)
TSBS_RC=(52,39);TSB2_RC=(32,39)
TSBS_LL=(120.9016,23.822);TSB2_LL=(120.9016,23.8293)
def d2np(s):return np.datetime64(f'{s[:4]}-{s[4:6]}-{s[6:8]}')
# ---- 1) 基線網路圖 ----
with h5py.File(f'{WD}/inputs/ifgramStack.h5','r') as f:
    d12=f['date'][:];bp=f['bperp'][:];drop=f['dropIfgram'][:]
dates=sorted(set(x.decode() for p in d12 for x in p))
# 逐期垂直基線: 用 MintPy timeseries.h5 內建 bperp (per-date, 相對參考期)
with h5py.File(f'{WD}/timeseries.h5','r') as f:
    _td=[x.decode() for x in f['date'][:]];_pb=f['bperp'][:]
bp_date={dt:float(_pb[i]) for i,dt in enumerate(_td)}
for dt in dates: bp_date.setdefault(dt,0.0)
fig,ax=plt.subplots(figsize=(11,5))
kept=0
for (a,b),pb,dr in zip(d12,bp,drop):
    a,b=a.decode(),b.decode()
    col='#1f6feb' if dr else '#d0d0d0';lw=1.3 if dr else 0.5;z=3 if dr else 1
    ax.plot([d2np(a),d2np(b)],[bp_date[a],bp_date[b]],'-',color=col,lw=lw,zorder=z,alpha=0.9 if dr else 0.4)
    if dr:kept+=1
ax.plot([d2np(d) for d in dates],[bp_date[d] for d in dates],'o',color='navy',ms=6,zorder=5)
ax.set_ylabel('垂直基線 Bperp (m)');ax.set_xlabel('日期')
ax.set_title(f'SBAS 基線網路圖 — Nearest N={N} (保留 {kept} 對, |Bperp|≤200m)')
ax.grid(alpha=0.3);ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
fig.autofmt_xdate();fig.tight_layout();fig.savefig(f'{OUT}/baseline_N{N}.png',dpi=130);plt.close(fig)
# ---- 讀速度/時序/幾何 ----
with h5py.File(f'{WD}/velocity.h5','r') as f:vel=f['velocity'][:]*1000;attrs=dict(f.attrs)
with h5py.File(f'{WD}/inputs/geometryGeo.h5','r') as f:inc=f['incidenceAngle'][:]
tcp=f'{WD}/temporalCoherence.h5'
tc=None
if os.path.exists(tcp):
    with h5py.File(tcp,'r') as f:tc=f['temporalCoherence'][:]
velm=np.where(tc>=0.4,vel,np.nan) if tc is not None else vel
vel_c=velm-velm[TSBS_RC]                    # ref TSBS
vu=vel_c/np.cos(np.radians(inc))            # LOS→Vu
X0=float(attrs['X_FIRST']);Y0=float(attrs['Y_FIRST']);dx=float(attrs['X_STEP']);dy=float(attrs['Y_STEP'])
W=int(attrs['WIDTH']);L=int(attrs['LENGTH'])
ext=[X0,X0+W*dx,Y0+L*dy,Y0]
# ---- 2) Vu 速度場 ----
# 測站標記慣例: 三角=GNSS Vu(上), 圓=該像素 InSAR Vu(下), 皆用同一 InSAR colorbar 填色
from matplotlib.colors import Normalize
GNSS_VU={'TSBS':0.0,'TSB2':-21.7}   # GNSS TSB2 Vu(InSAR期); TSBS=參考=0
fig,ax=plt.subplots(figsize=(8,7))
vmax=np.nanpercentile(np.abs(vu),97)
im=ax.imshow(vu,extent=ext,origin='upper',cmap='RdYlBu',vmin=-vmax,vmax=vmax,aspect='auto')
cmap=plt.get_cmap('RdYlBu');norm=Normalize(-vmax,vmax);off=abs(dy)*4.5
for nm,(lo,la),rc in [('TSBS',TSBS_LL,TSBS_RC),('TSB2',TSB2_LL,TSB2_RC)]:
    gvu=GNSS_VU[nm];ivu=float(vu[rc])
    ax.plot(lo,la+off,'^',mfc=cmap(norm(gvu)),mec='k',ms=15,mew=1.3,zorder=6)   # GNSS Vu
    ax.plot(lo,la-off,'o',mfc=cmap(norm(ivu)),mec='k',ms=14,mew=1.3,zorder=6)   # InSAR Vu
    ax.annotate(f'{nm}\n▲GNSS={gvu:+.1f}\n●InSAR={ivu:+.1f}',(lo,la),
                color='k',fontsize=8,ha='left',va='center',xytext=(11,0),textcoords='offset points',
                bbox=dict(boxstyle='round',fc='white',alpha=0.75,ec='gray'))
ax.set_title(f'參考校正 Vu 速度場 (ref TSBS) — N={N}\n▲=GNSS Vu ●=InSAR Vu (同colorbar填色); 紅=下沉 藍=上升 (mm/yr)')
ax.set_xlabel('經度');ax.set_ylabel('緯度')
plt.colorbar(im,ax=ax,shrink=0.8,label='Vu (mm/yr)')
fig.tight_layout();fig.savefig(f'{OUT}/velocity_Vu_N{N}.png',dpi=130);plt.close(fig)
# ---- 3) TSB2 時序 (ref TSBS) ----
with h5py.File(f'{WD}/timeseries_demErr.h5','r') as f:
    tsdates=[x.decode() for x in f['date'][:]];ts=f['timeseries'][:]*1000  # mm
tsb2=ts[:,TSB2_RC[0],TSB2_RC[1]];tsbs=ts[:,TSBS_RC[0],TSBS_RC[1]]
rel=(tsb2-tsbs);rel=rel-rel[0]      # ref TSBS, 起點0
inc_t=inc[TSB2_RC];vu_t=rel/np.cos(np.radians(inc_t))
dd=np.array([d2np(d) for d in tsdates])
fig,ax=plt.subplots(figsize=(11,5))
ax.axhline(0,color='gray',lw=0.8)
ax.plot(dd,vu_t,'o-',color='crimson',label='TSB2 Vu (LOS/cosθ, ref TSBS)')
# 線性擬合速度
t=(dd-dd[0]).astype('timedelta64[D]').astype(float)/365.25
p=np.polyfit(t,vu_t,1);ax.plot(dd,np.polyval(p,t),'--',color='navy',label=f'線性 {p[0]:+.1f} mm/yr')
ax.set_ylabel('垂直位移 Vu (mm)');ax.set_title(f'TSB2 時序 (參考 TSBS, 起點0) — N={N}')
ax.legend(fontsize=9);ax.grid(alpha=0.3);ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
fig.autofmt_xdate();fig.tight_layout();fig.savefig(f'{OUT}/timeseries_TSB2_N{N}.png',dpi=130);plt.close(fig)
print(f'[N={N}] 圖完成: baseline/velocity_Vu/timeseries_TSB2 → {OUT}; TSB2 Vu速度={p[0]:+.1f}mm/yr, 場值={vu[TSB2_RC]:+.1f}')
