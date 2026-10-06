"""
propagate.py
------------
Interactive species propagation for QGIS labelling ("Cach 1.5").

Draw ONE Geo-SAM polygon in QGIS, type its species into the attribute form (same
as the plain Geo-SAM flow in docs/2_geosam_qgis.md - one shared "labels" layer,
'species' text field), then let this engine auto-draw every visually-similar
patch of that species across the orthomosaic, with crisp SAM boundaries. Clean
up by hand in QGIS, re-run to refine, move to next species.

Reuses the DINOv2 dense features the automated pipeline already computes
(data/features/<site>/tile_*.npy, shape (h_p, w_p, C) float32).

Modes
-----
    # 1. one-time per site: stack per-tile features into a flat, L2-normed cache
    python src/propagate.py --config config.yaml --site <site> --build-feature-cache

    # 2. one-time per site: SAM auto-mask library + per-mask DINOv2 feature vector
    python src/propagate.py --config config.yaml --site <site> --build-sam-cache
    #    (or featurise an existing polygon set instead of running SAM:)
    python src/propagate.py --config config.yaml --site <site> --build-sam-cache \
        --from-vector data/labels/<site>/geosam_masks.gpkg

    # 3. per species (the interactive step, called by the QGIS Processing script):
    python src/propagate.py --config config.yaml --site <site> \
        --layer data/labels/<site>/<site>.gpkg --species Sphagnum_R \
        --threshold 0.60 --margin 0.05

    # 4. finish: burn every species layer into a class-id GeoTIFF for training
    python src/propagate.py --config config.yaml --site <site> --rasterize

Data layout (per site, created here)
------------------------------------
    data/labels/<site>/
        <site>.gpkg                 ONE polygon layer "labels" for every species
                                     (fields: species, source, score, run_ts).
                                     source is NULL/'seed' for hand-drawn polygons,
                                     'auto' for ones this script added.
        site.json                  {site, repo_root, venv_python} - read by the QGIS script
        _patch_feats.npy  (N, C) f32 L2-normed
        _patch_index.npy  (N, 3) int32  [tile_idx, patch_row, patch_col]
        _patch_xy.npy     (N, 2) f64    map-coord patch centres
        _tiles.json       {stems: [...], patch_grid: [h_p, w_p], crs_wkt, tile_px}
        sam_candidates/
            sam_masks.gpkg           polygons, fid
            sam_feats.npy  (M, C) f32 L2-normed, row i <-> fid i
            sam_meta.json
        prototypes/<species>.npy   (K, C) f32
"""

import os
import sys
import json
import argparse
import datetime as _dt
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
import rasterio.features
import shapely
from shapely import wkb as shapely_wkb
from shapely.geometry import shape as shapely_shape
import yaml
from tqdm import tqdm


# ============================================================
# Config
# ============================================================

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def prop_cfg(cfg: dict) -> dict:
    """propagate: block with defaults filled in."""
    p = dict(cfg.get("propagate") or {})
    p.setdefault("labels_dir", "data/labels")
    p.setdefault("sam", {})
    p.setdefault("prototype", {})
    p.setdefault("match", {})
    p.setdefault("rasterize", {})
    s = p["sam"]
    s.setdefault("backend", "vit_h")
    s.setdefault("checkpoint", None)
    s.setdefault("device", None)   # null = autodetect cuda -> mps -> cpu
    s.setdefault("tile_size", 1024)   # samgeo's own batch tiling window; no overlap knob exposed
    s.setdefault("points_per_side", 32)
    s.setdefault("pred_iou_thresh", 0.86)
    s.setdefault("min_mask_region_area", 400)
    pr = p["prototype"]
    pr.setdefault("topk", 3)
    pr.setdefault("seed_sources", ["seed", "auto", "nesp", None])
    m = p["match"]
    m.setdefault("metric", "cosine")
    m.setdefault("threshold", 0.60)
    m.setdefault("margin", 0.05)
    m.setdefault("dup_iou", 0.30)
    m.setdefault("min_area_m2", 0.05)
    m.setdefault("max_area_m2", 500.0)
    m.setdefault("simplify_tol_m", 0.02)
    p["rasterize"].setdefault("overlap_priority", "score")
    return p


def site_labels_dir(cfg: dict, site: str) -> Path:
    return Path(prop_cfg(cfg)["labels_dir"]) / site


def resolve_ortho(cfg: dict, site: str, ortho: str = None) -> str:
    """Where this site's orthomosaic lives, in order of preference:
    the --ortho argument, the path recorded in site.json by an earlier
    step, then the default <raw_dir>/<site>.tif. Lets the image stay
    wherever it already is instead of being copied into data/raw."""
    if ortho:
        return os.path.abspath(ortho)
    sj = site_labels_dir(cfg, site) / "site.json"
    if sj.exists():
        try:
            rec = json.load(open(sj)).get("ortho")
            if rec and os.path.isfile(rec):
                return rec
        except Exception:
            pass
    return os.path.join(cfg["paths"]["raw_dir"], f"{site}.tif")


