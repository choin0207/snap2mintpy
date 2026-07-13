# snap2mintpy

*[繁體中文說明](README.zh-TW.md)*

A one-stop GUI for Sentinel-1 TOPS SBAS time-series analysis. It drives the full
chain from raw IW SLCs to a deformation velocity field:

**Sentinel-1 TOPS SLC → SNAP GPT interferometric processing → SNAPHU unwrapping → MintPy SBAS inversion**

Everything runs from a single Tkinter GUI. Heavy interferogram processing can be
fanned out across a cluster of workstations over SSH, sharing one project
directory on common storage. Command-line tools cover cluster monitoring,
GNSS cross-validation, corner-reflector analysis, and a HyP3 cloud alternative
to local SNAP processing.

![Velocity field example](docs/figs/velocity_example.png)

---

## Features

### GUI (`snap2mintpy_gui.py`) — five tabs

1. **Input & Pairing** — Configure project, SNAP install, SLC/DEM paths, and AOI.
   Download SLCs from ASF (reads `~/.netrc`). Design the SBAS network with
   Nearest-N pairing or a custom day span, with a perpendicular-baseline filter.
   Preview the baseline network and manually select IW sub-swaths.
2. **Run SNAP + Cluster** — Execute the SNAP GPT pipeline
   (split → interferogram → SNAPHU → geocode). Switch between single-machine and
   cluster mode, watch a live progress bar and per-host log tabs, manage an SSD
   swapfile, and dispatch work with work-stealing.
3. **MintPy** — Edit `smallbaselineApp.cfg`, run `smallbaselineApp.py` with one
   click through a built-in terminal, and export results to GeoTIFF.
4. **GNSS Comparison** — Cross-validate InSAR against GNSS stations (vertical and
   LOS projections).
5. **Cumulative Deformation** — Render cumulative deformation maps as a grid of
   panels plus an animated GIF.

![GUI baseline selection](docs/figs/gui_baseline_select.png)

### Command-line tools

- **`cluster_progress.py`** — Standalone cluster progress monitor. Reads
  `dist_config.json` and the per-worker status files, and prints an overall
  progress bar, each host's current pair/step, throughput, and ETA. Runs on any
  node, independent of the GUI.
- **`hyp3_burst_to_mintpy.py`** — Alternative pipeline that runs Sentinel-1 burst
  InSAR on ASF HyP3 in the cloud: find burst SLCs via `asf_search`, pair them,
  submit interferograms to HyP3, multilook, and convert to MintPy format.
- **`analyze_N.py` / `report_N.py`** — Nearest-N network comparison experiment.
  `analyze_N.py` subsets an existing `ifgramStack.h5` with
  `modify_network.py --max-conn-num N` and re-runs inversion, DEM-error
  correction, and velocity estimation. `report_N.py` plots the baseline network,
  the reference-corrected vertical velocity, and per-point time series.
- **`gnss_compare.py`** — Core GNSS↔InSAR comparison logic (also driven by the
  GUI GNSS and cumulative-deformation tabs). Reads GNSS spreadsheets and a MintPy
  `timeseries.h5`, comparing vertical displacement and LOS projection.
- **`cr_report.py`** — Corner-reflector benefit report generator, with
  height-aware geolocation and SLC co-registration.
- **`view_basemap.py`** — Visualize `velocity.h5` / `timeseries.h5` over a
  basemap (satellite / OSM / topo) with the interferogram network overlaid.
- **`analyze_failures.py`** — Scan worker logs to summarize failed pairs and their
  causes.
- **`make_velocity_deramp.sh`** — Shell wrapper that produces a deramped velocity
  product via MintPy CLI.

---

## Processing pipeline

The SNAP stage runs one GPT graph per step; graph XML templates live in
`snap2stamps/graphs/`.

1. **Split** — TOPSAR-Split + Apply-Orbit on each (date, IW), reused across pairs.
2. **Interferogram + Deburst** — Coregistration, interferogram formation, and
   deburst.
3. **Goldstein filter + Multilook** — Phase filtering and multilooking.
4. **Smart ML** — Gap-fill NaN gaps left by multilooking
   (`scipy.interpolate.griddata` + Gaussian smoothing), with a three-panel QC PNG.
