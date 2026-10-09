# QGIS + EOPF Explorer GeoZarr: getting started

How to open the Sentinel-2 L2A **GeoZarr** scenes of the [EOPF Sentinel Zarr Explorer](https://explorer.eopf.copernicus.eu)
in QGIS. They are cloud-native Zarr v3 stores with built-in multiscales (r10m ... r720m).
This page gives the fastest working recipe first, then the alternatives, and notes what is still broken.

> **Requirements**
> - **QGIS 4.x bundling GDAL >= 3.13.** GDAL 3.13 added Zarr v3 sharding and multiscales-as-overviews.
>   Tested only with **QGIS 4.2.2 (Flathub Flatpak) + GDAL 3.13.3 on Ubuntu 24.04**.
> - Check in the QGIS Python console (*Plugins > Python Console*):
>   ```python
>   from osgeo import gdal; print(gdal.__version__)                          # want >= 3.13
>   print(gdal.GetDriverByName("Zarr").GetMetadataItem("COMPRESSORS"))      # must list blosc
>   ```
> - Linux: `flatpak install flathub org.qgis.qgis//stable`. Pick the `stable` branch; `lts` is QGIS 3.28 with an old GDAL.
> - Windows/macOS: the build must also ship GDAL >= 3.13. **Not checked here.** Run the check above first.

Demo scene used below: `S2A_MSIL2A_20260719T112131_N0512_R037_T30VWJ_20260719T194158` (Aberdeenshire, EPSG:32630).
It is stored in a *tests-output* bucket and **may disappear (around 17 October 2026)**. If it has gone,
find another scene with `search()` (method D).

---

## Ways to open EOPF GeoZarr in QGIS

### A. Python console helper (default: easiest and fastest, with real reflectance values)

1. Download [`eopf_geozarr.py`](https://raw.githubusercontent.com/lhoupert/qgis-eopf-geozarr/main/eopf_geozarr.py) (raw file), for example to `~/Downloads`:
   `curl -o ~/Downloads/eopf_geozarr.py https://raw.githubusercontent.com/lhoupert/qgis-eopf-geozarr/main/eopf_geozarr.py`
2. In the QGIS Python console:

```python
import os; exec(open(os.path.expanduser("~/Downloads/eopf_geozarr.py")).read())

item_id = "S2A_MSIL2A_20260719T112131_N0512_R037_T30VWJ_20260719T194158"
add_rgb(item_id)                                                            # true colour, stretch 0-0.3
add_rgb(item_id, bands=("b08", "b04", "b03"))                              # false colour (NIR)
add_band(item_id, "b05")                                                    # one band; finest level it has (r20m)
add_band(item_id, "b04", res="r60m")                                        # force a level
```

`item_id` can be a STAC item id, a STAC item dict, the `.zarr` store URL, or the `reflectance` asset href.
The helper:
- looks up the store through STAC;
- lists the levels with GDAL;
- writes a small **VRT** to `~/.cache/eopf_geozarr/` (override with the `EOPF_GEOZARR_CACHE` env var). The VRT reads the finest level and lists every coarser
  GeoZarr level as an explicit `<Overview>`;
- adds the VRT as a normal GDAL raster layer.

The bands keep their float reflectance values, so the Identify tool, the raster calculator and Processing all work on real data.

What to expect (QGIS 4.2.2, home connection). Adding the layer takes about 2 s. The VRT never reads the 10 m
arrays when zoomed out, but which coarser level GDAL picks depends on how many device pixels the map canvas has:
- **Real GUI, maximised window on a HiDPI screen** (~4400x2500 device px): the first full-scene RGB render read
  the whole r20m level, ~250 MB in ~35 s. After that, re-renders and pans at 1:250k and 1:10k come from cache in
  under 1 s; a new 1:50k area reads 40-90 MB of r10m in 13-16 s. QGIS memory grew to ~4.5 GB over a pan/zoom session.
- **Small canvas** (headless 1000x1000 px render): a full scene reads r120m, ~7 MB in ~2.7 s; a 10 km view ~4 s.

That HiDPI is why GDAL chose r20m, and that the memory growth is GDAL's block cache (default 5% of RAM), are
likely but not yet tested.

Without QGIS (plain GDAL Python), the same file can write the VRT for you to open in any GDAL tool:
```sh
python3 eopf_geozarr.py vrt <item_id> b04 b03 b02     # prints the .vrt path
```

### B. No-code quick look: XYZ tiles from EOPF TiTiler

Every STAC item has an `xyz` link to tiles rendered on the server. Open the item JSON
(`https://api.explorer.eopf.copernicus.eu/stac/collections/sentinel-2-l2a/items/<item_id>`) and copy the `href`
of the link with `"rel": "xyz"`. In the QGIS *Browser* panel, right-click *XYZ Tiles > New Connection...*, paste it as the URL,
set the max zoom to 14, then double-click the new connection. (This GUI route was not clicked through for this guide;
`add_xyz_preview(item_id)` from the helper was tested.)

This is the fastest way to get a picture (~1-6 s for a full scene). The tiles are **8-bit styled PNG in Web Mercator**,
so you get no reflectance values and cannot restyle them.
An XYZ layer has no real extent: *Zoom to Layer* zooms out to the whole world, so zoom to the scene another way
(for example zoom to a VRT layer from method A).

### C. Plain GUI, one band (no helper)

Go to *Layer > Add Raster Layer*, choose **Source type: File** (not "Protocol: HTTP(S)"), and paste into the source box:

```
ZARR:"/vsicurl/<store>":/measurements/reflectance/r60m/b04
```

`<store>` is the item's `store` link, for example
`https://s3.explorer.eopf.copernicus.eu/esa-zarr-sentinel-explorer-fra/tests-output/sentinel-2-l2a/<item_id>.zarr`.
Pasting `ZARR:"/vsicurl/<store>/measurements/reflectance"` instead lists all 63 band/level arrays to choose from.

This is the simplest route, but **avoid the r10m arrays unless you stay zoomed in**. With GDAL 3.13.3, a zoomed-out
view first downloads the whole 10 m shard (~336 MB per band) and only then reads the coarse level. One full-scene
render took 40-140 s and used ~1 GB of RAM. An RGB stack built with `gdal.BuildVRT` / *Build Virtual Raster*
pulls ~1 GB, so use method A instead.

### D. Find scenes (STAC)

```python
items = search(bbox=(-3.0, 56.8, -1.7, 57.7), start="2026-07-01", end="2026-09-30", max_cloud=20, limit=10)
print_items(items)          # date, MGRS tile, cloud %, id  (least cloudy first)
add_rgb(items[0])           # open the least cloudy one
add_stac_connection()       # adds "EOPF Explorer" under Browser > STAC (refresh the Browser panel)
```

From a terminal: `python3 eopf_geozarr.py search -3.0 56.8 -1.7 57.7 2026-07-01 2026-09-30 20`.

The **QGIS STAC browser** (QGIS 4.2) can browse and search the catalogue, but it cannot add these scenes yet.
It ignores assets of type `application/vnd.zarr; version=3; profile=multiscales`
(QGIS issue [#67460](https://github.com/qgis/QGIS/issues/67460); fix in PR [#67558](https://github.com/qgis/QGIS/pull/67558), not yet merged).

### Comparison

| Method | Keeps multiscales? | RGB? | Reflectance values? | Measured speed (full scene, 1000 px) | When to use |
|---|---|---|---|---|---|
| **A. Helper VRT (default)** | Yes (r10m ... r720m) | Yes | Yes (float) | 1000 px canvas: ~2-3 s, ~7 MB. HiDPI GUI: first render ~35 s, ~250 MB (r20m), then cached | Almost always |
| B. TiTiler XYZ | On the server | Yes (styled) | No (8-bit PNG) | ~1-6 s | Quick look, browsing many scenes |
| C. Plain `ZARR:` layer, r60m | No (fixed 60 m) | One band | Yes | ~3.5 s, ~12 MB (+ ~2.5 s to open) | No-code, single band |
| C. Plain `ZARR:` layer, r10m | Detected but defeated by the GDAL bug | One band | Yes | 40-140 s, ~338 MB, ~1 GB RAM | Only when zoomed in close |
| `gdal.BuildVRT` over r10m arrays | Nominally | Yes | Yes | 120-160 s, ~1 GB | Avoid |
| [qgis_geozarr](https://github.com/wietzesuijker/qgis-geozarr) plugin v0.2.0 | Reportedly the same VRT approach (not verified) | Yes | Yes | **untested** | If you prefer a plugin |

Timings are from headless 1000 px renders unless stated, and vary 2-3x with the network. The byte counts are the
reliable comparison. Method A was also checked in the real GUI (numbers above); the other rows were not.

---

## Recommended GDAL settings

```
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
GDAL_PAM_ENABLED=NO
```

These stop GDAL from probing for sidecar files over HTTP, which makes opening a plain `ZARR:` layer a little
faster (in a GUI test, 9 requests dropped to 5 and 1.6-2.0 s to 1.2 s; an earlier headless test saw a bigger gain).
Rendering is unchanged. `eopf_geozarr.py` already sets both, so method A doesn't need them.
- In QGIS: *Settings > Options > System > Environment*. Tick **Use custom variables**, add both (Apply: *Overwrite*),
  then **restart QGIS**. (This is not *Options > Variables*: those are expression variables.)
- Flatpak, once from a terminal:
  `flatpak override --user --env=GDAL_PAM_ENABLED=NO --env=GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR org.qgis.qgis`
- `eopf_geozarr.py` also sets them with `gdal.SetConfigOption` when you load it. Environment variables are the reliable route.

Neither GUI route was clicked through for this guide. Both use standard QGIS and Flatpak mechanisms.

---

## Known issues

- [#1](https://github.com/lhoupert/qgis-eopf-geozarr/issues/1) GDAL 3.13.3 Zarr: zoomed-out reads download the whole full-res shard before using the overview
- [#2](https://github.com/lhoupert/qgis-eopf-geozarr/issues/2) QGIS STAC browser ignores Zarr assets whose media type has parameters
- [#3](https://github.com/lhoupert/qgis-eopf-geozarr/issues/3) Add Raster Layer 'Protocol: HTTP(S)' cannot open a Zarr store
- [#4](https://github.com/lhoupert/qgis-eopf-geozarr/issues/4) GDAL Zarr: group path in `ZARR:"root":/group` gives a misleading error (intended behaviour, docs gap)
- [#5](https://github.com/lhoupert/qgis-eopf-geozarr/issues/5) Possible half-pixel georeferencing offset (spatial:transform looks like pixel centres)
- [#6](https://github.com/lhoupert/qgis-eopf-geozarr/issues/6) EOPF Explorer TiTiler: collection mosaic tiles fail for Zarr items (422/500)
- [#7](https://github.com/lhoupert/qgis-eopf-geozarr/issues/7) STAC items lack a data-coverage / nodata percentage for picking the best scene

---

## Headless / scripting (PyQGIS outside the GUI)

The Flatpak's `python3` does not find PyQGIS by default. Set `PYTHONPATH` and the prefix, or the GDAL/XYZ (`wms`)
providers fail to load ("Cannot instantiate the 'wms' data provider"):

```sh
flatpak run --filesystem=home --env=PYTHONPATH=/app/share/qgis/python --env=QT_QPA_PLATFORM=offscreen \
  --command=python3 org.qgis.qgis my_script.py
```
```python
from qgis.core import QgsApplication
QgsApplication.setPrefixPath("/app", True)
app = QgsApplication([], False)
app.initQgis()
import eopf_geozarr as eg                # with eopf_geozarr.py next to the script
layer = eg.add_rgb(eg.DEMO_ITEM)
```

Plain GDAL Python gotcha: `gdal.Open(uri).GetRasterBand(1).ReadAsArray()` crashes, because the dataset is
garbage-collected. Keep a reference: `ds = gdal.Open(uri); a = ds.GetRasterBand(1).ReadAsArray()`.

## Examples

- [`examples/uk_mosaic.py`](examples/uk_mosaic.py) (advanced) builds an offline preview mosaic in EPSG:27700
  for the UK or any bbox. It picks the least cloudy scene per MGRS tile, with backups from other orbits, and adds
  clickable footprints that carry the `item_id`. The full UK run makes a ~130 MB COG. Tested on a small bbox.
  The output is a picture, not reflectance.
- [`tools/zarr_overview_repro.py`](tools/zarr_overview_repro.py) reproduces issue #1 (and #4) without EOPF data:
  it writes a small local sharded Zarr v3 store with multiscales, serves it over HTTP and counts the bytes GDAL
  reads. On GDAL 3.13.3 a 1024 px read of a 4096 px array pulls 64 MB instead of the 3.6 MB overview.
  Run `python3 tools/zarr_overview_repro.py /tmp/work` with any Python that has GDAL >= 3.13 and numpy.
- [`examples/screenshot.py`](examples/screenshot.py) saves the QGIS window and map canvas as PNG from the
  console (tested only with an offscreen stand-in window, not the real QGIS GUI).

## How it works (GeoZarr multiscales and why the VRT is needed)

Each scene's `measurements/reflectance` group holds the bands at six levels: `r10m` (b02, b03, b04, b08),
`r20m`, `r60m`, `r120m`, `r360m` and `r720m` (all bands, b09 from r60m). Each array is Zarr v3 with
**sharding**: a 10 m band is a single ~336 MB shard of 244x244 inner chunks, which GDAL reads with HTTP range
requests. GDAL 3.13 turns the GeoZarr `multiscales` metadata into overviews, so `gdalinfo` on `r10m/b04` shows
overviews 5490, 1830, 915, 305 and 152.

The catch is in GDAL 3.13.3, and it is still there on master. When a read is downsampled,
`GDALRasterBandFromArray::IRasterIO` calls `AdviseRead` on the *full-resolution* window before it picks the
overview. For a sharded array that prefetches every 10 m chunk in view, and no config option turns it off.
A VRT whose bands have explicit `<Overview>` entries pointing at the r20m ... r720m arrays avoids this, because
the overview is chosen at VRT level. A full-scene view then reads a coarse level (r120m on a small canvas, r20m on a large HiDPI one) instead of r10m; a close zoom reads
only the r10m chunks in the window. `eopf_geozarr.build_vrt()` writes exactly that.

## Licence

MIT; see [LICENSE](LICENSE). Sentinel data: [Copernicus Sentinel data legal notice](https://sentinel.esa.int/documents/247904/690755/Sentinel_Data_Legal_Notice).
