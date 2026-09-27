"""
propagate_species_algorithm.py
------------------------------
QGIS Processing script: "Propagate species from seed".

ONE LAYER PER SPECIES. The layer NAME is the species, so there is no attribute
to fill in when you draw.

Layer schema (data/labels/<site>/<site>.gpkg -> one layer per species)
----------------------------------------------------------------------
  Geo-SAM    : lb_name, lb_note, group_ulid, N_GM, id, Area, N_FG, N_BG, BBox
  Bunjilview : species (a redundant copy of the layer name), source, score, run_ts

  source  NULL/'seed' = hand-drawn seed, 'auto' = script-propagated

Flow
----
1. Pick the species layer in QGIS, Toggle Editing, draw with Geo-SAM, save.
2. Processing Toolbox > Scripts > Bunjilview > Propagate species from seed.
3. Pick that same layer. Run. Auto polygons land in the same layer.

Install
-------
Point Processing at the repo's qgis/ folder (see README.md, Install section).
Do NOT use "Add Script to Toolbox" - it copies the file into the QGIS profile
and that stale copy then shadows this one.

Prerequisites
-------------
Run once per site, in the venv:
  python src/propagate.py --config config.yaml --site <site> --build-feature-cache
  python src/propagate.py --config config.yaml --site <site> --build-sam-cache
  python scripts/init_labels_gpkg.py --config config.yaml --site <site>

Those create data/labels/<site>/site.json and the per-species layers.
"""

import os
import json
import sqlite3
import subprocess
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterFile,
    QgsProcessingParameterEnum,
)


def _species_options():
    """Read config.yaml `classes` for the dropdown. Returns [] on failure."""
    try:
        import yaml
        repo_root = Path(__file__).resolve().parent.parent
        with open(repo_root / "config.yaml") as f:
            cfg = yaml.safe_load(f)
        names = [v for _, v in sorted(cfg["classes"].items(), key=lambda kv: int(kv[0]))
                 if v and v != "None"]
        return names
    except Exception:
        return []