5. **SNAPHU unwrapping** — Three steps: export → run `snaphu` → import.
6. **Terrain correction** — Geocode the wrapped and unwrapped phase to
   `*_tc.dim` / GeoTIFF.
7. **MintPy format conversion** — Convert geocoded products into a MintPy input
   stack.
8. **`smallbaselineApp.py`** — SBAS inversion: load data → reference point →
   network inversion → velocity.

Optional follow-on analysis: DEM-error correction, deramped velocity, GNSS
comparison, cumulative-deformation maps, and the Nearest-N network comparison.

![Baseline network example](docs/figs/baseline_network_example.png)

---

## Requirements

### External software (not Python packages)

| Software | Purpose |
|---|---|
| **ESA SNAP** (`gpt`) | GPT graph engine for split / coregistration / interferogram / filtering / terrain correction |
| **SNAPHU** | Phase unwrapping (Stanford) |
| **MintPy** (`smallbaselineApp.py` and friends) | SBAS time-series inversion (installed in a conda env) |
| **GDAL** (`osgeo.gdal` Python binding) | DEM download and coordinate transforms |
| **python3-tk** | Tkinter GUI toolkit (system package) |
| **ssh** client | Cluster dispatch, monitoring, and cleanup |

The tools auto-detect `gpt` and `snaphu` at common install locations, and detect
a MintPy conda env under `~/miniconda3/envs/*`. Custom paths can be set in the GUI.

### Python packages

**Required:** `asf_search` `h5py` `numpy` `scipy` `matplotlib` `pandas`
`openpyxl` `rasterio`

**Optional:** `contextily` `pyproj` (basemap overlay in `view_basemap.py`),
`hyp3_sdk` (only for the HyP3 alternative pipeline).

`scipy`, `rasterio`, and `osgeo` degrade gracefully when missing (gap-fill is
skipped, GeoTIFF export prompts to install, DEM steps are logged and skipped).

---

## Installation

### 1. Miniconda (Python environment manager)

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3
echo 'export PATH="$HOME/miniconda3/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
conda --version
```

### 2. MintPy environment

```bash
conda create -n mintpy python=3.10 -y
conda activate mintpy
conda install -c conda-forge mintpy -y
smallbaselineApp.py --version
```

### 3. ESA SNAP

Download the Linux installer from
<https://step.esa.int/main/download/snap-download/>, then:

```bash
bash esa-snap_sentinel_unix_9_0_0.sh          # default path: ~/esa-snap
~/esa-snap/bin/gpt --version                   # verify gpt
~/esa-snap/bin/snap --nosplash --nogui --modules --update-all   # update S1 plugins
```

The GUI probes `~/esa-snap/bin/gpt`, `/opt/esa-snap/bin/gpt`, `/opt/snap/bin/gpt`,
and `$PATH`.

### 4. SNAPHU

Build from the Stanford source, or install via package manager. The GUI probes
`~/tools/snaphu/bin/snaphu`, `/usr/local/bin/snaphu`, `/usr/bin/snaphu`, and
`/opt/snaphu/bin/snaphu`.

### 5. This repository

```bash
git clone https://github.com/YOUR_USERNAME/snap2mintpy.git
cd snap2mintpy
sudo apt install -y python3-tk        # if Tkinter is missing
python3 snap2mintpy_gui.py
```

Required Python packages install into your base or MintPy environment. If a
package is missing, the GUI attempts `pip install --user` on first use.

---

## Credentials setup

This tool **only reads** credentials you place in standard files. It never stores
or transmits your username or password anywhere else.

### NASA Earthdata (`~/.netrc`) — for ASF download and HyP3

Register a free account at <https://urs.earthdata.nasa.gov>, then create
`~/.netrc`:

```
machine urs.earthdata.nasa.gov
    login YOUR_USERNAME
    password YOUR_PASSWORD
