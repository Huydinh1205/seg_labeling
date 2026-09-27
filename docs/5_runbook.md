# Guide 5: Start to finish (runbook)

Every step of "approach 1.5" in one runnable sequence. For how it works, see
`4_interactive_propagation.md`. For the condensed version, see the repository
`README.md`.

`<site>` is the site name, for example `test1`. Every shell command runs from
the repo root in the conda environment:

```bash
cd /path/to/segmentation_model
conda activate bunjilview
```

---

## PART A - Prepare a new site (once per site)

### A1. Put the orthomosaic in place

```
data/raw/<site>.tif
```

### A2. Cut tiles

```bash
python src/tiling.py --config config.yaml --ortho data/raw/<site>.tif
```
-> `data/tiles/<site>/images/`

### A3. Extract DINOv2 features

```bash
python src/features.py --config config.yaml --site <site>
```

If it is slow, force the device: `--device mps` on Apple Silicon, or
`--device cpu`.
-> `data/features/<site>/`

### A4. Flatten the features into one cache

```bash
python src/propagate.py --config config.yaml --site <site> --build-feature-cache
```
-> `_patch_feats.npy`, `_patch_index.npy`, `_patch_xy.npy`, `_tiles.json`

### A5. Build the SAM candidate library

```bash
python src/propagate.py --config config.yaml --site <site> --build-sam-cache
```
-> `data/labels/<site>/sam_candidates/`

Without `segment-geospatial`, use an existing vector file as the candidates:

```bash
python src/propagate.py --config config.yaml --site <site> --build-sam-cache \
    --from-vector data/labels/<site>/geosam_masks.gpkg
```

A4 and A5 both write `data/labels/<site>/site.json`, which the QGIS button
reads to find the venv and the repo root.

### A6. Create the species layers

**This step is required. Do not create the layers by hand in QGIS.**

```bash
python scripts/init_labels_gpkg.py --config config.yaml --site <site>
```

Creates `data/labels/<site>/<site>.gpkg` with **one layer per species** from
`config.yaml -> classes` (minus `None`), each carrying **both** field sets.

**The layer name is the species.** There is no field to fill in when you draw.

Creating a layer by hand only gives you the fields you type, and Geo-SAM then
rejects it with *"The fields of this vector do not match the SAM feature
fields"*.

Expected output:

```
  Empidisma_minus            0 features, 15 fields
  Dracophyllum_continentis   0 features, 15 fields
  ...
ok - Geo-SAM will accept all of these layers
```

Re-running on a file that already holds polygons is refused. Add `--force` to
recreate it, which **deletes** the existing data.

---

## PART B - Every time you open QGIS (once per session)

### B1. Register the Processing script (once per machine)

**Settings > Options > Processing > Scripts > Scripts folders** -> add the
repo's `qgis/` folder -> OK -> restart QGIS.

Or from the Python Console:

```python
from processing.core.ProcessingConfig import ProcessingConfig
from qgis.core import QgsApplication
folder = '/path/to/segmentation_model/qgis'
cur = ProcessingConfig.getSetting('SCRIPTS_FOLDERS') or ''
if folder not in cur:
    ProcessingConfig.setSettingValue('SCRIPTS_FOLDERS', (cur + ';' + folder).strip(';'))
QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()
```

**Do not** use "Add Script to Toolbox". It copies the file into the QGIS
profile, and that copy shadows the one in the repo without ever following your
edits. See trap 5 in `4_interactive_propagation.md`.

### B2. Start the session

In the Python Console:

```python
import qgis.utils
qgis.utils.BV_REPO   = r'/path/to/segmentation_model'
qgis.utils.BV_SITE   = '<site>'
exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
```

On Windows keep the `r'...'` prefix so backslashes are taken literally.
Optionally add `qgis.utils.BV_PYTHON = r'C:\...\python.exe'`; leave it out and
the scripts read the interpreter path from `site.json`.

