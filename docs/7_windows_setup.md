# Guide 7: Install and run on Windows

Start to finish. Follow the order, do not skip a step.

Conventions:

- `<REPO>` is the path to the `segmentation_model` folder on the Windows
  machine, for example `D:\Bunjilview\segmentation_model`
- `<site>` is the site name, which **matches the ortho file name** (for example
  `walpolla` -> `walpolla.tif`)

---

# PART A - INSTALL (once per machine)

## A1. Copy the repo to the Windows machine

Copy the whole `segmentation_model` folder. Avoid putting it inside OneDrive or
any auto-synced folder. See the warning at the end.

**You need:** `src\`, `scripts\`, `qgis\`, `docs\`, `config.yaml`,
`requirements.txt`, `README.md`

**You do not need** (these are regenerated, and they are large):

```
data\tiles\     data\features\     data\clusters\     data\pseudo\
data\labels\<site>\_patch_*.npy    data\labels\<site>\sam_candidates\
__pycache__\
```

Cloning from git already excludes all of those, via `.gitignore`.

## A2. Install QGIS

Download the **Long Term Version** from https://qgis.org/download, run the
`.msi`, click Next through to the end.

## A3. Install the Geo-SAM plugin

In QGIS:

1. **Plugins > Manage and Install Plugins**
2. Tab **All**, search for `Geo SAM`
3. Select **Geo SAM** -> **Install Plugin**

Then **Plugins > Geo-SAM > Settings**:

4. Tab **Dependencies** -> **Install Missing Dependencies** -> let it finish
5. **Close QGIS and reopen it** (required)
6. **Plugins > Geo-SAM > Settings** -> tab **Model Management** -> select
   `SAM2.1 Small` -> **Download** (~150-200 MB)

## A4. Install Miniconda

https://docs.conda.io/en/latest/miniconda.html, the 64-bit Windows build. If it
asks, tick **"Add Miniconda3 to my PATH environment variable"**.

## A5. Create the Python environment

Open **Anaconda Prompt** (press the Windows key, type `Anaconda Prompt`):

```bat
conda create -n bunjilview python=3.11 -y
conda activate bunjilview
cd /d D:\Bunjilview\segmentation_model
pip install -r requirements.txt
```

`pip install` takes 10 to 20 minutes, mostly downloading torch.

If a GDAL wheel fails to build, install it through conda first and re-run the
pip line:

```bat
conda install -c conda-forge gdal rasterio -y
```

Then get the interpreter path, you need it in part C:

```bat
where python
```

Something like `C:\Users\<you>\miniconda3\envs\bunjilview\python.exe`.
**Copy that line down.**

> If the machine has an NVIDIA GPU, install the CUDA build of torch instead for
> a large speedup: see https://pytorch.org/get-started/locally/

---

# PART B - PREPARE THE SITE (once per orthomosaic)

In **Anaconda Prompt**, with `conda activate bunjilview` done and `cd` into
`<REPO>`.

## B1. Put the ortho in place

```
<REPO>\data\raw\<site>.tif
```

## B2. Run these four commands, in order

```bat
python src\tiling.py    --config config.yaml --ortho data\raw\<site>.tif
python src\features.py  --config config.yaml --site <site>
python src\propagate.py --config config.yaml --site <site> --build-feature-cache
python src\propagate.py --config config.yaml --site <site> --build-sam-cache
```

`features.py` is the heavy one and can take hours on a large image. Add
`--device cuda` with an NVIDIA GPU, or `--device cpu` without one.

The two `propagate.py` commands also write
`data\labels\<site>\site.json`, which records the interpreter path. The
Propagate button in QGIS reads it.

## B3. Create the species layers

```bat
python scripts\init_labels_gpkg.py --config config.yaml --site <site>
```

Creates `data\labels\<site>\<site>.gpkg` with **one layer per species** from
`config.yaml`, each carrying both the Geo-SAM and the Bunjilview field sets.

It should end with:

```
ok - Geo-SAM will accept all of these layers
```

> **Do not create the layers by hand in QGIS.** A hand-made layer is missing
> Geo-SAM's fields and the plugin rejects it with "The fields of this vector do
> not match the SAM feature fields".

---

# PART C - WIRE IT INTO QGIS

## C1. Open the image

Open QGIS and drag `<REPO>\data\raw\<site>.tif` into the window.

## C2. Open the Python Console

**Plugins > Python Console**. An input box appears at the bottom.

## C3. Declare the paths (every time you open QGIS)

Paste this block, **editing the two lines for your machine**:

```python
import qgis.utils
qgis.utils.BV_REPO = r'D:\Bunjilview\segmentation_model'
qgis.utils.BV_SITE = 'site_name'
```

**The `r` before the quote is required on Windows.** Without it, `\t`, `\n` and
`\U` inside the path are read as escape characters and it fails.

If `site.json` does not exist yet, add:

```python
qgis.utils.BV_PYTHON = r'C:\Users\<you>\miniconda3\envs\bunjilview\python.exe'
```

## C4. Register the Propagate button (once per machine)

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

> **Do not use "Add Script to Toolbox".** It copies the file into the QGIS
> profile, and that copy shadows the repo version and never follows your edits.

## C5. Run setup (every time you open QGIS)

```python
exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
```

One command does it all: patch the plugin, verify the patches, load the image
and the species layers each in its own colour, bind Geo-SAM, zoom to the image,
and self-test.

**It must print:**

```
RESULT: 6/6 PASS
```

Anything less, stop and read the FAIL line, then check the trouble table below.

---

# PART D - LABELLING

## D1. Pick the species

**Click that species layer** in the Layers panel on the left. Geo-SAM follows,
enables edit mode, and saves any unsaved work on the previous layer.

Nothing to type: the layer name is the species.

## D2. Draw

On the **Geo-SAM Tool** panel, tab **Prompts**:

| Button | Key | What it does |
|---|---|---|
| **FG** | Tab | Click in the MIDDLE of the patch |
| **BG** | Tab | Click on an area that leaked in, to cut it out |
| **BBox** | Tab | Drag a rectangle around the target |
| **Undo** | Z | Remove the last point |
| **Clear** | C | Clear all points and start over |
| **Save** | S | Save the polygon into the layer |

Press **FG**, click in the middle of the patch. A preview polygon appears. Not
right yet? Another FG click extends it, or BG then a click cuts an area out.
Press **S** when it looks right.

## D3. Write it to disk

**Ctrl+S**. Required. Without it, propagation cannot read the polygon you just
drew.

## D4. Propagate

**Processing Toolbox** (on the right) -> **Scripts > Bunjilview > Propagate
species from seed**

| Field | Value |
|---|---|
| Species layer | the layer you just drew into |
| Similarity threshold | 0.60 |
| One-vs-rest margin | 0.05 |

Press **Run**. There is no species dropdown: the script reads the species from
the layer name.

The log shows how many polygons were added, and they appear on the map straight
away, in the layer colour.

If the result is poor: too many wrong polygons, raise the threshold to 0.70;
too many missed, lower it to 0.50.

## D5. Delete the wrong polygons

After Propagate runs, the layer is **out of edit mode**. Turn it back on first:
**Layer > Toggle Editing** (or the pencil icon).

Then:

1. Switch to the **Select Features** tool (the arrow with a yellow square on the
   Attributes toolbar). While Geo-SAM is active, clicking on the image ADDS a
   prompt point rather than selecting a polygon. This trips everyone up once.
2. Click the wrong polygon, hold **Shift** to select several.
3. Press **Delete**.
4. **Ctrl+S**.
5. Press **FG** again to go back to drawing.

If you cannot see the delete button, enable **View > Toolbars > Digitizing
Toolbar**.

**In bulk:** right-click the layer -> **Open Attribute Table**. Click the
`source` column header to sort (empty = drawn by you, `auto` = machine-made),
select the rows, press **Delete selected features**.

## D6. Run again to refine

Press Run again on the same layer. The prototype is now computed over every
polygon present, including the auto ones you kept, so the result tightens.

> **Clean up the wrong polygons BEFORE pressing Run again.** Machine-made
> polygons count as seeds on the next pass, so a wrong one left in place drags
> more wrong ones in with it. Do not press Run repeatedly without looking at the
> result.

## D7. Next species

Click another species layer in the Layers panel and go back to D2.

The more species have prototypes, the cleaner the result, because each
candidate has to beat the prototype of every other species.

---

# PART E - EXPORT LABELS AND TRAIN

In Anaconda Prompt:

```bat
python src\propagate.py --config config.yaml --site <site> --rasterize
python src\tiling.py --config config.yaml --ortho data\raw\<site>.tif --mask data\pseudo\<site>\<site>_labels.tif
python src\train.py --config config.yaml --site <site>
```

---

# PART F - QUICK COMMAND REFERENCE

Paste into the QGIS Python Console. Always declare `BV_REPO` and `BV_SITE`
first.

| Task | Command |
|---|---|
| Set up a session | `exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())` |
| Load the cleanup shortcuts | `exec(open(qgis.utils.BV_REPO + '/qgis/clean_polygons.py').read())` |
| Wipe everything and start over | `exec(open(qgis.utils.BV_REPO + '/scripts/reset_site_labels.py').read())` |
| Repair a broken or read-only layer | `exec(open(qgis.utils.BV_REPO + '/scripts/repair_labels_gpkg.py').read())` |
| Test the whole chain | `exec(open(qgis.utils.BV_REPO + '/scripts/test_pipeline.py').read())` |

After loading `clean_polygons.py`:

| Command | What it does |
|---|---|
| `count()` | Polygon count per layer |
| `delete_selected()` | Delete the currently selected polygons |
| `delete_auto()` | Delete every machine-made polygon on the active layer, keeping the ones you drew |
| `delete_all('Sphagnum_R')` | Wipe one layer completely |

---

# PART G - TROUBLE

| Symptom | Cause and fix |
|---|---|
| `The fields of this vector do not match the SAM feature fields` | The layer is missing Geo-SAM fields. Re-run `scripts\init_labels_gpkg.py`. |
| `Cannot create field group_ulid ... already exists` | A patch is missing. Re-run part C5. |
| You press S and the polygon **disappears**, with no error | A patch is missing. Re-run part C5. |
| The polygon lands in the **wrong layer** | A patch is missing. Re-run part C5. |
| `Layer not editable` | Edit mode was turned off when Propagate committed. **Layer > Toggle Editing**. |
| `Cannot reopen datasource ... in read-only mode` | Run `scripts\repair_labels_gpkg.py`. |
| The image is not visible and the Scale number is enormous | The canvas is zoomed too far out. Right-click the ortho layer -> **Zoom to Layer**. |
| A red error bar for something you already fixed | The QGIS banner **does not clear itself**, you have to press the **X**. Dismiss it and try again. |
| The Propagate dialog shows a free-text box instead of a layer picker | You are running a stale copy from the profile. Redo part C4 and do not use "Add Script to Toolbox". |

---

# PART H - THE FOUR PLUGIN PATCHES

`repair_session.py` applies these for you. They are documented here so you know
what changed.

| Patch | File | The problem |
|---|---|---|
| 1 | canvasTool.py | Geo-SAM assigns attributes **by column POSITION**, because it was written for Shapefiles, which have no `fid` column. A GeoPackage always has `fid` in column 0, so the ULID string is pushed into an integer primary key, the insert fails, and **the polygon disappears when you press S with no error**. The patch assigns by column NAME. |
| 2 | canvasTool.py | `show_layer()` calls `renderer().setSymbol()`, which only exists on a single-symbol renderer, so a multi-colour layer makes the plugin throw. |
| 3 | canvasTool.py | `_init_layer()` adds fields unconditionally -> "already exists". The patch adds only the missing ones. |
| 4 | widgetTool.py | Safety net: before writing, compare the bound layer with the selected one and re-bind if they differ, so a polygon cannot land in the wrong layer. |

**The patches live in the QGIS profile, so they are LOST when the Geo-SAM
plugin is reinstalled.** After reinstalling, re-run part C5.

---

# IMPORTANT WARNING

**Do not let OneDrive, Dropbox or Google Drive sync the `data\labels\` folder
while you are working.**

A `.gpkg` file is a SQLite database. It copes with several processes reading and
writing on the same disk, but not with two sides seeing the same file through
two different paths. A sync service does exactly that, and the result is a
corrupted spatial index, layers turning read-only, and in the worst case lost
data.

If you have no choice but to keep it in a synced folder, **pause syncing**
before opening QGIS.