class PropagateSpeciesAlgorithm(QgsProcessingAlgorithm):
    LAYER = "LAYER"
    THRESHOLD = "THRESHOLD"
    MARGIN = "MARGIN"
    SITE = "SITE"
    VENV_PYTHON = "VENV_PYTHON"
    CONFIG = "CONFIG"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return PropagateSpeciesAlgorithm()

    def name(self):
        return "propagate_species_from_seed"

    def displayName(self):
        return self.tr("Propagate species from seed")

    def group(self):
        return self.tr("Bunjilview")

    def groupId(self):
        return "bunjilview"

    def shortHelpString(self):
        return self.tr(
            "One layer per species: the layer NAME is the species, so there is "
            "no attribute to fill in.\n\n"
            "Draw one Geo-SAM seed polygon into the species layer, save, then "
            "pick that same layer here and Run.\n\n"
            "Auto-drawn polygons for every visually-similar patch are appended "
            "to the same layer with source='auto'; your own polygons are left "
            "untouched. Re-run after cleaning up to refine.\n\n"
            "The layer name must match a species in config.yaml `classes`.\n\n"
            "Build feature + SAM caches once per site first (see script header)."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.LAYER,
            self.tr("Species layer (the layer you just drew the seed into - "
                    "its name is the species)"),
            [QgsProcessing.TypeVectorPolygon],
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD, self.tr("Similarity threshold (absolute)"),
            QgsProcessingParameterNumber.Double, defaultValue=0.60,
            minValue=0.0, maxValue=1.0,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.MARGIN, self.tr("One-vs-rest margin"),
            QgsProcessingParameterNumber.Double, defaultValue=0.05,
            minValue=0.0, maxValue=1.0,
        ))
        p = QgsProcessingParameterString(
            self.SITE, self.tr("Site name (blank = read from site.json)"),
            defaultValue="", optional=True,
        )
        p.setFlags(p.flags() | p.FlagAdvanced)
        self.addParameter(p)
        p = QgsProcessingParameterFile(
            self.VENV_PYTHON, self.tr("venv Python interpreter (blank = read from site.json)"),
            optional=True, behavior=QgsProcessingParameterFile.File,
        )
        p.setFlags(p.flags() | p.FlagAdvanced)
        self.addParameter(p)
        p = QgsProcessingParameterString(
            self.CONFIG, self.tr("Config file (relative to repo root)"),
            defaultValue="config.yaml", optional=True,
        )
        p.setFlags(p.flags() | p.FlagAdvanced)
        self.addParameter(p)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_source(source):
        """'/path/to/test1.gpkg|layername=Sphagnum_R' -> gpkg path only."""
        return source.split("|")[0]

    @staticmethod
    def _layer_name_from_source(source, fallback=""):
        """Pull layername=... out of an OGR source string."""
        for part in source.split("|")[1:]:
            if part.startswith("layername="):
                return part.split("=", 1)[1]
        return fallback

    @staticmethod
    def _find_site_json(start_dir):
        """Walk up from start_dir looking for site.json (up to 6 levels)."""
        d = os.path.abspath(start_dir)
        for _ in range(6):
            cand = os.path.join(d, "site.json")
            if os.path.isfile(cand):
                return cand
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return None

    @staticmethod
    def _child_env(venv_python):
        """Environment for the venv interpreter, with QGIS's own Python scrubbed.

        QGIS exports PYTHONHOME, PYTHONPATH, GDAL_DATA and PROJ_LIB pointing at
        its bundled copies (on Windows the OSGeo4W launcher sets all of them).
        A conda interpreter that inherits those loads the wrong stdlib and the
        wrong proj.db and dies before propagate.py starts. Conda environments
        on Windows also need their Library\bin on PATH when run without
        `conda activate`, or GDAL's DLLs are not found.
        """
        env = dict(os.environ)
        for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONNOUSERSITE",
                  "GDAL_DATA", "GDAL_DRIVER_PATH", "PROJ_LIB", "PROJ_DATA",
                  "GEOTIFF_CSV", "QT_PLUGIN_PATH", "QGIS_PREFIX_PATH"):
            env.pop(k, None)
        env["PYTHONIOENCODING"] = "utf-8"
        root = os.path.dirname(os.path.abspath(venv_python))
        if os.path.basename(root).lower() == "bin":       # conda/venv on POSIX
            root = os.path.dirname(root)
        extra = [root, os.path.join(root, "Library", "bin"),
                 os.path.join(root, "Scripts"), os.path.join(root, "bin")]
        extra = [d for d in extra if os.path.isdir(d)]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        for var, rel in (("PROJ_LIB", ("Library", "share", "proj")),
                         ("PROJ_LIB", ("share", "proj")),
                         ("GDAL_DATA", ("Library", "share", "gdal")),
                         ("GDAL_DATA", ("share", "gdal"))):
            d = os.path.join(root, *rel)
            if var not in env and os.path.isdir(d):
                env[var] = d
        return env

    @staticmethod
    def _ensure_fields(gpkg_path, layer_name, feedback):
        """
        Backfill any column this species layer is missing, then stamp `species`
        on rows that have it blank. Geo-SAM only fills its own fields, so a
        hand-drawn seed arrives with species/source empty. Safe to re-run.
        """
        needed = {
            "lb_name": "TEXT", "lb_note": "TEXT", "group_ulid": "TEXT",
            "N_GM": "INTEGER", "id": "INTEGER", "Area": "REAL",
            "N_FG": "INTEGER", "N_BG": "INTEGER", "BBox": "TEXT",
            "species": "TEXT", "source": "TEXT", "score": "REAL", "run_ts": "TEXT",
        }
        try:
            conn = sqlite3.connect(gpkg_path)
            c = conn.cursor()
            c.execute("SELECT 1 FROM gpkg_contents "
                      "WHERE table_name=? AND data_type='features'", (layer_name,))
            if c.fetchone() is None:
                conn.close()
                feedback.pushWarning(
                    "'%s' is not a feature layer in %s" % (layer_name, gpkg_path))
                return
            c.execute('PRAGMA table_info("%s")' % layer_name)
            existing = {r[1].lower() for r in c.fetchall()}
            for col, typ in needed.items():
                if col.lower() not in existing:
                    c.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s'
                              % (layer_name, col, typ))
                    feedback.pushInfo("Added missing column '%s' (%s) to '%s'"
                                      % (col, typ, layer_name))
            c.execute('UPDATE "%s" SET species = ? '
                      "WHERE species IS NULL OR species = ''" % layer_name,
                      (layer_name,))
            n = c.rowcount
            conn.commit()
            conn.close()
            if n:
                feedback.pushInfo("Stamped species='%s' on %d row(s) that had it blank"
                                  % (layer_name, n))
        except Exception as e:
            feedback.pushWarning("Could not ensure fields on '%s': %s" % (layer_name, e))

    # ------------------------------------------------------------------
    # Main algorithm
    # ------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.LAYER, context)
        if layer is None:
            raise QgsProcessingException(self.tr("No layer selected."))

        # One layer per species: the layer NAME is the species.
        species = self._layer_name_from_source(layer.source(), layer.name()).strip()
        if not species:
            raise QgsProcessingException(self.tr(
                "Could not work out the species from the layer: %s" % layer.source()))

        known = _species_options()
        if known and species not in known:
            raise QgsProcessingException(self.tr(
                "Layer name '%s' is not a species in config.yaml `classes`.\n"
                "Pick the layer named after the species you drew into.\n"
                "Known species: %s" % (species, ", ".join(known))))
        feedback.pushInfo("Species (from the layer name): %s" % species)

        threshold  = self.parameterAsDouble(parameters, self.THRESHOLD, context)
        margin     = self.parameterAsDouble(parameters, self.MARGIN, context)
        site       = (self.parameterAsString(parameters, self.SITE, context) or "").strip()
        venv_python = (self.parameterAsString(parameters, self.VENV_PYTHON, context) or "").strip()
        config     = (self.parameterAsString(parameters, self.CONFIG, context) or "config.yaml").strip()

        # Commit any pending edits so the seed polygon is on disk
        if layer.isEditable():
            feedback.pushInfo("Committing open edits on the layer...")
            if not layer.commitChanges():
                raise QgsProcessingException(self.tr(
                    "Could not commit layer edits - toggle editing off and save first."))

        gpkg = self._parse_source(layer.source())
        if not gpkg.lower().endswith(".gpkg"):
            raise QgsProcessingException(self.tr(
                "Layer must be a GeoPackage (.gpkg): %s" % layer.source()))

        # Backfill any missing column on this species layer, and stamp `species`
        # on rows Geo-SAM drew (it only fills its own fields).
        self._ensure_fields(gpkg, species, feedback)

        # Read site.json for venv path and repo root
        site_json = self._find_site_json(os.path.dirname(gpkg))
        info = {}
        if site_json:
            try:
                with open(site_json) as f:
                    info = json.load(f)
                feedback.pushInfo("Read site.json: %s" % site_json)
            except Exception as e:
                feedback.pushWarning("Could not read site.json: %s" % e)

        site        = site or info.get("site")
        venv_python = venv_python or info.get("venv_python")
        repo_root   = info.get("repo_root") or os.getcwd()

        if not site:
            raise QgsProcessingException(self.tr(
                "Site unknown - fill the 'Site name' advanced parameter, or rebuild "
                "the caches so site.json exists next to the GeoPackage."))
        if not venv_python or not os.path.isfile(venv_python):
            raise QgsProcessingException(self.tr(
                "venv Python interpreter not found: '%s'.\n"
                "Set the 'venv Python' advanced parameter to the interpreter that "
                "has torch, rasterio, and segment-geospatial installed.\n"
                "Quick check: run 'conda activate bunjilview && which python' in "
                "a terminal to find the right path." % venv_python))

        script = os.path.join(repo_root, "src", "propagate.py")
        if not os.path.isfile(script):
            raise QgsProcessingException(self.tr(
                "src/propagate.py not found under %s" % repo_root))

        cmd = [
            venv_python, script,
            "--config",    config,
            "--site",      site,
            "--layer",     gpkg,
            "--species",   species,
            "--threshold", "%.4f" % threshold,
            "--margin",    "%.4f" % margin,
        ]
        feedback.pushInfo("Running: %s" % " ".join(cmd))
        feedback.pushInfo("cwd: %s" % repo_root)

        popen_kw = dict(cwd=repo_root, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                        encoding="utf-8", errors="replace",
                        env=self._child_env(venv_python))
        if os.name == "nt":                     # no console window popping up
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, **popen_kw)
        for line in proc.stdout:
            if feedback.isCanceled():
                proc.terminate()
                raise QgsProcessingException(self.tr("Canceled by user."))
            feedback.pushInfo(line.rstrip())
        code = proc.wait()
        if code != 0:
            raise QgsProcessingException(self.tr(
                "propagate.py exited with code %d - see log above for details." % code))

        # Refresh the layer so new polygons appear on the canvas
        try:
            layer.dataProvider().reloadData()
        except Exception:
            pass
        layer.reload()
        layer.triggerRepaint()
        feedback.pushInfo("Done - species '%s' propagated, layer reloaded." % species)

        return {"LAYER": layer.id(), "SITE": site, "SPECIES": species}
