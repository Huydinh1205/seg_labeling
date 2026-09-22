# Guide 4: Propagating from a single seed polygon ("approach 1.5")

Draw **one** Geo-SAM polygon for a species, press **Run**, and the system
outlines every similar patch across the whole orthomosaic with SAM-quality
boundaries. Delete or add polygons by hand in QGIS, run it again to refine,
then move to the next species.

It combines the strengths of the other two approaches: the crisp boundaries of
Geo-SAM, without tracing every polygon, because DINOv2 does the finding.

Engine: `src/propagate.py`. The button in QGIS:
`qgis/propagate_species_algorithm.py`.

---

## How it works

1. DINOv2 has already produced a 768-dimension feature vector for every image
   patch (the `features.py` step).
2. `--build-sam-cache` runs SAM once per site, producing a library of a few
   hundred candidate masks with good boundaries. Each mask carries a feature
   vector, the average of the patches it covers.
3. Your seed polygon becomes a **prototype**: k-means over the feature patches
   inside it.
4. Every candidate mask is scored by cosine similarity against that prototype.
   **One-vs-rest**: a mask is only kept if this species beats every other
   species that already has a prototype, by at least `margin`. That is what
   stops two layers bleeding into each other.
5. Masks above the threshold are appended to the layer with `source='auto'`.
   Polygons you drew (`source` empty or `'seed'`) are never touched.

**Layer structure: one layer per species**, all inside
`data/labels/<site>/<site>.gpkg`. **The layer name is the species**, so there
is nothing to type after drawing: pick the layer, draw, press S, done.

Every layer carries both field sets, because Geo-SAM rejects a layer that is
missing one of its own. `scripts/init_labels_gpkg.py` creates them correctly.

This replaced an earlier design with one shared `labels` layer and the species
as an attribute. That version needed the species typed in after every polygon,
and one typo meant the polygon was silently dropped at rasterisation time. The
current layout has nowhere to make that mistake.

---

## One-time setup per site

Assuming the ortho is `data/raw/<site>.tif` and you have already run `tiling.py`
and `features.py` for it (guide 3, steps 1 and 2):

```bash
conda activate bunjilview

# 1. Flatten every tile's features into one L2-normalised cache
python src/propagate.py --config config.yaml --site <site> --build-feature-cache

# 2. SAM auto-mask library + a feature vector per mask (needs segment-geospatial)
python src/propagate.py --config config.yaml --site <site> --build-sam-cache

# 3. One layer per species, with the correct schema
python scripts/init_labels_gpkg.py --config config.yaml --site <site>
```

Steps 1 and 2 also write `data/labels/<site>/site.json`, which the QGIS button
reads to find the venv interpreter and the repo root.

**Without `segment-geospatial`**, or if you already have masks from the Geo-SAM
plugin, skip SAM and use an existing vector file as the candidate source:

```bash
python src/propagate.py --config config.yaml --site <site> --build-sam-cache \
    --from-vector data/labels/<site>/geosam_masks.gpkg
```

---

## The labelling loop

### Step 1: Start the session

In the QGIS Python Console:

```python
import qgis.utils
qgis.utils.BV_REPO = r'/path/to/segmentation_model'
qgis.utils.BV_SITE = '<site>'
exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
```

This patches and verifies the plugin, loads the ortho and every species layer
in its own colour, binds Geo-SAM, and self-tests. You want `RESULT: 6/6 PASS`.

### Step 2: Draw a seed polygon

- Click the species layer in the **Layers** panel. Geo-SAM follows that click,
  which is how you switch species. The layer colour is the polygon colour.
- Press **FG**, click on a clear, representative patch, press **S**.
- No attribute form to fill in. The layer is the species.

One good seed is enough to start. Two or three that look genuinely different
from each other work better.

### Step 3: Propagate

**Processing Toolbox > Scripts > Bunjilview > Propagate species from seed**

Pick that species layer, adjust `threshold` (0.60) and `margin` (0.05) if you
want, Run. The button calls the venv, prints the log, and reloads the layer, so
the `source='auto'` polygons appear on the canvas as soon as it finishes.

The equivalent by hand:

```bash
python src/propagate.py --config config.yaml --site <site> \
    --layer data/labels/<site>/<site>.gpkg --species Sphagnum_R \
    --threshold 0.60 --margin 0.05
```

### Step 4: Clean up

Delete the wrong polygons, and draw in any obvious patch it missed.

```python
exec(open(qgis.utils.BV_REPO + '/qgis/clean_polygons.py').read())
count()
delete_selected()
delete_auto()          # every auto polygon on the active layer, keeping yours
```

Or by hand: Toggle Editing (the pencil, Ctrl+E), select, Delete, save.

**Do the cleanup before running again.** `seed_sources` counts `auto` polygons
as seeds on the next pass, so a bad polygon left in place teaches the next
round to find more like it. If you would rather keep propagation anchored only
to what you drew, remove `"auto"` from `propagate.seed_sources` in
`config.yaml`.

### Step 5: Run again to refine

