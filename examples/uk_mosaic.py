"""Advanced example: a local true-colour Sentinel-2 L2A preview mosaic of the UK (or any bbox).

For each MGRS tile it picks the least-cloudy, near-complete scene in the date range (plus the
least-cloudy scene of up to three other orbits, drawn underneath to fill swath-edge gaps),
downloads the server-rendered RGBA preview (~120 m, from the EOPF TiTiler) and warps everything
into one British National Grid (EPSG:27700) Cloud-Optimised GeoTIFF.

The result is a fast, offline *picture* (8-bit, styled) - not reflectance. It also writes
uk_scenes.geojson (one footprint per tile with its item_id), so you can click a tile in QGIS
and open the real data with eopf_geozarr.add_rgb(item_id).

Run with the GDAL bundled in the QGIS Flatpak (or any Python with GDAL >= 3.x):
    flatpak run --filesystem=home --command=python3 org.qgis.qgis examples/uk_mosaic.py
Options via environment variables: OUT (default ~/eopf_uk_previews), BBOX="W,S,E,N",
DATETIME="2026-06-01/2026-09-30", MAX_CLOUD (30). The full UK run is ~300+ previews and a
~130 MB mosaic; try a small BBOX first, e.g. (Aberdeenshire, 2 tiles, ~10 s):
    flatpak run --filesystem=home --env=BBOX=-3.0,56.9,-1.8,57.4 --env=DATETIME=2026-07-01/2026-07-31 --command=python3 org.qgis.qgis examples/uk_mosaic.py

Then, in the QGIS Python console, add the mosaic and the footprints:
    import os; exec(open(os.path.expanduser("~/qgis-eopf-geozarr/examples/uk_mosaic.py")).read())
    load_in_qgis()                          # or load_in_qgis("/path/to/OUT")
"""
import gzip
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from osgeo import gdal

STAC = "https://api.explorer.eopf.copernicus.eu/stac"
RASTER = "https://api.explorer.eopf.copernicus.eu/raster"
COLLECTION = "sentinel-2-l2a"
UK_BBOX = tuple(float(v) for v in os.environ.get("BBOX", "-8.7,49.8,2.0,60.9").split(","))
_start, _end = os.environ.get("DATETIME", "2026-06-01/2026-09-30").split("/")
DATETIME = f"{_start}T00:00:00Z/{_end}T23:59:59Z"
MAX_CLOUD = float(os.environ.get("MAX_CLOUD", 30))
BACKUP_MAX_CLOUD = 60  # tile-level cloud cover; a cloudier scene can still be clear where the gap is
OUT = os.path.expanduser(os.environ.get("OUT", "~/eopf_uk_previews"))
RENDER = urllib.parse.urlencode([
    ("max_size", 1024),
    ("rescale", "0,1"),
    ("color_formula", "gamma rgb 1.3, sigmoidal rgb 6 0.1, saturation 1.2"),
    ("variables", "/measurements/reflectance:b04"),
    ("variables", "/measurements/reflectance:b03"),
    ("variables", "/measurements/reflectance:b02"),
    ("bidx", 1),
])


def search(max_cloud=MAX_CLOUD):
    params = {
        "collections": COLLECTION,
        "bbox": ",".join(map(str, UK_BBOX)),
        "datetime": DATETIME,
        "filter": f"eo:cloud_cover < {max_cloud}",
        "filter-lang": "cql2-text",
        "limit": 100,
    }
    url, items = f"{STAC}/search?{urllib.parse.urlencode(params)}", []
    while url:
        with urllib.request.urlopen(url, timeout=120) as r:
            page = json.load(r)
        items += page["features"]
        url = next((l["href"] for l in page.get("links", []) if l["rel"] == "next"), None)
    return items


def footprint_area(item):
    """Approximate footprint area (deg² scaled by cos(lat)) — only used to compare scenes of the same tile."""
    geom = item["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    area = 0.0
    for ring in (p[0] for p in polys):
        k = math.cos(math.radians(sum(y for _, y in ring) / len(ring)))
        area += abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(ring, ring[1:]))) / 2 * k
    return area


def best_per_tile(items):
    tiles = {}
    for item in items:
        tiles.setdefault(item["properties"]["grid:code"], []).append(item)
    best = {}
    for tile, scenes in tiles.items():
        full = max(footprint_area(s) for s in scenes)
        complete = [s for s in scenes if footprint_area(s) >= 0.9 * full]
        best[tile] = min(complete, key=lambda s: s["properties"]["eo:cloud_cover"])
    return best


