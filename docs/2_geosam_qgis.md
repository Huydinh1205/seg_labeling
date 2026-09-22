# Guide 2: Fast labelling with Geo-SAM in QGIS

Instead of clicking twenty to fifty vertices to trace a polygon, you click one
point in the middle of a patch and Geo-SAM draws the boundary. Roughly 70-80%
less time per polygon.

**Faster still:** draw one example per species and press a button to have the
system outline every similar patch across the whole site. See
`4_interactive_propagation.md`. That is the workflow this project actually
uses day to day, and the repository `README.md` covers it end to end.

This guide covers the plain Geo-SAM flow: click, draw, save.

---

## Install Geo-SAM (once)

1. **Plugins > Manage and Install Plugins**, tab "All", search `Geo SAM`,
   select **Geo SAM** and press **Install Plugin**.
2. **Plugins > Geo-SAM > Settings**, tab **Dependencies**, press **Install
   Missing Dependencies**, then restart QGIS.
3. **Plugins > Geo-SAM > Settings**, tab **Model Management**, choose the
   checkpoint the plugin offers (SAM 2 Hires is a good default) and download it
   (~170 MB).

Then apply this project's patches, once per machine:

```python
exec(open('/path/to/segmentation_model/scripts/patch_geosam_fields.py').read())
```

Without them Geo-SAM writes attributes by column position, which is wrong for
a GeoPackage, and your polygon silently disappears when you press **S**. See
the README, section 1.5. The patches are lost whenever the plugin is
reinstalled or updated, so in practice you let `scripts/repair_session.py`
apply and verify them at the start of every session.

---

## Layer structure

**One layer per species, all inside one GeoPackage**:
`data/labels/<site>/<site>.gpkg`. The layer name *is* the species, so there is
no attribute form to fill in after each polygon.

Create the layers with:

```bash
python scripts/init_labels_gpkg.py --config config.yaml --site <site>
```

Do not create them by hand in QGIS. Geo-SAM needs its full field set
(`group_ulid`, `N_GM`, `id`, `Area`, `N_FG`, `N_BG`, `BBox`, `lb_name`,
`lb_note`) and rejects any layer that is missing one with *"The fields of this
vector do not match the SAM feature fields"*.

---

## Daily workflow

1. Start the session. In the QGIS Python Console:

   ```python
   import qgis.utils
   qgis.utils.BV_REPO = r'/path/to/segmentation_model'
   qgis.utils.BV_SITE = 'test1'
   exec(open(qgis.utils.BV_REPO + '/scripts/repair_session.py').read())
   ```

   This loads the ortho and every species layer with its own colour, binds
   Geo-SAM, and self-tests. You want `RESULT: 6/6 PASS`.

2. The current plugin uses **Live-Encoding**: there is no separate "Encode
   Image" step and no `.pkl` file to manage. The prompt buttons in the panel
   are **BBox / FG / BG / Clear / Undo / Save**.

3. Click the species layer in the **Layers** panel. Geo-SAM follows your click,
   so that is how you switch species.

4. Press **FG** and click in the middle of the patch. A preview polygon
   appears.

5. Refine if needed:
   - **FG** and another click to extend the polygon into more of the patch.
   - **BG** and a click to cut an area back out.
   - **BBox** and drag a rectangle when a single point is not enough.
   - **Undo** removes the last prompt, **Clear** starts the polygon over.

6. Press **S** to save.

---

## Prompting tips per species

| Species | How to prompt | Notes |
|---|---|---|
| Sphagnum R / G / Y | FG in the middle of the patch | Distinct colours, SAM separates them well |
| Dracophyllum continentis | FG once or twice, extend as needed | Large patches often need a second point |
| Empidisma minus | FG in the middle | Olive; easy to confuse with Poa |
| Poa costiniana | FG at the base of the tussock | A BBox is often more accurate |
| Celmisia pugioniformis | **BBox** around the flower | Small; a single point rarely catches it |
| Carex gaudichaudiana | FG in the middle of the sedge | Wet ground, clear boundaries |

---

## Deleting a wrong polygon

Select the layer, click **Toggle Editing** (the pencil, or Ctrl+E), select the
polygon, press Delete, then click the pencil again and save. "Layer not
editable" means you skipped the pencil.

Or from the console:

```python
exec(open(qgis.utils.BV_REPO + '/qgis/clean_polygons.py').read())
count()
delete_selected()
```

---

## Using hand-drawn polygons with the training code

Rasterise the polygons into a label GeoTIFF, then tile it alongside the ortho:

```bash
gdal_rasterize \
  -a species_id \
  -tr 0.01 0.01 \
  -te $(python -c "import rasterio; s=rasterio.open('data/raw/site_A.tif'); print(*s.bounds)") \
  data/labels/site_A/site_A.gpkg \
  data/pseudo/site_A/site_A_labels.tif

python src/tiling.py --config config.yaml \
  --ortho data/raw/site_A.tif \
  --mask data/pseudo/site_A/site_A_labels.tif
```

---

## Before and after

| | By hand | With Geo-SAM |
|---|---|---|
| Drawing a polygon | 20-50 vertices | 1-3 clicks |
| Time per polygon | 2-5 minutes | 10-20 seconds |
| 100 polygons | ~5 hours | ~30 minutes |
| What does not change | You still identify the species | You still identify the species |

---

## Links

- GitHub: https://github.com/coolzhao/Geo-SAM
- Plugin page: https://plugins.qgis.org/plugins/Geo-SAM/
