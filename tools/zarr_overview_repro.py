"""Self-contained repro: GDAL Zarr v3 multiscales - downsampled read fetches the full-res shard
before selecting the overview (lhoupert/qgis-eopf-geozarr#1) + group-path open failure (#4).
Usage: python3 zarr_overview_repro.py [workdir]   (needs GDAL >= 3.13 Python bindings + numpy)"""
import gzip, json, os, struct, sys, threading, http.server, numpy as np
from osgeo import gdal
gdal.UseExceptions()
W = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "repro_work")
CONSOLIDATED = os.environ.get("CONSOLIDATED", "YES") == "YES"  # EOPF store has inline consolidated metadata
STORE, GRP, N, INNER = os.path.join(W, "s.zarr"), "measurements/reflectance", 4096, 256
LEVELS = [("r10m", 1), ("r20m", 2), ("r40m", 4)]

def wjson(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True); json.dump(d, open(path, "w"), indent=1)

def write_sharded(path, a):  # one shard covering the whole array, gzip inner chunks, index at end
    n, parts, idx, off = a.shape[0], [], [], 0
    for i in range(0, n, INNER):
        for j in range(0, n, INNER):
            c = gzip.compress(np.ascontiguousarray(a[i:i+INNER, j:j+INNER]).tobytes(), 1)
            parts.append(c); idx += [off, len(c)]; off += len(c)
    os.makedirs(os.path.join(path, "c", "0"), exist_ok=True)
    open(os.path.join(path, "c", "0", "0"), "wb").write(b"".join(parts) + struct.pack("<%dQ" % len(idx), *idx))
    wjson(os.path.join(path, "zarr.json"), {"zarr_format": 3, "node_type": "array", "shape": [n, n],
        "data_type": "float32", "fill_value": "NaN", "dimension_names": ["y", "x"],
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [n, n]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [{"name": "sharding_indexed", "configuration": {"chunk_shape": [INNER, INNER],
            "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": 1}}],
            "index_codecs": [{"name": "bytes", "configuration": {"endian": "little"}}], "index_location": "end"}}],
        "attributes": {"grid_mapping": "spatial_ref"}})