One command does everything: cleans the session, applies the four plugin
patches, verifies them, loads the ortho and all species layers each in its own
colour, binds Geo-SAM, and self-tests. Expected: `RESULT: 6/6 PASS`.

**If any patch is missing it stops there and says so**, rather than carrying on
and reporting a false pass.

The four patches, all in `<QGIS profile>/python/plugins/GeoSAM/tools/`:

| Patch | File | What it fixes |
|---|---|---|
| 1 | canvasTool.py | Assign attributes by column NAME. Geo-SAM assigns by POSITION, which was written for Shapefiles (no `fid`). A GeoPackage has `fid` in column 0, so the ULID string is pushed into the primary key, the insert fails, and the polygon disappears when you press S with no error. |
| 2 | canvasTool.py | `show_layer()` calls `renderer().setSymbol()`, which only exists on a single-symbol renderer. |
| 3 | canvasTool.py | `_init_layer()` calls `addAttributes` unconditionally -> "field group_ulid already exists". |
| 4 | widgetTool.py | Safety net: before writing, compare the bound layer with the combo box and re-bind if they differ, so a polygon cannot land in the wrong layer. |

The patches are lost when the plugin is reinstalled. Run the same line again.

`repair_session.py` also installs a **hook**: from then on, clicking a layer in
the Layers panel switches Geo-SAM to it, enables editing, and saves any pending
edits on the previous layer. The hook is gone when you close QGIS, so B2 runs
once per session.

You can still switch by hand if you want:

```python
exec(open(qgis.utils.BV_REPO + '/qgis/setup_geosam_session.py').read())
setup(species='Sphagnum_R')
```

---

## PART C - The labelling loop (repeat per species)

### C1. Pick the species layer

**Click that layer in the Layers panel.** That is all.

Geo-SAM follows, enables edit mode, and saves any unsaved edits on the previous
layer first. The message bar shows "Geo-SAM: switched to layer <name>".

> Before the hook existed, selecting a layer in the panel did **not** move
> Geo-SAM: the plugin only listened to its own combo box. Polygons landed in
> the previous layer with no warning. If you see a polygon come out in the
> wrong colour, that is almost certainly what happened. Check with C0.

### C0. Quick check when in doubt

```python
from qgis.utils import plugins, iface
sel = plugins.get('GeoSAM').wdg_select
print('Geo-SAM bound to:', sel.polygon.get_layer().name())
print('active layer    :', iface.activeLayer().name())
```

They must match. If they do not, re-run B2.

### C2. Draw a seed polygon

Press **`FG`** on the Prompts tab of the Geo-SAM panel and click in the middle
of the patch. A preview polygon appears.

Refine if needed: another **FG** click extends it, **`BG`** then a click cuts an
area out, **`Z`** undoes the last prompt, **`C`** clears and starts over.

Press **`S`** to save. **Nothing to type**, the layer name is the species.

Then **Ctrl+S** to write it to disk. This is required: the script cannot read a
seed that is still sitting in the edit buffer.

### C3. Press the propagate button

Processing Toolbox -> **Scripts > Bunjilview > Propagate species from seed**

| Field | Value |
|---|---|
| Species layer | the layer you just drew into |
| Similarity threshold | 0.60 |
| One-vs-rest margin | 0.05 |

There is no species dropdown: the script reads the species **from the layer
name**. If the layer name is not in `config.yaml` it stops with an error rather
than running on the wrong thing.

The log shows:

```
Species (from the layer name): Poa_costiniana
  seeds:              1
  other species:      N
  candidates scored:  M
  passed threshold:   K
  ADDED:              K  -> layer 'Poa_costiniana' of .../test1.gpkg
```

The `source='auto'` polygons appear on the canvas immediately, in the layer
colour.

### C4. Clean up by hand

The layer is still in edit mode.

**With the mouse:**

1. Switch to the **Select Features** tool (the arrow with a yellow square on
   the Attributes toolbar). While Geo-SAM is active, clicking on the image adds
   a prompt point rather than selecting a polygon. This is the most common
   point of confusion.
