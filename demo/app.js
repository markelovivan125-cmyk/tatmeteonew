/* radar-wms-plus: карта, анимация с предзагрузкой, проба, вертикальный срез */
const WMS = "/wms", CROSS = "/cross", TOOLS = "/tools";
const $ = id => document.getElementById(id);
let map, radar, animLayer = null, lineSource, markerFeature, popup;
let capsDoc = null, layerName = null, times = [], timeValue = null;
let playing = false, playTimer = null, draw = null, drawing = false;
let sectionGeom = null, secReq = 0, secObjUrl = null;

const status = t => { $("status").textContent = t || ""; };
const op = () => +$("opacity").value / 100;

function init() {
  lineSource = new ol.source.Vector();
  const markerSource = new ol.source.Vector();
  markerFeature = new ol.Feature();
  markerSource.addFeature(markerFeature);
  radar = new ol.layer.Image({ opacity: .7, source: new ol.source.ImageWMS({
    url: WMS, params: { LAYERS: "", VERSION: "1.1.1" }, ratio: 1 }) });
  map = new ol.Map({ target: "map", layers: [
    new ol.layer.Tile({ source: new ol.source.OSM() }),
    radar,
    new ol.layer.Vector({ source: lineSource, style: new ol.style.Style({
      stroke: new ol.style.Stroke({ color: "#c62828", width: 3 }) }) }),
    new ol.layer.Vector({ source: markerSource, style: new ol.style.Style({
      image: new ol.style.Circle({ radius: 6, fill: new ol.style.Fill({ color: "#c62828" }),
        stroke: new ol.style.Stroke({ color: "#fff", width: 2 }) }) }) })
  ], view: new ol.View({ center: ol.proj.fromLonLat([37.6, 55.7]), zoom: 6 }) });
  popup = new ol.Overlay({ element: $("popup"), positioning: "bottom-center", offset: [0, -12] });
  map.addOverlay(popup);
  map.on("click", probe);
  map.on("movestart", () => { if (playing) stop(); });

  $("layer").onchange = () => { stop(); layerName = $("layer").value; fillTimes(); setTime(times[0]); legend(); };
  $("time").onchange = () => { stop(); setTime($("time").value); };
  $("play").onclick = () => playing ? stop() : play();
  $("speed").onchange = () => { if (playing) { clearInterval(playTimer); playTimer = setInterval(tick, +$("speed").value); } };
  $("opacity").oninput = () => { radar.setOpacity(op()); if (animLayer) animLayer.setOpacity(op()); };
  $("xsec").onclick = toggleDraw;
  $("secclose").onclick = closeSection;
  $("geotiff").onclick = () => { window.location = `${TOOLS}?ACTION=download_geotiff&LAYER=${layerName}&TIME=${timeValue}`; };
  $("legendbtn").onclick = () => { const l = $("legend"); l.style.display = l.style.display === "none" ? "block" : "none"; };
  $("locate").onclick = () => navigator.geolocation.getCurrentPosition(
    p => map.getView().animate({ center: ol.proj.fromLonLat([p.coords.longitude, p.coords.latitude]), zoom: 8 }),
    e => console.warn("геолокация недоступна:", e.message));
  $("secimg").addEventListener("mousemove", secHover);
  $("secimg").addEventListener("mouseleave", () => markerFeature.setGeometry(null));
  document.addEventListener("keydown", e => { if (e.key === "Escape") { cancelDraw(); $("popup").style.display = "none"; } });
  loadCaps(true);
  setInterval(() => loadCaps(false), 300000);
}