```

```bash
chmod 600 ~/.netrc      # required, or the file is ignored
```

ASF SLC download and the HyP3 pipeline both read this file. The GUI password
field, if used, lives only for the current session.

### SSH key-based login (cluster) — required for cluster mode

Every SSH call uses `-o BatchMode=yes`, so **password prompts are disabled**: if
key-based login is not set up, cluster commands fail immediately. Set it up once:

```bash
ssh-keygen -t ed25519 -C "insar-cluster"
ssh-copy-id -i ~/.ssh/id_ed25519 your_user@192.168.1.101
ssh-copy-id -i ~/.ssh/id_ed25519 your_user@192.168.1.102
```

Then add host aliases to `~/.ssh/config`:

```
Host worker01
    HostName 192.168.1.101
    User your_user
    IdentityFile ~/.ssh/id_ed25519

Host worker02
    HostName 192.168.1.102
    User your_user
    IdentityFile ~/.ssh/id_ed25519
```

The GUI reads `~/.ssh/config` and lists every `Host` alias as a checkbox
(the local machine is always listed first).

### sudo (optional)

`SNAP2MINTPY_SUDO_PASS` is an optional environment variable used only for a pip
fallback and for swapfile management. Leave it unset to skip both — the GUI will
prefer `pip install --user` and let you manage swap manually.

---

## Cluster setup

Cluster mode has the machine running the GUI (the master) launch
`snap2mintpy_worker.py` on each worker over SSH. Each worker imports the same
`snap2mintpy_gui.py` and works on the same project directory, so the setup must
be uniform across nodes.

Per-node requirements:

1. **Shared storage** — The project directory (e.g. under a shared NFS/CIFS mount)
   is accessible at the **same path** on every node.
2. **Identical repo path** — The `snap2mintpy` checkout lives at the same path on
   every node (the worker imports it directly).
3. **Same Python environment** — Every node can `import snap2mintpy_gui` and run
   MintPy.
4. **SNAP + SNAPHU installed** — `gpt` and `snaphu` are executable on each node,
   via `$PATH` or a `~/FastISCE.config` sourced by the remote command.
5. **SSH key login + `~/.ssh/config` alias** for each host (see above).
6. **JVM memory tuning** — Adjust `-Xmx` and `tileCache` in `gpt.vmoptions` to fit
   each machine's RAM. The worker auto-computes a safe `tileCache` from
   `/proc/meminfo` (about 12% of RAM, floor 4 GB, ceiling 24 GB) and overrides the
   shared config value.
7. **(Optional) swapfile** to guard against OOM on large frames:

   ```bash
   sudo fallocate -l 100G ~/snap_swap.img
   sudo chmod 600 ~/snap_swap.img
   sudo mkswap ~/snap_swap.img
   sudo swapon ~/snap_swap.img
   ```

The master splits pairs across hosts and supports **work-stealing** (idle hosts
claim remaining pairs). Only the first host builds the DEM, to avoid a race. Each
worker writes an atomic `logs/worker_<label>.json` status file that the master —
or `cluster_progress.py` on any node — polls to track progress. When the GUI
closes, it cleans up leftover remote processes.

---

## Quick start

Single machine, one small AOI:

```bash
conda activate mintpy      # or your environment with the required packages
python3 snap2mintpy_gui.py
```

1. **Tab 1 (Input & Pairing)** — Set the project directory, SNAP install, SLC/DEM
   paths, and AOI. Download SLCs if needed, choose a Nearest-N value, and preview
   the baseline network.
2. **Tab 2 (Run SNAP)** — Keep cluster mode off. Click Start and watch the pipeline
   run split → interferogram → SNAPHU → geocode.
3. **Tab 3 (MintPy)** — Review `smallbaselineApp.cfg`, run `smallbaselineApp.py`,
   and export the velocity field to GeoTIFF.

![Time-series example](docs/figs/timeseries_example.png)

---

## License & Acknowledgements

This repository is licensed under
**[CC BY-NC-SA 4.0](LICENSE.md)** (Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International).

The SNAP GPT graph templates in `snap2stamps/graphs/` are derived from
[snap2stamps](https://github.com/mdelgadoblasco/snap2stamps) by Jose Manuel
Delgado Blasco et al., also released under CC BY-NC-SA 4.0.

With thanks to:

- **ESA SNAP** — Sentinel-1 TOPS interferometric processing
- **SNAPHU** — statistical-cost phase unwrapping (Stanford)
- **MintPy** — SBAS time-series analysis (insarlab)
- **ASF** — Sentinel-1 data access and HyP3 on-demand processing