2. Click the wrong polygon, hold **Shift** to select several.
3. Press **Delete**, or **Edit > Delete Selected**.
4. **Ctrl+S**.
5. Press **FG** again to go back to drawing.

If you cannot see the delete button, enable **View > Toolbars > Digitizing
Toolbar**. The Delete key always works.

**In bulk:** right-click the layer -> **Open Attribute Table**. Click the
`source` column header to sort (empty = drawn by you, `auto` = machine-made),
select the rows, press **Delete selected features**.

**From the console, fastest when there is a lot to clean:**

```python
exec(open(qgis.utils.BV_REPO + '/qgis/clean_polygons.py').read())
```

| Command | What it does |
|---|---|
| `delete_selected()` | Delete the currently selected polygons (the yellow ones) |
| `delete_auto()` | Delete every machine-made polygon on the active layer, keeping the ones you drew |
| `delete_auto('Sphagnum_R')` | The same, on a named layer |
| `delete_all('Sphagnum_R')` | Wipe one layer completely |
| `count()` | Polygon count per layer |

All of these commit to disk, so no Ctrl+S is needed.

`delete_auto()` is the one to reach for when a propagation run came out badly:
drop everything machine-made, keep your seeds, adjust `threshold`, Run again.

### C5. Run again to refine (optional)

Press Run again on the same layer. The prototype is now computed over **every**
polygon in the layer, including the auto ones you kept. Raise `threshold` if
false positives remain, lower it if patches are still missing.

### C6. Next species

Click the next species layer in the Layers panel and go back to C2.

The more species have prototypes, the cleaner one-vs-rest separates them: each
candidate has to beat the prototype of every other species.

Turn off the layers you have finished to keep the canvas readable.

---

## PART D - Finishing: export a label raster and train

```bash
# Burn every polygon into one class-id GeoTIFF, 1:1 with the ortho
python src/propagate.py --config config.yaml --site <site> --rasterize
# -> data/pseudo/<site>/<site>_labels.tif

# Cut it into mask tiles matching the image tiles
python src/tiling.py --config config.yaml --ortho data/raw/<site>.tif \
    --mask data/pseudo/<site>/<site>_labels.tif
# -> data/tiles/<site>/masks/

python src/train.py --config config.yaml --site <site>
```

---

## PART E - Where the species names come from

**Nothing is hardcoded.** The list lives in `config.yaml -> classes`:

```yaml
classes:
  0:  None
  1:  Empidisma_minus
  2:  Dracophyllum_continentis
  3:  Poa_costiniana
  4:  Carex_gaudichaudiana
  5:  Celmisia_pugioniformis
  6:  Sphagnum_R
  7:  Sphagnum_G
  8:  Sphagnum_Y
  9:  Other
num_classes: 10
```

`init_labels_gpkg.py` reads it to know how many layers to create and what to
call them. The Processing script reads it to check that the layer you picked is
a valid species.

Because **the layer name is the species**, there is nowhere left to misspell
it. That is the main reason for the one-layer-per-species design.

### Adding or renaming a species

Edit `config.yaml`:

1. Add a line to `classes` with a new id.
2. Update `num_classes` to match (the number of ids, including `0`).
3. Create the layer. Easiest is to re-run `init_labels_gpkg.py` on an empty
   file; otherwise duplicate an existing species layer in QGIS (Layer >
   Duplicate Layer, rename, delete all features), because the layer needs the
   full Geo-SAM field set.
4. To rename an existing species, rename the layer (DB Manager, or
   `ALTER TABLE`) and update the `species` column inside it:

   ```sql
   ALTER TABLE "Old_name" RENAME TO "New_name";
   UPDATE "New_name" SET species = 'New_name';
   ```

5. Delete the stale prototype:
   `data/labels/<site>/prototypes/<Old_name>.npy`

After editing `config.yaml`, refresh the provider (B1) and re-run B2.

---

## PART F - Self-test the whole chain

When you suspect something is broken, or after reinstalling the plugin or
moving to another machine:

