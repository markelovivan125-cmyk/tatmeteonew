#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""radar-wms-plus — улучшенная копия savelov/radar-wms (BALTRAD WMS + ветка xsection).
Чистый Python вместо MapServer: WMS 1.1.1/1.3.0 + ВЕРТИКАЛЬНЫЙ СРЕЗ + JSON-проба.
БД (radar_dataset) и конфиг совместимы с оригиналом. База: numpy, pillow.
rasterio/pyproj — опционально (сложные GeoTIFF и нестандартные проекции)."""
import ast, io, json, math, os, sqlite3, struct, threading
import configparser
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import parse_qs
from xml.sax.saxutils import escape
import numpy as np
from PIL import Image, ImageDraw, ImageFont
try: import rasterio
except ImportError: rasterio = None
try: from pyproj import CRS as _PCRS, Transformer as _PTr, Geod as _PGeod
except ImportError: _PCRS = _PTr = _PGeod = None
HERE = os.path.dirname(os.path.abspath(__file__))
def _find_cfg():
    env = os.environ.get("RADAR_CFG")
    if env: return env
    for name in ("baltrad_wms.cfg", "demo.cfg"):
        p = os.path.join(HERE, name)
        if os.path.exists(p): return p
    raise SystemExit("Нет конфига: положите baltrad_wms.cfg рядом или запустите --demo")
CFG_PATH = _find_cfg()
config = configparser.ConfigParser(); config.read(CFG_PATH, encoding="utf-8")
DB_PATH = config.get("locations", "db_uri").split("///")[-1]
ONLINE  = config.get("locations", "online_resource", fallback="http://localhost:8080/wms")
XSEC    = dict(config.items("cross_section")) if config.has_section("cross_section") else {}
def load_datasets():
    ds = {}
    for sec in config.sections():
        if sec.startswith("dataset") and config.has_option(sec, "name"):
            d = dict(config.items(sec)); ds[d["name"]] = d
    return ds
DATASETS = load_datasets()
_MERC = {3857, 3785, 900913, 102100, 102113}; _R = 6378137.0
def norm_crs(s):
    s = str(s or "EPSG:3857").strip(); u = s.upper()
    if u in ("CRS:84", "URN:OGC:DEF:CRS:OGC:1.3:CRS84"): return 4326
    if u.startswith("URN:OGC:DEF:CRS:EPSG:"): return int(u.rsplit(":", 1)[-1])
    if u.startswith("EPSG:"): return int(u.split(":")[1])
    return s
def _to_merc(lon, lat):
    lat = np.clip(lat, -85.05112878, 85.05112878)
    return np.radians(lon)*_R, np.log(np.tan(np.pi/4 + np.radians(lat)/2))*_R
def _from_merc(x, y):
    return np.degrees(x/_R), np.degrees(2*np.arctan(np.exp(y/_R)) - np.pi/2)
class _Tr:
    def __init__(self, s, d):
        self.s, self.d = norm_crs(s), norm_crs(d); self._p = None
        if not ((self.s in _MERC or self.s == 4326) and (self.d in _MERC or self.d == 4326)):
            if _PTr is None: raise ValueError("для проекции %s->%s установите pyproj" % (s, d))
            self._p = _PTr.from_crs(_PCRS.from_user_input(s), _PCRS.from_user_input(d), always_xy=True)
    def transform(self, x, y):
        scalar = np.isscalar(x)
        if self._p is not None:
            X, Y = self._p.transform(x, y)
            return (float(X), float(Y)) if scalar else (np.asarray(X), np.asarray(Y))
        x = np.asarray(x, float); y = np.asarray(y, float)
        lon, lat = (x, y) if self.s == 4326 else _from_merc(x, y)
        X, Y = (lon, lat) if self.d == 4326 else _to_merc(lon, lat)
        return (float(X), float(Y)) if scalar else (X, Y)
@lru_cache(maxsize=64)
def transformer(src, dst): return _Tr(str(src), str(dst))
def lat_first(crs, ver):
    if ver != "1.3.0": return False
    n = norm_crs(crs)
    if n == 4326: return True
    if _PCRS is not None and isinstance(n, int):
        ax = _PCRS.from_epsg(n).axis_info
        return bool(ax) and ax[0].direction == "north"
    return False
class _Geod:
    R = 6371000.0
    def inv(self, lon1, lat1, lon2, lat2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = p2-p1, math.radians(lon2-lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        dist = 2*self.R*math.asin(min(1.0, math.sqrt(a)))
        az = math.degrees(math.atan2(math.sin(dl)*math.cos(p2),
             math.cos(p1)*math.sin(p2)-math.sin(p1)*math.cos(p2)*math.cos(dl))) % 360
        return az, 0.0, dist
    def fwd(self, lon, lat, az, dist):
        d = dist/self.R; t = math.radians(az)
        p1, l1 = math.radians(lat), math.radians(lon)
        p2 = math.asin(math.sin(p1)*math.cos(d)+math.cos(p1)*math.sin(d)*math.cos(t))
        l2 = l1 + math.atan2(math.sin(t)*math.sin(d)*math.cos(p1), math.cos(d)-math.sin(p1)*math.sin(p2))
        return math.degrees(l2), math.degrees(p2), 0.0
GEOD = _PGeod(ellps="WGS84") if _PGeod else _Geod()
def _read_tiff_simple(path):
    b = open(path, "rb").read()
    en = "<" if b[:2] == b"II" else ">"
    ifd = struct.unpack(en+"I", b[4:8])[0]
    n = struct.unpack(en+"H", b[ifd:ifd+2])[0]
    sizes = {1:1, 2:1, 3:2, 4:4, 5:8, 12:8}; fmts = {1:"B", 3:"H", 4:"I", 12:"d"}
    tags = {}
    for k in range(n):
        e = ifd + 2 + 12*k
        tag, typ, cnt = struct.unpack(en+"HHI", b[e:e+8])
        sz = sizes.get(typ, 1)*cnt
        vo = e+8 if sz <= 4 else struct.unpack(en+"I", b[e+8:e+12])[0]
        raw = b[vo:vo+sz]
        if typ == 2: val = raw.rstrip(b"\0").decode("utf-8", "replace")
        elif typ == 5: val = struct.unpack(en+"%dI" % (2*cnt), raw)
        else: val = struct.unpack(en+"%d%s" % (cnt, fmts[typ]), raw)
        tags[tag] = val
    W, H = tags[256][0], tags[257][0]
    bps = tags.get(258, [8])[0]; sf = tags.get(339, [1])[0]
    dt = {(8,1):"u1",(8,2):"i1",(16,1):"u2",(16,2):"i2",(32,3):"f4",(32,1):"u4",(64,3):"f8"}[(bps, sf)]
    buf = b"".join(b[o:o+c] for o, c in zip(tags[273], tags[279]))
    arr = np.frombuffer(buf, dtype=np.dtype(en+dt)).reshape(H, W)
    sx, sy = tags[33550][0], tags[33550][1]; tp = tags[33922]
    t = (sx, 0.0, tp[3]-sx*tp[0], 0.0, -sy, tp[4]+sy*tp[1])
    epsg = None; gk = tags.get(34735, [])
    for i in range(4, len(gk)-3, 4):
        key, loc, cnt, val = gk[i:i+4]
        if key in (3072, 2048) and loc == 0: epsg = val
    return arr, t, ("EPSG:%d" % epsg if epsg else None)
_SAFE = {"log10": np.log10, "log": np.log, "exp": np.exp, "sqrt": np.sqrt,
         "abs": np.abs, "pow": np.power, "min": np.minimum, "max": np.maximum}
def safe_eval(expr, v):
    tree = ast.parse(expr, mode="eval")
    for nd in ast.walk(tree):
        if isinstance(nd, ast.Name) and nd.id != "v" and nd.id not in _SAFE:
            raise ValueError("запрещённое имя в decode: " + nd.id)
        if isinstance(nd, ast.Call) and not (isinstance(nd.func, ast.Name) and nd.func.id in _SAFE):
            raise ValueError("запрещённый вызов в decode")
        if isinstance(nd, (ast.Attribute, ast.Subscript, ast.Lambda)):
            raise ValueError("запрещённая конструкция в decode")
    return eval(compile(tree, "<decode>", "eval"), {"__builtins__": {}}, dict(_SAFE, v=v))
def nodata_set(d): return {float(x) for x in d.get("nodata", "").split(",") if x.strip()}
def decode_array(d, raw):
    out = np.asarray(raw, float).copy()
    for nd in nodata_set(d): out[np.asarray(raw) == nd] = np.nan
    if d.get("decode"):
        m = np.isfinite(out); out[m] = safe_eval(d["decode"], out[m])
    return out
def style_classes(style):
    out = []
    for _, val in sorted(config.items(style), key=lambda kv: kv[0]):
        p = [x.strip() for x in val.split(",")]
        out.append((p[0], float(p[1]), float(p[2]), tuple(int(c) for c in p[3:6])))
    return out
def phys_palette(d):
    expr = d.get("decode"); nod = nodata_set(d); pal = []
    for label, lo, hi, rgb in style_classes(d["style"]):
        if nod and lo >= min(nod)-1e-9 and hi <= max(nod)+1e-9: continue
        plo = float(safe_eval(expr, lo)) if expr else lo
        phi = float(safe_eval(expr, hi)) if expr else hi
        if plo > phi: plo, phi = phi, plo
        pal.append((plo, phi, rgb, label))
    return pal
def colorize(vals, pal):
    rgba = np.zeros(vals.shape+(4,), np.uint8)
    for lo, hi, rgb, _ in pal:
        m = (vals > lo) & (vals <= hi)
        rgba[m, :3] = rgb; rgba[m, 3] = 255
    if pal:
        m = np.isfinite(vals) & (vals > pal[-1][1])
        rgba[m, :3] = pal[-1][2]; rgba[m, 3] = 255
    return rgba_cache_lock = threading.Lock(); _cache = {"stamp": None, "layers": {}}
def _db_stamp():
    st = []
    for p in (DB_PATH, DB_PATH + "-wal"):
        try: st.append(os.path.getmtime(p))
        except OSError: st.append(None)
    return tuple(st)
def layer_datasets(name):
    stamp = _db_stamp()
    with _cache_lock:
        if stamp != _cache["stamp"]:
            _cache["layers"].clear(); _cache["stamp"] = stamp
        if name in _cache["layers"]: return _cache["layers"][name]
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        rows = con.execute("SELECT timestamp, geotiff_path, projdef, bbox_original, title, unit, style "
                           "FROM radar_dataset WHERE name=? ORDER BY timestamp DESC", (name,)).fetchall()
    finally:
        con.close()
    ds = [{"timestamp": datetime.fromisoformat(r[0]), "geotiff_path": r[1], "projdef": r[2],
           "bbox_original": r[3], "title": r[4], "unit": r[5], "style": r[6]} for r in rows]
    with _cache_lock: _cache["layers"][name] = ds
    return ds
def base_name(layer): return layer.replace("_contour", "")
def naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
def parse_time(s): return naive_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))
def pick_dataset(layer, time_value, tolerance_min=0):
    rows = layer_datasets(base_name(layer))
    if not rows: return None
    if not time_value or time_value in ("-1", ""): return rows[0]
    if "/" in time_value:
        t0, t1 = (parse_time(t) for t in time_value.split("/", 1))
        for r in rows:
            if t0 < r["timestamp"] <= t1: return r
        return None
    t = parse_time(time_value); best = None; bd = timedelta(minutes=tolerance_min)
    for r in rows:
        d = abs(r["timestamp"] - t)
        if d <= bd: best, bd = r, d
    return best
@lru_cache(maxsize=64)
def _raster(path, mtime):
    if rasterio is not None:
        with rasterio.open(path) as src:
            return src.read(1), tuple(src.transform), (src.crs.to_string() if src.crs else None)
    return _read_tiff_simple(path)
def raster_physical(rec, d):
    data, t, crs = _raster(rec["geotiff_path"], os.path.getmtime(rec["geotiff_path"]))
    return decode_array(d, data), t, crs or rec["projdef"] or "EPSG:3857"
def _pixels(t, x, y):
    a, b, c, dd, e, f = t
    det = a*e - b*dd
    col = np.floor((e*(x-c) - b*(y-f)) / det).astype(np.int64)
    row = np.floor((-dd*(x-c) + a*(y-f)) / det).astype(np.int64)
    return col, row
def sample_grid(rec, d, sx, sy):
    vals, t, crs = raster_physical(rec, d)
    col, row = _pixels(t, sx, sy)
    ok = (col >= 0) & (row >= 0) & (col < vals.shape[1]) & (row < vals.shape[0])
    out = np.full(sx.shape, np.nan); out[ok] = vals[row[ok], col[ok]]
    return out
def sample_lonlat(rec, d, lons, lats):
    vals, t, crs = raster_physical(rec, d)
    x, y = transformer("EPSG:4326", crs).transform(np.asarray(lons), np.asarray(lats))
    col, row = _pixels(t, x, y)
    ok = (col >= 0) & (row >= 0) & (col < vals.shape[1]) & (row < vals.shape[0])
    out = np.full(len(lons), np.nan); out[ok] = vals[row[ok], col[ok]]
    return out
def bbox_lonlat(rec):
    b = [float(v) for v in rec["bbox_original"].split(",")]
    crs = _raster(rec["geotiff_path"], os.path.getmtime(rec["geotiff_path"]))[2] or rec["projdef"] or "EPSG:3857"
    xs = np.linspace(b[0], b[2], 9); ys = np.linspace(b[1], b[3], 9)
    px = np.concatenate([xs, xs, np.full(9, b[0]), np.full(9, b[2])])
    py = np.concatenate([np.full(9, b[1]), np.full(9, b[3]), ys, ys])
    lon, lat = transformer(crs, "EPSG:4326").transform(px, py)
    return [float(lon.min())-.3, float(lat.min())-.3, float(lon.max())+.3, float(lat.max())+.3]
def font(sz):
    for p in (config.get("settings", "font", fallback=""),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              os.path.join(HERE, "DejaVuSans.ttf")):
        if p:
            try: return ImageFont.truetype(p, sz)
            except Exception: pass
    try:
        import matplotlib
        return ImageFont.truetype(os.path.join(os.path.dirname(matplotlib.__file__),
                                  "mpl-data", "fonts", "ttf", "DejaVuSans.ttf"), sz)
    except Exception:
        return ImageFont.load_default()
def png_bytes(img):
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()
def _target_grid(p):
    w, h = int(p["WIDTH"]), int(p["HEIGHT"])
    crs = p.get("CRS") or p.get("SRS") or "EPSG:3857"
    b = [float(v) for v in p["BBOX"].split(",")]
    if lat_first(crs, p.get("VERSION", "1.1.1")): b = [b[1], b[0], b[3], b[2]]
    x = b[0] + (np.arange(w)+.5)*(b[2]-b[0])/w
    y = b[3] - (np.arange(h)+.5)*(b[3]-b[1])/h
    X, Y = np.meshgrid(x, y)
    return crs, b, X, Y, w, h
def get_map(p):
    crs, b, X, Y, w, h = _target_grid(p)
    names = (p.get("LAYERS") or p.get("LAYER", "")).split(",")
    ops = (p.get("OPACITY") or "70").split(",")
    out = np.zeros((h, w, 4), float)
    for i, name in enumerate(names):
        name = name.strip()
        if not name or base_name(name) not in DATASETS: continue
        rec = pick_dataset(name, p.get("TIME"))
        if rec is None: continue
        d = DATASETS[base_name(name)]
        sx, sy = transformer(crs, raster_physical(rec, d)[2]).transform(X, Y)
        rgba = colorize(sample_grid(rec, d, sx, sy), phys_palette(d)).astype(float)
        try: op = max(0, min(100, int(ops[i])))/100.0
        except (IndexError, ValueError): op = .7
        a = rgba[..., 3:4]/255.0*op
        out[..., :3] = out[..., :3]*(1-a) + rgba[..., :3]*a
        out[..., 3:4] = a + out[..., 3:4]*(1-a)
    img = Image.fromarray(out.astype(np.uint8), "RGBA")
    fmt = (p.get("FORMAT") or "image/png").lower()
    if "jpeg" in fmt or (p.get("TRANSPARENT") or "true").lower() == "false":
        bg = Image.new("RGB", img.size, (255, 255, 255)); bg.paste(img, mask=img.split()[3]); img = bg
    buf = io.BytesIO()
    if "jpeg" in fmt: img.save(buf, "JPEG", quality=88); return buf.getvalue(), "image/jpeg"
    img.save(buf, "PNG"); return buf.getvalue(), "image/png"
def get_capabilities(p):
    ver = p.get("VERSION", "1.1.1"); v13 = ver == "1.3.0"
    tag = "CRS" if v13 else "SRS"; root = "WMS_Capabilities" if v13 else "WMT_MS_Capabilities"
    ns = 'xmlns:xlink="http://www.w3.org/1999/xlink"' + (' xmlns="http://www.opengis.net/wms"' if v13 else "")
    L = ['<?xml version="1.0" encoding="UTF-8"?>', '<%s version="%s" %s>' % (root, ver, ns),
         '<Service><Name>OGC:WMS</Name><Title>radar-wms-plus</Title></Service>', '<Capability><Request>']
    for req, fmts in (("GetCapabilities", ("text/xml",)), ("GetMap", ("image/png", "image/jpeg")),
                      ("GetFeatureInfo", ("application/json", "text/html"))):
        L.append('<%s>%s<DCPType><HTTP><Get><OnlineResource xlink:href="%s"/></Get></HTTP></DCPType></%s>'
                 % (req, "".join("<Format>%s</Format>" % f for f in fmts), escape(ONLINE+"?"), req))
    L.append("</Request><Layer><Title>radar-wms-plus</Title>")
    for name, d in DATASETS.items():
        rows = layer_datasets(name)
        if not rows: continue
        rec = rows[0]; times = [r["timestamp"].strftime("%Y-%m-%dT%H:%M:00Z") for r in rows]
        bb = bbox_lonlat(rec)
        L.append('<Layer queryable="1"><Name>%s</Name><Title>%s</Title>' % (escape(name), escape(d.get("title", name))))
        for c in ("EPSG:4326", "EPSG:3857") + (("CRS:84",) if v13 else ()):
            L.append("<%s>%s</%s>" % (tag, c, tag))
        L.append('<LatLonBoundingBox minx="%f" miny="%f" maxx="%f" maxy="%f"/>' % tuple(bb))
        if v13:
            L.append('<BoundingBox CRS="EPSG:4326" minx="%f" miny="%f" maxx="%f" maxy="%f"/>' % (bb[1], bb[0], bb[3], bb[2]))
            L.append('<Dimension name="time" units="ISO8601" default="%s">%s</Dimension>' % (times[0], ",".join(times)))
        else:
            L.append('<Dimension name="time" units="ISO8601"/>')
            L.append('<Extent name="time" default="%s">%s</Extent>' % (times[0], ",".join(times)))
        legend = ONLINE+"?SERVICE=WMS&VERSION="+ver+"&REQUEST=GetLegendGraphic&LAYER="+name+"&FORMAT=image/png"
        L.append('<Style><Name>default</Name><Title>default</Title><LegendURL width="230" height="%d">'
                 '<Format>image/png</Format><OnlineResource xlink:href="%s"/></LegendURL></Style>'
                 % (24*len(style_classes(d["style"]))+30, escape(legend)))
        L.append("</Layer>")
    L.append("</Layer></Capability></%s>" % root)
    return "".join(L).encode("utf-8"), "text/xml; charset=utf-8"
def get_feature_info(p):
    crs, b, X, Y, w, h = _target_grid(p)
    i = int(p.get("I") or p.get("X")); j = int(p.get("J") or p.get("Y"))
    lon, lat = transformer(crs, "EPSG:4326").transform(X[j, i], Y[j, i])
    results = []
    for name in (p.get("QUERY_LAYERS") or p.get("LAYERS", "")).split(","):
        name = name.strip()
        if not name or base_name(name) not in DATASETS: continue
        d = DATASETS[base_name(name)]
        rec = pick_dataset(name, p.get("TIME"))
        if rec is None: continue
        raw, t, crs2 = _raster(rec["geotiff_path"], os.path.getmtime(rec["geotiff_path"]))
        x, y = transformer("EPSG:4326", crs2 or rec["projdef"] or "EPSG:3857").transform(lon, lat)
        col, row = _pixels(t, np.array(x), np.array(y))
        inside = 0 <= col < raw.shape[1] and 0 <= row < raw.shape[0]
        v = float(raw[row, col]) if inside else None
        dec = float(decode_array(d, np.array([v]))[0]) if v is not None else None
        results.append({"layer": name, "title": d.get("title", name), "raw": v,
                        "value": None if dec is None or not math.isfinite(dec) else round(dec, 2),
                        "unit": d.get("unit", ""), "time": rec["timestamp"].strftime("%Y-%m-%dT%H:%M:00Z")})
    doc = {"lon": round(float(lon), 5), "lat": round(float(lat), 5), "results": results}
    if "html" in (p.get("INFO_FORMAT") or "").lower():
        rows_html = "".join("<tr><td>%s</td><td><b>%s</b> %s</td></tr>"
                            % (escape(r["title"]), "нет данных" if r["value"] is None else r["value"], escape(r["unit"]))
                            for r in results)
        return ("<html><meta charset='utf-8'><body><b>%.4f E, %.4f N</b><table>%s</table></body></html>"
                % (lon, lat, rows_html)).encode("utf-8"), "text/html; charset=utf-8"
    return json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
def get_legend(p):
    name = base_name(p.get("LAYER") or p.get("LAYERS", "")); d = DATASETS[name]
    classes = style_classes(d["style"]); f = font(13); fh = font(14)
    W, rh, top = 230, 22, 28
    img = Image.new("RGBA", (W, top + rh*len(classes) + 6), (255, 255, 255, 255))
    dr = ImageDraw.Draw(img)
    dr.text((8, 6), "%s, %s" % (d.get("title", name), d.get("unit", "")), fill=(0, 0, 0), font=fh)
    for k, (label, lo, hi, rgb) in enumerate(classes):
        y = top + k*rh
        dr.rectangle([8, y+2, 36, y+rh-3], fill=rgb+(255,), outline=(120, 120, 120))
        dr.text((44, y+3), label, fill=(0, 0, 0), font=f)
    return png_bytes(img), "image/png"def xsec_levels():
    return sorted((float(d["cappi_height"]), n) for n, d in DATASETS.items() if d.get("cappi_height"))
def line_points(line, step_km):
    pts = [[float(v) for v in s.split(",")] for s in line.split(";") if s.strip()]
    if len(pts) < 2: raise ValueError("LINE: нужно минимум 2 точки lon,lat;lon,lat")
    segs = []; total = 0.0
    for a, b in zip(pts[:-1], pts[1:]):
        az, _, dist = GEOD.inv(a[0], a[1], b[0], b[1]); segs.append((a, az, dist)); total += dist
    n = max(2, min(int(total/1000.0/step_km)+1, 2000))
    lons, lats, dists = [], [], []
    for d in np.linspace(0, total, n):
        dd = float(d)
        for k, (a, az, dist) in enumerate(segs):
            if dd <= dist or k == len(segs)-1:
                lon, lat, _ = GEOD.fwd(a[0], a[1], az, min(dd, dist))
                lons.append(lon); lats.append(lat); dists.append(d/1000.0); break
            dd -= dist
    return pts, np.array(lons), np.array(lats), np.array(dists)
def build_section(p):
    step_km = float(p.get("STEP", XSEC.get("sample_km", 1.0)))
    maxh    = float(p.get("MAXH", XSEC.get("max_height_km", 12)))
    dh      = float(XSEC.get("step_m", 100))
    lapse   = float(XSEC.get("lapse_dbz_km", -6.5))
    min_dbz = float(XSEC.get("min_dbz", 0))
    tol     = int(XSEC.get("time_tolerance_min", 30))
    tv      = p.get("TIME")
    pts, lons, lats, dists = line_points(p["LINE"], step_km)
    levels = xsec_levels()
    if not levels: raise ValueError("нет слоёв с cappi_height — нечего резать по высоте")
    used, cols, hs = [], [], []
    for h_m, name in levels:
        rec = pick_dataset(name, tv, tolerance_min=tol)
        if rec is None: continue
        vals = sample_lonlat(rec, DATASETS[name], lons, lats)
        if not np.isfinite(vals).any(): continue
        cols.append(vals); hs.append(h_m)
        used.append({"layer": name, "height_m": h_m,
                     "time": rec["timestamp"].strftime("%Y-%m-%dT%H:%M:00Z")})
    if not cols: raise ValueError("нет данных вдоль линии на выбранное время")
    V = np.array(cols); ha = np.array(hs)
    heights = np.arange(0, maxh*1000 + dh, dh)
    Z = np.full((len(heights), V.shape[1]), np.nan)
    etop = np.full(V.shape[1], np.nan)
    et = XSEC.get("echo_top")
    if et and et in DATASETS:
        rec = pick_dataset(et, tv, tolerance_min=tol)
        if rec is not None: etop = sample_lonlat(rec, DATASETS[et], lons, lats)
    for c in range(V.shape[1]):
        ok = np.isfinite(V[:, c])
        if not ok.any(): continue
        hv, vv = ha[ok], V[ok, c]
        col = np.interp(heights, hv, vv)
        above = heights > hv[-1]
        col[above] = vv[-1] + lapse*(heights[above]-hv[-1])/1000.0
        col[above & (col < min_dbz)] = np.nan
        if np.isfinite(etop[c]): col[heights > etop[c]*1000.0] = np.nan
        Z[:, c] = col
    pal = phys_palette(DATASETS[used[0]["layer"]])
    return {"time": used[0]["time"], "unit": "dBZ", "points": pts,
            "distance_km": np.round(dists, 3).tolist(), "height_km": (heights/1000.0).tolist(),
            "echo_top_km": [None if not np.isfinite(e) else round(float(e), 2) for e in etop],
            "levels_used": used,
            "palette": [{"from": lo, "to": hi, "rgb": rgb, "label": lb} for lo, hi, rgb, lb in pal],
            "dbz": [[None if not np.isfinite(v) else round(float(v), 1) for v in row] for row in Z]}
def render_section_png(sec, W=960, H=420):
    Z = np.array([[np.nan if v is None else v for v in row] for row in sec["dbz"]])
    dist = np.array(sec["distance_km"]); hk = np.array(sec["height_km"])
    pal = [(c["from"], c["to"], tuple(c["rgb"]), c["label"]) for c in sec["palette"]]
    rgb = colorize(Z, pal)[..., :3]
    rgb[~np.isfinite(Z)] = (248, 249, 252)
    L, R, T, B = 58, 14, 40, 46
    pw, ph = W-L-R, H-T-B
    img = Image.new("RGB", (W, H), (255, 255, 255))
    img.paste(Image.fromarray(rgb[::-1].astype(np.uint8)).resize((pw, ph), Image.NEAREST), (L, T))
    dr = ImageDraw.Draw(img); f = font(12); fb = font(14)
    def px(d): return L + d/max(dist[-1], 1e-9)*pw
    def py(h): return T + (1 - h/hk[-1])*ph
    xmax = dist[-1]
    xstep = next(s for s in (5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000) if xmax/s <= 12)
    for d in np.arange(0, xmax+1e-9, xstep):
        x = px(d); dr.line([x, T, x, T+ph], fill=(210, 214, 220), width=1)
        dr.text((x-8, T+ph+6), "%g" % d, fill=(0, 0, 0), font=f)
    for h in np.arange(0, hk[-1]+1e-9, 2):
        y = py(h); dr.line([L, y, L+pw, y], fill=(210, 214, 220), width=1)
        dr.text((L-26, y-7), "%g" % h, fill=(0, 0, 0), font=f)
    line = [(px(d), py(min(e, hk[-1]))) for d, e in zip(dist, sec["echo_top_km"]) if e is not None]
    if len(line) > 1: dr.line(line, fill=(20, 20, 20), width=2)
    st = float(XSEC.get("surface_temp_c", 15)); lt = float(XSEC.get("temp_lapse_c_km", 6.5))
    for ti in (0, -10, -20):
        hi = (st-ti)/lt
        if 0 < hi < hk[-1]:
            y = py(hi)
            for x in range(L, L+pw, 12): dr.line([x, y, x+6, y], fill=(90, 90, 90), width=1)
            dr.text((L+pw-44, y-14), "%d°C" % ti, fill=(60, 60, 60), font=f)
    dr.rectangle([L, T, L+pw, T+ph], outline=(60, 60, 60))
    dr.text((L, 8), "Вертикальный срез  %s UTC  (ВГО — чёрная линия)" % sec["time"], fill=(0, 0, 0), font=fb)
    dr.text((L, T+ph+24), "Расстояние вдоль линии, км", fill=(0, 0, 0), font=f)
    dr.text((6, T-22), "км", fill=(0, 0, 0), font=f)
    dr.text((L-10, T-16), "A", fill=(180, 0, 0), font=fb)
    dr.text((L+pw-4, T-16), "A'", fill=(180, 0, 0), font=fb)
    return png_bytes(img)
def cross_section(p):
    sec = build_section(p)
    if (p.get("FORMAT") or "image/png").lower() in ("application/json", "json"):
        return json.dumps(sec, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
    W = max(320, min(2000, int(p.get("WIDTH", 960)))); H = max(200, min(1200, int(p.get("HEIGHT", 420))))
    return render_section_png(sec, W, H), "image/png"
def tools(p):
    a = p.get("ACTION", "")
    if a == "download_geotiff":
        rec = pick_dataset(p.get("LAYER", ""), p.get("TIME"))
        if rec is None: raise ValueError("нет такого слоя/времени")
        with open(rec["geotiff_path"], "rb") as fh:
            return fh.read(), "image/tiff", os.path.basename(rec["geotiff_path"])
    if a == "cross_section":
        body, ct = cross_section(p); return body, ct, None
    raise ValueError("unknown ACTION")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".json": "application/json"}
def _reply(sr, body, ctype, status="200 OK", extra=()):
    sr(status, [("Content-Type", ctype), ("Content-Length", str(len(body))),
                ("Access-Control-Allow-Origin", "*")] + list(extra))
    return [body]
def application(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    p = {k.upper(): v[0] for k, v in qs.items()}
    path = environ.get("PATH_INFO", "/") or "/"
    try:
        if path in ("/", "/index.html"): path = "/demo/index.html"
        if path.startswith("/demo/"):
            fp = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
            if not fp.startswith(os.path.join(HERE, "demo")) or not os.path.isfile(fp):
                return _reply(start_response, b"not found", "text/plain", "404 Not Found")
            with open(fp, "rb") as fh: body = fh.read()
            return _reply(start_response, body, MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))
        if path == "/tools" or p.get("ACTION"):
            body, ct, fn = tools(p)
            extra = [("Content-Disposition", 'attachment; filename="%s"' % fn)] if fn else []
            return _reply(start_response, body, ct, extra=extra)
        if path == "/cross" or p.get("REQUEST", "").lower() == "crosssection":
            body, ct = cross_section(p); return _reply(start_response, body, ct)
        req = p.get("REQUEST", "").lower()
        if path == "/wms" or req:
            if req == "getcapabilities": body, ct = get_capabilities(p)
            elif req == "getmap": body, ct = get_map(p)
            elif req == "getfeatureinfo": body, ct = get_feature_info(p)
            elif req == "getlegendgraphic": body, ct = get_legend(p)
            else: raise ValueError("unknown REQUEST")
            cache = "public, max-age=60"
            if p.get("TIME") and p["TIME"] not in ("-1", ""): cache = "public, max-age=3600"
            return _reply(start_response, body, ct, extra=[("Cache-Control", cache)])
        return _reply(start_response, b"radar-wms-plus: /wms /cross /tools /demo/", "text/plain; charset=utf-8")
    except Exception as e:
        return _reply(start_response, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                      "application/json; charset=utf-8", "400 Bad Request")
def main():
    import argparse, sys
    ap = argparse.ArgumentParser(description="radar-wms-plus")
    ap.add_argument("--port", type=int, default=8080); ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--demo", action="store_true", help="сгенерировать демо-данные и демо-конфиг")
    args = ap.parse_args()
    if args.demo:
        import make_demo_data; make_demo_data.main()
        os.environ["RADAR_CFG"] = os.path.join(HERE, "demo.cfg")
        os.execv(sys.executable, [sys.executable, __file__, "--port", str(args.port), "--host", args.host])
    from wsgiref.simple_server import make_server, WSGIServer
    import socketserver
    class TS(socketserver.ThreadingMixIn, WSGIServer): daemon_threads = True
    print("radar-wms-plus: http://localhost:%d/  (конфиг: %s)" % (args.port, CFG_PATH))
    make_server(args.host, args.port, application, server_class=TS).serve_forever()
if __name__ == "__main__":
    main()
