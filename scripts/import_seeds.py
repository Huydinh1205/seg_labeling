"""
import_seeds.py
---------------
Bring polygons the company already drew (the "NESP Labelling" QGIS project,
hand-traced shapefiles) into a site's label GeoPackage, so that propagation

  1. learns from them: imported polygons count as seeds (source='nesp' is in
     config.yaml -> propagate.prototype.seed_sources), and
  2. does not propose polygons on top of them: the overlap check only sees
     polygons inside data/labels/<site>/<site>.gpkg.

    python scripts/import_seeds.py --config config.yaml --site <site> \
        --from "Z:\\NESP\\all_species_fixed.shp"

    # several per-species files at once, species taken from the file name
    python scripts/import_seeds.py --config config.yaml --site <site> \
        --from "E. minus.shp" "D. continentis.shp" "Sphagnum.shp"

    # a layer inside a GeoPackage
    python scripts/import_seeds.py ... --from "labels.gpkg|layername=all_species"

What it does, per input file:
  * reads every polygon, reprojects to the site's CRS, splits multipolygons;
  * works out which attribute holds the species name by matching values
    against config.yaml classes (so the merged shapefile whose `Species`
    column was wrong and `layer` column was right still imports correctly),
    falling back to the layer / file name for per-species files;
  * maps spellings: "E. minus" -> Empidisma_minus, "Sphagnum" -> Sphagnum ...
    (--map "their name=config name" for anything it cannot match);
  * keeps only polygons that lie (mostly) inside the site's orthomosaic,
    since the project holds all sites in one layer;
  * appends them to the matching species layer with source='nesp', species
    set, and a provenance key in lb_note ("nesp:<file>|<layer>|<fid>[.<part>]"),
    which is what makes re-running this script a no-op instead of a duplicate.

Nothing in the company's files is modified. Run with --dry-run first.
"""
import argparse
import datetime as _dt
import os
import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import rasterio                                             # noqa: E402
import shapely                                              # noqa: E402

import nesp                                                 # noqa: E402
from propagate import (load_config, site_labels_dir, resolve_ortho,  # noqa: E402
                       append_polygons, list_layers, read_layer_polygons, _ensure_layer)


def class_names(cfg):
    return [v for _, v in sorted(cfg["classes"].items(), key=lambda kv: int(kv[0]))
            if v and v != "None"]


