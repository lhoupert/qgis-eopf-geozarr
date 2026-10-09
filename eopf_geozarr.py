"""Open EOPF Explorer GeoZarr (Sentinel-2 L2A) scenes in QGIS, fast, with real reflectance values.

Single-file helper. Use it from the QGIS Python console:

    import os; exec(open(os.path.expanduser("~/Downloads/eopf_geozarr.py")).read())
    items = search(bbox=(-3.0, 56.8, -1.7, 57.7), start="2026-07-01", end="2026-09-30", max_cloud=20)
    add_rgb(items[0]["id"])                 # true colour, multiscale, float reflectance
    add_band(items[0]["id"], "b08")         # one band (NIR)
    add_xyz_preview(items[0]["id"])         # server-rendered quick look (8-bit PNG tiles)
    add_stac_connection()                   # EOPF STAC API in Browser > STAC

or import it (``import eopf_geozarr``) from your own scripts. ``build_vrt()`` and ``search()``
need only GDAL, not QGIS.

Why a VRT? With GDAL 3.13.3 a zoomed-out read of a sharded Zarr v3 array first downloads the
whole full-resolution shard (~336 MB per 10 m band) before it switches to the coarser
multiscale level. A VRT that lists the GeoZarr levels (r20m ... r720m) as explicit
<Overview> elements makes GDAL go straight to the right level. See README.md.

MIT licence - https://github.com/lhoupert/qgis-eopf-geozarr
"""
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

from osgeo import gdal

try:  # QGIS is optional: search() and build_vrt() work with plain GDAL Python.
    from qgis.core import (QgsContrastEnhancement, QgsMultiBandColorRenderer, QgsProject,
                           QgsRasterLayer, QgsSingleBandGrayRenderer)
except ImportError:  # pragma: no cover
    QgsProject = None

STAC = "https://api.explorer.eopf.copernicus.eu/stac"
COLLECTION = "sentinel-2-l2a"
CACHE_DIR = os.environ.get("EOPF_GEOZARR_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "eopf_geozarr")
REFLECTANCE = "/measurements/reflectance"
DEMO_ITEM = "S2A_MSIL2A_20260719T112131_N0512_R037_T30VWJ_20260719T194158"  # test data, may expire

# Cut the number of HTTP probes per opened dataset (layer open ~3x faster, same bytes).
# Setting these as environment variables before QGIS starts is the reliable route (README).
GDAL_OPTIONS = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_PAM_ENABLED": "NO"}
for _k, _v in GDAL_OPTIONS.items():
    if not os.environ.get(_k):
        gdal.SetConfigOption(_k, _v)
gdal.UseExceptions()


# --------------------------------------------------------------------------- STAC

def _get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def get_item(item_id, collection=COLLECTION):
    """Return the STAC item (dict) for an item id."""
    return _get_json(f"{STAC}/collections/{collection}/items/{urllib.parse.quote(item_id)}")


def _link(item, rel):
    return next((l["href"] for l in item.get("links", []) if l.get("rel") == rel), None)


def search(bbox, start, end, max_cloud=100, limit=20, collection=COLLECTION):
    """Search the EOPF Explorer STAC API.

    bbox: (west, south, east, north) in degrees; start/end: 'YYYY-MM-DD'.
    Returns at most `limit` items (dicts), least cloudy first.
    """
    params = {
        "collections": collection,
        "bbox": ",".join(map(str, bbox)),
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "filter": f"eo:cloud_cover <= {max_cloud}",
        "filter-lang": "cql2-text",
        "limit": 100,
    }
    url, items = f"{STAC}/search?{urllib.parse.urlencode(params)}", []
    while url and len(items) < 2000:
        page = _get_json(url)
        items += page.get("features", [])
        url = _link(page, "next")
    # Filter again client-side in case the server ignored the CQL2 filter.
    items = [i for i in items if i["properties"].get("eo:cloud_cover", 0) <= max_cloud]
    items.sort(key=lambda i: (i["properties"].get("eo:cloud_cover", 0), i["properties"]["datetime"]))
    return items[:limit]


def print_items(items):
    """One line per item: date, MGRS tile, cloud cover, id."""
    for i in items:
        p = i["properties"]
        print(f'{p["datetime"][:10]}  {p.get("grid:code", "?"):12}  {p.get("eo:cloud_cover", 0):5.1f}%  {i["id"]}')


def resolve_root(item):
    """Item id, STAC item dict or URL -> URL of the reflectance Zarr group (the multiscales root).

    Accepts: an item id, a STAC item dict, a store URL (.../<id>.zarr) or the reflectance href.
    """
    if isinstance(item, dict):
        asset = item.get("assets", {}).get("reflectance")
        if asset:
            return asset["href"].rstrip("/")
        return _link(item, "store").rstrip("/") + REFLECTANCE
    item = item.strip().rstrip("/")
    if item.startswith(("http://", "https://")):
        return item if item.endswith(REFLECTANCE) else item + REFLECTANCE
    return resolve_root(get_item(item))


def _short_name(item):
    """'2026-07-19 T30VWJ' from an S2 product id or URL."""
    m = re.search(r"MSIL2A_(\d{4})(\d{2})(\d{2})T\d+_N\d+_R\d+_(T\w{5})", str(item))
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}" if m else str(item)[-40:]


