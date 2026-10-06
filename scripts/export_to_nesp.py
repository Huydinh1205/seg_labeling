"""
export_to_nesp.py
-----------------
Hand the polygons made in this pipeline (Geo-SAM seeds you drew, polygons
propagation added) back to the company's "NESP Labelling" layers, in their
format: their species spelling, a fresh unique `id`, empty Review/Comments.

    # into the merged layer
    python scripts/export_to_nesp.py --config config.yaml --site <site> \
        --to "Z:\\NESP\\all_species_fixed.shp"

    # into one per-species file (only that species is exported)
    python scripts/export_to_nesp.py --config config.yaml --site <site> \
        --to "Z:\\NESP\\E. minus.shp"

    # into a layer of a GeoPackage, or a brand-new file
    python scripts/export_to_nesp.py ... --to "labels.gpkg|layername=all_species"
    python scripts/export_to_nesp.py ... --to "BettsNorth_ai.shp"

Rules, so nothing comes back twice and nothing of theirs is overwritten:
  * polygons that came FROM the project (source='nesp', see import_seeds.py)
    are never exported back;
  * every export is logged in data/labels/<site>/nesp_exports.json, so a
    second run only sends what is new since (--all to ignore the log);
  * on top of that, a polygon overlapping an existing target polygon above
    --dedup-iou (default 0.9) is skipped;
  * the target file is backed up first (<name>_backup_<timestamp>.*) unless
    --no-backup; the target's own polygons are only ever appended to;
  * `id` continues from the largest id already in the target; `Species`
    (or whatever column holds their names) gets their spelling; a `layer`
    column, if present, gets the same; everything else stays NULL.

Geometries are reprojected to the target's CRS. Run with --dry-run first.
"""
import argparse
import datetime as _dt
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import shapely                                              # noqa: E402

import nesp                                                 # noqa: E402
from propagate import load_config, site_labels_dir, list_layers  # noqa: E402

SPECIES_FIELD_CANDIDATES = ["Species", "species", "SPECIES", "layer", "name", "class"]


def class_names(cfg):
    return [v for _, v in sorted(cfg["classes"].items(), key=lambda kv: int(kv[0]))
            if v and v != "None"]


def load_log(path):
    if path.exists():
        try:
            return json.load(open(path))
        except Exception:
            return {}
    return {}


def read_site_polygons(gpkg, layers, dst_srs):
    """[(layer, fid, geom_in_target_crs, source)] for every species layer."""
    out = []
    ds = nesp.open_vector(gpkg)
    for name in layers:
        lyr = ds.GetLayerByName(name)
        if lyr is None:
            continue
        for r in nesp.read_polygons(lyr, dst_srs=dst_srs):
            out.append((name, r["fid"], r["geom"], (r["attrs"].get("source") or None)))
    ds = None
    return out


