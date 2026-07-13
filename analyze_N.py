#!/usr/bin/env python3
"""每個 Nearest-N 的 MintPy 反演 + TSB2/TSBS 時序 + 速度場 (子集現有 ifgramStack)。
用法: python3 analyze_N.py <N> [src_mintpy] [work_root]"""
import subprocess,shutil,os,sys,h5py,numpy as np
N=int(sys.argv[1])
SRC=sys.argv[2] if len(sys.argv)>2 else '/mnt/SARDB/snap2mintpy/TSBS_test2/mintpy'
ROOT=sys.argv[3] if len(sys.argv)>3 else '/mnt/SARDB/snap2mintpy/TSBS_test2/N_compare'
wd=f'{ROOT}/mintpy_N{N}';os.makedirs(f'{wd}/inputs',exist_ok=True)
def sh(cmd,**kw):
    print('$',' '.join(cmd));return subprocess.run(cmd,check=True,**kw)
shutil.copy(f'{SRC}/inputs/ifgramStack.h5',f'{wd}/inputs/ifgramStack.h5')
shutil.copy(f'{SRC}/inputs/geometryGeo.h5',f'{wd}/inputs/geometryGeo.h5')
# 1) 網路子集: 最多 N 鄰居
sh(['modify_network.py',f'{wd}/inputs/ifgramStack.h5','--max-conn-num',str(N)])
# 保留對數
with h5py.File(f'{wd}/inputs/ifgramStack.h5','r') as f:
    drop=f['dropIfgram'][:];print(f"[N={N}] 保留 {int(drop.sum())}/{drop.size} 對")
# 2) 反演 (weightFunc=no, minTempCoh=0.4)
sh(['ifgram_inversion.py',f'{wd}/inputs/ifgramStack.h5','-w','no','--md','coherence','--mt','0.4'],cwd=wd)
# 3) demErr
sh(['dem_error.py',f'{wd}/timeseries.h5','-g',f'{wd}/inputs/geometryGeo.h5','-o',f'{wd}/timeseries_demErr.h5'])
# 4) velocity
sh(['timeseries2velocity.py',f'{wd}/timeseries_demErr.h5','-o',f'{wd}/velocity.h5'])
print(f'[N={N}] 完成 → {wd}')
