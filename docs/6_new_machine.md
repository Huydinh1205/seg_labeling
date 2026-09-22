# Guide 6: Installing on another machine

Run through this once on the new machine. After that, every working session is
one command.

`<REPO>` is the path to the `segmentation_model` folder on that machine.
`<site>` is the site name, which matches the ortho file name.

For Windows specifically, `7_windows_setup.md` walks through the same thing
with Windows paths throughout.

---

## PART 1 - Install (once per machine)

### 1.1 Copy the repo across

Copy the whole `segmentation_model` folder. You do **not** need to copy the
heavy folders, they are regenerated on the new machine:

```
data/tiles/      data/features/     data/clusters/     data/pseudo/
data/labels/<site>/_patch_*.npy    data/labels/<site>/sam_candidates/
__pycache__/     *.pyc
```

What you do need: `src/`, `scripts/`, `qgis/`, `docs/`, `config.yaml`,
`requirements.txt`, `README.md`.

If you are cloning from git, `.gitignore` already excludes all of the above.

### 1.2 QGIS

Install QGIS 3.28 or newer: https://qgis.org/download

### 1.3 The Geo-SAM plugin

In QGIS: **Plugins > Manage and Install Plugins** -> tab All -> search
`Geo SAM` -> Install.

Then **Plugins > Geo-SAM > Settings**:

- Tab **Dependencies** -> Install Missing Dependencies -> **restart QGIS**
- Tab **Model Management** -> download a model (for example `SAM2.1 Small`)

### 1.4 The Python environment for the compute side

QGIS has its own Python, but without torch or rasterio, so the heavy work runs
in a separate environment. Conda is the easiest:

```bash
conda create -n bunjilview python=3.11 -y
conda activate bunjilview
pip install -r requirements.txt
```

Note the interpreter path, you need it shortly:

```bash
which python        # macOS / Linux
where python        # Windows
```

For example:

- macOS: `/opt/homebrew/Caskroom/miniconda/base/envs/bunjilview/bin/python`
- Windows: `C:\Users\<you>\miniconda3\envs\bunjilview\python.exe`

> A plain venv usually works too. All that matters is that `torch` and
> `rasterio` install cleanly.

---

## PART 2 - Prepare the site (once per ortho)

Open a terminal, `cd` into `<REPO>`, `conda activate bunjilview`.

### 2.1 Put the ortho in place

```
data/raw/<site>.tif
```

### 2.2 Run these four commands

```bash
python src/tiling.py    --config config.yaml --ortho data/raw/<site>.tif
python src/features.py  --config config.yaml --site <site>
python src/propagate.py --config config.yaml --site <site> --build-feature-cache
python src/propagate.py --config config.yaml --site <site> --build-sam-cache
```

`features.py` is the slow one. If it drags, force the device: `--device cuda`
(NVIDIA), `--device mps` (Apple Silicon), `--device cpu`.

The two `propagate.py` commands also write `data/labels/<site>/site.json`,
which records the interpreter path. The Processing button reads it.

### 2.3 Create the species layers

```bash
python scripts/init_labels_gpkg.py --config config.yaml --site <site>
```

Creates `data/labels/<site>/<site>.gpkg` with **one layer per species** from
`config.yaml`, each with both field sets. **Do not create the layers by hand in
QGIS**: they end up missing fields and Geo-SAM rejects them.

---

## PART 3 - Wire it into QGIS (once per machine)

Open QGIS, drag `data/raw/<site>.tif` in, then open
**Plugins > Python Console**.

### 3.1 Declare the paths for this session

Paste this, **editing the two marked lines**:

```python
import qgis.utils
qgis.utils.BV_REPO = r'C:\path\to\segmentation_model'   # edit
qgis.utils.BV_SITE = 'site_name'                        # edit
```

The `r` before the quote is required on Windows, so backslashes are not read as
escapes.

If `site.json` does not exist yet, or you want to be explicit:

```python
qgis.utils.BV_PYTHON = r'C:\Users\<you>\miniconda3\envs\bunjilview\python.exe'
```

### 3.2 Register the Processing script