# --------------------------------------------------------------------------- VRT

def _zarr_uri(root, res, band):
    return f'ZARR:"/vsicurl/{root}":/{res}/{band}'


def list_levels(root):
    """Return {resolution_name: set(bands)} for a reflectance group, e.g. {'r10m': {'b02', ...}, ...}."""
    ds = gdal.Open(f'ZARR:"/vsicurl/{root}"')
    levels = {}
    for name, _ in ds.GetSubDatasets():
        m = re.search(r'":/?(r\d+m)/(\w+)$', name)
        if m:
            levels.setdefault(m[1], set()).add(m[2])
    return dict(sorted(levels.items(), key=lambda kv: int(kv[0][1:-1])))


def build_vrt(item, bands=("b04", "b03", "b02"), res=None, out=None):
    """Write a multiscale VRT for one or more bands and return its path.

    Each VRT band reads the finest available level (or `res`, e.g. 'r60m') and lists every
    coarser GeoZarr level of the same band as an explicit <Overview>.
    """
    if isinstance(bands, str):
        bands = (bands,)
    root = resolve_root(item)
    levels = list_levels(root)
    usable = [r for r, b in levels.items() if set(bands) <= b]
    if not usable:
        raise ValueError(f"bands {bands} not found together at any level; available: {levels}")
    base = res or usable[0]
    if base not in usable:
        raise ValueError(f"{bands} not all available at {base}; try one of {usable}")
    overviews = [r for r in usable if int(r[1:-1]) > int(base[1:-1])]

    # Size, georeferencing and data type come from the base array (metadata only, cheap).
    src = gdal.Open(_zarr_uri(root, base, bands[0]))
    gt, srs = src.GetGeoTransform(), src.GetSpatialRef()
    b1 = src.GetRasterBand(1)
    dtype = gdal.GetDataTypeName(b1.DataType)
    nodata = b1.GetNoDataValue()
    scale, offset = b1.GetScale(), b1.GetOffset()
    w, h = src.RasterXSize, src.RasterYSize

    q = {'"': "&quot;"}
    colors = {0: "Red", 1: "Green", 2: "Blue"} if len(bands) == 3 else {}
    xml = [f'<VRTDataset rasterXSize="{w}" rasterYSize="{h}">',
           f"  <SRS>{escape(srs.ExportToWkt()) if srs else ''}</SRS>",
           f"  <GeoTransform>{', '.join(repr(v) for v in gt)}</GeoTransform>"]
    for i, band in enumerate(bands):
        xml += [f'  <VRTRasterBand dataType="{dtype}" band="{i + 1}">',
                f"    <Description>{band}</Description>"]
        if i in colors:
            xml.append(f"    <ColorInterp>{colors[i]}</ColorInterp>")
        if nodata is not None:
            xml.append(f"    <NoDataValue>{'nan' if nodata != nodata else repr(nodata)}</NoDataValue>")
        if scale not in (None, 1.0):
            xml.append(f"    <Scale>{scale!r}</Scale>")
        if offset not in (None, 0.0):
            xml.append(f"    <Offset>{offset!r}</Offset>")
        xml.append(
            f'    <SimpleSource><SourceFilename relativeToVRT="0">{escape(_zarr_uri(root, base, band), q)}'
            f'</SourceFilename><SourceBand>1</SourceBand>'
            f'<SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/>'
            f'<DstRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/></SimpleSource>')
        for r in overviews:
            xml.append(f'    <Overview><SourceFilename relativeToVRT="0">{escape(_zarr_uri(root, r, band), q)}'
                       f'</SourceFilename><SourceBand>1</SourceBand></Overview>')
        xml.append("  </VRTRasterBand>")
    xml.append("</VRTDataset>")

    if out is None:
        out = os.path.join(_cache_dir(), f"{_item_id(root)}_{'_'.join(bands)}_{base}.vrt")
    with open(out, "w") as f:
        f.write("\n".join(xml) + "\n")
    return out