async function loadCaps(first) {
  try {
    const r = await fetch(`${WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetCapabilities`);
    capsDoc = new DOMParser().parseFromString(await r.text(), "text/xml");
    const ls = [...capsDoc.querySelectorAll("Layer > Layer")];
    const sel = $("layer"), prev = layerName;
    sel.innerHTML = "";
    ls.forEach(l => {
      const n = l.querySelector("Name")?.textContent;
      const t = l.querySelector("Title")?.textContent || n;
      if (n) sel.add(new Option(t, n));
    });
    layerName = ls.some(l => l.querySelector("Name")?.textContent === prev) ? prev : sel.value;
    sel.value = layerName;
    fillTimes();
    const t = (first || $("follow").checked || !times.includes(timeValue)) ? times[0] : timeValue;
    setTime(t); legend();
  } catch (e) { status("ошибка GetCapabilities: " + e); }
}
function fillTimes() {
  times = [];
  const ls = [...capsDoc.querySelectorAll("Layer > Layer")];
  const el = ls.find(l => l.querySelector("Name")?.textContent === layerName);
  const ext = el?.querySelector("Extent")?.textContent;
  if (ext) times = ext.split(",").filter(Boolean);
  const sel = $("time"); sel.innerHTML = "";
  times.forEach(t => sel.add(new Option(t.replace("T", " ").replace(":00Z", " UTC"), t)));
}
function setTime(t) {
  if (!t) return;
  timeValue = t; $("time").value = t;
  radar.getSource().updateParams({ TIME: t, LAYERS: layerName });
  status(t);
  if (sectionGeom) requestSection();
}
function legend() {
  $("legendimg").src = `${WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetLegendGraphic&LAYER=${layerName}&FORMAT=image/png`;
}

/* ---- анимация: предзагрузка кадров текущего вида + двойной буфер ---- */
const mapUrl = (t, ext, w, h) =>
  `${WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS=${encodeURIComponent(layerName)}` +
  `&STYLES=&SRS=EPSG:3857&BBOX=${ext.join(",")}&WIDTH=${w}&HEIGHT=${h}` +
  `&FORMAT=image/png&TRANSPARENT=true&OPACITY=100&TIME=${encodeURIComponent(t)}`;
let frames = [], fi = 0, animExt = null;
async function play() {
  if (playing || !times.length) return;
  const size = map.getSize(), ext = map.getView().calculateExtent(size);
  const w = Math.round(size[0]), h = Math.round(size[1]);
  status("загрузка кадров…");
  const ts = times.slice(0, 30).reverse();
  frames = (await Promise.all(ts.map(t => new Promise(res => {
    const url = mapUrl(t, ext, w, h), im = new Image();
    im.onload = () => res({ t, url }); im.onerror = () => res(null); im.src = url;
  })))).filter(Boolean);
  if (!frames.length) { status("кадры не загрузились"); return; }
  animExt = ext; fi = 0;
  animLayer = new ol.layer.Image({ opacity: op() });
  map.addLayer(animLayer); radar.setVisible(false);
  playing = true; $("play").textContent = "⏸";
  tick(); playTimer = setInterval(tick, +$("speed").value);
}
function tick() {
  const f = frames[fi];
  animLayer.setSource(new ol.source.ImageStatic({
    url: f.url, imageExtent: animExt, projection: "EPSG:3857", interpolate: false }));
  timeValue = f.t; $("time").value = f.t; status(f.t);
  if (sectionGeom) requestSection();
  fi = (fi + 1) % frames.length;
}
function stop() {
  if (!playing) return;
  playing = false; clearInterval(playTimer); $("play").textContent = "▶";
  if (animLayer) { map.removeLayer(animLayer); animLayer = null; }
  radar.setVisible(true); setTime(timeValue);
}

/* ---- проба кликом (JSON GetFeatureInfo) ---- */
async function probe(e) {
  if (drawing) return;
  $("popup").style.display = "none";
  const size = map.getSize(), ext = map.getView().calculateExtent(size);
  const url = `${WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetFeatureInfo&LAYERS=${layerName}` +
    `&QUERY_LAYERS=${layerName}&SRS=EPSG:3857&BBOX=${ext.join(",")}&WIDTH=${Math.round(size[0])}` +
    `&HEIGHT=${Math.round(size[1])}&X=${Math.round(e.pixel[0])}&Y=${Math.round(e.pixel[1])}` +
    `&TIME=${encodeURIComponent(timeValue || "")}&INFO_FORMAT=application/json`;
  try {
    const d = await (await fetch(url)).json();
    const rows = d.results.map(r =>
      `<tr><td>${r.title}</td><td><b>${r.value == null ? "нет эхо" : r.value}</b> ${r.unit}</td></tr>`).join("");
    $("popup").innerHTML = `<b>${d.lon.toFixed(3)} E, ${d.lat.toFixed(3)} N</b><table>${rows}</table>`;
    $("popup").style.display = "block";
    popup.setPosition(e.coordinate);
  } catch (err) { console.warn(err); }
}