def backups_per_tile(items, best, n=3):
    """Least-cloudy scene from each relative orbit, drawn underneath to fill swath-edge gaps.

    Different orbits image different parts of the tile, so between them they cover what the best scene missed.
    """
    backups = []
    for tile, top in best.items():
        per_orbit = {}
        for i in sorted((i for i in items if i["properties"]["grid:code"] == tile and i["id"] != top["id"]),
                        key=lambda s: s["properties"]["eo:cloud_cover"]):
            per_orbit.setdefault(i["id"].split("_")[4], i)
        # Least cloudy last, so it is drawn on top of the other backups.
        backups += sorted(list(per_orbit.values())[:n], key=lambda s: -s["properties"]["eo:cloud_cover"])
    return backups


def download(item):
    path = os.path.join(OUT, "tiles", f"{item['id']}.tif")
    if not os.path.exists(path):
        url = f"{RASTER}/collections/{COLLECTION}/items/{item['id']}/preview.tif?{RENDER}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=300) as r, open(path + ".part", "wb") as f:
                    data = r.read()
                    f.write(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
                os.replace(path + ".part", path)
                break
            except Exception as e:
                if attempt == 2:
                    print("  FAILED", item["id"], e)
                    return None
    return path


def main():
    gdal.UseExceptions()
    os.makedirs(os.path.join(OUT, "tiles"), exist_ok=True)
    items = search()
    best = best_per_tile(items)
    print(f"{len(items)} scenes < {MAX_CLOUD}% cloud -> {len(best)} tiles")

    backups = backups_per_tile(search(BACKUP_MAX_CLOUD), best)
    with ThreadPoolExecutor(8) as pool:
        paths = [p for p in pool.map(download, best.values()) if p]
        backup_paths = [p for p in pool.map(download, backups) if p]
    print(f"downloaded {len(paths)}/{len(best)} previews + {len(backup_paths)}/{len(backups)} gap-fill backups")

    # Scene footprints, so you can click a tile in QGIS and get its item id for eopf_geozarr.add_rgb().
    features = [{
        "type": "Feature",
        "geometry": s["geometry"],
        "properties": {"tile": t, "item_id": s["id"], "date": s["properties"]["datetime"][:10],
                       "cloud_cover": round(s["properties"]["eo:cloud_cover"], 1)},
    } for t, s in sorted(best.items())]
    with open(os.path.join(OUT, "uk_scenes.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)

    # Least-cloudy scenes last, so they are drawn on top where tiles overlap.
    order = sorted(paths, key=lambda p: -next(s["properties"]["eo:cloud_cover"] for s in best.values()
                                              if p.endswith(s["id"] + ".tif")))
    mosaic = os.path.join(OUT, "uk_mosaic_27700.tif")
    gdal.Warp(mosaic + ".tmp.tif", backup_paths + order, format="COG", dstSRS="EPSG:27700", xRes=120, yRes=120,
              resampleAlg="bilinear", srcAlpha=True, dstAlpha=True, multithread=True,
              creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "OVERVIEWS=AUTO", "NUM_THREADS=ALL_CPUS"],
              warpOptions=["NUM_THREADS=ALL_CPUS"])
    os.replace(mosaic + ".tmp.tif", mosaic)
    print("mosaic:", mosaic, f"{os.path.getsize(mosaic) / 1e6:.0f} MB")


def load_in_qgis(folder=OUT):
    """From the QGIS Python console: add the mosaic and the clickable scene footprints."""
    from qgis.core import QgsFillSymbol, QgsProject, QgsRasterLayer, QgsVectorLayer
    scenes = QgsVectorLayer(os.path.join(folder, "uk_scenes.geojson"), "Scenes (identify -> item_id)", "ogr")
    scenes.renderer().setSymbol(QgsFillSymbol.createSimple(
        {"color": "0,0,0,0", "outline_color": "255,255,0", "outline_width": "0.3"}))
    mosaic = QgsRasterLayer(os.path.join(folder, "uk_mosaic_27700.tif"), "S2 L2A preview mosaic")
    QgsProject.instance().addMapLayer(mosaic)
    QgsProject.instance().addMapLayer(scenes)
    return mosaic


if __name__ == "__main__" and os.path.basename(sys.argv[0] if sys.argv else "") == "uk_mosaic.py":
    main()
