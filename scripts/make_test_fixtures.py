"""
make_test_fixtures.py
---------------------
Exercise src/propagate.py WITHOUT segment-geospatial.

Builds, from the existing k-means cluster maps of a site:
  data/labels/<site>/_test_candidates.gpkg  - one polygon per connected cluster
        region (stands in for the SAM auto-mask library; feed via --from-vector)
  data/labels/<site>/_test_seeds.gpkg       - ONE "labels" layer (matches the
        real data/labels/<site>/<site>.gpkg schema) with one seed polygon per
        (species, cluster) pair below - the biggest region of that cluster,
        species set as an attribute, not a separate layer. Species names come
        from data/clusters/<site>/cluster_class_mapping.yaml so one-vs-rest has
        something real to separate.

Usage:
    python scripts/make_test_fixtures.py --site test1
    # then see docs/4_interactive_propagation.md "Cach test" / the module header.

These files are throwaway test data - delete data/labels/<site>/ when done.
"""

import argparse
import glob
import os

import numpy as np
import rasterio
import rasterio.features
import yaml
from shapely.geometry import shape
from shapely import to_wkb
from osgeo import ogr, osr

# (species-name, cluster-id) seed layers to create. Cluster ids are the test1
# k-means run; adjust if you re-clustered.
SEEDS = [
    ("Sphagnum_Y", 13),
    ("Carex_gaudichaudiana", 18),
    ("Poa_costiniana", 2),
]
MIN_REGION_PX = 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--min-px", type=int, default=MIN_REGION_PX)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    clusters_dir = os.path.join(cfg["paths"]["clusters_dir"], args.site)
    labels_dir = os.path.join(cfg["propagate"]["labels_dir"], args.site)
    os.makedirs(labels_dir, exist_ok=True)

    tifs = sorted(glob.glob(f"{clusters_dir}/tile_*.tif"))
    if not tifs:
        raise SystemExit(f"No cluster tiles in {clusters_dir} - run src/cluster.py first")

    crs_wkt = None
    by_cluster = {}          # cluster_id -> list[shapely Polygon]
    for tp in tifs:
        with rasterio.open(tp) as src:
            arr = src.read(1).astype(np.int32)
            tr = src.transform
            if crs_wkt is None:
                crs_wkt = src.crs.to_wkt()
            px_area = abs(tr.a * tr.e)
        for geom, val in rasterio.features.shapes(arr, mask=(arr >= 0), transform=tr):
            cid = int(val)
            if cid < 0:
                continue
            shp = shape(geom).buffer(0)
            if shp.area < args.min_px * px_area:
                continue
            by_cluster.setdefault(cid, []).append(shp)

    n_cand = sum(len(v) for v in by_cluster.values())
    print(f"{n_cand} candidate polygons across {len(by_cluster)} clusters")

    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs_wkt)
    drv = ogr.GetDriverByName("GPKG")

    def new_ds(path):
        if os.path.exists(path):
            os.remove(path)
        return drv.CreateDataSource(path)

    def add_layer(ds, name, rows, source):
        """rows: list of (shapely geom, species-or-None)."""
        lyr = ds.CreateLayer(name, srs, ogr.wkbPolygon)
        for fn in ("species", "source", "run_ts"):
            lyr.CreateField(ogr.FieldDefn(fn, ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("score", ogr.OFTReal))
        defn = lyr.GetLayerDefn()
        for shp, sp in rows:
            f = ogr.Feature(defn)
            f.SetGeometry(ogr.CreateGeometryFromWkb(to_wkb(shp)))
            f.SetField("source", source)
            if sp is not None:
                f.SetField("species", sp)
            lyr.CreateFeature(f)
            f = None

    cand_path = os.path.join(labels_dir, "_test_candidates.gpkg")
    ds = new_ds(cand_path)
    all_polys = [p for v in by_cluster.values() for p in v]
    add_layer(ds, "cand", [(p, None) for p in all_polys], "sam")
    ds = None
    print(f"wrote {cand_path}  ({len(all_polys)} polys, 1 layer 'cand')")

    # ONE "labels" layer, species as an attribute - matches the real <site>.gpkg
    seeds_path = os.path.join(labels_dir, "_test_seeds.gpkg")
    ds = new_ds(seeds_path)
    seed_rows = []
    for sp, cid in SEEDS:
        polys = by_cluster.get(cid, [])
        if not polys:
            print(f"  ! cluster {cid} empty - skipping {sp}")
            continue
        biggest = max(polys, key=lambda g: g.area)
        seed_rows.append((biggest, sp))
        print(f"  species={sp:<24} <- cluster {cid}  (seed area {biggest.area:.0f} m2)")
    add_layer(ds, "labels", seed_rows, "seed")
    ds = None
    print(f"wrote {seeds_path}  (layer 'labels', {len(seed_rows)} seed polygons)")

    print("\nNext:")
    print(f"  python src/propagate.py --config {args.config} --site {args.site} --build-feature-cache")
    print(f"  python src/propagate.py --config {args.config} --site {args.site} "
          f"--build-sam-cache --from-vector {cand_path}")
    for sp, _ in SEEDS:
        print(f"  python src/propagate.py --config {args.config} --site {args.site} "
              f"--layer {seeds_path} --species '{sp}'")
    print(f"  python src/propagate.py --config {args.config} --site {args.site} "
          f"--layer {seeds_path} --rasterize")


if __name__ == "__main__":
    main()
