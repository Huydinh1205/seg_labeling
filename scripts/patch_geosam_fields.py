"""
patch_geosam_fields.py
----------------------
Four patches applied to the Geo-SAM plugin. Run in the QGIS Python Console:

    exec(open('/path/to/segmentation_model/scripts/patch_geosam_fields.py').read())

To check (prints a dict, every value must be True):   print(verify())

PATCH 1 - canvasTool.py - assign attributes BY COLUMN NAME
    Geo-SAM does feature.setAttributes([group_ulid, 0, id, area, n_fg, n_bg,
    has_bbox]): the values land in columns 0..6 BY POSITION. That was written
    for Shapefiles, which have no `fid` column, so column 0 really is
    group_ulid. A GeoPackage ALWAYS has `fid` (an Integer64 primary key) in
    column 0, so the ULID string is pushed into the primary key, every other
    value is shifted by one, the insert fails, and the polygon "disappears"
    when you press S without any error message.

PATCH 2 - canvasTool.py - allow multi-colour layer styling
    show_layer() calls renderer().setSymbol(), a method that only exists on a
    single-symbol renderer.

PATCH 3 - canvasTool.py - only add the fields that are missing
    _init_layer() calls prov.addAttributes(SAM_Feature_Fields) UNCONDITIONALLY.
    On a layer that already has those fields, OGR reports "Cannot create field
    group_ulid. A field with the same name already exists."

PATCH 4 - widgetTool.py - force writing into the layer that is selected
    A safety net: before writing, save_shp_file() compares the bound layer with
    the layer in the combo box and re-binds if they differ. Whatever resets the
    binding, the polygon still goes into the right layer.

The patches live inside the QGIS profile, so they are LOST when the plugin is
reinstalled. Just run this again. The script makes a backup, checks the syntax,
SELF-REVERTS on failure, and verifies again after writing. Running it repeatedly
is safe.
"""
import os
import py_compile
import re
import shutil
import time

def _plugin_dir():
    """The Geo-SAM plugin folder. Asks QGIS itself, so it works on macOS,
    Windows and Linux, and also when you use a profile other than 'default'."""
    try:
        from qgis.core import QgsApplication
        p = os.path.join(QgsApplication.qgisSettingsDirPath(),
                         'python', 'plugins', 'GeoSAM', 'tools')
        if os.path.isdir(p):
            return p
    except Exception:
        pass
    # fallback: the default macOS path
    return os.path.expanduser(
        '~/Library/Application Support/QGIS/QGIS3/profiles/default'
        '/python/plugins/GeoSAM/tools')


_PROF = _plugin_dir()
CANVAS = os.path.join(_PROF, 'canvasTool.py')
WIDGET = os.path.join(_PROF, 'widgetTool.py')

MARK_FIELDS = '# --- patched: assign attributes BY COLUMN NAME ---'
MARK_RENDER = '# --- patched: keep the user multi-colour style ---'
MARK_INIT = '# --- patched: only add the fields that are missing ---'
MARK_SAVE = '# --- patched: force writing into the selected layer ---'

RE_FIELDS = re.compile(
    r"[ \t]*feature = QgsFeature\(\)\n"
    r"[ \t]*feature\.setGeometry\(geometry\)\n"
    r"[ \t]*feature\.setAttributes\(\s*\n"
    r"(?:.*\n)*?"
    r"[ \t]*\)\n")
RE_NGM = re.compile(
    r"([ \t]*)for feature in features:\n"
    r"[ \t]*feature\[1\] = len\(features\)\n")
RE_RENDER = re.compile(r"([ \t]*)self\.layer\.renderer\(\)\.setSymbol\(symbol\)\n")
RE_INIT = re.compile(r"([ \t]*)prov\.addAttributes\(SAM_Feature_Fields\)\n")
RE_SAVE = re.compile(
    r"(    def save_shp_file\(self\):\n"
    r"        \"\"\"[^\n]*\"\"\"\n)")

BLOCK_FIELDS = (
    "            " + MARK_FIELDS + "\n"
    "            feature = QgsFeature(self.layer.fields())\n"
    "            feature.setGeometry(geometry)\n"
    "            _names = [_f.name() for _f in self.layer.fields()]\n"
    "            for _k, _v in (\n"
    "                ('group_ulid', group_ulid),\n"
    "                ('N_GM', 0),\n"
    "                ('id', num_polygons+idx+1),\n"
    "                ('Area', geometry_area),\n"
    "                ('N_FG', prompt_history.count('fgpt')),\n"
    "                ('N_BG', prompt_history.count('bgpt')),\n"
    "                ('BBox', 'bbox' in prompt_history),\n"
    "            ):\n"
    "                if _k in _names:\n"
    "                    feature[_k] = _v\n")


# Use replacement FUNCTIONS, never a template string containing \1: one missing
# backslash turns \1 into the control character U+0001 and the generated file
# no longer compiles.
def _sub_ngm(m):
    i = m.group(1)
    return (i + "for feature in features:\n"
            + i + "    if 'N_GM' in [_f.name() for _f in self.layer.fields()]:\n"
            + i + "        feature['N_GM'] = len(features)\n")


def _sub_render(m):
    i = m.group(1)
    return (i + MARK_RENDER + "\n"
            + i + "_r = self.layer.renderer()\n"
            + i + "if hasattr(_r, 'setSymbol'):\n"
            + i + "    _r.setSymbol(symbol)\n")