Press Run again. The prototype is now computed over every polygon of that
species, including the auto ones you kept and anything you added by hand, so
the result tightens.

Raise `threshold` if false positives remain, lower it if patches are still
being missed.

It converges. On a real site the three runs added 24, then 12, then 4
polygons, and a full pairwise check across the resulting 51 polygons found a
maximum IoU of 0.000: no duplicates.

### Step 6: Next species

Click the next species layer and repeat from step 2. The more species have
prototypes, the cleaner one-vs-rest separates them.

---

## Finishing: export a label raster for training

```bash
# Burn every polygon of every species into one class-id GeoTIFF, 1:1 with the ortho
python src/propagate.py --config config.yaml --site <site> --rasterize
# -> data/pseudo/<site>/<site>_labels.tif

# Cut it into mask tiles matching the image tiles
python src/tiling.py --config config.yaml --ortho data/raw/<site>.tif \
    --mask data/pseudo/<site>/<site>_labels.tif
# -> data/tiles/<site>/masks/   (train.py reads straight from here)

python src/train.py --config config.yaml --site <site>
```

When two polygons overlap during `--rasterize`, a hand-drawn polygon (`source`
empty or `seed`) always wins. Between two `auto` areas of different species,
whichever is read first wins. Use `margin` during propagation to avoid the
overlap in the first place, or set
`propagate.rasterize.overlap_priority` to `class_order` so the higher class id
wins instead.

---

## Quick test without segment-geospatial

`scripts/make_test_fixtures.py` builds fake candidate masks out of existing
cluster maps, plus a seed layer with three seeds taken from
`cluster_class_mapping.yaml`. Enough to exercise every code path without
installing SAM.

```bash
conda activate bunjilview

python scripts/make_test_fixtures.py --site test1
python src/propagate.py --config config.yaml --site test1 --build-feature-cache
python src/propagate.py --config config.yaml --site test1 --build-sam-cache \
    --from-vector data/labels/test1/_test_candidates.gpkg

python src/propagate.py --config config.yaml --site test1 \
    --layer data/labels/test1/_test_seeds.gpkg --species 'Sphagnum_Y'
python src/propagate.py --config config.yaml --site test1 \
    --layer data/labels/test1/_test_seeds.gpkg --species 'Carex_gaudichaudiana'
python src/propagate.py --config config.yaml --site test1 \
    --layer data/labels/test1/_test_seeds.gpkg --species 'Poa_costiniana'

python src/propagate.py --config config.yaml --site test1 \
    --layer data/labels/test1/_test_seeds.gpkg --rasterize
python src/tiling.py --config config.yaml --ortho data/raw/test1.tif \
    --mask data/pseudo/test1/test1_labels.tif
```

What to expect:

- feature cache: `273,456 patches x 768 dim`, every row with norm 1.
- each `--species` run: a line `ADDED: N -> species='...' in layer '...'`; the
  `other species` count rising 0, 1, 2, and the `passed` count of later species
  pushed down by one-vs-rest.
- `--rasterize`: a `5217x8364` GeoTIFF whose values are only class ids and 255.
- Open `_test_seeds.gpkg` and `sam_candidates/sam_masks.gpkg` in QGIS to look at
  them.

Afterwards delete `data/labels/test1/`, `data/tiles/test1/masks/` and
`data/pseudo/test1/test1_labels.tif`. They are test data.

This only exercises the mechanism. Real candidates have to come from SAM
(`--build-sam-cache` without `--from-vector`) to get good boundaries and a
sensible count, a few hundred rather than 55,000 cluster fragments.

---

## Full test suite

```python
exec(open(qgis.utils.BV_REPO + '/scripts/test_pipeline.py').read())
```

Runs the whole chain: patching, binding to every layer, drawing into each one,
editing, propagating, propagating again, deleting auto polygons and recovering
them. It deletes every polygon it created, so your data comes back exactly as
it was. The report also goes to `test_report.txt`.

---

## Settings (`config.yaml -> propagate:`)

| Key | Meaning |
|---|---|
| `match.threshold` | Absolute cosine similarity a candidate must reach against the prototype. Higher means fewer, safer polygons. |
| `match.margin` | One-vs-rest: `sim_species - best_other_species` must be at least this. |
| `match.dup_iou` | Drop a candidate that overlaps an existing polygon by more than this. |
| `match.min_area_m2` / `max_area_m2` | Filter polygons that are too small or too large. |
| `prototype.topk` | Number of k-means centroids in a prototype (1 = plain average). |
| `seed_sources` | Which polygons count as seeds. Drop `"auto"` to anchor on hand-drawn ones only. |
| `sam.*` | SAM auto-mask parameters, used only by `--build-sam-cache`. |

---

## Files it creates

```
data/labels/<site>/
  <site>.gpkg                one layer per species (species, source, score, run_ts)
  site.json                  {site, repo_root, venv_python}
  _patch_feats.npy           (N, 768) f32, L2-normalised feature cache
  _patch_index.npy  _patch_xy.npy  _tiles.json
  sam_candidates/
    sam_masks.gpkg           candidate polygons
    sam_feats.npy            (M, 768) f32, one feature per candidate (matched by fid)
    sam_meta.json
  prototypes/<species>.npy   (K, 768) f32, the accumulated prototype
```

