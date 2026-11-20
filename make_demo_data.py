#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Демо-данные radar-wms-plus: 6 кадров по 10 мин, штормовые ячейки вокруг Москвы.
Слои: CAPPI 1/2/4 км (dBZ), Высота ВГО (км), осадки (мм/ч). Коды пикселей — как у GIMET."""
import os, sqlite3, math, struct
from datetime import datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("RADAR_DEMO_DATA", os.path.join(HERE, "demo_data"))
W, H = 1400, 1000
LON0, LON1, LAT0, LAT1 = 30.0, 46.0, 52.0, 60.0
R = 6378137.0
def write_tiff(path, arr, transform, epsg=3857):
    h, w = arr.shape; a, b, c, d, e, f = transform
    data = arr.tobytes(); entries = []
    def E(tag, typ, vals):
        if typ == 2: payload = vals.encode() + b"\0"; cnt = len(payload)
        else:
            fmt = {1: "B", 3: "H", 4: "I", 12: "d"}[typ]
            payload = struct.pack("<%d%s" % (len(vals), fmt), *vals); cnt = len(vals)
        entries.append((tag, typ, cnt, payload))
    E(256, 4, [w]); E(257, 4, [h]); E(258, 3, [8]); E(259, 3, [1]); E(262, 3, [1])
    E(273, 4, [0]); E(277, 3, [1]); E(278, 4, [h]); E(279, 4, [len(data)]); E(339, 3, [1])
    E(33550, 12, [a, -e, 0.0]); E(33922, 12, [0, 0, 0, c, f, 0])
    E(34735, 3, [1, 1, 0, 1, 3072, 0, 1, epsg]); E(34737, 2, "EPSG:%d|" % epsg)
    entries.sort(key=lambda t: t[0])
    ifd_size = 2 + 12*len(entries) + 4
    data_off = 8 + ifd_size; off = data_off + len(data)
    recs = []; blobs = []
    for tag, typ, cnt, payload in entries:
        if tag == 273:
            recs.append((tag, typ, cnt, struct.pack("<I", data_off))); continue
        if len(payload) <= 4: recs.append((tag, typ, cnt, payload.ljust(4, b"\0")))
        else:
            if off % 2: blobs.append(b"\0"); off += 1
            recs.append((tag, typ, cnt, struct.pack("<I", off)))
            blobs.append(payload); off += len(payload)
    out = b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries))
    for tag, typ, cnt, v4 in recs: out += struct.pack("<HHI", tag, typ, cnt) + v4
    out += struct.pack("<I", 0) + data + b"".join(blobs)
    open(path, "wb").write(out)
STYLES = """
[Gimet_dbzh_style]
00 = empty, 0, 1, 204, 204, 204
01 = -30 dBZ, 1, 44, 220, 220, 220
02 = -10 dBZ, 44, 54, 167, 167, 167
03 = -5 dBZ, 54, 64, 171, 210, 235
04 = 0 dBZ, 64, 74, 206, 255, 153
05 = 5 dBZ, 74, 84, 84, 255, 58
06 = 10 dBZ, 84, 94, 0, 170, 255
07 = 15 dBZ, 94, 104, 1, 77, 255
08 = 20 dBZ, 104, 114, 0, 0, 207
09 = 25 dBZ, 114, 124, 0, 0, 127
10 = 30 dBZ, 124, 134, 255, 255, 0
11 = 35 dBZ, 134, 144, 255, 128, 0
12 = 40 dBZ, 144, 154, 255, 61, 55
13 = 45 dBZ, 154, 164, 255, 0, 0
14 = 50 dBZ, 164, 174, 73, 221, 0
15 = 55 dBZ, 174, 184, 0, 170, 0
16 = 60 dBZ, 184, 194, 245, 0, 245
17 = 65 dBZ, 194, 204, 170, 0, 255
18 = 70 dBZ, 204, 214, 113, 0, 0
[Gimet_height_style]
00 = нет эхо, 0, 1, 204, 204, 204
01 = 0 км, 1, 4, 7, 254, 164
02 = 0.2 км, 4, 7, 27, 243, 107
03 = 0.5 км, 7, 12, 27, 191, 128
04 = 1 км, 12, 22, 34, 149, 112
05 = 2 км, 22, 32, 37, 113, 100
06 = 3 км, 32, 42, 22, 65, 55
07 = 4 км, 42, 52, 0, 204, 235
08 = 5 км, 52, 62, 3, 135, 229
09 = 6 км, 62, 72, 3, 54, 206
10 = 7 км, 72, 82, 5, 22, 109
11 = 8 км, 82, 92, 211, 181, 31
12 = 9 км, 92, 102, 217, 74, 27
13 = 10 км, 102, 112, 216, 27, 17
14 = 11 км, 112, 122, 132, 5, 11
15 = 12 км, 122, 132, 67, 168, 4
16 = 13 км, 132, 142, 3, 63, 3
17 = 14 км, 142, 152, 161, 17, 72
18 = 15 км, 152, 224, 218, 47, 158
[Gimet_precip_style]
00 = нет осадков, 0, 1, 204, 204, 204
01 = 0.10 мм/ч, 1, 78, 155, 155, 155
02 = 0.30 мм/ч, 78, 93, 135, 135, 135
03 = 0.50 мм/ч, 93, 100, 0, 85, 255
04 = 1.00 мм/ч, 100, 110, 0, 0, 127
05 = 3.00 мм/ч, 110, 125, 255, 255, 0
06 = 5.00 мм/ч, 125, 132, 200, 239, 4
07 = 7.00 мм/ч, 132, 137, 255, 170, 0
08 = 10.0 мм/ч, 137, 142, 255, 85, 0
09 = 20.0 мм/ч, 142, 151, 255, 0, 0
10 = 30.0 мм/ч, 151, 157, 128, 255, 128
11 = 50.0 мм/ч, 157, 164, 0, 170, 0
12 = 100 мм/ч, 164, 174, 255, 131, 245
13 = >100 мм/ч, 174, 255, 208, 0, 208
"""
DATASETS = [
    ("demo_dbz1", "Отражаемость 1 км", "Gimet_dbzh_style", "dBZ", "-31.5+(v-1)*127.0/254.0", "0,1", 1000),
    ("demo_dbz2", "Отражаемость 2 км", "Gimet_dbzh_style", "dBZ", "-31.5+(v-1)*127.0/254.0", "0,1", 2000),
    ("demo_dbz4", "Отражаемость 4 км", "Gimet_dbzh_style", "dBZ", "-31.5+(v-1)*127.0/254.0", "0,1", 4000),
    ("demo_etop", "Высота ВГО", "Gimet_height_style", "км", "(v-2)/10.0", "0,1", None),
    ("demo_precip", "Интенсивность осадков", "Gimet_precip_style", "мм/ч",
     "10**(((-31.5+(v-1)*127.0/254.0)/10.0-log10(200))/1.6)", "0,1", None),
]
CELLS = [(36.8, 55.0, 58, 11.0, 45, 9, 40), (38.6, 56.6, 46, 8.0, 70, 7, 90),
         (34.4, 53.9, 36, 6.0, 90, 6, 45), (41.2, 57.6, 51, 9.0, 55, 5, 0)]
def fields(frame):
    LO, LA = np.meshgrid(np.linspace(LON0, LON1, W), np.linspace(LAT1, LAT0, H))
    surf = np.zeros_like(LO); top = np.zeros_like(LO)
    for i, (clon, clat, peak, tkm, sig, spd, az) in enumerate(CELLS):
        d = spd*frame
        clon += d*math.sin(math.radians(az))/(111.32*math.cos(math.radians(clat)))
        clat += d*math.cos(math.radians(az))/110.57
        dx = (LO-clon)*111.32*math.cos(math.radians(clat)); dy = (LA-clat)*110.57
        g = np.exp(-(dx*dx+dy*dy)/(2*sig*sig))
        surf = np.maximum(surf, peak*(0.85+0.15*math.sin(frame/2.0+i))*g)
        top = np.maximum(top, tkm*np.sqrt(g))
    surf[surf < 5] = 0
    return surf, top
def dbz_at(surf, top, hkm):
    v = surf - 6.5*np.maximum(0, hkm-2.0) + 3.0*np.exp(-((hkm-2.0)/0.4)**2)
    v[(surf <= 0) | (hkm >= top)] = np.nan
    return v
def enc_dbz(v):
    out = np.ones(v.shape, np.uint8); m = np.isfinite(v) & (v > 5)
    out[m] = np.clip(np.round(1+(v[m]+31.5)*254.0/127.0), 2, 255).astype(np.uint8)
    return out
def main():
    os.makedirs(DATA, exist_ok=True)
    x0 = math.radians(LON0)*R; x1 = math.radians(LON1)*R
    y0 = math.log(math.tan(math.pi/4+math.radians(LAT0)/2))*R
    y1 = math.log(math.tan(math.pi/4+math.radians(LAT1)/2))*R
    tr = ((x1-x0)/W, 0, x0, 0, -(y1-y0)/H, y1)
    bbox = "%f,%f,%f,%f" % (x0, y0, x1, y1)
    db = os.path.join(DATA, "radar.db")
    if os.path.exists(db): os.remove(db)
    con = sqlite3.connect(db); con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE radar_dataset (id INTEGER PRIMARY KEY, name VARCHAR(50),
        title VARCHAR(300) NOT NULL, timestamp DATETIME, geotiff_path VARCHAR(500) NOT NULL,
        hdf_path VARCHAR(500), projdef VARCHAR(150), bbox_original VARCHAR(150),
        unit VARCHAR(100) NOT NULL, dataset_type VARCHAR(10), style VARCHAR(50))""")
    con.execute("CREATE INDEX ix_name ON radar_dataset (name)")
    con.execute("CREATE INDEX ix_ts ON radar_dataset (timestamp)")
    now = datetime.utcnow().replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 10)
    frames = [now - timedelta(minutes=10*k) for k in range(5, -1, -1)]
    for fi, ts in enumerate(frames):
        surf, top = fields(fi)
        layers = {"demo_dbz1": enc_dbz(dbz_at(surf, top, 1.0)),
                  "demo_dbz2": enc_dbz(dbz_at(surf, top, 2.0)),
                  "demo_dbz4": enc_dbz(dbz_at(surf, top, 4.0))}
        et = np.ones(top.shape, np.uint8); m = surf > 0
        et[m] = np.clip(np.round(top[m]*10+2), 2, 222).astype(np.uint8)
        layers["demo_etop"] = et
        rr = np.power(np.power(10, surf/10.0)/200.0, 1/1.6)
        layers["demo_precip"] = enc_dbz(np.where(rr >= 0.1, 10*np.log10(200*np.power(np.maximum(rr, 1e-6), 1.6)), np.nan))
        for name, title, style, unit, *_ in DATASETS:
            dd = os.path.join(DATA, name); os.makedirs(dd, exist_ok=True)
            path = os.path.join(dd, ts.strftime("%Y%m%dT%H%M") + ".tif")
            write_tiff(path, layers[name], tr)
            con.execute("INSERT INTO radar_dataset (name,title,timestamp,geotiff_path,projdef,"
                        "bbox_original,unit,dataset_type,style) VALUES (?,?,?,?,?,?,?,?,?)",
                        (name, title, ts.strftime("%Y-%m-%d %H:%M:%S"), path, "epsg:3857",
                         bbox, unit, "geotiff", style))
    con.commit(); con.close()
    cfg = ["[locations]", "db_uri = sqlite:///" + db, "online_resource = http://localhost:8080/wms", "",
           "[settings]", "", "[cross_section]", "echo_top = demo_etop", "sample_km = 1.0",
           "max_height_km = 12", "step_m = 100", "lapse_dbz_km = -6.5", "min_dbz = 0",
           "time_tolerance_min = 30", ""]
    for i, (name, title, style, unit, decode, nod, hgt) in enumerate(DATASETS, 1):
        cfg += ["[dataset_%d]" % i, "name = " + name, "title = " + title, "dataset_type = geotiff",
                "unit = " + unit, "style = " + style, "decode = " + decode, "nodata = " + nod,
                "cleanup_time = 120"]
        if hgt: cfg.append("cappi_height = %d" % hgt)
        cfg.append("")
    open(os.environ.get("RADAR_CFG", os.path.join(HERE, "demo.cfg")), "w").write("\n".join(cfg) + STYLES)
    print("Демо-данные готовы:", DATA, "| кадров:", len(frames))
if __name__ == "__main__":
    main()