def create_target(path, layer_name, srs, species_field="Species"):
    """A fresh Shapefile / GeoPackage with the project's four columns."""
    ogr, _ = nesp._ogr()
    ext = Path(path).suffix.lower()
    drv_name = "GPKG" if ext == ".gpkg" else "ESRI Shapefile"
    drv = ogr.GetDriverByName(drv_name)
    if os.path.exists(path):
        ds = ogr.Open(path, 1)
    else:
        ds = drv.CreateDataSource(path)
    lyr = ds.CreateLayer(layer_name or Path(path).stem, srs, ogr.wkbPolygon)
    for fname, ftype, width in (("id", ogr.OFTInteger, 0), ("Review", ogr.OFTString, 254),
                                ("Comments", ogr.OFTString, 254), (species_field, ogr.OFTString, 80)):
        fd = ogr.FieldDefn(fname, ftype)
        if width:
            fd.SetWidth(width)
        lyr.CreateField(fd)
    return ds, lyr


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--to", required=True,
                    help="target shapefile / gpkg ('file.gpkg|layername=x'); created if missing")
    ap.add_argument("--species", default=None,
                    help="export only this class (default: all; a per-species target implies it)")
    ap.add_argument("--species-field", default=None,
                    help="target column for the species name (default: detected, or 'Species')")
    ap.add_argument("--id-field", default="id")
    ap.add_argument("--map", nargs="*", default=[],
                    help="spelling overrides, e.g. --map \"Snow grass=Poa_costiniana\"")
    ap.add_argument("--include-nesp", action="store_true",
                    help="also export polygons that were imported from the project (normally never)")
    ap.add_argument("--all", action="store_true", help="ignore the export log, consider every polygon")
    ap.add_argument("--dedup-iou", type=float, default=0.9,
                    help="skip a polygon overlapping an existing target polygon above this IoU")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--allow-truncate", action="store_true",
                    help="write names longer than a Shapefile column allows (they get cut)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    classes = class_names(cfg)
    namemap = nesp.NameMap(classes, args.map)

    labels_dir = site_labels_dir(cfg, args.site)
    gpkg = labels_dir / f"{args.site}.gpkg"
    if not gpkg.exists():
        sys.exit(f"{gpkg} does not exist, nothing to export.")
    site_layers = [n for n in list_layers(str(gpkg)) if n in classes]

    target_path, target_layer = nesp.split_source(args.to)
    target_key = os.path.abspath(target_path) + (f"|{target_layer}" if target_layer else "")
    log_path = labels_dir / "nesp_exports.json"
    log = load_log(log_path)
    already = {(e[0], e[1]) for e in log.get(target_key, {}).get("exported", [])}

    # ---- open or create the target -------------------------------------
    ogr, osr = nesp._ogr()
    creating = not os.path.exists(target_path)
    if not creating:
        ds = nesp.open_vector(target_path, update=not args.dry_run)
        lname = target_layer or (nesp.feature_layers(ds) or [None])[0]
        lyr = ds.GetLayerByName(lname) if lname else None
        if lyr is None and target_layer and Path(target_path).suffix.lower() == ".gpkg":
            creating = True            # gpkg exists, this layer does not
            ds = None
        elif lyr is None:
            sys.exit(f"{target_path}: no polygon layer found")
    if creating:
        # CRS of a new target = the site's CRS (from the gpkg's first layer)
        sds = nesp.open_vector(str(gpkg))
        site_srs = nesp.srs_of(sds.GetLayerByName(site_layers[0]))
        sds = None
        if args.dry_run:
            print(f"--dry-run: would create {args.to}")
            ds, lyr = None, None
        else:
            ds, lyr = create_target(target_path, target_layer, site_srs, args.species_field or "Species")
        fields = ["id", "Review", "Comments", args.species_field or "Species"]
        target_srs = site_srs
        existing = []
        existing_names = []
        width = {}
    else:
        fields = nesp.layer_fields(lyr)
        target_srs = nesp.srs_of(lyr)
        defn = lyr.GetLayerDefn()
        width = {defn.GetFieldDefn(i).GetName(): defn.GetFieldDefn(i).GetWidth()
                 for i in range(defn.GetFieldCount())}
        existing = nesp.read_polygons(lyr)
        existing_names = []

    # which column holds their species names
    if args.species_field:
        sp_field = args.species_field
        if sp_field not in fields:
            sys.exit(f"--species-field {sp_field!r} not in target fields {fields}")
    else:
        sp_field = None
        if existing:
            best, best_n = nesp.guess_species_field(existing, fields, namemap)
            if best_n > 0:
                sp_field = best
        if sp_field is None:
            sp_field = next((f for f in SPECIES_FIELD_CANDIDATES if f in fields), None)
        if sp_field is None:
            sys.exit(f"cannot tell which target column holds species names; fields are {fields}. "
                     f"Pass --species-field.")
    existing_names = [r["attrs"].get(sp_field) for r in existing if r["attrs"].get(sp_field)]
    namemap.learn_their_spelling(existing_names)

    # a per-species target ("E. minus.shp") only takes that species
    only = args.species
    if only is None:
        guess = namemap.to_class(target_layer or Path(target_path).stem)
        if guess:
            only = guess
            print(f"target looks like a per-species file -> exporting only {only}")
    if only and only not in classes:
        sys.exit(f"--species {only!r} is not a class in config.yaml ({classes})")

    # next id
    next_id = None
    if args.id_field in fields:
        ids = [r["attrs"].get(args.id_field) for r in existing]
        ids = [int(i) for i in ids if i is not None]
        next_id = (max(ids) + 1) if ids else 1

    # ---- what to send ---------------------------------------------------
    recs = read_site_polygons(str(gpkg), site_layers, target_srs)
    tree = shapely.STRtree([r["geom"] for r in existing]) if existing else None
    ex_geoms = [r["geom"] for r in existing]

    report = OrderedDict((c, {"theirs": namemap.to_theirs(c), "have": 0, "nesp": 0,
                              "logged": 0, "overlap": 0, "send": 0}) for c in site_layers)
    to_send = []
    for layer, fid, geom, source in recs:
        row = report[layer]
        row["have"] += 1
        if only and layer != only:
            continue
        if source == "nesp" and not args.include_nesp:
            row["nesp"] += 1
            continue
        if not args.all and (layer, fid) in already:
            row["logged"] += 1
            continue
        if tree is not None and args.dedup_iou > 0:
            hit = False
            for j in tree.query(geom):
                if nesp.iou(geom, ex_geoms[j]) >= args.dedup_iou:
                    hit = True
                    break
            if hit:
                row["overlap"] += 1
                continue
        row["send"] += 1
        to_send.append((layer, fid, geom))

    print(f"\nsite    : {args.site}  ({gpkg})")
    print(f"target  : {args.to}  ({'new file' if creating else 'append'})")
    print(f"species column: {sp_field}   id column: {args.id_field if next_id else '(none)'}"
          + (f"   next id: {next_id}" if next_id else ""))
    print("\n%-26s %-20s %5s %5s %7s %8s %5s" % ("class", "-> their name", "have", "nesp", "logged", "overlap", "send"))
    for c, r in report.items():
        print("%-26s %-20s %5d %5d %7d %8d %5d" % (c[:26], r["theirs"][:20], r["have"], r["nesp"],
                                                   r["logged"], r["overlap"], r["send"]))

    # shapefile column width check
    w = width.get(sp_field, 0) if width else 0
    if w:
        too_long = sorted({report[l]["theirs"] for l, _, _ in to_send if len(report[l]["theirs"]) > w})
        if too_long and not args.allow_truncate:
            sys.exit(f"\n{sp_field!r} column is {w} characters wide, too short for {too_long}. "
                     f"Widen it in QGIS (or --allow-truncate to cut the names).")

    if args.dry_run:
        print(f"\n--dry-run: would append {len(to_send)} polygon(s). Nothing written.")
        return
    if not to_send:
        print("\nnothing new to export.")
        return

    if not args.no_backup and not creating:
        tag = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        copied = nesp.backup_vector(target_path, tag)
        if copied:
            print(f"\nbackup: {copied[0]}" + (f" (+{len(copied)-1} sidecar files)" if len(copied) > 1 else ""))

    defn = lyr.GetLayerDefn()
    # the detected column, plus any other name-like column the target has
    # (their merged file carries both `Species` and `layer`)
    name_cols = [sp_field] + [c for c in ("Species", "species", "layer") if c in fields and c != sp_field]
    n = 0
    for layer, fid, geom in to_send:
        feat = ogr.Feature(defn)
        feat.SetGeometry(ogr.CreateGeometryFromWkb(shapely.to_wkb(geom)))
        for c in name_cols:
            feat.SetField(c, report[layer]["theirs"])
        if next_id is not None:
            feat.SetField(args.id_field, next_id)
            next_id += 1
        lyr.CreateFeature(feat)
        feat = None
        n += 1
    ds = None

    entry = log.setdefault(target_key, {"exported": []})
    entry["exported"].extend([[l, f] for l, f, _ in to_send])
    entry["last"] = _dt.datetime.now().isoformat(timespec="seconds")
    labels_dir.mkdir(parents=True, exist_ok=True)
    json.dump(log, open(log_path, "w"), indent=1)
    print(f"\nappended {n} polygon(s) to {args.to}. Logged in {log_path}.")


if __name__ == "__main__":
    main()
