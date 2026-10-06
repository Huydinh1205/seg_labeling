"""
nesp.py
-------
Helpers shared by scripts/import_seeds.py and scripts/export_to_nesp.py, the
two bridges between this pipeline and the company's "NESP Labelling" QGIS
project:

  * read any OGR vector (Shapefile, GeoPackage layer) into shapely polygons,
    reprojected into a target CRS, multipolygons split into polygons;
  * match the project's species spellings ("E. minus", "C. gaudichaudiana",
    "Sphagnum") to config.yaml class names ("Empidisma_minus", ...) and back.

Matching is by (genus initial, epithet), so "E. minus", "E.minus",
"Empidisma minus" and "Empidisma_minus" all land on the same class without a
hand-written table. Explicit overrides ("Foo bar=Empidisma_minus") win.
"""
import os
import re
from pathlib import Path

import shapely
import shapely.wkb


def _ogr():
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
    return ogr, osr


# ------------------------------------------------------------------ names

def name_key(name):
    """'E. minus' / 'Empidisma_minus' -> ('e', 'minus'); 'Sphagnum' -> ('sphagnum', '')."""
    s = re.sub(r"[_.\-]+", " ", str(name or "")).strip().lower()
    parts = s.split()
    if not parts:
        return None
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0][0], " ".join(parts[1:]))


class NameMap:
    """Project spelling <-> config class name, both directions."""

    def __init__(self, class_names, overrides=None):
        self.classes = list(class_names)
        self._by_key = {name_key(c): c for c in self.classes}
        self._exact = {}
        for o in overrides or []:
            if "=" not in o:
                raise SystemExit(f"--map expects 'their name=config name', got {o!r}")
            theirs, ours = (x.strip() for x in o.split("=", 1))
            if ours not in self.classes:
                raise SystemExit(f"--map {o!r}: {ours!r} is not a class in config.yaml")
            self._exact[theirs.strip().lower()] = ours
        # reverse map, filled from the project's own data when available
        self._theirs_for = {}

    def to_class(self, their_name):
        if their_name is None:
            return None
        t = str(their_name).strip()
        if not t:
            return None
        if t.lower() in self._exact:
            return self._exact[t.lower()]
        if t in self.classes:
            return t
        return self._by_key.get(name_key(t))

    def learn_their_spelling(self, values):
        """Remember how the project spells each class, from values seen in its data."""
        for v in values:
            c = self.to_class(v)
            if c and c not in self._theirs_for:
                self._theirs_for[c] = str(v).strip()

    def to_theirs(self, class_name):
        if class_name in self._theirs_for:
            return self._theirs_for[class_name]
        for theirs, ours in self._exact.items():
            if ours == class_name:
                return theirs
        # default abbreviation: Empidisma_minus -> "E. minus", Sphagnum -> "Sphagnum"
        parts = class_name.split("_", 1)
        if len(parts) == 2:
            return f"{parts[0][0].upper()}. {parts[1].replace('_', ' ')}"
        return class_name


# ------------------------------------------------------------------ vectors

def split_source(path_spec):
    """'file.gpkg|layername=foo' -> ('file.gpkg', 'foo'); plain path -> (path, None)."""
    if "|layername=" in path_spec:
        p, l = path_spec.split("|layername=", 1)
        return p, l
    return path_spec, None


def open_vector(path, update=False):
    ogr, _ = _ogr()
    ds = ogr.Open(str(path), 1 if update else 0)
    if ds is None:
        raise SystemExit(f"cannot open vector file: {path}")
    return ds


def feature_layers(ds):
    names = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        if lyr.GetGeomType() != 100:          # 100 = wkbNone (attribute-only tables)
            names.append(lyr.GetName())
    return names


def layer_fields(lyr):
    defn = lyr.GetLayerDefn()
    return [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]


def srs_of(lyr):
    srs = lyr.GetSpatialRef()
    return srs.Clone() if srs is not None else None


def transformer(src_srs, dst_srs):
    """osr transform src->dst with GIS (x, y) axis order, or None when equal/unknown."""
    _, osr = _ogr()
    if src_srs is None or dst_srs is None:
        return None
    if src_srs.IsSame(dst_srs):
        return None
    s = src_srs.Clone()
    d = dst_srs.Clone()
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    d.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(s, d)


def read_polygons(lyr, dst_srs=None):
    """Every feature of an OGR layer as dicts {fid, geom (shapely Polygon), attrs}.

    Multipolygons are split into one record per part (same fid, part index in
    'part'). Geometries are reprojected to dst_srs when it differs from the
    layer's own CRS. Invalid rings are repaired with buffer(0).
    """
    tr = transformer(srs_of(lyr), dst_srs) if dst_srs is not None else None
    fields = layer_fields(lyr)
    out = []
    lyr.ResetReading()
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        if tr is not None:
            g.Transform(tr)
        shp = shapely.wkb.loads(bytes(g.ExportToWkb()))
        if shp.is_empty:
            continue
        shp = shapely.make_valid(shp) if not shp.is_valid else shp
        attrs = {f: feat.GetField(f) for f in fields}
        parts = list(shp.geoms) if shp.geom_type in ("MultiPolygon", "GeometryCollection") else [shp]
        k = 0
        for part in parts:
            if part.geom_type != "Polygon" or part.is_empty or part.area <= 0:
                continue
            out.append({"fid": feat.GetFID(), "part": k, "geom": part, "attrs": attrs})
            k += 1
    return out


def guess_species_field(records, fields, namemap, preferred=None):
    """Pick the attribute that actually holds species names.

    The project's merged shapefile had the names in `layer` while `Species`
    held the file name, so this does not trust column names: it scores every
    candidate column by how many of its values match a known class and takes
    the best. Returns (field_name or None, matched_count).
    """
    if preferred:
        if preferred not in fields:
            raise SystemExit(f"--species-field {preferred!r} not in layer fields: {fields}")
        n = sum(1 for r in records if namemap.to_class(r["attrs"].get(preferred)))
        return preferred, n
    best, best_n = None, 0
    for f in fields:
        n = sum(1 for r in records if namemap.to_class(r["attrs"].get(f)))
        if n > best_n:
            best, best_n = f, n
    return best, best_n


def iou(a, b):
    if not a.intersects(b):
        return 0.0
    inter = a.intersection(b).area
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def shapefile_siblings(path):
    """All files of a Shapefile set (.shp .shx .dbf .prj .cpg .qix ...)."""
    p = Path(path)
    if p.suffix.lower() != ".shp":
        return [p] if p.exists() else []
    return sorted(p.parent.glob(p.stem + ".*"))


def backup_vector(path, tag):
    """Copy a vector file (and shapefile sidecars) to <stem>_backup_<tag>.<ext>."""
    import shutil
    copied = []
    for f in shapefile_siblings(path):
        if "_backup_" in f.stem:
            continue
        dst = f.with_name(f"{f.stem}_backup_{tag}{f.suffix}")
        shutil.copy2(f, dst)
        copied.append(str(dst))
    return copied
