# QGIS side of the project

Three files, all driven from QGIS. None of them does machine learning: the
heavy work is shelled out to the project's Python environment, because QGIS's
bundled Python has no torch or rasterio.

Full setup and workflow: the repository `README.md`.

| File | What it is |
|---|---|
| `setup_geosam_session.py` | Loads the ortho and every species layer, one colour each, binds Geo-SAM, and installs a hook so clicking a layer switches species. |
| `propagate_species_algorithm.py` | A QGIS Processing script: the **Propagate species from seed** button. Calls `src/propagate.py` and reloads the layer when it finishes. |
| `clean_polygons.py` | Console shortcuts: `count()`, `delete_selected()`, `delete_auto()`, `delete_all()`. |

You normally do not run `setup_geosam_session.py` directly.
`scripts/repair_session.py` runs it for you, after applying and verifying the
Geo-SAM patches.

## Install (once)

QGIS -> **Settings > Options > Processing > Scripts > Scripts folders** -> add
this `qgis/` folder -> OK -> restart QGIS.

The Processing script then appears under
**Scripts > Bunjilview > Propagate species from seed**.

**Do not use "Add Script to Toolbox".** It copies the file into the QGIS
profile, and that stale copy shadows the one in the repo: every edit you make
afterwards appears to do nothing.

## Prerequisites

Once per site, in the Python environment (not in QGIS):

```bash
conda activate bunjilview
python src/propagate.py --config config.yaml --site <site> --build-feature-cache
python src/propagate.py --config config.yaml --site <site> --build-sam-cache
python scripts/init_labels_gpkg.py --config config.yaml --site <site>
```

Those write `data/labels/<site>/site.json`, which records the repo root and the
interpreter path, and create the GeoPackage with one layer per species.

The Processing script finds `site.json` by walking up from the GeoPackage
folder, so the default layout needs no configuration. If yours differs, set
**Site**, **venv python** and **Config file** under the script's **Advanced**
parameters.

## Use

**One layer per species.** The layer name *is* the species, so there is no
attribute to fill in when you draw.

1. In the **Layers** panel, click the layer for the species you are working on.
   Geo-SAM follows that click.
2. Press **FG**, click on the plant, press **S** to save. One good seed is
   enough to start, two or three that look different from each other work
   better.
3. **Processing Toolbox > Scripts > Bunjilview > Propagate species from seed**,
   pick that same layer, Run. Accepted candidates are appended to the layer
   with `source='auto'`.
4. Delete the wrong ones, then run it again to refine. Do the cleanup before
   re-running: auto polygons count as seeds on the next pass, so a bad polygon
   left in place teaches the next round to find more like it.
