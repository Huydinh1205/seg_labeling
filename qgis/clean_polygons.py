"""
clean_polygons.py
-----------------
Shortcuts for cleaning up polygons. Run in the QGIS Python Console:

    exec(open('<REPO>/qgis/clean_polygons.py').read())

Then:

    count()                     # polygon count per species layer
    delete_selected()           # delete the currently selected polygons
    delete_auto()               # delete every machine-made polygon on the
                                # active layer, keep the ones you drew
    delete_auto('Sphagnum_R')   # ... on a specific layer
    delete_all('Sphagnum_R')    # wipe one layer (hand-drawn ones too)

Everything is committed to disk, so no Ctrl+S needed afterwards.
There is no undo, except for delete_selected() before you save.
"""
import qgis.utils
from qgis.core import QgsProject
from qgis.utils import iface

DEFAULT_SITE = 'test1'
SITE = getattr(qgis.utils, 'BV_SITE', '') or DEFAULT_SITE


def _layers():
    return {l.name(): l for l in QgsProject.instance().mapLayers().values()
            if SITE + '.gpkg' in l.source()}


def _pick(name=None):
    if name:
        l = _layers().get(name)
        if l is None:
            print('no layer named %r. Available: %s'
                  % (name, ', '.join(sorted(_layers()))))
        return l
    l = iface.activeLayer()
    if l is None or SITE + '.gpkg' not in l.source():
        print('no species layer is active. Click one in the Layers panel first, '
              'or pass a name: delete_auto("Sphagnum_R")')
        return None
    return l


def _save(l):
    if l.isEditable():
        l.commitChanges()
    l.reload()
    l.triggerRepaint()
    iface.mapCanvas().refresh()


def delete_selected():
    """Delete the polygons currently selected on the active layer."""
    l = _pick()
    if l is None:
        return
    ids = l.selectedFeatureIds()
    if not ids:
        print('nothing selected. Switch to the Select Features tool and click a '
              'polygon (hold Shift for several).')
        return
    if not l.isEditable():
        l.startEditing()
    l.deleteFeatures(ids)
    _save(l)
    print('deleted %d polygon(s) from %s, %d left'
          % (len(ids), l.name(), l.featureCount()))


def delete_auto(name=None):
    """Delete every machine-made polygon (source='auto'), keep hand-drawn ones."""
    l = _pick(name)
    if l is None:
        return
    if l.isEditable():
        l.commitChanges()
    ids = [f.id() for f in l.getFeatures() if str(f['source']) == 'auto']
    if not ids:
        print('%s has no auto polygons' % l.name())
        return
    l.dataProvider().deleteFeatures(ids)
    _save(l)
    print('deleted %d auto polygon(s) from %s, %d left (all hand-drawn)'
          % (len(ids), l.name(), l.featureCount()))


def delete_all(name=None):
    """Wipe a layer completely, hand-drawn polygons included."""
    l = _pick(name)
    if l is None:
        return
    if l.isEditable():
        l.commitChanges()
    n = l.featureCount()
    if not n:
        print('%s is already empty' % l.name())
        return
    l.dataProvider().deleteFeatures([f.id() for f in l.getFeatures()])
    _save(l)
    print('wiped %d polygon(s) from %s' % (n, l.name()))
    print('NOTE: the old prototype is still at '
          'data/labels/%s/prototypes/%s.npy - delete it too if you want this '
          'species to start learning from scratch.' % (SITE, l.name()))


def count():
    """Print the polygon count for every species layer."""
    act = iface.activeLayer()
    for n, l in sorted(_layers().items()):
        hand = sum(1 for f in l.getFeatures() if str(f['source']) != 'auto')
        auto = sum(1 for f in l.getFeatures() if str(f['source']) == 'auto')
        print('  %-26s %3d total  (hand-drawn %d, machine-made %d)%s'
              % (n, l.featureCount(), hand, auto,
                 '   <<< active' if act is not None and act.id() == l.id() else ''))


print('Ready: count()  delete_selected()  delete_auto()  delete_all("LayerName")')
count()