```python
exec(open(qgis.utils.BV_REPO + '/scripts/test_pipeline.py').read())
```

Takes about six seconds and checks: binding to all 9 layers, drawing into each
one with the values in the right columns, deleting, adding, propagating,
propagating again, and deleting auto polygons then propagating once more.

It **cleans up after itself**: every polygon it creates is deleted at the end
and your data comes back exactly as it was. If the report ever contains
`LOST n ORIGINAL FEATURES`, that is a bug in the test, so say so.

The report is written to `test_report.txt` in the repo root. Expected result:
all checks PASS.

---

## PART G - Warning: repeated runs drift

`config.yaml` has `propagate.prototype.seed_sources: ["seed", "auto", null]`,
which means machine-made polygons count as seeds on the next run.

So each Run widens the prototype and finds more patches. Measured on the test
site:

| Run | Seeds | Added |
|---|---|---|
| 1 | 15 | 24 |
| 2 | 39 | 12 |
| 3 | 48 | 4 |

It **converges** rather than running away, and never produces a duplicate
(largest measured IoU 0.000 across 51 polygons). But it is a feedback loop:
**a wrong auto polygon left in place becomes a seed and pulls in more like
it.**

So:

- **Clean up before pressing Run again.** Do not run it back to back.
- Do not go more than two or three rounds without looking at the result.
- To make re-runs learn only from polygons you drew by hand:

  ```yaml
  propagate:
    prototype:
      seed_sources: ["seed", null]     # drop "auto"
  ```

  Safer, but slower: you have to draw more seeds yourself.

---

## PART H - Trouble: the layer turns read-only

Usually it shows up exactly when you press `S` to save a Geo-SAM polygon:

```
Cannot reopen datasource .../test1.gpkg|layername=Poa_costiniana
in read-only mode
```

Related errors: `database is locked`, `disk I/O error`, a
`PRAGMA integrity_check` reporting `unable to get the page`, or a layer that
suddenly will not open.

### The fix

```python
exec(open(qgis.utils.BV_REPO + '/scripts/repair_labels_gpkg.py').read())
```

It backs up, diagnoses, and **only if the file is genuinely corrupt** builds a
fresh one and copies every feature across, layer by layer. The old files are
kept as `<site>.gpkg.corrupt` and `<site>.gpkg.bak-<time>`. Nothing is deleted.

If it reports that the file is *not* corrupt, then another process is holding
it. Close QGIS, make sure no stray python process is running, and reopen.

### Why it happens

A GeoPackage is SQLite. It copes with several processes reading and writing on
**the same filesystem**, which is why the Processing button can call
`propagate.py` while QGIS has the layer open. What it does **not** cope with is
two processes seeing the same file through **two different mounts**, say a
local disk on one side and a network share or FUSE on the other. The two sides
fight over the WAL `-shm` file, the spatial index is corrupted, and the layer
goes read-only.

The tell-tale signs are a `.fuse_hidden*` file in the folder, or `-wal` and
`-shm` files left behind after QGIS has closed.

### Avoiding it

- Only ever open `<site>.gpkg` from **one machine, through one path**.
- Do not let Dropbox, Google Drive or OneDrive sync `data/labels/` while you
  work.
- To inspect the data with your own script, wait until QGIS is closed, or read
  it from the QGIS Python Console.
- Always Ctrl+S before running any script that touches that file.

---

## PART I - Starting a site over

To throw away every labelled polygon and start again from empty layers:

```python
exec(open(qgis.utils.BV_REPO + '/scripts/reset_site_labels.py').read())
```

It deletes `<site>.gpkg` and the `prototypes/` folder, recreates the empty
layers with the correct schema, and self-tests. **This cannot be undone.**

The expensive caches are kept and do not need rebuilding: `_patch_feats.npy`
(around 840 MB), `_patch_index.npy`, `_patch_xy.npy`, `_tiles.json`,
`sam_candidates/` and `site.json`.

Deleting `prototypes/` is deliberate: keeping them would let one-vs-rest go on
learning from the data you just discarded.
