"""
init_labels_gpkg.py
-------------------
Create data/labels/<site>/<site>.gpkg with ONE LAYER PER SPECIES.

    python scripts/init_labels_gpkg.py --config config.yaml --site <site>

The layer name IS the species, taken from config.yaml `classes` (minus `None`).
There is no `species` field to fill in when drawing: pick the layer for the
species you are working on, draw, done.

Every layer carries BOTH field sets:
  Geo-SAM   : lb_name, lb_note, group_ulid, N_GM, id, Area, N_FG, N_BG, BBox
  Bunjilview: species, source, score, run_ts

Miss any Geo-SAM field and the plugin rejects the layer with "The fields of
this vector do not match the SAM feature fields". Creating a layer by hand in
QGIS only gives you the fields you type, so do not do that.

Safe to re-run: it refuses to touch a file that already holds polygons unless
you pass --force.
"""
import argparse
import os
import sqlite3
import sys

import geopandas as gpd
import rasterio
import yaml
from shapely.geometry import Polygon

GEOSAM_FIELDS = {
    "lb_name": "", "lb_note": "", "group_ulid": "",
    "N_GM": 0, "id": 0, "Area": 0.0, "N_FG": 0, "N_BG": 0, "BBox": "",
}
BUNJIL_FIELDS = {"species": "", "source": "", "score": 0.0, "run_ts": ""}


def species_names(cfg):
    return [v for _, v in sorted(cfg["classes"].items(), key=lambda kv: int(kv[0]))
            if v and v != "None"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--ortho", default=None, help="defaults to <raw_dir>/<site>.tif")
    ap.add_argument("--force", action="store_true",
                    help="recreate even if layers already hold polygons (DELETES them)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    ortho = args.ortho or os.path.join(cfg["paths"]["raw_dir"], args.site + ".tif")
    if not os.path.isfile(ortho):
        sys.exit("orthomosaic not found: %s" % ortho)

    names = species_names(cfg)
    if not names:
        sys.exit("config.yaml has no species under `classes`")

    out_dir = os.path.join("data/labels", args.site)
    os.makedirs(out_dir, exist_ok=True)
    gpkg = os.path.join(out_dir, args.site + ".gpkg")

    with rasterio.open(ortho) as src:
        crs, b = src.crs, src.bounds
    print("ortho : %s" % ortho)
    print("crs   : %s" % crs)
    print("species: %d (%s)" % (len(names), ", ".join(names)))

    if os.path.exists(gpkg) and not args.force:
        total = 0
        try:
            con = sqlite3.connect(gpkg)
            for n in names:
                try:
                    total += con.execute('SELECT COUNT(*) FROM "%s"' % n).fetchone()[0]
                except sqlite3.Error:
                    pass
            con.close()
        except sqlite3.Error:
            pass
        if total:
            sys.exit("%s already holds %d polygon(s). Pass --force to recreate "
                     "(this DELETES them)." % (gpkg, total))

    # geopandas cannot write an empty layer, so write one throwaway row
    # to fix the schema and delete it afterwards.
    dummy = Polygon([(b.left, b.bottom), (b.left, b.bottom + 1e-6),
                     (b.left + 1e-6, b.bottom + 1e-6), (b.left + 1e-6, b.bottom)])
    attrs = {**GEOSAM_FIELDS, **BUNJIL_FIELDS}

    print()
    for name in names:
        row = dict(attrs)
        row["species"] = name      # redundant copy, handy if a layer is exported alone
        gdf = gpd.GeoDataFrame({k: [v] for k, v in row.items()},
                               geometry=[dummy], crs=crs)
        gdf.to_file(gpkg, layer=name, driver="GPKG",
                    mode="w" if name == names[0] and not os.path.exists(gpkg) else "a")
        print("  created layer %s" % name)

    con = sqlite3.connect(gpkg)
    for name in names:
        con.execute('DELETE FROM "%s"' % name)
    con.commit()

    print("\ncreated: %s" % gpkg)
    bad = []
    for name in names:
        cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % name)]
        n = con.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
        missing = [f for f in list(GEOSAM_FIELDS) + list(BUNJIL_FIELDS)
                   if f not in cols]
        if missing:
            bad.append((name, missing))
        print("  %-26s %d features, %d fields%s"
              % (name, n, len(cols), "" if not missing else "  MISSING %r" % missing))
    con.close()

    if bad:
        sys.exit("\nFAILED, some layers are missing fields: %r" % bad)
    print("\nok - Geo-SAM will accept every one of these layers")
    print("Next: open QGIS and run qgis/setup_geosam_session.py with SPECIES "
          "set to the species you want to draw.")


if __name__ == "__main__":
    main()