/* ---- вертикальный срез ---- */
function toggleDraw() {
  if (drawing) { cancelDraw(); return; }
  drawing = true; $("xsec").classList.add("active");
  lineSource.clear(); markerFeature.setGeometry(null);
  draw = new ol.interaction.Draw({ source: lineSource, type: "LineString" });
  draw.on("drawend", e => {
    sectionGeom = e.feature.getGeometry();
    const c = sectionGeom.getCoordinates();
    [["A", c[0]], ["A′", c[c.length - 1]]].forEach(([t, coord]) => {
      const f = new ol.Feature(new ol.geom.Point(coord));
      f.setStyle(new ol.style.Style({ text: new ol.style.Text({ text: t, font: "bold 14px sans-serif",
        fill: new ol.style.Fill({ color: "#c62828" }), stroke: new ol.style.Stroke({ color: "#fff", width: 3 }),
        offsetY: -14 }) }));
      lineSource.addFeature(f);
    });
    cancelDraw(); requestSection();
  });
  map.addInteraction(draw);
  status("проведите линию A–A′ (двойной клик — конец)");
}
function cancelDraw() {
  if (draw) { map.removeInteraction(draw); draw = null; }
  drawing = false; $("xsec").classList.remove("active");
}
function closeSection() {
  sectionGeom = null; lineSource.clear(); markerFeature.setGeometry(null);
  $("section").style.display = "none";
  if (secObjUrl) { URL.revokeObjectURL(secObjUrl); secObjUrl = null; }
}
async function requestSection() {
  if (!sectionGeom) return;
  const line = sectionGeom.getCoordinates().map(c => ol.proj.toLonLat(c))
    .map(c => c[0].toFixed(5) + "," + c[1].toFixed(5)).join(";");
  const W = Math.min(1600, Math.max(640, $("section").clientWidth || 960));
  const url = `${CROSS}?LINE=${encodeURIComponent(line)}&TIME=${encodeURIComponent(timeValue || "")}&WIDTH=${W}&HEIGHT=400`;
  const my = ++secReq;
  try {
    const r = await fetch(url);
    if (my !== secReq) return;
    if (!r.ok) {
      const j = await r.json().catch(() => ({ error: r.statusText }));
      $("secimg").style.display = "none";
      $("secerr").textContent = "Срез: " + (j.error || "ошибка");
      $("secerr").style.display = "block"; $("section").style.display = "block";
      return;
    }
    const blob = await r.blob();
    if (my !== secReq) return;
    if (secObjUrl) URL.revokeObjectURL(secObjUrl);
    secObjUrl = URL.createObjectURL(blob);
    $("secimg").src = secObjUrl; $("secimg").style.display = "block";
    $("secerr").style.display = "none"; $("section").style.display = "block";
    $("sectitle").textContent = `Вертикальный срез A–A′ · ${timeValue || "последний кадр"} UTC`;
  } catch (e) { console.warn(e); }
}
function secHover(e) {
  if (!sectionGeom || !$("secimg").naturalWidth) return;
  const r = $("secimg").getBoundingClientRect();
  const scale = $("secimg").naturalWidth / r.width;
  const x = (e.clientX - r.left) * scale;
  const frac = Math.min(1, Math.max(0, (x - 58 * scale) / ($("secimg").naturalWidth - 72 * scale)));
  markerFeature.setGeometry(new ol.geom.Point(sectionGeom.getCoordinateAt(frac)));
}
window.addEventListener("load", init);