```python
from processing.core.ProcessingConfig import ProcessingConfig
from qgis.core import QgsApplication
import os
folder = os.path.join(qgis.utils.BV_REPO, 'qgis')
cur = ProcessingConfig.getSetting('SCRIPTS_FOLDERS') or ''
if folder not in cur:
    ProcessingConfig.setSettingValue('SCRIPTS_FOLDERS', (cur + ';' + folder).strip(';'))
QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()
```

**Do not use "Add Script to Toolbox".** It copies the file into the QGIS
profile, and that copy shadows the repo version without following your edits.
The giveaway is a free-text Species box instead of a layer picker.

---

## PART 4 - Every QGIS session (one command)

Once part 3 is done, opening QGIS needs only:

```python
import qgis.utils
qgis.utils.BV_REPO = r'C:\path\to\segmentation_model'
qgis.utils.BV_SITE = 'site_name'
exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
```

That cleans the session, applies the four plugin patches, **verifies** them,
loads the ortho and the species layers each in its own colour, binds Geo-SAM,
and self-tests.

Expected: `RESULT: 6/6 PASS`.

If any patch is missing it **stops and says so** rather than continuing. The
four patches and the reasons for them are in `5_runbook.md`, part B2.

> The patches live in the QGIS profile, so they are LOST when the Geo-SAM
> plugin is reinstalled. Run the same line again.

---

## PART 5 - The labelling loop

1. **Click the species layer** in the Layers panel. Geo-SAM follows, enables
   edit mode, and saves any unsaved work on the previous layer.
2. Press **`FG`** and click in the middle of the patch. More **`FG`** to
   extend, **`BG`** to cut back, **`Z`** to undo, **`C`** to start over.
3. Press **`S`** to save. **Nothing to type**, the layer name is the species.
4. **Ctrl+S** to write it to disk.
5. Processing Toolbox -> **Scripts > Bunjilview > Propagate species from seed**
   -> pick **that same layer** -> Run.
6. Delete the wrong polygons, Ctrl+S, run again if needed. For the next
   species, go back to step 1.

Details and the traps: `5_runbook.md` and `4_interactive_propagation.md`.

---

## PART 6 - Finishing: export labels and train

```bash
python src/propagate.py --config config.yaml --site <site> --rasterize
python src/tiling.py --config config.yaml --ortho data/raw/<site>.tif \
    --mask data/pseudo/<site>/<site>_labels.tif
python src/train.py --config config.yaml --site <site>
```

---

## Windows differences

| | macOS / Linux | Windows |
|---|---|---|
| Paths in the Python Console | `'/Users/...'` | `r'C:\...'` (keep the `r`) |
| Interpreter | `.../envs/bunjilview/bin/python` | `...\envs\bunjilview\python.exe` |
| QGIS profile folder | `~/Library/Application Support/QGIS/...` | `%APPDATA%\QGIS\QGIS3\profiles\default` |

The scripts **ask QGIS** for the profile folder, so you never have to type it,
and it works with a profile other than `default`.

---

## When something goes wrong

| Symptom | What to do |
|---|---|
| "The fields of this vector do not match the SAM feature fields" | Re-run `scripts/init_labels_gpkg.py`. The layer is missing Geo-SAM fields. |
| "Cannot create field group_ulid ... already exists" | Patch 3 is not applied. Re-run part 4. |
| The polygon disappears after pressing S | Patch 1 is not applied. Re-run part 4. |
| The polygon lands in the wrong layer | Patch 4 is not applied. Re-run part 4. |
| The layer is read-only or corrupt | `exec(open(BV_REPO + '/scripts/repair_labels_gpkg.py').read())` |
| You want to discard everything and start over | `exec(open(BV_REPO + '/scripts/reset_site_labels.py').read())` |
| General suspicion that something is off | `exec(open(BV_REPO + '/scripts/test_pipeline.py').read())` |

One thing worth knowing: the red banner in QGIS **does not clear itself**, it
stays until you press the `X`. It is easy to mistake an old error for a new
one. Dismiss it, try again, and only treat it as real if it comes back.