Add `--rebuild` to `--build-feature-cache` or `--build-sam-cache` to force them
to regenerate.

---

## Traps, and why the setup script exists

Geo-SAM will refuse to attach to a layer if things happen in the wrong order,
and several of its failure modes are **silent**. `scripts/repair_session.py`
handles all of them. Read on only if you need to debug it by hand.

### 1. The layer needs BOTH field sets in the same table

| Set | Fields |
|---|---|
| Geo-SAM | `group_ulid`, `N_GM`, `id`, `Area`, `N_FG`, `N_BG`, `BBox`, `lb_name`, `lb_note` |
| Bunjilview | `species`, `source`, `score`, `run_ts` |

Miss any Geo-SAM field and you get *"The fields of this vector do not match the
SAM feature fields"*. The Processing script backfills missing Bunjilview fields
on its first run, but it does not add Geo-SAM's.

### 2. The image source must be set BEFORE the annotation layer

The biggest trap, because it **fails silently**. The first lines of
`set_vector_layer()` in the plugin:

```python
if not hasattr(self, "img_crs_manager"):
    MessageTool.MessageBar("Geo-SAM", "Choose an input source before ...")
    return
```

`img_crs_manager` is only created when the Live-Encoding combo box **changes
value**. Until then the function returns immediately, `self.polygon` is never
created, and Geo-SAM is attached to nothing. No exception, no log entry.

### 3. Never route the annotation combo through None

`setLayer(None)` followed by `setLayer(x)` looks like a harmless way to force
the signal, but the trip through `None` calls `reset_layer(None)` ->
`_init_layer()`, which calls `addAttributes` unconditionally and raises:

```
Cannot create field group_ulid. A field with the same name already exists.
```

Patch 3 makes `_init_layer()` add only the fields that are actually missing.

### 4. The layer must use a single-symbol renderer

`show_layer()` calls `renderer().setSymbol(...)`, which a categorized or
graduated renderer does not have:

```
AttributeError: 'QgsCategorizedSymbolRenderer' object has no attribute 'setSymbol'
```

Patch 2 guards that call, which is what lets each species layer keep its own
colour.

### 5. A stale copy of the Processing script in the QGIS profile

"Add Script to Toolbox" **copies** the file into
`<QGIS profile>/processing/scripts/`. The copy does not follow your edits, and
it **shadows** the one in the repo.

The giveaway used to be a free-text **Species** box instead of a dropdown: the
copy sits in the profile, where it cannot see `config.yaml`.

Do not use "Add Script to Toolbox". Point Processing at the repo's `qgis/`
folder instead: **Settings > Options > Processing > Scripts > Scripts
folders**. Or from the console:

```python
from processing.core.ProcessingConfig import ProcessingConfig
from qgis.core import QgsApplication
folder = '/path/to/segmentation_model/qgis'
cur = ProcessingConfig.getSetting('SCRIPTS_FOLDERS') or ''
ProcessingConfig.setSettingValue('SCRIPTS_FOLDERS', (cur + ';' + folder).strip(';'))
QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()
```

### 6. Geo-SAM assigns attributes BY COLUMN POSITION

Symptom: you draw a polygon, press **S**, and it **disappears**. No error, no
new row in the layer.

In `canvasTool.py`:

```python
feature.setAttributes([group_ulid, 0, id, area, n_fg, n_bg, has_bbox])
```

Those seven values land in columns 0 to 6 by position. Geo-SAM was written for
**Shapefiles**, which have no `fid` column, so column 0 really is `group_ulid`.
A **GeoPackage** always has `fid`, an Integer64 primary key, in column 0:

| Column | Gets |
|---|---|
| `fid` | the ULID string |
| `lb_name` | 0 |
| `lb_note` | id |
| `group_ulid` | the area |
| ... | everything shifted by one |

Pushing a string into an integer primary key fails the insert, and Geo-SAM
clears the preview as usual, so the polygon looks like it evaporated.

Patch 1 assigns by column name instead. Apply it with:

```python
exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
```

The patches live in the QGIS profile, so they are lost whenever the plugin is
reinstalled or updated. Running that line again fixes it. To check the current
state:

```python
ns = {}
exec(open(qgis.utils.BV_REPO + '/scripts/patch_geosam_fields.py').read(), ns)
print(ns['verify']())     # four True values
```

### 7. Never read the GeoPackage from outside while QGIS holds it

A GeoPackage is SQLite. It survives several processes on the same filesystem,
but not two processes reaching it through different mounts. The result is a
corrupted index, a leftover `.fuse_hidden` file, and layers that turn
read-only.

If it happens: `scripts/repair_labels_gpkg.py` backs up, diagnoses, rebuilds
and copies every feature across. Then keep to one machine and one path, and do
not let Dropbox, Google Drive or OneDrive sync `data/labels/` while you work.
