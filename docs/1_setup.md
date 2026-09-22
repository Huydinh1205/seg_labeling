# Guide 1: Environment setup

Once per machine. The short version lives in the repository `README.md`,
section 1. This is the same thing with more detail and the failure modes
spelled out.

---

## Requirements

| | Minimum | Recommended |
|---|---|---|
| RAM | 16 GB | 32 GB |
| GPU | not required | NVIDIA 8 GB+ or Apple M1+ |
| Disk | 50 GB free | 100 GB+ |
| OS | macOS 12+ / Ubuntu 20+ / Windows 10+ | |
| Python | 3.10+ | 3.11 |

---

## Step 1: Install Python 3.11

**macOS**

```bash
# Homebrew, if you do not have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.11
```

**Windows**

Download from https://www.python.org/downloads/, choose the 64-bit Windows
installer, and tick **Add Python to PATH** during installation.

**Or use conda**, which is the easier route on Windows because it brings its
own GDAL. Install Miniconda from
https://docs.conda.io/en/latest/miniconda.html.

---

## Step 2: Open a terminal

- macOS: Cmd+Space, type "Terminal", Enter.
- Windows: press Win, type "cmd" or "PowerShell", Enter. With conda installed,
  use the **Anaconda Prompt**.

---

## Step 3: Go to the project folder

```bash
# macOS / Linux
cd ~/Desktop/Home/Bunjilview/segmentation_model

# Windows
cd C:\Users\YourName\Desktop\Bunjilview\segmentation_model
```

---

## Step 4: Create the environment

**Conda (recommended, especially on Windows)**

```bash
conda create -n bunjilview python=3.11 -y
conda activate bunjilview
conda install -c conda-forge gdal rasterio -y
```

**venv**

```bash
python3.11 -m venv .venv

source .venv/bin/activate      # macOS / Linux
.venv\Scripts\activate         # Windows
```

Once activated you see `(bunjilview)` or `(.venv)` at the start of the prompt.
You have to activate it again in every new terminal.

---

## Step 5: Install the libraries

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

The first install takes 5 to 15 minutes depending on your connection.

**If GDAL fails on macOS**

```bash
brew install gdal
pip install gdal==$(gdal-config --version)
```

**If GDAL fails on Windows**

Use the conda route in step 4, which avoids the problem entirely. If you are
committed to venv, download a matching wheel from
https://github.com/cgohlke/geospatial-wheels/releases and install it before
`requirements.txt`:

```bash
pip install GDAL-3.8.4-cp311-cp311-win_amd64.whl
```

---

## Step 6: Check the install

```bash
python -c "
import torch, rasterio, segmentation_models_pytorch as smp
print('torch:', torch.__version__)
print('rasterio:', rasterio.__version__)
print('smp:', smp.__version__)
print('GPU:', torch.cuda.is_available() or torch.backends.mps.is_available())
print('all ok')
"
```

Expected:

```
torch: 2.x.x
rasterio: 1.x.x
smp: 0.x.x
GPU: True        (False is fine, it just runs slower)
all ok
```

Note the interpreter path, you need it for the QGIS side:

```bash
python -c "import sys; print(sys.executable)"
```

---

## Step 7: Download the DINOv2 weights

`features.py` downloads them on its first run, but it is worth confirming your
network allows it:

```bash
python -c "from transformers import AutoModel; AutoModel.from_pretrained('facebook/dinov2-base')"
```

About 350 MB, cached in `~/.cache/huggingface/` and downloaded only once.

---

## Step 8: QGIS and Geo-SAM

1. Install QGIS 3.28 or newer from https://qgis.org/download.
2. **Plugins > Manage and Install Plugins**, search `Geo SAM`, install it.
3. **Plugins > Geo-SAM > Settings > Dependencies**, install the missing
   dependencies, restart QGIS.
4. **Plugins > Geo-SAM > Settings > Model Management**, download a checkpoint.
5. Apply this project's patches, in the QGIS Python Console:

   ```python
   exec(open('/path/to/segmentation_model/scripts/patch_geosam_fields.py').read())
   ```

   Four `True` values means it worked. Skipping this makes polygons vanish when
   you press **S**: see the README, section 1.5, for why.

QGIS has its own bundled Python without torch or rasterio. That is expected.
QGIS drives the interface and shells the heavy work out to the environment you
just built.

---

## Next

- `2_geosam_qgis.md` for fast labelling in QGIS.
- `4_interactive_propagation.md` for drawing one example and propagating it.
- `3_pipeline.md` for the fully automated route.
- The repository `README.md` for the day-to-day workflow.
