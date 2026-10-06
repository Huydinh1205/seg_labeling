"""
prepare_ortho.py
----------------
Make an orthomosaic fit for the pipeline and for Geo-SAM: projected in
metres (UTM), and no finer than the working resolution in config.yaml.

    python scripts/prepare_ortho.py --config config.yaml --site <site> --ortho <image.tif>

Why this step exists. The drone orthos arrive in EPSG:4326 (degrees) at a
few millimetres per pixel. In degrees, every area threshold in config.yaml
(min_area_m2, max_area_m2) is meaningless, so propagation drops every
candidate; Geo-SAM's live encoding compares the clicked point with a chip it
has silently projected to metres and reports "Point prompt lies outside the
chip bounds"; and at 3 to 4 mm per pixel a 1024 px SAM window shows grass
texture, not plants, so SAM finds almost nothing. One warp fixes all three.

What it does, reading `prepare:` from config.yaml:
  * geographic CRS            -> reproject to the UTM zone under the image
                                 centre (or prepare.utm_epsg when set);
  * pixels finer than target  -> resample down to prepare.target_res_m
                                 (never up: a coarser image keeps its own
                                 resolution, with a warning);
  * writes <prepared_dir>/<site>.tif (tiled DEFLATE GeoTIFF with internal
    overviews, alpha band kept) plus <site>.prepare.json describing the
    source, and ALWAYS writes <prepared_dir>/<site>.path.txt holding the
    path every later step should use: the prepared file, or the original
    when nothing needed doing.
Re-running with the same source and settings is a no-op. --force redoes it.
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _gdal():
    from osgeo import gdal, osr
    gdal.UseExceptions()
    osr.UseExceptions()
    return gdal, osr


def prepare_cfg(cfg):
    p = dict(cfg.get("prepare") or {})
    p.setdefault("prepared_dir", "data/prepared")
    p.setdefault("target_res_m", 0.005)
    p.setdefault("resampling", "bilinear")
    p.setdefault("utm_epsg", None)
    p.setdefault("overviews", True)
    return p


def centre_lonlat(ds, srs):
    """Image centre in lon/lat, whatever the image CRS."""
    _, osr = _gdal()
    gt = ds.GetGeoTransform()
    cx = gt[0] + gt[1] * ds.RasterXSize / 2 + gt[2] * ds.RasterYSize / 2
    cy = gt[3] + gt[4] * ds.RasterXSize / 2 + gt[5] * ds.RasterYSize / 2
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    s = srs.Clone()
    for x in (s, wgs):
        x.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lon, lat, _ = osr.CoordinateTransformation(s, wgs).TransformPoint(cx, cy)
    return lon, lat


def utm_epsg_for(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def pixel_size_m(ds, srs, lat):
    """Approximate ground pixel size in metres (x, y)."""
    gt = ds.GetGeoTransform()
    if srs.IsGeographic():
        return (abs(gt[1]) * 111320.0 * math.cos(math.radians(lat)),
                abs(gt[5]) * 110540.0)
    unit = srs.GetLinearUnits() or 1.0
    return abs(gt[1]) * unit, abs(gt[5]) * unit


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--site", required=True)
    ap.add_argument("--ortho", required=True, help="the original orthomosaic")
    ap.add_argument("--res", type=float, default=None,
                    help="target pixel size in metres (default: prepare.target_res_m)")
    ap.add_argument("--out", default=None, help="output path (default <prepared_dir>/<site>.tif)")
    ap.add_argument("--force", action="store_true", help="redo even if up to date")
    ap.add_argument("--print-path", action="store_true",
                    help="print only the path to use afterwards (for shell scripts)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    p = prepare_cfg(cfg)
    target = float(args.res if args.res is not None else p["target_res_m"])
    out_dir = Path(p["prepared_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"{args.site}.tif"
    path_file = out_dir / f"{args.site}.path.txt"
    meta_file = out_dir / f"{args.site}.prepare.json"

    say = (lambda *a, **k: None) if args.print_path else print

    if not os.path.isfile(args.ortho):
        sys.exit(f"orthomosaic not found: {args.ortho}")

    gdal, osr = _gdal()
    ds = gdal.Open(args.ortho)
    srs = ds.GetSpatialRef()
    if srs is None:
        sys.exit(f"{args.ortho} has no coordinate reference system; cannot prepare it")
    lon, lat = centre_lonlat(ds, srs)
    px, py = pixel_size_m(ds, srs, lat)
    cur_res = min(px, py)
    geographic = bool(srs.IsGeographic())
    auth = f"{srs.GetAuthorityName(None)}:{srs.GetAuthorityCode(None)}" \
        if srs.GetAuthorityCode(None) else srs.GetName()

    say(f"source  : {args.ortho}")
    say(f"size    : {ds.RasterXSize} x {ds.RasterYSize} px, {ds.RasterCount} band(s)")
    say(f"crs     : {auth} ({'geographic, degrees' if geographic else 'projected, metres'})")
    say(f"pixel   : {px*1000:.2f} x {py*1000:.2f} mm  (target {target*1000:.1f} mm)")

    need_reproject = geographic
    need_resample = cur_res < target * 0.98
    if cur_res > target * 1.02:
        say(f"note    : image is coarser than the target ({cur_res*1000:.1f} mm), "
            f"keeping its own resolution (never upsampling)")

    source_sig = {"source": os.path.abspath(args.ortho),
                  "mtime": os.path.getmtime(args.ortho),
                  "size": os.path.getsize(args.ortho),
                  "target_res_m": target, "resampling": p["resampling"],
                  "utm_epsg": p["utm_epsg"]}

    if not need_reproject and not need_resample and not args.force:
        say("result  : nothing to do, the pipeline can read the original directly")
        path_file.write_text(os.path.abspath(args.ortho))
        if args.print_path:
            print(os.path.abspath(args.ortho))
        return

    if out_path.exists() and meta_file.exists() and not args.force:
        try:
            old = json.load(open(meta_file))
        except Exception:
            old = {}
        if old.get("signature") == source_sig:
            say(f"result  : {out_path} is up to date (same source and settings), skipping")
            path_file.write_text(str(out_path.resolve()))
            if args.print_path:
                print(out_path.resolve())
            return

    if geographic:
        epsg = int(p["utm_epsg"]) if p["utm_epsg"] else utm_epsg_for(lon, lat)
        dst_srs = f"EPSG:{epsg}"
    else:
        epsg = None
        dst_srs = srs.ExportToWkt()
    res = target if need_resample else None

    say(f"action  : {'reproject to ' + dst_srs if geographic else 'keep CRS'}"
        f"{', resample to %.1f mm' % (target*1000) if res else ''}")
    say(f"output  : {out_path}")

    tmp = out_path.with_suffix(".partial.tif")
    if tmp.exists():
        tmp.unlink()
    opts = gdal.WarpOptions(
        dstSRS=dst_srs,
        xRes=res, yRes=res,
        targetAlignedPixels=bool(res),
        resampleAlg=p["resampling"],
        multithread=True,
        warpOptions=["NUM_THREADS=ALL_CPUS"],
        warpMemoryLimit=2048,
        format="GTiff",
        creationOptions=["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512",
                         "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER",
                         "NUM_THREADS=ALL_CPUS"],
        callback=None if args.print_path else gdal.TermProgress_nocb,
    )
    gdal.Warp(str(tmp), ds, options=opts)
    ds = None

    if p["overviews"]:
        say("overviews: building (makes QGIS display fast)")
        o = gdal.Open(str(tmp), gdal.GA_Update)
        o.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32, 64])
        o = None

    if out_path.exists():
        out_path.unlink()
    os.replace(tmp, out_path)

    chk = gdal.Open(str(out_path))
    say(f"done    : {chk.RasterXSize} x {chk.RasterYSize} px, "
        f"{abs(chk.GetGeoTransform()[1])*1000:.2f} mm/px, "
        f"{chk.GetSpatialRef().GetAuthorityName(None)}:{chk.GetSpatialRef().GetAuthorityCode(None)}, "
        f"{os.path.getsize(out_path)/1e9:.2f} GB")
    chk = None

    json.dump({"signature": source_sig, "output": str(out_path.resolve()),
               "epsg": epsg, "res_m": res or cur_res}, open(meta_file, "w"), indent=1)
    path_file.write_text(str(out_path.resolve()))
    if args.print_path:
        print(out_path.resolve())


if __name__ == "__main__":
    main()