def _sub_init(m):
    i = m.group(1)
    return (i + MARK_INIT + "\n"
            + i + "_have = {_f.name().lower() for _f in self.layer.fields()}\n"
            + i + "_missing = [_f for _f in SAM_Feature_Fields\n"
            + i + "            if _f.name().lower() not in _have]\n"
            + i + "if _missing:\n"
            + i + "    prov.addAttributes(_missing)\n")


def _sub_save(m):
    return (m.group(1)
            + "        " + MARK_SAVE + "\n"
            + "        try:\n"
            + "            _cur = self.wdg_sel.MapLayerComboBox.currentLayer()\n"
            + "            if _cur is not None and hasattr(self, 'polygon'):\n"
            + "                _b = self.polygon.get_layer()\n"
            + "                if _b is None or _b.id() != _cur.id():\n"
            + "                    self.polygon.reset_layer(_cur)\n"
            + "        except Exception:\n"
            + "            pass\n")


# Detect the patches by the REAL CODE, not by a comment line. Changing a comment
# would break idempotency: the script would think it is not patched, patch again,
# and corrupt the file.
SIG_FIELDS = "feature = QgsFeature(self.layer.fields())"
SIG_RENDER = "if hasattr(_r, 'setSymbol'):"
SIG_INIT = "_missing = [_f for _f in SAM_Feature_Fields"
SIG_SAVE = "self.polygon.reset_layer(_cur)"


def verify():
    out = {}
    try:
        c = open(CANVAS, encoding='utf-8').read()
        out['patch1_column_name'] = SIG_FIELDS in c
        out['patch2_renderer'] = SIG_RENDER in c
        out['patch3_missing_fields'] = SIG_INIT in c
    except Exception as e:
        out['canvasTool_error'] = str(e)
    try:
        w = open(WIDGET, encoding='utf-8').read()
        out['patch4_right_layer'] = SIG_SAVE in w
    except Exception as e:
        out['widgetTool_error'] = str(e)
    return out


def _write_checked(path, text, bak):
    open(path, 'w', encoding='utf-8').write(text)
    try:
        py_compile.compile(path, doraise=True)
    except Exception as e:
        shutil.copy2(bak, path)
        raise RuntimeError('%s has a syntax error after patching, REVERTED: %r'
                           % (os.path.basename(path), e))


def patch(verbose=True):
    say = (lambda m: print(m)) if verbose else (lambda m: None)
    for p in (CANVAS, WIDGET):
        if not os.path.isfile(p):
            raise RuntimeError('not found: %s' % p)

    st = verify()
    if len(st) == 4 and all(v is True for v in st.values()):
        say('all four patches already present: %r' % st)
        return st

    stamp = time.strftime('%Y%m%d-%H%M%S')

    src = open(CANVAS, encoding='utf-8').read()
    if not (SIG_FIELDS in src and SIG_RENDER in src and SIG_INIT in src):
        bak = '%s.bak-%s' % (CANVAS, stamp)
        shutil.copy2(CANVAS, bak)
        say('backup -> %s' % os.path.basename(bak))
        new = src
        if SIG_FIELDS not in new:
            new, n = RE_FIELDS.subn(BLOCK_FIELDS, new, count=1)
            if n != 1:
                shutil.copy2(bak, CANVAS)
                raise RuntimeError('could not find setAttributes([group_ulid, ...])')
            new, n2 = RE_NGM.subn(_sub_ngm, new, count=1)
            say('patch 1 (column name): setAttributes=%d feature[1]=%d' % (n, n2))
        if SIG_RENDER not in new:
            new, n = RE_RENDER.subn(_sub_render, new, count=1)
            if n != 1:
                shutil.copy2(bak, CANVAS)
                raise RuntimeError('could not find renderer().setSymbol(symbol)')
            say('patch 2 (renderer): %d site(s)' % n)
        if SIG_INIT not in new:
            new, n = RE_INIT.subn(_sub_init, new, count=1)
            if n != 1:
                shutil.copy2(bak, CANVAS)
                raise RuntimeError('could not find prov.addAttributes(SAM_Feature_Fields)')
            say('patch 3 (missing fields): %d site(s)' % n)
        _write_checked(CANVAS, new, bak)

    src = open(WIDGET, encoding='utf-8').read()
    if SIG_SAVE not in src:
        bak = '%s.bak-%s' % (WIDGET, stamp)
        shutil.copy2(WIDGET, bak)
        say('backup -> %s' % os.path.basename(bak))
        new, n = RE_SAVE.subn(_sub_save, src, count=1)
        if n != 1:
            shutil.copy2(bak, WIDGET)
            raise RuntimeError('could not find the save_shp_file function header')
        say('patch 4 (right layer): %d site(s)' % n)
        _write_checked(WIDGET, new, bak)

    st = verify()
    if not (len(st) == 4 and all(v is True for v in st.values())):
        raise RuntimeError('patched, but the re-check still reports missing: %r' % st)
    say('patched: %r' % st)
    return st


if True:
    try:
        PATCH_RESULT = patch()
    except Exception as exc:
        PATCH_RESULT = {'PATCH FAILED': str(exc)}
        print('PATCH FAILED: %s' % exc)
