#!/bin/bash
# 產出 deramp 版 velocity 當對照 (velocity_deramp.h5)。
# 用途: cfg 設 mintpy.deramp=no 後, velocity.h5 = 只 demErr、不 deramp (保留真實
#       區域形變)。本腳本另外對 timeseries_demErr.h5 做 linear deramp, 再算 velocity,
#       產出 velocity_deramp.h5 供比較 "deramp 前後差多少 (被移除的線性趨勢)"。
#
# 用法: ./make_velocity_deramp.sh <mintpy_dir>
#   e.g. ./make_velocity_deramp.sh TSBS_test2/mintpy
set -e
MP="${1:?用法: $0 <mintpy_dir>}"
cd "$MP"
[ -f timeseries_demErr.h5 ] || { echo "缺 timeseries_demErr.h5 (先跑 MintPy, deramp=no)"; exit 1; }
echo "[1/2] linear deramp on timeseries_demErr.h5 ..."
remove_ramp.py timeseries_demErr.h5 -s linear -m maskTempCoh.h5 -o timeseries_demErr_ramp.h5
echo "[2/2] velocity from deramped timeseries ..."
timeseries2velocity.py timeseries_demErr_ramp.h5 -o velocity_deramp.h5
echo "完成: $MP/velocity_deramp.h5 (deramp+demErr, 對照用)"
echo "      velocity.h5 = demErr-only (保留真實形變, 主要輸出)"