def write_coord(path, dim, v):  # 1-D uncompressed coordinate array, as in the EOPF store
    os.makedirs(os.path.join(path, "c"), exist_ok=True); open(os.path.join(path, "c", "0"), "wb").write(v.tobytes())
    wjson(os.path.join(path, "zarr.json"), {"zarr_format": 3, "node_type": "array", "shape": [len(v)], "data_type": "float32",
        "fill_value": "NaN", "dimension_names": [dim], "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [len(v)]}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}], "attributes": {}})

def build():
    conv = [{"uuid": "d35379db-88df-4056-af3a-620245f8e347", "name": "multiscales"},
            {"uuid": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4", "name": "spatial:"},
            {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:"}]
    sp = lambda f: {"spatial:dimensions": ["y", "x"], "spatial:registration": "pixel", "proj:code": "EPSG:32630",
                    "spatial:shape": [N // f, N // f], "spatial:transform": [10.0*f, 0, 500000, 0, -10.0*f, 6400000]}
    wjson(os.path.join(STORE, "zarr.json"), {"zarr_format": 3, "node_type": "group", "attributes": {}})
    wjson(os.path.join(STORE, "measurements", "zarr.json"), {"zarr_format": 3, "node_type": "group", "attributes": {}})
    layout = [dict(asset=n, **{k: v for k, v in sp(f).items() if k.startswith("spatial:s") or k == "spatial:transform"},
                   **({"derived_from": "r10m", "transform": {"scale": [f, f], "translation": [0, 0]}} if f > 1 else {}))
              for n, f in LEVELS]
    wjson(os.path.join(STORE, GRP, "zarr.json"), {"zarr_format": 3, "node_type": "group", "attributes": dict(
        multiscales={"layout": layout, "resampling_method": "average"}, zarr_conventions=conv, **sp(1))})
    base = np.random.default_rng(0).random((N, N), dtype=np.float32)
    for name, f in LEVELS:
        wjson(os.path.join(STORE, GRP, name, "zarr.json"), {"zarr_format": 3, "node_type": "group",
              "attributes": dict(zarr_conventions=conv[1:], **sp(f))})
        write_sharded(os.path.join(STORE, GRP, name, "b04"), base.reshape(N//f, f, N//f, f).mean(axis=(1, 3)))
        for dim, v in (("x", 500000 + 10.0*f*(np.arange(N//f) + .5)), ("y", 6400000 - 10.0*f*(np.arange(N//f) + .5))):
            write_coord(os.path.join(STORE, GRP, name, dim), dim, v.astype(np.float32))
    if CONSOLIDATED:
        for g in (os.path.join(STORE, GRP), os.path.join(STORE, "measurements"), STORE): consolidate(g)

def consolidate(g):  # inline consolidated metadata in a group's zarr.json, as zarr-python 3 / the EOPF store do
    md = {}
    for dp, _, fs in os.walk(g):
        if "zarr.json" in fs and dp != g:
            d = json.load(open(os.path.join(dp, "zarr.json"))); d.pop("consolidated_metadata", None)
            md[os.path.relpath(dp, g)] = d
    d = json.load(open(os.path.join(g, "zarr.json")))
    d["consolidated_metadata"] = {"kind": "inline", "must_understand": False, "metadata": md}; wjson(os.path.join(g, "zarr.json"), d)

SERVED = {"bytes": 0, "reqs": []}
class H(http.server.SimpleHTTPRequestHandler):  # minimal Range-capable static server that counts bytes
    def __init__(s, *a, **k): super().__init__(*a, directory=W, **k)
    def log_message(s, *a): pass
    def do_GET(s, head=False):
        p = s.translate_path(s.path)
        if os.path.isdir(p) and s.path.endswith("/") and not head: return super().do_GET()  # HTML index, like the EOPF S3 proxy
        if not os.path.isfile(p): return s.send_error(404)
        size, rng = os.path.getsize(p), s.headers.get("Range")
        a, b = 0, size - 1
        if rng:
            x, y = rng.split("=")[1].split(",")[0].split("-")
            a, b = (size - int(y), size - 1) if x == "" else (int(x), min(int(y), size - 1) if y else size - 1)
        s.send_response(206 if rng else 200); s.send_header("Content-Length", str(b - a + 1))
        s.send_header("Accept-Ranges", "bytes")
        if rng: s.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
        s.end_headers()
        if head: return
        f = open(p, "rb"); f.seek(a); s.wfile.write(f.read(b - a + 1))
        SERVED["bytes"] += b - a + 1; SERVED["reqs"].append((s.path, a, b - a + 1))
    def do_HEAD(s): s.do_GET(head=True)
    def list_directory(s, p):  # "Index of" HTML listing that GDAL's /vsicurl/ parser understands
        names = sorted(n + "/" if os.path.isdir(os.path.join(p, n)) else n for n in os.listdir(p))
        body = ("<html><head><title>Index of %s</title></head><body><h1>Index of %s</h1><a href='../'>Parent Directory</a><br>%s</body></html>" % (s.path, s.path,
                "".join('<a href="%s%s">%s</a><br>' % (s.path, n, n) for n in names))).encode()
        s.send_response(200); s.send_header("Content-Type", "text/html"); s.send_header("Content-Length", str(len(body)))
        s.end_headers(); s.wfile.write(body)

def measure(label, fn):
    gdal.VSICurlClearCache(); SERVED["bytes"] = 0; SERVED["reqs"] = []
    r = fn()
    big = [(p, n) for p, _, n in SERVED["reqs"] if n > 4096]
    print("%-58s %10d bytes  %3d GETs  %s" % (label, SERVED["bytes"], len(SERVED["reqs"]), r))
    for p, n in big[:6]: print("      %-48s %9d" % (p, n))

if __name__ == "__main__":
    print("GDAL", gdal.__version__)
    if not os.path.exists(os.path.join(STORE, GRP, "r40m", "x", "c", "0")): build()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    U = "/vsicurl/http://127.0.0.1:%d/s.zarr" % srv.server_port
    for k, v in {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_PAM_ENABLED": "NO"}.items(): gdal.SetConfigOption(k, v)
    print("shard sizes:", {n: os.path.getsize(os.path.join(STORE, GRP, n, "b04/c/0/0")) for n, _ in LEVELS})
    path = lambda lvl: 'ZARR:"%s":/%s/%s/b04' % (U, GRP, lvl)
    ds = gdal.Open(path("r10m")); b = ds.GetRasterBand(1)
    print("r10m overviews:", [(b.GetOverview(i).XSize, b.GetOverview(i).YSize) for i in range(b.GetOverviewCount())])
    def rd(lvl, **kw):
        d = gdal.Open(path(lvl)); bb = d.GetRasterBand(1); bb.ReadRaster(**kw); return "ok"
    measure("1) r10m whole raster -> 1024x1024 buffer (should use r40m)", lambda: rd("r10m", buf_xsize=1024, buf_ysize=1024))
    measure("2) r40m direct full read 1024x1024", lambda: rd("r40m"))
    measure("3) r10m 1:1 window 256x256 at (1024,1024)", lambda: rd("r10m", xoff=1024, yoff=1024, xsize=256, ysize=256))
    print("--- #4: classic open of a group path")
    for s in ['ZARR:"%s":/%s' % (U, GRP), 'ZARR:"%s/%s"' % (U, GRP), 'ZARR:"%s":/%s' % (STORE, GRP), 'ZARR:"%s/%s"' % (STORE, GRP)]:
        try: d = gdal.Open(s); print("OK  ", len(d.GetSubDatasets()), "subdatasets |", s)
        except Exception as e: print("FAIL", str(e)[:90], "|", s)
    srv.shutdown()