# ============================================================
# Feature cache
# ============================================================

def _tile_transform_and_size(tif_path: str):
    with rasterio.open(tif_path) as src:
        return src.transform, src.width, src.height, src.crs


def build_feature_cache(features_dir: str, tiles_dir: str, out_dir: str,
                        rebuild: bool = False) -> dict:
    """
    Stack every data/features/<site>/tile_*.npy (h_p, w_p, C) into one flat array.

    Writes _patch_feats.npy (N, C) L2-normed, _patch_index.npy (N, 3),
    _patch_xy.npy (N, 2), _tiles.json. Resume-safe unless rebuild=True.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    feats_path = out_dir / "_patch_feats.npy"
    idx_path = out_dir / "_patch_index.npy"
    xy_path = out_dir / "_patch_xy.npy"
    tiles_path = out_dir / "_tiles.json"

    if feats_path.exists() and tiles_path.exists() and not rebuild:
        print(f"Feature cache already present: {feats_path}  (use --rebuild to force)")
        return load_feature_cache(out_dir)

    feat_paths = sorted(Path(features_dir).glob("tile_*.npy"))
    if not feat_paths:
        raise ValueError(f"No tile_*.npy features in {features_dir} "
                         f"(run src/features.py --site first)")

    all_feats, all_idx, all_xy = [], [], []
    stems, patch_grid, crs_wkt, tile_px = [], None, None, None

    for tile_idx, fp in enumerate(tqdm(feat_paths, desc="Stacking features")):
        stem = fp.stem
        feat = np.load(str(fp)).astype(np.float32)          # (h_p, w_p, C)
        h_p, w_p, C = feat.shape
        if patch_grid is None:
            patch_grid = [h_p, w_p]

        img_tile = Path(tiles_dir) / f"{stem}.tif"
        if not img_tile.exists():
            raise FileNotFoundError(f"Image tile missing for {stem}: {img_tile}")
        transform, W_px, H_px, crs = _tile_transform_and_size(str(img_tile))
        if crs_wkt is None:
            crs_wkt = crs.to_wkt()
            tile_px = [W_px, H_px]

        # patch-cell (pr, pc) -> pixel centre -> world xy  (nearest-neighbour scheme,
        # same index math as cluster.py:150-152, here forward instead of inverse)
        prc = (np.arange(h_p) + 0.5) * (H_px / h_p)
        pcc = (np.arange(w_p) + 0.5) * (W_px / w_p)
        pcs, prs = np.meshgrid(pcc, prc)                    # (h_p, w_p)
        xs, ys = rasterio.transform.xy(transform, prs.ravel(), pcs.ravel())
        xy = np.column_stack([np.asarray(xs), np.asarray(ys)])  # (h_p*w_p, 2)

        pr_grid, pc_grid = np.meshgrid(np.arange(h_p), np.arange(w_p), indexing="ij")
        idx = np.column_stack([
            np.full(h_p * w_p, tile_idx, dtype=np.int32),
            pr_grid.ravel().astype(np.int32),
            pc_grid.ravel().astype(np.int32),
        ])

        all_feats.append(feat.reshape(-1, C))
        all_idx.append(idx)
        all_xy.append(xy)
        stems.append(stem)

    feats = np.concatenate(all_feats, axis=0).astype(np.float32)
    feats = _l2norm(feats)
    idx = np.concatenate(all_idx, axis=0)
    xy = np.concatenate(all_xy, axis=0)

    np.save(str(feats_path), feats)
    np.save(str(idx_path), idx)
    np.save(str(xy_path), xy)
    with open(tiles_path, "w") as f:
        json.dump({"stems": stems, "patch_grid": patch_grid,
                   "crs_wkt": crs_wkt, "tile_px": tile_px}, f, indent=2)

    print(f"Feature cache: {feats.shape[0]:,} patches x {feats.shape[1]} dim -> {out_dir}")
    return load_feature_cache(out_dir)


def load_feature_cache(labels_dir) -> dict:
    labels_dir = Path(labels_dir)
    with open(labels_dir / "_tiles.json") as f:
        meta = json.load(f)
    feats = np.load(str(labels_dir / "_patch_feats.npy"), mmap_mode="r")
    idx = np.load(str(labels_dir / "_patch_index.npy"))
    xy = np.load(str(labels_dir / "_patch_xy.npy"))
    h_p, w_p = meta["patch_grid"]
    # row of a patch in the flat arrays = tile_idx * (h_p*w_p) + pr*w_p + pc
    row_of = lambda t, pr, pc: t * (h_p * w_p) + pr * w_p + pc
    return {
        "feats": feats, "idx": idx, "xy": xy,
        "stems": meta["stems"], "patch_grid": (h_p, w_p),
        "crs_wkt": meta["crs_wkt"], "tile_px": meta["tile_px"],
        "row_of": row_of,
    }


# ============================================================
# Small numeric helpers
# ============================================================

def _l2norm(a: np.ndarray, axis: int = -1, eps: float = 1e-8) -> np.ndarray:
    n = np.linalg.norm(a, axis=axis, keepdims=True)
    return a / np.maximum(n, eps)


def _patch_transform(tile_transform: Affine, tile_px, patch_grid) -> Affine:
    """world<-patch-cell affine: scale pixel->patch, then tile pixel->world."""
    W_px, H_px = tile_px
    h_p, w_p = patch_grid
    return tile_transform * Affine.scale(W_px / w_p, H_px / h_p)


# ============================================================
# Vector I/O  (osgeo.ogr - GDAL already required, no geopandas dep)
# ============================================================

def _ogr():
    from osgeo import ogr, osr
    ogr.UseExceptions()
    return ogr, osr


def list_layers(gpkg_path: str) -> list:
    ogr, _ = _ogr()
    ds = ogr.Open(str(gpkg_path), 0)
    if ds is None:
        return []
    names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    ds = None
    return names


def read_layer_polygons(gpkg_path: str, layer_name: str) -> list:
    """Returns list of dicts: {fid, geom (shapely), species, source, score}."""
    ogr, _ = _ogr()
    ds = ogr.Open(str(gpkg_path), 0)
    if ds is None:
        return []
    lyr = ds.GetLayerByName(layer_name)
    if lyr is None:
        ds = None
        return []
    fields = {lyr.GetLayerDefn().GetFieldDefn(i).GetName()
              for i in range(lyr.GetLayerDefn().GetFieldCount())}
    out = []
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        shp = shapely_wkb.loads(bytes(g.ExportToWkb()))
        if shp.is_empty:
            continue
        out.append({
            "fid": feat.GetFID(),
            "geom": shp,
            "species": feat.GetField("species") if "species" in fields else None,
            "source": feat.GetField("source") if "source" in fields else None,
            "score": feat.GetField("score") if "score" in fields else None,
        })
    ds = None
    return out


# One layer per species. The layer NAME is the species; the `species` field is
# kept as a redundant copy so a layer stays self-describing if it is exported.
# The Geo-SAM Tool refuses any layer that is missing one of its own fields, so
# every species layer carries both sets.
_GEOSAM_FIELDS = [
    ("lb_name", "str"), ("lb_note", "str"), ("group_ulid", "str"),
    ("N_GM", "int"), ("id", "int"), ("Area", "real"),
    ("N_FG", "int"), ("N_BG", "int"), ("BBox", "str"),
]
_BUNJILVIEW_FIELDS = [
    ("species", "str"), ("source", "str"), ("score", "real"), ("run_ts", "str"),
]
_SPECIES_SCHEMA = _GEOSAM_FIELDS + _BUNJILVIEW_FIELDS


def _ensure_layer(ds, layer_name: str, crs_wkt: str, schema=_SPECIES_SCHEMA):
    """Get-or-create the layer, and backfill any schema field the layer is
    missing (e.g. a layer the user hand-created in QGIS with only 'species')."""
    ogr, osr = _ogr()
    type_map = {"str": ogr.OFTString, "real": ogr.OFTReal, "int": ogr.OFTInteger}
    lyr = ds.GetLayerByName(layer_name)
    if lyr is None:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(crs_wkt)
        lyr = ds.CreateLayer(layer_name, srs, ogr.wkbPolygon)
        for fname, ftype in schema:
            lyr.CreateField(ogr.FieldDefn(fname, type_map[ftype]))
        return lyr
    have = {lyr.GetLayerDefn().GetFieldDefn(i).GetName()
            for i in range(lyr.GetLayerDefn().GetFieldCount())}
    for fname, ftype in schema:
        if fname not in have:
            lyr.CreateField(ogr.FieldDefn(fname, type_map[ftype]))
    return lyr


def species_layers(gpkg_path: str, valid_names=None) -> list:
    """Feature layers of <site>.gpkg that are species layers.

    With one layer per species the layer name IS the species, so this is just
    list_layers() filtered to the names config.yaml knows about. Pass
    valid_names=None to accept every feature layer in the file.
    """
    names = list_layers(gpkg_path)
    if valid_names is None:
        return names
    valid = set(valid_names)
    return [n for n in names if n in valid]


def append_polygons(gpkg_path: str, layer_name: str, geoms: list,
                    attrs: list, crs_wkt: str) -> int:
    """Create the GeoPackage / layer if needed, append geoms with attrs dicts."""
    ogr, _ = _ogr()
    gpkg_path = str(gpkg_path)
    Path(gpkg_path).parent.mkdir(parents=True, exist_ok=True)
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(gpkg_path):
        ds = ogr.Open(gpkg_path, 1)
    else:
        ds = drv.CreateDataSource(gpkg_path)
    lyr = _ensure_layer(ds, layer_name, crs_wkt)
    defn = lyr.GetLayerDefn()
    n = 0
    for geom, a in zip(geoms, attrs):
        if geom.is_empty:
            continue
        feat = ogr.Feature(defn)
        feat.SetGeometry(ogr.CreateGeometryFromWkb(shapely.to_wkb(geom)))
        for k, v in a.items():
            if v is not None:
                feat.SetField(k, v)
        lyr.CreateFeature(feat)
        feat = None
        n += 1
    ds = None
    return n


# ============================================================
# SAM candidate cache
# ============================================================

def _decompress_for_sam(ortho_path: str, out_dir: str) -> str:
    """
    samgeo writes its output mask by copying the SOURCE raster's rasterio
    profile verbatim, including photometric=ycbcr + compress=jpeg on drone
    orthos - then fails writing a 1-band mask with a photometric tag that's
    only legal on 3-band RGB ("PHOTOMETRIC=YCBCR not supported on a 1-band
    raster"). Give SAM a plain deflate-compressed copy instead (same data,
    same CRS/transform) so its own writer never inherits that tag. Cached:
    skipped if the copy already exists.
    """
    with rasterio.open(ortho_path) as src:
        if src.profile.get("photometric") != "ycbcr" and src.profile.get("compress") != "jpeg":
            return ortho_path  # nothing to work around

        out_path = str(Path(out_dir) / "_ortho_rgb.tif")
        if Path(out_path).exists():
            return out_path

        profile = src.profile.copy()
        for k in ("photometric", "compress", "interleave"):
            profile.pop(k, None)
        profile.update({"compress": "deflate"})
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(src.read())
    print(f"Decompressed ortho copy for SAM: {out_path}")
    return out_path


def run_sam_automask(ortho_path: str, out_mask_tif: str, sam_cfg: dict) -> str:
    """
    Run SAM automatic mask generation over the ortho -> a labelled mask GeoTIFF
    (0 = background, 1..K = distinct masks). Lazy import: segment-geospatial is
    only needed for this one step.
    """
    try:
        from samgeo import SamGeo
    except Exception as e:  # pragma: no cover - depends on optional heavy dep
        raise ImportError(
            "segment-geospatial not installed. Either:\n"
            "  pip install segment-geospatial\n"
            "or skip SAM and featurise an existing polygon set:\n"
            "  python src/propagate.py ... --build-sam-cache --from-vector <file.gpkg>"
        ) from e

    import torch
    # segment-anything's SamAutomaticMaskGenerator hard-crashes on mps as of
    # this writing (TypeError: MPS doesn't support float64, in _process_batch's
    # point-grid tensor) - default to cpu on Apple Silicon, not mps. Verified
    # on torch/segment-anything current as of 2026-09. Override with
    # propagate.sam.device if a future version fixes this.
    device = sam_cfg.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"SAM device: {device}")

    Path(out_mask_tif).parent.mkdir(parents=True, exist_ok=True)
    ortho_path = _decompress_for_sam(ortho_path, str(Path(out_mask_tif).parent))
    sam = SamGeo(
        model_type=sam_cfg["backend"],
        device=device,
        checkpoint_dir=sam_cfg.get("checkpoint"),
        sam_kwargs={
            "points_per_side": sam_cfg["points_per_side"],
            "pred_iou_thresh": sam_cfg["pred_iou_thresh"],
            "min_mask_region_area": sam_cfg["min_mask_region_area"],
        },
    )
    sam.generate(
        ortho_path, output=out_mask_tif, foreground=True, unique=True,
        batch=True, batch_sample_size=(sam_cfg["tile_size"], sam_cfg["tile_size"]),
        min_size=sam_cfg["min_mask_region_area"],
    )
    return out_mask_tif


def polygonize_mask(mask_tif: str, min_area_px: int = 400) -> tuple:
    """Labelled mask raster -> (list[shapely Polygon], crs_wkt). One polygon per label."""
    with rasterio.open(mask_tif) as src:
        arr = src.read(1)
        transform = src.transform
        crs_wkt = src.crs.to_wkt()
        px_area = abs(transform.a * transform.e)

    polys = []
    for geom, val in rasterio.features.shapes(arr, mask=(arr > 0), transform=transform):
        if val == 0:
            continue
        shp = shapely_shape(geom)
        if shp.area < min_area_px * px_area:
            continue
        polys.append(shp.buffer(0))
    return polys, crs_wkt


def _tiles_meta(cache: dict, tiles_dir: str) -> list:
    meta = []
    for i, stem in enumerate(cache["stems"]):
        tif = Path(tiles_dir) / f"{stem}.tif"
        transform, W_px, H_px, _ = _tile_transform_and_size(str(tif))
        b = rasterio.transform.array_bounds(H_px, W_px, transform)  # (w,s,e,n)... actually (left,bottom,right,top)
        meta.append({"idx": i, "stem": stem, "transform": transform,
                     "tile_px": [W_px, H_px], "bounds": b})
    return meta


def polygons_to_patch_feats(polys: list, cache: dict, tiles_meta: list) -> np.ndarray:
    """
    For each polygon, gather feature vectors of every patch cell it covers, across
    all overlapping tiles. Returns (P, C) (NOT averaged) - unnormalised rows come
    straight from the (already L2-normed) cache.
    """
    from shapely.geometry import box as shapely_box
    h_p, w_p = cache["patch_grid"]
    row_of = cache["row_of"]
    feats = cache["feats"]
    rows = []
    for poly in polys:
        pminx, pminy, pmaxx, pmaxy = poly.bounds
        for tm in tiles_meta:
            left, bottom, right, top = tm["bounds"]
            if pmaxx < left or pminx > right or pmaxy < bottom or pminy > top:
                continue
            ptrans = _patch_transform(tm["transform"], tm["tile_px"], (h_p, w_p))
            covered = rasterio.features.rasterize(
                [(poly, 1)], out_shape=(h_p, w_p), transform=ptrans,
                fill=0, all_touched=True, dtype="uint8",
            )
            pr_idx, pc_idx = np.nonzero(covered)
            for pr, pc in zip(pr_idx.tolist(), pc_idx.tolist()):
                rows.append(row_of(tm["idx"], pr, pc))
    if not rows:
        return np.empty((0, feats.shape[1]), dtype=np.float32)
    rows = np.unique(np.asarray(rows, dtype=np.int64))
    return np.asarray(feats[rows], dtype=np.float32)


def featurize_polygons(polys: list, cache: dict, tiles_meta: list) -> np.ndarray:
    """One L2-normed mean feature vector per polygon (M, C). Zero vector if no cover."""
    C = cache["feats"].shape[1]
    out = np.zeros((len(polys), C), dtype=np.float32)
    for m, poly in enumerate(tqdm(polys, desc="Featurising masks")):
        pf = polygons_to_patch_feats([poly], cache, tiles_meta)
        if len(pf):
            out[m] = pf.mean(axis=0)
    return _l2norm(out)


def build_sam_cache(cfg: dict, site: str, from_vector: str = None,
                    rebuild: bool = False, ortho: str = None) -> dict:
    p = prop_cfg(cfg)
    labels_dir = site_labels_dir(cfg, site)
    cand_dir = labels_dir / "sam_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    masks_gpkg = cand_dir / "sam_masks.gpkg"
    feats_npy = cand_dir / "sam_feats.npy"
    meta_json = cand_dir / "sam_meta.json"

    if feats_npy.exists() and masks_gpkg.exists() and not rebuild:
        print(f"SAM cache already present: {cand_dir}  (use --rebuild to force)")
        return {"masks_gpkg": str(masks_gpkg), "feats_npy": str(feats_npy)}

    cache = load_feature_cache(labels_dir)
    tiles_dir = os.path.join(cfg["paths"]["tiles_dir"], site, "images")
    tiles_meta = _tiles_meta(cache, tiles_dir)

    ortho_path = resolve_ortho(cfg, site, ortho)

    if from_vector:
        recs = []
        for lname in list_layers(from_vector):
            recs += read_layer_polygons(from_vector, lname)
        polys = [r["geom"] for r in recs]
        crs_wkt = cache["crs_wkt"]
        print(f"Featurising {len(polys)} polygons from {from_vector}")
    else:
        mask_tif = str(cand_dir / "sam_mask.tif")
        if Path(mask_tif).exists() and not rebuild:
            print(f"Reusing SAM mask from an earlier run: {mask_tif}  (use --rebuild to rerun SAM)")
        else:
            # Write under a temp name and rename only once SAM has finished, so an
            # interrupted run never leaves a half-written mask that gets reused.
            tmp_tif = str(cand_dir / "sam_mask.partial.tif")
            run_sam_automask(ortho_path, tmp_tif, p["sam"])
            os.replace(tmp_tif, mask_tif)
        polys, crs_wkt = polygonize_mask(mask_tif, p["sam"]["min_mask_region_area"])
        print(f"SAM produced {len(polys)} candidate masks")
        # Candidates outside match.min/max_area_m2 can never be accepted later,
        # so drop them now: one image-sized mask alone cost 2h20 of featurising.
        lo, hi = p["match"]["min_area_m2"], p["match"]["max_area_m2"]
        kept = [poly for poly in polys if lo <= poly.area <= hi]
        if len(kept) != len(polys):
            print(f"dropped {len(polys) - len(kept)} candidate(s) outside "
                  f"{lo}-{hi} m2 before featurising")
        polys = kept

    if not polys:
        raise ValueError("No candidate polygons to cache.")

    sam_feats = featurize_polygons(polys, cache, tiles_meta)

    # keep only masks that actually landed on computed feature patches
    keep = np.linalg.norm(sam_feats, axis=1) > 1e-6
    polys = [poly for poly, k in zip(polys, keep) if k]
    sam_feats = sam_feats[keep]

    if masks_gpkg.exists():
        masks_gpkg.unlink()
    n = append_polygons(
        masks_gpkg, "sam_masks", polys,
        [{"species": None, "source": "sam", "score": None,
          "run_ts": _now()} for _ in polys],
        crs_wkt,
    )
    np.save(str(feats_npy), sam_feats.astype(np.float32))
    with open(meta_json, "w") as f:
        json.dump({"n_masks": n, "from_vector": from_vector,
                   "areas_m2": [float(poly.area) for poly in polys],
                   "built": _now()}, f, indent=2)

    _write_site_json(cfg, site, ortho=ortho_path)
    print(f"SAM cache: {n} masks x {sam_feats.shape[1]} dim -> {cand_dir}")
    return {"masks_gpkg": str(masks_gpkg), "feats_npy": str(feats_npy)}


# ============================================================
# Prototype + matching
# ============================================================

def build_prototype(patch_feats: np.ndarray, topk: int = 3) -> np.ndarray:
    """(P, C) covered-patch vectors -> (K, C) L2-normed prototype centroids."""
    if len(patch_feats) == 0:
        raise ValueError("Seed polygons cover no feature patches - draw a bigger seed.")
    if len(patch_feats) <= topk or topk <= 1:
        proto = patch_feats.mean(axis=0, keepdims=True) if topk <= 1 else patch_feats
        return _l2norm(np.asarray(proto, dtype=np.float32))
    from sklearn.cluster import MiniBatchKMeans
    km = MiniBatchKMeans(n_clusters=topk, random_state=42, n_init=10,
                         batch_size=min(4096, len(patch_feats)))
    km.fit(patch_feats)
    return _l2norm(km.cluster_centers_.astype(np.float32))


def max_cosine(feats: np.ndarray, proto: np.ndarray) -> np.ndarray:
    """feats (M, C) L2-normed, proto (K, C) L2-normed -> (M,) max cosine over K."""
    if proto is None or len(proto) == 0:
        return np.zeros(len(feats), dtype=np.float32)
    return (feats @ proto.T).max(axis=1)


def _iou(a, b) -> float:
    if not a.intersects(b):
        return 0.0
    inter = a.intersection(b).area
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def propagate_species(cfg: dict, site: str, gpkg_path: str, species: str,
                      threshold: float = None, margin: float = None) -> dict:
    p = prop_cfg(cfg)
    m = p["match"]
    threshold = m["threshold"] if threshold is None else threshold
    margin = m["margin"] if margin is None else margin

    labels_dir = site_labels_dir(cfg, site)
    cache = load_feature_cache(labels_dir)
    tiles_dir = os.path.join(cfg["paths"]["tiles_dir"], site, "images")
    tiles_meta = _tiles_meta(cache, tiles_dir)
    crs_wkt = cache["crs_wkt"]

    cand_dir = labels_dir / "sam_candidates"
    sam_feats = np.load(str(cand_dir / "sam_feats.npy")).astype(np.float32)
    sam_recs = read_layer_polygons(str(cand_dir / "sam_masks.gpkg"), "sam_masks")
    sam_polys = [r["geom"] for r in sam_recs]
    if len(sam_polys) != len(sam_feats):
        raise ValueError(f"SAM cache mismatch: {len(sam_polys)} polys vs "
                         f"{len(sam_feats)} feats - rebuild --build-sam-cache")

    # --- one layer per species: the layer NAME is the species, so the seeds
    #     are simply the polygons of that layer whose `source` is allowed ---
    seed_sources = set(p["prototype"]["seed_sources"])
    species_recs = read_layer_polygons(gpkg_path, species)
    # `source` is NULL on a polygon Geo-SAM drew, but an empty string also shows
    # up (edited by hand, exported and re-imported). Treat both as "hand-drawn".
    seeds = [r["geom"] for r in species_recs
             if (r["source"] or None) in seed_sources]
    if not seeds:
        raise ValueError(
            f"Layer '{species}' of {gpkg_path} has no polygon to learn from. "
            f"Draw a seed into that layer with Geo-SAM first "
            f"(the layer name is the species, there is no field to fill in)."
        )

    # The overlap check below must see every polygon already drawn, of every
    # species, so read all the species layers, not just this one.
    all_recs = []
    for _lyr in species_layers(gpkg_path, cfg.get("classes", {}).values()):
        all_recs.extend(read_layer_polygons(gpkg_path, _lyr))

    seed_patch_feats = polygons_to_patch_feats(seeds, cache, tiles_meta)
    proto_x = build_prototype(seed_patch_feats, p["prototype"]["topk"])
    (labels_dir / "prototypes").mkdir(parents=True, exist_ok=True)
    np.save(str(labels_dir / "prototypes" / f"{species}.npy"), proto_x)

    # --- one-vs-rest: prototypes of every OTHER species already saved ---
    rest = []
    for pf in sorted((labels_dir / "prototypes").glob("*.npy")):
        if pf.stem == species:
            continue
        rest.append(np.load(str(pf)).astype(np.float32))
    proto_rest = np.concatenate(rest, axis=0) if rest else None

    sim_x = max_cosine(sam_feats, proto_x)
    sim_rest = max_cosine(sam_feats, proto_rest)
    keep = (sim_x >= threshold) & ((sim_x - sim_rest) >= margin)

    # --- drop candidates overlapping any existing polygon (any species) ---
    existing = [r["geom"] for r in all_recs]
    dup_iou = m["dup_iou"]
    tree = shapely.STRtree(existing) if existing else None

    add_geoms, add_attrs = [], []
    n_dup = n_area = 0
    ts = _now()
    for i in np.nonzero(keep)[0]:
        poly = sam_polys[int(i)].buffer(0)
        if not (m["min_area_m2"] <= poly.area <= m["max_area_m2"]):
            n_area += 1
            continue
        if tree is not None and any(
            _iou(poly, existing[j]) > dup_iou for j in tree.query(poly)
        ):
            n_dup += 1
            continue
        poly = poly.simplify(m["simplify_tol_m"], preserve_topology=True)
        add_geoms.append(poly)
        add_attrs.append({"species": species, "source": "auto",
                          "score": float(sim_x[int(i)]), "run_ts": ts})

    n_added = append_polygons(gpkg_path, species, add_geoms, add_attrs, crs_wkt)

    hist = np.histogram(sim_x, bins=10, range=(0.0, 1.0))[0].tolist()
    print(f"\nspecies={species}  threshold={threshold:.2f}  margin={margin:.2f}")
    print(f"  seeds:              {len(seeds)}  ({len(seed_patch_feats)} patches, "
          f"{len(proto_x)} prototype centroids)")
    print(f"  other species:      {len(rest)}")
    print(f"  candidates scored:  {len(sam_feats)}")
    print(f"  passed threshold:   {int(keep.sum())}")
    print(f"  skipped (overlap):  {n_dup}")
    print(f"  skipped (area):     {n_area}")
    print(f"  ADDED:              {n_added}  -> layer '{species}' of {gpkg_path}")
    print(f"  sim_X histogram 0..1: {hist}")
    return {"added": n_added, "scored": len(sam_feats), "passed": int(keep.sum())}


# ============================================================
# Rasterise all species layers -> class-id GeoTIFF
# ============================================================

def rasterize_labels(cfg: dict, site: str, gpkg_path: str, out_tif: str,
                     overlap_priority: str = "score", ortho: str = None) -> str:
    classes = cfg["classes"]                      # {id: name}
    name_to_id = {v: int(k) for k, v in classes.items()}
    ortho_path = resolve_ortho(cfg, site, ortho)

    with rasterio.open(ortho_path) as src:
        H, W = src.height, src.width
        transform = src.transform
        profile = src.profile.copy()
        ref = src.read(1)
        nodata = src.nodata

    label = np.full((H, W), 255, dtype=np.uint8)
    prio = np.full((H, W), -np.inf, dtype=np.float32)

    # One layer per species: the layer name is the class name.
    by_species = {}
    for _lyr in species_layers(gpkg_path, name_to_id):
        recs = read_layer_polygons(gpkg_path, _lyr)
        if recs:
            by_species[_lyr] = recs
    if not by_species:
        raise ValueError(
            f"No polygons in any species layer of {gpkg_path}. Expected one layer "
            f"per species, named after the config classes {sorted(name_to_id)}.")

    ignored = [n for n in list_layers(gpkg_path) if n not in name_to_id]
    if ignored:
        print(f"  ({len(ignored)} layer(s) ignored, name is not a config class: "
              f"{ignored})")
    empty = [n for n in name_to_id if n in list_layers(gpkg_path)
             and n not in by_species]
    if empty:
        print(f"  ({len(empty)} species layer(s) still empty: {empty})")

    species_list = sorted(by_species, key=lambda sp: name_to_id[sp]) \
        if overlap_priority == "class_order" else list(by_species)

    def _burn(geoms, cid, w):
        """One rasterize call for a whole priority band; higher w wins ties by >."""
        if not geoms:
            return
        m = rasterio.features.rasterize(
            [(g, 1) for g in geoms], out_shape=(H, W), transform=transform,
            fill=0, all_touched=False, dtype="uint8",
        ).astype(bool)
        take = m & (w > prio)
        label[take] = cid
        prio[take] = w

    for sp in tqdm(species_list, desc="burn species"):
        cid = name_to_id[sp]
        recs = by_species[sp]
        if overlap_priority == "class_order":
            _burn([r["geom"] for r in recs], cid, float(cid))
        else:  # "score": auto polys first, hand-drawn / kept seeds always on top
            auto = [r["geom"] for r in recs if r["source"] not in (None, "seed")]
            manual = [r["geom"] for r in recs if r["source"] in (None, "seed")]
            _burn(auto, cid, 1.0)
            _burn(manual, cid, 2.0)

    if nodata is not None:
        label[ref == nodata] = 255

    for k in ("photometric", "compress", "interleave"):
        profile.pop(k, None)
    profile.update({"count": 1, "dtype": "uint8", "nodata": 255, "compress": "deflate"})
    Path(out_tif).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_tif, "w", **profile) as dst:
        dst.write(label[np.newaxis])

    vals, counts = np.unique(label[label != 255], return_counts=True)
    total = counts.sum() or 1
    print(f"\nLabel raster: {out_tif}  ({W}x{H})")
    for v, c in zip(vals.tolist(), counts.tolist()):
        print(f"  class {v:2d} {classes.get(v, '?'):<24} {c:>12,} px  ({c/total*100:.1f}%)")
    return out_tif


# ============================================================
# misc
# ============================================================

def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _write_site_json(cfg: dict, site: str, ortho: str = None) -> None:
    labels_dir = site_labels_dir(cfg, site)
    labels_dir.mkdir(parents=True, exist_ok=True)
    sj = labels_dir / "site.json"
    info = {}
    if sj.exists():                     # keep keys earlier steps recorded
        try:
            info = json.load(open(sj))
        except Exception:
            info = {}
    info.update({
        "site": site,
        "repo_root": str(Path.cwd()),
        "venv_python": sys.executable,
        "gpkg": str(labels_dir / f"{site}.gpkg"),
    })
    if ortho:
        info["ortho"] = os.path.abspath(ortho)
    with open(sj, "w") as f:
        json.dump(info, f, indent=2)


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="Interactive species propagation for QGIS labelling")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--build-feature-cache", action="store_true")
    ap.add_argument("--build-sam-cache", action="store_true")
    ap.add_argument("--from-vector", default=None,
                    help="With --build-sam-cache: featurise this polygon file instead of running SAM")
    ap.add_argument("--rebuild", action="store_true", help="Force cache rebuild")
    ap.add_argument("--ortho", default=None,
                    help="Orthomosaic path, if not at <raw_dir>/<site>.tif. Recorded in "
                         "site.json, so later steps and the QGIS session find it too")
    ap.add_argument("--species", default=None,
                    help="Species to propagate (defaults to the layer name)")
    ap.add_argument("--layer", default=None, help="Path to <site>.gpkg (default: data/labels/<site>/<site>.gpkg)")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--margin", type=float, default=None)
    ap.add_argument("--rasterize", action="store_true")
    ap.add_argument("--out", default=None, help="With --rasterize: output GeoTIFF path")
    args = ap.parse_args()

    cfg = load_config(args.config)
    labels_dir = site_labels_dir(cfg, args.site)
    gpkg = args.layer or str(labels_dir / f"{args.site}.gpkg")

    did = False

    if args.build_feature_cache:
        build_feature_cache(
            features_dir=os.path.join(cfg["paths"]["features_dir"], args.site),
            tiles_dir=os.path.join(cfg["paths"]["tiles_dir"], args.site, "images"),
            out_dir=str(labels_dir),
            rebuild=args.rebuild,
        )
        _write_site_json(cfg, args.site, ortho=args.ortho)
        did = True

    if args.build_sam_cache:
        build_sam_cache(cfg, args.site, from_vector=args.from_vector, rebuild=args.rebuild,
                        ortho=args.ortho)
        did = True

    if args.species:
        propagate_species(cfg, args.site, gpkg, args.species,
                          threshold=args.threshold, margin=args.margin)
        did = True

    if args.rasterize:
        out = args.out or os.path.join(cfg["paths"]["pseudo_dir"], args.site,
                                       f"{args.site}_labels.tif")
        rasterize_labels(cfg, args.site, gpkg, out,
                         overlap_priority=prop_cfg(cfg)["rasterize"]["overlap_priority"],
                         ortho=args.ortho)
        did = True

    if not did:
        ap.error("nothing to do: pass one of --build-feature-cache / --build-sam-cache "
                 "/ --species NAME / --rasterize")


if __name__ == "__main__":
    main()