def existing_keys(gpkg, layers):
    """Provenance keys already present (lb_note starting with 'nesp:')."""
    keys = set()
    ds = nesp.open_vector(gpkg)
    for name in layers:
        lyr = ds.GetLayerByName(name)
        if lyr is None or "lb_note" not in nesp.layer_fields(lyr):
            continue
        for feat in lyr:
            v = feat.GetField("lb_note")
            if v and str(v).startswith("nesp:"):
                keys.add(str(v))
    ds = None
    return keys


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--from", dest="sources", nargs="+", required=True,
                    help="shapefile(s) / gpkg layer(s) to import; 'file.gpkg|layername=x' for a layer")
    ap.add_argument("--species-field", default=None,
                    help="attribute holding the species name (default: detected by content)")
    ap.add_argument("--map", nargs="*", default=[],
                    help="extra spellings, e.g. --map \"Snow grass=Poa_costiniana\"")
    ap.add_argument("--source", default="nesp",
                    help="value written to the `source` field (default nesp)")
    ap.add_argument("--min-inside", type=float, default=0.5,
                    help="keep a polygon when at least this fraction of it lies inside the "
                         "ortho's bounds (default 0.5); 0 disables the extent filter")
    ap.add_argument("--dup-iou", type=float, default=0.95,
                    help="skip a polygon overlapping one already in the layer above this IoU")
    ap.add_argument("--ortho", default=None, help="override the ortho used for extent/CRS")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    classes = class_names(cfg)
    namemap = nesp.NameMap(classes, args.map)

    labels_dir = site_labels_dir(cfg, args.site)
    gpkg = labels_dir / f"{args.site}.gpkg"
    if not gpkg.exists():
        sys.exit(f"{gpkg} does not exist. Run the pipeline for this site first "
                 f"(step 5, scripts/init_labels_gpkg.py, creates it).")
    have_layers = set(list_layers(str(gpkg)))
    missing = [c for c in classes if c not in have_layers]

    ortho = resolve_ortho(cfg, args.site, args.ortho)
    if not os.path.isfile(ortho):
        sys.exit(f"orthomosaic not found: {ortho}  (pass --ortho, or 0 for --min-inside "
                 f"only disables the filter, the CRS is still read from the ortho)")
    with rasterio.open(ortho) as src:
        crs_wkt = src.crs.to_wkt()
        crs_str = str(src.crs)
        b = src.bounds
    extent = shapely.box(b.left, b.bottom, b.right, b.top)
    _, osr = nesp._ogr()
    site_srs = osr.SpatialReference()
    site_srs.ImportFromWkt(crs_wkt)

    print(f"site   : {args.site}")
    print(f"gpkg   : {gpkg}")
    print(f"ortho  : {ortho}")
    print(f"extent : {b.left:.1f} {b.bottom:.1f} {b.right:.1f} {b.top:.1f}  ({crs_str})")
    print(f"classes: {', '.join(classes)}")
    if missing:
        # a site created before a class was added to config.yaml (e.g. Sphagnum
        # replacing Sphagnum_R/G/Y): add the empty layer with the full Geo-SAM
        # schema instead of asking for a destructive --force re-init.
        print(f"adding missing species layer(s) to the GeoPackage: {missing}")
        if not args.dry_run:
            ogr, _ = nesp._ogr()
            ds = ogr.Open(str(gpkg), 1)
            for c in missing:
                _ensure_layer(ds, c, crs_wkt)
            ds = None

    known = existing_keys(str(gpkg), classes)
    # geometry-level duplicate guard too: the project keeps the same polygon in
    # a per-species file AND in the merged file, and both may get passed here.
    have_geoms = {c: [r["geom"] for r in read_layer_polygons(str(gpkg), c)]
                  for c in classes if c in have_layers}
    now = _dt.datetime.now().isoformat(timespec="seconds")

    # per (their spelling) -> counters, kept in first-seen order for the report
    report = OrderedDict()

    def bump(theirs, mapped, key, n=1):
        row = report.setdefault(theirs, {"mapped": mapped, "read": 0, "inside": 0,
                                         "new": 0, "dup": 0})
        row[key] += n

    to_write = {c: ([], []) for c in classes}      # class -> (geoms, attrs)

    for spec in args.sources:
        path, layer_name = nesp.split_source(spec)
        if not os.path.exists(path):
            sys.exit(f"input not found: {path}")
        ds = nesp.open_vector(path)
        layers = [layer_name] if layer_name else nesp.feature_layers(ds)
        for lname in layers:
            lyr = ds.GetLayerByName(lname)
            if lyr is None:
                sys.exit(f"{path}: no layer named {lname!r}; has {nesp.feature_layers(ds)}")
            recs = nesp.read_polygons(lyr, dst_srs=site_srs)
            fields = nesp.layer_fields(lyr)
            field, n_match = nesp.guess_species_field(recs, fields, namemap, args.species_field)
            file_class = namemap.to_class(lname) or namemap.to_class(Path(path).stem)
            if n_match == 0 and file_class is None:
                print(f"\n{path} [{lname}]: no attribute holds recognisable species names and "
                      f"the file name is not a species either; skipped. Use --species-field "
                      f"or --map.")
                continue
            if n_match == 0:
                field = None
            print(f"\n{path} [{lname}]: {len(recs)} polygon part(s), species from "
                  f"{'field ' + repr(field) if field else 'the layer/file name (' + file_class + ')'}")

            stem = Path(path).stem
            for r in recs:
                theirs = r["attrs"].get(field) if field else lname
                if field and not namemap.to_class(theirs) and file_class:
                    theirs = lname           # per-species file with a junk column
                mapped = namemap.to_class(theirs)
                label = str(theirs).strip() if theirs is not None else "(empty)"
                bump(label, mapped, "read")
                if mapped is None:
                    continue
                g = r["geom"]
                if args.min_inside > 0:
                    frac = g.intersection(extent).area / g.area if g.area > 0 else 0.0
                    if frac < args.min_inside:
                        continue
                bump(label, mapped, "inside")
                key = f"nesp:{stem}|{lname}|{r['fid']}" + (f".{r['part']}" if r["part"] else "")
                if key in known:
                    bump(label, mapped, "dup")
                    continue
                prior = have_geoms.setdefault(mapped, [])
                if any(nesp.iou(g, h) >= args.dup_iou for h in prior if h.intersects(g)):
                    bump(label, mapped, "dup")
                    continue
                known.add(key)
                prior.append(g)
                bump(label, mapped, "new")
                geoms, attrs = to_write[mapped]
                geoms.append(g)
                attrs.append({"species": mapped, "source": args.source, "run_ts": now,
                              "lb_note": key, "Area": float(g.area)})
        ds = None

    print("\n%-26s %-26s %6s %7s %5s %5s" % ("their name", "-> class", "read", "inside", "new", "dup"))
    for theirs, row in report.items():
        print("%-26s %-26s %6d %7d %5d %5d" % (
            theirs[:26], (row["mapped"] or "(no match, skipped)")[:26],
            row["read"], row["inside"], row["new"], row["dup"]))
    total_new = sum(len(g) for g, _ in to_write.values())
    unmatched = [t for t, r in report.items() if r["mapped"] is None]
    if unmatched:
        print(f"\nnot matched to any class (use --map \"<their name>=<class>\"): {unmatched}")

    if args.dry_run:
        print(f"\n--dry-run: would add {total_new} polygon(s) to {gpkg}")
        return
    if total_new == 0:
        print("\nnothing new to import.")
        return
    for cname, (geoms, attrs) in to_write.items():
        if geoms:
            n = append_polygons(str(gpkg), cname, geoms, attrs, crs_wkt)
            print(f"  {cname}: +{n}")
    print(f"\nimported {total_new} polygon(s) into {gpkg} with source='{args.source}'. "
          f"They now count as seeds and block duplicate proposals.")


if __name__ == "__main__":
    main()
