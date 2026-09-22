# Guide 3: Running the automated pipeline

This pipeline cuts hand labelling down to roughly 40 decisions per site,
instead of thousands of polygons.

---

## The six steps

```
[1]  cut the ortho into tiles        tiling.py       ~5 min
[2]  extract DINOv2 features         features.py     ~1-2 h (first run)
[3]  k-means, 40 clusters            cluster.py      ~10-20 min
[4a] export the montage              naming.py       ~5 min
[4b] HUMAN: fill species into a CSV  ~15 min (look at 40 images, type names)
[4c] apply the mapping               naming.py       ~1 min
[5]  write pseudo-label GeoTIFFs     pseudolabel.py  ~10 min
[6]  train the model                 train.py        ~2-4 h
```

Machine time: 4 to 6 hours, fine to leave running overnight.
Your time: about 30 minutes, all of it in step 4b.

---

## Before you start

1. Put the orthomosaic in `data/raw/`:

   ```
   data/raw/cope_saddle.tif
   ```

2. Activate the environment:

   ```bash
   conda activate bunjilview          # or: source .venv/bin/activate
   cd /path/to/segmentation_model
   ```

---

## Step 1: Tile the orthomosaic

Cuts the large `.tif` into 512x512 pixel tiles, keeping the georeference.

```bash
python src/tiling.py --config config.yaml --ortho data/raw/cope_saddle.tif
```

Output:

```
data/tiles/cope_saddle/images/
    tile_r0000_c0000.tif
    tile_r0000_c0001.tif
    ...   (often thousands of tiles)
```

To cut a label mask with the identical scheme at the same time, if you already
have one:

```bash
python src/tiling.py \
  --config config.yaml \
  --ortho data/raw/cope_saddle.tif \
  --mask data/pseudo/cope_saddle/labels.tif
```

---

## Step 2: Extract DINOv2 features

Runs DINOv2 over every tile. The first run downloads the model (~350 MB).

```bash
python src/features.py --config config.yaml --site cope_saddle
```

Device selection is automatic: MPS on Apple M1/M2/M3, CUDA on an NVIDIA GPU,
CPU otherwise (5-10x slower, but it works). To force it:

```bash
python src/features.py --config config.yaml --site cope_saddle --device mps
python src/features.py --config config.yaml --site cope_saddle --device cpu
```

Output: `data/features/cope_saddle/*.npy`

Resume-safe: if it is interrupted, run the same command again and it skips the
tiles it has already done.

---

## Step 3: K-means clustering

Groups pixels into 40 clusters based on their features. Over-clustering is
deliberate: it lets Sphagnum R/G/Y fall into separate clusters, and
Dracophyllum split by season.

```bash
python src/cluster.py --config config.yaml --site cope_saddle
```

Output:

```
data/clusters/cope_saddle/
    tile_r0000_c0000.tif   (cluster map, int16)
    centroids.npy          (40 cluster centres)
```

To change the number of clusters, edit `config.yaml`:

```yaml
clustering:
  n_clusters: 40      # try 30 or 50
```

---

## Step 4a: Export the montage

Builds a PNG with 40 rows, each row showing 12 representative patches of one
cluster.

```bash
python src/naming.py --config config.yaml --site cope_saddle --export-montage
```

Output:

```
data/clusters/cope_saddle/
    cluster_montage.png    <- look at this
    cluster_names.csv      <- fill this in
```

---

## Step 4b: Name the clusters (the only manual part)

Open `data/clusters/cope_saddle/cluster_names.csv` in Excel or a text editor.
It starts empty:

```csv
cluster_id,species
0,
1,
2,
...
39,
```

Look at each row of `cluster_montage.png` and type the species into the
`species` column:

```csv
cluster_id,species
0,Sphagnum_R
1,Sphagnum_G
2,Dracophyllum_continentis
3,Empidisma_minus
4,None
5,Poa_costiniana
...
```

The names must match exactly. Copy them from here:

```
None
Empidisma_minus
Dracophyllum_continentis
Poa_costiniana
Carex_gaudichaudiana
Celmisia_pugioniformis
Sphagnum_R
Sphagnum_G
Sphagnum_Y
Other
```

Tips:

- A cluster often contains more than one species. Pick the majority.
- Background or bare ground: use `None`.
- Not sure: use `Other`.
- Green and red Dracophyllum may land in two different clusters. Give both the
  same species name.

---

## Step 4c: Apply the mapping

```bash
python src/naming.py --config config.yaml --site cope_saddle --apply-mapping
```

If a species name is misspelled the script stops and lists the valid names.

Output: `data/clusters/cope_saddle/cluster_class_mapping.yaml`

---

## Step 5: Write the pseudo-label GeoTIFFs

Combines the cluster maps with the mapping.

```bash
python src/pseudolabel.py --config config.yaml --site cope_saddle
```

Output: `data/pseudo/cope_saddle/*_label.tif`

It prints the class distribution so you can sanity-check the balance:

```
Class  1 Empidisma_minus:              1,234,567 px  (12.3%)
Class  2 Dracophyllum_continentis:     2,345,678 px  (23.4%)
...
```

Check it visually: drag a `*_label.tif` into QGIS and set Symbology to
**Paletted/Unique values**.

---

## Step 6: Train

```bash
# DeepLabV3+ (default, faster)
python src/train.py --config config.yaml --site cope_saddle

# SegFormer (more accurate, needs more memory)
python src/train.py --config config.yaml --site cope_saddle --model segformer
```

Per-class IoU is printed every `val_interval` epochs. The best checkpoint goes
to `checkpoints/cope_saddle/deeplabv3plus/best.pt`.

---

## Step 7: Inference on a new site

```bash
python src/infer.py \
  --config config.yaml \
  --ortho data/raw/new_site.tif \
  --checkpoint checkpoints/cope_saddle/deeplabv3plus/best.pt \
  --out data/pseudo/new_site_pred.tif
```

Open `new_site_pred.tif` in QGIS to see the result.

---

## All in one

```bash
# Steps 1 to 4a, then it stops for you to fill in the CSV
bash run_pipeline.sh cope_saddle data/raw/cope_saddle.tif

# once cluster_names.csv is filled in
bash run_pipeline.sh cope_saddle data/raw/cope_saddle.tif --from-naming
```

---

## Common errors

**"No .tif tiles found"**
Check that `data/tiles/cope_saddle/images/*.tif` exists. If not, run
`tiling.py` again.

**"CUDA out of memory"**
Lower the batch size in `config.yaml`: `training: batch_size: 4`, or `2`.

**"No image-mask pairs found"**
`data/val/` needs both an `images/` and a `masks/` folder, with at least one
matching pair.

**"species not in class map"**
A typo in `cluster_names.csv`. Copy the names from the list in step 4b.

---

## Settings worth knowing

```yaml
clustering:
  n_clusters: 40           # raise to 50 for finer separation
  patches_per_cluster: 12  # images per cluster in the montage

training:
  epochs: 50               # raise to 100 when you have more data
  batch_size: 8            # lower to 4 if you run out of VRAM
  lr: 0.0001               # rarely needs changing

tiling:
  tile_size: 512           # leave alone
  overlap: 64              # leave alone
```