def _item_id(root):
    m = re.search(r"/([^/]+)\.zarr", root)
    return m[1] if m else re.sub(r"\W+", "_", root)[-60:]


def _cache_dir():
    for d in (CACHE_DIR, os.path.join(tempfile.gettempdir(), "eopf_geozarr")):
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            continue
    raise OSError("no writable cache directory")


# --------------------------------------------------------------------------- QGIS layers

def _need_qgis():
    if QgsProject is None:
        raise RuntimeError("this function needs QGIS (run it from the QGIS Python console or PyQGIS)")


def _stretch(provider, band, vmin, vmax):
    ce = QgsContrastEnhancement(provider.dataType(band))
    ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
    ce.setMinimumValue(vmin)
    ce.setMaximumValue(vmax)
    return ce


def _add_layer(path, name):
    layer = QgsRasterLayer(path, name, "gdal")
    if not layer.isValid():
        raise RuntimeError(f"could not open {path}: {layer.error().summary()}")
    return layer


def add_rgb(item=DEMO_ITEM, bands=("b04", "b03", "b02"), stretch=(0.0, 0.3), res=None, name=None):
    """Add a 3-band multiscale VRT layer (default true colour) with a fixed min/max stretch.

    item: item id, STAC item dict, store URL or reflectance href.
    bands: e.g. ("b08", "b04", "b03") for false colour (the finest common level is used).
    """
    _need_qgis()
    vrt = build_vrt(item, bands, res)
    layer = _add_layer(vrt, name or f"{_short_name(vrt)} {'/'.join(bands)}")
    p = layer.dataProvider()
    r = QgsMultiBandColorRenderer(p, 1, 2, 3)
    r.setRedContrastEnhancement(_stretch(p, 1, *stretch))
    r.setGreenContrastEnhancement(_stretch(p, 2, *stretch))
    r.setBlueContrastEnhancement(_stretch(p, 3, *stretch))
    layer.setRenderer(r)
    return QgsProject.instance().addMapLayer(layer)


def add_band(item=DEMO_ITEM, band="b04", res=None, stretch=(0.0, 0.3), name=None):
    """Add one band as a multiscale VRT layer (grey stretch). res=None -> finest level of that band."""
    _need_qgis()
    vrt = build_vrt(item, (band,), res)
    layer = _add_layer(vrt, name or f"{_short_name(vrt)} {band}")
    r = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
    r.setContrastEnhancement(_stretch(layer.dataProvider(), 1, *stretch))
    layer.setRenderer(r)
    return QgsProject.instance().addMapLayer(layer)


def add_xyz_preview(item=DEMO_ITEM, name=None, zmax=14, collection=COLLECTION):
    """Add the item's server-rendered true-colour XYZ tiles (EOPF TiTiler). Pictures only, 8-bit."""
    _need_qgis()
    stac_item = item if isinstance(item, dict) else get_item(item, collection)
    xyz = _link(stac_item, "xyz")
    if not xyz:
        raise RuntimeError("item has no rel=xyz link")
    uri = f"type=xyz&zmin=0&zmax={zmax}&url={urllib.parse.quote(xyz, safe='')}"
    layer = QgsRasterLayer(uri, name or f"{_short_name(stac_item['id'])} preview (XYZ)", "wms")
    if not layer.isValid():
        raise RuntimeError(layer.error().summary())
    return QgsProject.instance().addMapLayer(layer)


def add_stac_connection(name="EOPF Explorer", url=STAC):
    """Register the STAC API in Browser > STAC (refresh the Browser panel to see it).

    Note: QGIS 4.2 does not yet recognise the 'application/vnd.zarr; version=3; profile=multiscales'
    assets, so you can browse/search items there but not add the reflectance data (README).
    """
    from qgis.core import QgsStacConnection
    data = QgsStacConnection.Data()
    data.url = url
    QgsStacConnection.addConnection(name, data)
    return name


# --------------------------------------------------------------------------- CLI

def _main(argv):
    """python3 eopf_geozarr.py search W S E N START END [MAX_CLOUD] | vrt ITEM [BAND ...]"""
    if len(argv) >= 7 and argv[0] == "search":
        bbox = tuple(float(v) for v in argv[1:5])
        print_items(search(bbox, argv[5], argv[6], float(argv[7]) if len(argv) > 7 else 100))
    elif len(argv) >= 2 and argv[0] == "vrt":
        print(build_vrt(argv[1], tuple(argv[2:]) or ("b04", "b03", "b02")))
    else:
        print(_main.__doc__)


if __name__ == "__main__" and os.path.basename(sys.argv[0] if sys.argv else "") == "eopf_geozarr.py":
    _main(sys.argv[1:])
