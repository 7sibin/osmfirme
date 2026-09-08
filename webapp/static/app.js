"use strict";

/* OSM firme — the map is a permanent surface, the sidebar is the control column
 * and results are a third zone beside them. Nothing navigates away: every state
 * (searching, drawing, working, failed, listing) is a band that appears in place.
 */

const POLL_INTERVAL_MS = 1000;
const SEARCH_DEBOUNCE_MS = 400;
const FILTER_DEBOUNCE_MS = 300;
const PAGE_SIZE = 50;
/** Mirrors DEFAULT_LIMIT in webapp/enrich_runner.py: how many businesses one
 *  website-search pass will take on, so the button can name the number. */
const CHECK_LIMIT = 150;
/** Mirrors DEFAULT_LIMIT in webapp/contact_runner.py. Higher than the search's
 *  cap because reading a site costs a fraction of a second, not two of them. */
const READ_LIMIT = 500;
const MIN_RADIUS_M = 50;
const MAX_RADIUS_M = 50000;
const VISIBLE_CHIPS = 5;
const TOTAL_CATEGORIES = 6;

/** Serbian glosses for the OSM values that actually show up here. Anything
 *  unlisted keeps its raw key — inventing a translation would be worse. */
const GLOSSES = {
  cafe: "kafici", bar: "barovi", pub: "kafane", restaurant: "restorani",
  fast_food: "brza hrana", bakery: "pekare", butcher: "mesare",
  supermarket: "marketi", convenience: "prodavnice", greengrocer: "piljarnice",
  kiosk: "kiosci", clothes: "odeca", shoes: "obuca", jewelry: "nakit",
  hairdresser: "frizeri", beauty: "kozmeticki saloni", optician: "optike",
  pharmacy: "apoteke", doctor: "lekari", dentist: "stomatolozi",
  veterinary: "veterinari", car_repair: "auto servisi", car: "auto placevi",
  fuel: "pumpe", tyres: "vulkanizeri", bank: "banke", atm: "bankomati",
  bureau_de_change: "menjacnice", insurance: "osiguranje",
  estate_agent: "agencije za nekretnine", lawyer: "advokati",
  accountant: "knjigovodje", company: "firme", travel_agency: "turisticke agencije",
  hotel: "hoteli", guest_house: "prenocista", apartment: "apartmani",
  hostel: "hosteli", florist: "cvecare", bookshop: "knjizare",
  hardware: "gvozdjare", furniture: "namestaj", electronics: "tehnika",
  mobile_phone: "mobilni telefoni", computer: "racunari", laundry: "perionice",
  tailor: "krojaci", photographer: "fotografi", carpenter: "stolari",
  electrician: "elektricari", plumber: "vodoinstalateri", painter: "moleri",
  gym: "teretane", fitness_centre: "teretane", school: "skole",
  kindergarten: "vrtici", clinic: "klinike", library: "biblioteke",
  post_office: "poste", copyshop: "kopirnice", pet: "pet shopovi",
  toys: "igracke", sports: "sportska oprema", alcohol: "prodavnice pica",
  confectionery: "poslasticarnice", ice_cream: "sladoledzije",
};

const $ = (attribute) => document.querySelector(`[${attribute}]`);
const app = $("data-app");

const state = {
  area: null,        // { kind, area_id?, label?, bbox?, center?, radius_m? }
  areaSource: null,  // "search" | "draw"
  jobId: null,
  busy: false,       // a job is in flight; set before the id comes back
  cached: false,
  elapsed: 0,        // seconds the finished job took; the cache stamp reports it
  drawing: null,     // "Rectangle" | "Circle"
  pollTimer: null,
  searchTimer: null,
  filterTimer: null,
  candidatesOpen: false,
  results: null,
  checkTimer: null,
  readTimer: null,
};

const view = {
  page: 1,
  sort: "name",
  order: "asc",
  categories: new Set(),
  withSite: false,
  facets: [],
  facetFilter: "",
  deadOnly: false,  // only businesses whose website has stopped answering
};

/** The website search. Its timer lives on `state` with the others; these two
 *  are what the band itself needs to remember between polls. */
const check = {
  running: false,
  autoHidden: false,  // the hide toggle is ticked for the user once, not every pass
};

/** Reading the sites: the same shape as `check`, its own pass. */
const read = { running: false };

// --- small helpers ----------------------------------------------------------

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function clear(node) {
  node.replaceChildren();
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 1 firma, 2-4 firme, 5+ firmi — and the same shape for filters and categories. */
function plural(count, one, few, many) {
  const last = count % 10;
  const lastTwo = count % 100;
  if (last === 1 && lastTwo !== 11) return one;
  if (last >= 2 && last <= 4 && !(lastTwo >= 12 && lastTwo <= 14)) return few;
  return many;
}

async function messageOf(response) {
  try {
    const body = await response.json();
    return body.detail || `Greska ${response.status}`;
  } catch {
    return `Greska ${response.status}`;
  }
}

const OFFLINE_MESSAGE = "Server ne odgovara. Proveri da li je pokrenut, pa pokusaj ponovo.";

/** fetch() rejects with "Failed to fetch" when the server is down — not a sentence
 *  anyone wants to read, and in the wrong language. Everything else is already ours. */
function humanError(error) {
  return error instanceof TypeError ? OFFLINE_MESSAGE : error.message;
}

// --- map --------------------------------------------------------------------

const map = L.map($("data-map"), { zoomControl: false }).setView([44.0, 21.0], 7);
L.control.zoom({ position: "topleft" }).addTo(map);
L.control.scale({ position: "bottomleft", imperial: false, maxWidth: 74 }).addTo(map);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
}).addTo(map);

const pointRenderer = L.canvas({ padding: 0.3 });
const pointLayer = L.layerGroup().addTo(map);
let areaLayer = null;

function areaStyle() {
  const survey = token("--survey");
  return { color: survey, weight: 2.5, fillColor: survey, fillOpacity: 0.13 };
}

map.pm.setGlobalOptions({ snappable: false });

function applyDrawStyles() {
  const survey = token("--survey");
  map.pm.setGlobalOptions({
    templineStyle: { color: survey, weight: 2.5 },
    hintlineStyle: { color: survey, weight: 2, dashArray: "5,5" },
    pathOptions: areaStyle(),
  });
}
applyDrawStyles();

// The tokens flip with the OS theme; the layers already on the map have to follow.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  applyDrawStyles();
  if (areaLayer && areaLayer.setStyle) areaLayer.setStyle(areaStyle());
  renderPoints(lastPoints);
});

function showArea(layer) {
  if (areaLayer) map.removeLayer(areaLayer);
  areaLayer = layer;
  if (layer) {
    layer.addTo(map);
    map.fitBounds(layer.getBounds(), { padding: [24, 24] });
  }
}

let lastPoints = [];

function renderPoints(points) {
  lastPoints = points;
  pointLayer.clearLayers();
  const survey = token("--survey");
  const paper = token("--paper");
  for (const [lat, lon] of points) {
    L.circleMarker([lat, lon], {
      renderer: pointRenderer,
      radius: 3,
      color: paper,
      weight: 1.5,
      fillColor: survey,
      fillOpacity: 1,
      interactive: false,
    }).addTo(pointLayer);
  }
}

function updateFootNote() {
  const center = map.getCenter();
  $("data-foot-note").textContent =
    `${center.lat.toFixed(4)}, ${center.lng.toFixed(4)} — z${map.getZoom()}`;
}
map.on("move zoom", updateFootNote);
updateFootNote();

/** The three columns resize each other; Leaflet has to be told. */
function relayoutMap() {
  requestAnimationFrame(() => map.invalidateSize());
}

// --- area selection ---------------------------------------------------------

const EXCLUSIVITY = {
  idle: "Crtanje ponistava pretragu i obrnuto.",
  searching: "Klik na kandidata iscrtava granicu.",
  drawing: "Crtanje je aktivno — pretraga mesta je ponistena.",
  search: "Pretraga mesta je aktivna — crtanje ce je ponistiti.",
  draw: "Nacrtana oblast je aktivna — pretraga ce je ponistiti.",
};

function renderArea() {
  const summary = state.area === null ? "Nije izabrana nijedna oblast." : areaLabel();
  $("data-area-summary").textContent = summary;
  $("data-area-block").classList.toggle("is-empty", state.area === null);

  if (state.drawing) {
    $("data-excl-note").textContent = EXCLUSIVITY.drawing;
  } else if (state.candidatesOpen) {
    $("data-excl-note").textContent = EXCLUSIVITY.searching;
  } else if (state.areaSource) {
    $("data-excl-note").textContent = EXCLUSIVITY[state.areaSource];
  } else {
    $("data-excl-note").textContent = EXCLUSIVITY.idle;
  }

  for (const [attribute, shape] of [["data-draw-rect", "Rectangle"], ["data-draw-circle", "Circle"]]) {
    $(attribute).classList.toggle("is-active", state.drawing === shape);
  }
  $("data-tool-rect").classList.toggle("is-active", state.drawing === "Rectangle");
  $("data-tool-circle").classList.toggle("is-active", state.drawing === "Circle");
  app.classList.toggle("is-drawing", state.drawing !== null);

  refreshRunButton();
}

function areaLabel() {
  const area = state.area;
  if (area.kind === "area") return area.label;
  if (area.kind === "bbox") {
    const [south, west, north, east] = area.bbox;
    return `Pravougaonik: ${south.toFixed(3)}, ${west.toFixed(3)} - ${north.toFixed(3)}, ${east.toFixed(3)}`;
  }
  return `Krug: ${area.radius_m} m oko ${area.center[0].toFixed(4)}, ${area.center[1].toFixed(4)}`;
}

function clearArea() {
  state.area = null;
  state.areaSource = null;
  closeCandidates();
  showArea(null);
  renderArea();
}

$("data-clear-area").addEventListener("click", stopEverythingAndClear);
$("data-tool-clear").addEventListener("click", stopEverythingAndClear);

function stopEverythingAndClear() {
  map.pm.disableDraw();
  state.drawing = null;
  $("data-search-input").value = "";
  clearArea();
}

// --- place search -----------------------------------------------------------

function closeCandidates() {
  state.candidatesOpen = false;
  $("data-candidates").hidden = true;
}

$("data-search-input").addEventListener("input", (event) => {
  clearTimeout(state.searchTimer);
  const query = event.target.value.trim();
  if (query.length < 2) {
    closeCandidates();
    renderArea();
    return;
  }
  state.searchTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  closeCandidates();
  closeFacetMenu();
  if (state.drawing) {
    map.pm.disableDraw();
    state.drawing = null;
  }
  renderArea();
});

async function runSearch(query) {
  try {
    const response = await fetch(`/api/places?q=${encodeURIComponent(query)}&country=RS`);
    if (!response.ok) throw new Error(await messageOf(response));
    renderCandidates(await response.json());
  } catch (error) {
    renderCandidateMessage(humanError(error));
  }
}

function renderCandidateMessage(text) {
  const list = $("data-candidates");
  clear(list);
  const item = element("li");
  item.appendChild(element("div", "cand-empty", text));
  list.appendChild(item);
  list.hidden = false;
  state.candidatesOpen = true;
  renderArea();
}

function renderCandidates(candidates) {
  if (candidates.length === 0) {
    renderCandidateMessage("Nema rezultata. Probaj drugaciji naziv ili nacrtaj oblast na mapi.");
    return;
  }
  const list = $("data-candidates");
  clear(list);
  for (const candidate of candidates) {
    // "Nis, Gradska opstina Medijana, ..." — the head is the place, the tail is context.
    const [head, ...rest] = candidate.display_name.split(", ");
    const button = element("button");
    button.type = "button";
    button.appendChild(element("span", "cand-name", head));
    if (rest.length) button.appendChild(element("span", "cand-rest", `, ${rest.join(", ")}`));
    button.addEventListener("click", () => pickCandidate(candidate));
    const item = element("li");
    item.appendChild(button);
    list.appendChild(item);
  }
  list.hidden = false;
  state.candidatesOpen = true;
  renderArea();
}

async function pickCandidate(candidate) {
  map.pm.disableDraw();
  state.drawing = null;
  state.area = { kind: "area", area_id: candidate.area_id, label: candidate.display_name };
  state.areaSource = "search";
  $("data-search-input").value = candidate.display_name;
  closeCandidates();
  renderArea();

  try {
    const response = await fetch(`/api/places/${candidate.osm_type}/${candidate.osm_id}/geometry`);
    if (!response.ok) return;  // the boundary is a nicety, not a requirement
    const { geojson } = await response.json();
    showArea(L.geoJSON(geojson, { style: areaStyle() }));
  } catch (error) {
    console.warn("boundary unavailable", error);
  }
}

// --- drawing ----------------------------------------------------------------

function startDraw(shape) {
  if (state.drawing === shape) {
    map.pm.disableDraw();
    state.drawing = null;
    renderArea();
    return;
  }
  // The search box keeps its name until a shape actually lands — arming the tool
  // is not yet a choice, and Escape puts everything back.
  closeCandidates();
  state.drawing = shape;
  map.pm.enableDraw(shape);
  renderArea();
}

$("data-draw-rect").addEventListener("click", () => startDraw("Rectangle"));
$("data-draw-circle").addEventListener("click", () => startDraw("Circle"));
$("data-tool-rect").addEventListener("click", () => startDraw("Rectangle"));
$("data-tool-circle").addEventListener("click", () => startDraw("Circle"));

map.on("pm:create", (event) => {
  map.pm.disableDraw();
  state.drawing = null;
  state.areaSource = "draw";
  // The drawn shape replaces whatever the search box found.
  $("data-search-input").value = "";
  const layer = event.layer;

  if (event.shape === "Rectangle") {
    const bounds = layer.getBounds();
    state.area = {
      kind: "bbox",
      bbox: [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()],
    };
  } else {
    const center = layer.getLatLng();
    state.area = {
      kind: "circle",
      center: [center.lat, center.lng],
      radius_m: Math.round(layer.getRadius()),
    };
  }
  showArea(layer);
  renderArea();
});

// --- categories -------------------------------------------------------------

function selectedCategories() {
  return [...$("data-categories").querySelectorAll("input:checked")].map((input) => input.value);
}

function refreshRunButton() {
  const chosen = selectedCategories().length;
  const ready = state.area !== null && chosen > 0 && !state.busy;
  $("data-run").disabled = !ready;
  $("data-run-hint").hidden = ready || state.busy;
  $("data-run-hint").textContent =
    chosen === 0 ? "Cekiraj bar jednu kategoriju." : "Izaberi oblast pa je pretraga moguca.";
  $("data-category-note").textContent =
    `${chosen} / ${TOTAL_CATEGORIES} ${plural(chosen, "kategorija", "kategorije", "kategorija")}`;
  $("data-category-say").textContent =
    chosen === TOTAL_CATEGORIES ? "Sve kategorije su cekirane"
      : chosen === 0 ? "Nijedna kategorija nije cekirana"
        : `${chosen} ${plural(chosen, "kategorija je cekirana", "kategorije su cekirane", "kategorija je cekirano")}`;
}

$("data-categories").addEventListener("change", refreshRunButton);

// --- running a job ----------------------------------------------------------

const PHASE_ORDER = ["start", "querying", "parsing", "finished"];

function renderPhases(job) {
  let active = 0;
  if (job) {
    if (job.status === "done") active = 3;
    else if (job.phase === "parsing" || job.phase === "finished") active = 2;
    else if (job.phase === "querying") active = 1;
  }
  const finished = job !== null && job.status === "done";
  for (const [index, name] of PHASE_ORDER.entries()) {
    const node = $("data-phases").querySelector(`[data-phase="${name}"]`);
    node.classList.toggle("is-done", index < active || (finished && index === active));
    node.classList.toggle("is-active", index === active && !finished);
  }
}

function showWorking(job) {
  $("data-working").hidden = false;
  $("data-error").hidden = true;
  renderPhases(job);
  $("data-elapsed").textContent = job ? `${Math.round(job.elapsed_s)} s` : "0 s";
}

function hideWorking() {
  $("data-working").hidden = true;
}

function showError(message) {
  state.busy = false;  // every path into here is a dead end for the job
  hideWorking();
  $("data-error").hidden = false;
  $("data-error-text").textContent = message;
  $("data-error-note").textContent = state.area
    ? "oblast i kategorije su sacuvane"
    : "izaberi oblast pa pokusaj ponovo";
  $("data-retry").disabled = state.area === null;
}

function hideError() {
  $("data-error").hidden = true;
}

$("data-retry").addEventListener("click", startJob);
$("data-run").addEventListener("click", startJob);

$("data-cancel").addEventListener("click", async () => {
  if (!state.jobId) return;
  try {
    await fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" });
  } catch (error) {
    console.warn("cancel failed", error);
  }
});

function validArea() {
  if (state.area?.kind !== "circle") return true;
  const radius = state.area.radius_m;
  if (radius >= MIN_RADIUS_M && radius <= MAX_RADIUS_M) return true;
  showError(`Krug mora biti izmedju ${MIN_RADIUS_M} m i ${MAX_RADIUS_M / 1000} km. Nacrtaj drugi.`);
  return false;
}

function setStep3(note, active) {
  $("data-step3-note").textContent = note;
  $("data-step3").classList.toggle("is-idle", !active);
}

/** Show or hide the results zone without throwing the results away. Closing it
 *  is a view choice — the rows stay rendered and the map keeps its markers, so
 *  reopening is instant. Step 3 grows a way back in while it is closed. */
function setPanelOpen(open) {
  $("data-panel").hidden = !open;
  app.classList.toggle("has-panel", open);
  $("data-show-panel").hidden = open || !state.results;
  relayoutMap();
}

/** The user closed the panel. On a phone tab 3 is the panel, so step back to
 *  the input tabs — otherwise the screen would be empty. */
function closePanel() {
  if (app.dataset.tab === "3") selectTab("1");
  setPanelOpen(false);
}

$("data-panel-close").addEventListener("click", closePanel);
$("data-show-panel").addEventListener("click", () => setPanelOpen(true));

/** A new run wipes the zone: no results left to come back to. */
function hidePanel() {
  setPanelOpen(false);
  $("data-tabs").querySelector('[data-tab-to="3"]').disabled = true;
  pointLayer.clearLayers();
  lastPoints = [];
}

function resetResultsView() {
  clearTimeout(state.filterTimer);
  view.page = 1;
  view.sort = "name";
  view.order = "asc";
  view.categories.clear();
  view.withSite = false;
  view.deadOnly = false;
  view.facets = [];
  view.facetFilter = "";
  state.results = null;
  clearTimeout(state.checkTimer);
  clearTimeout(state.readTimer);
  check.running = false;
  check.autoHidden = false;
  read.running = false;
  $("data-filter-q").value = "";
  $("data-filter-contact").checked = false;
  $("data-filter-with-site").checked = false;
  $("data-filter-hide-found").checked = false;
  $("data-filter-collapse").checked = true;
  $("data-filter-commercial").checked = true;
  $("data-check-band").hidden = true;
  closeFacetMenu();
  hidePanel();
}

async function startJob() {
  if (!state.area || !validArea()) return;

  hideError();
  resetResultsView();
  setStep3("Cekam odgovor servera.", false);
  state.jobId = null;
  state.busy = true;
  showWorking(null);
  refreshRunButton();

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ area: state.area, categories: selectedCategories() }),
    });
    if (!response.ok) throw new Error(await messageOf(response));
    const { job_id, cached } = await response.json();
    state.jobId = job_id;
    state.cached = Boolean(cached);
    pollJob();
  } catch (error) {
    setStep3("Upit nije izvrsen.", false);
    showError(humanError(error));
    refreshRunButton();
  }
}

function pollJob() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/jobs/${state.jobId}`);
      if (!response.ok) throw new Error(await messageOf(response));
      const job = await response.json();
      if (job.status === "running" || job.status === "pending") {
        showWorking(job);
        pollJob();
      } else {
        onJobFinished(job);
      }
    } catch (error) {
      setStep3("Upit nije izvrsen.", false);
      showError(humanError(error));
      refreshRunButton();
    }
  }, POLL_INTERVAL_MS);
}

function onJobFinished(job) {
  clearTimeout(state.pollTimer);
  state.busy = false;
  hideWorking();
  refreshRunButton();

  if (job.status === "error") {
    setStep3("Upit nije izvrsen.", false);
    showError(job.error || "Nesto je poslo naopako. Pokusaj ponovo.");
    return;
  }
  if (job.status === "cancelled") {
    setStep3("Otkazano.", false);
    showError("Posao je otkazan. Oblast i kategorije su sacuvane.");
    return;
  }
  if (job.rows === 0) {
    setStep3("0 firmi u oblasti.", false);
    showError("Nije pronadjena nijedna firma u ovoj oblasti. Probaj vecu oblast ili vise kategorija.");
    return;
  }
  state.elapsed = job.elapsed_s;
  loadResults();
}

// --- results ----------------------------------------------------------------

/** Businesses that already have a website are hidden unless the box is ticked.
 *  The default is the useful one: whoever is missing a site.
 *
 *  `dead` is the same axis rather than a filter of its own - it is another
 *  answer to "what is the state of their website" - so it simply wins while it
 *  is on, and the checkbox goes back to deciding once it is off. */
function websiteFilter() {
  if (view.deadOnly) return "dead";
  return view.withSite ? "any" : "no";
}

function filterParams() {
  const params = new URLSearchParams();
  for (const category of view.categories) params.append("categories", category);
  if ($("data-filter-contact").checked) params.set("require_contact", "true");
  const website = websiteFilter();
  if (website !== "any") params.set("website", website);
  const query = $("data-filter-q").value.trim();
  if (query) params.set("q", query);
  // These two default to on, so only the unticked state is worth sending.
  if (!$("data-filter-collapse").checked) params.set("collapse", "false");
  if (!$("data-filter-commercial").checked) params.set("commercial_only", "false");
  if ($("data-filter-hide-found").checked) params.set("hide_found", "true");
  params.set("sort", view.sort);
  params.set("order", view.order);
  return params;
}

async function loadResults() {
  const params = filterParams();
  params.set("page", String(view.page));
  params.set("page_size", String(PAGE_SIZE));

  let body;
  try {
    const response = await fetch(`/api/jobs/${state.jobId}/results?${params}`);
    if (!response.ok) throw new Error(await messageOf(response));
    body = await response.json();
  } catch (error) {
    showError(humanError(error));
    return;
  }

  state.results = body;
  view.facets = body.facets;

  const wasHidden = $("data-panel").hidden;
  setPanelOpen(true);
  $("data-tabs").querySelector('[data-tab-to="3"]').disabled = false;
  if (wasHidden) selectTab("3");

  renderHead(body);
  renderCheckBand(body);
  renderFilterNote();
  renderFacets();
  renderRows(body.rows);
  renderPager(body);
  renderStep3(body);
  loadPoints();
}

async function loadPoints() {
  try {
    const response = await fetch(`/api/jobs/${state.jobId}/points?${filterParams()}`);
    if (!response.ok) return;
    const { points } = await response.json();
    renderPoints(points);
  } catch (error) {
    console.warn("points unavailable", error);
  }
}

function renderHead(body) {
  const { area_total: areaTotal, area_without_site: areaWithoutSite } = body.counts;
  // With the dead filter on, "how many are missing a site" is not the sentence
  // the table is answering, so the headline reports the area as a whole.
  const headline = view.withSite || view.deadOnly ? areaTotal : areaWithoutSite;

  const line = $("data-count-line");
  clear(line);
  line.append(
    document.createTextNode("Pronadjeno: "),
    element("span", "num", String(headline)),
    document.createTextNode(
      ` ${plural(headline, "firma", "firme", "firmi")} u oblasti ${body.area_label}.`,
    ),
  );

  const cached = state.cached;
  $("data-cache-stamp").hidden = !cached;
  if (cached) {
    $("data-cache-note").textContent = `${state.elapsed.toFixed(1)} s — bez upita ka Overpass-u`;
  }

  renderSiteBand(areaTotal, areaWithoutSite);

  $("data-top-note-m").textContent =
    `${body.total} ${plural(body.total, "firma", "firme", "firmi")}`;
}

function renderSiteBand(areaTotal, areaWithoutSite) {
  const hiddenCount = areaTotal - areaWithoutSite;

  // While the dead filter is on the table is neither "those without a site" nor
  // "everyone", so the band says what it is instead of doing sums about a split
  // it is not showing.
  if (view.deadOnly) {
    $("data-site-band").classList.remove("is-hiding");
    $("data-site-say").hidden = false;
    $("data-site-say").textContent = "Prikazane su samo firme kojima sajt ne radi.";
    $("data-site-chip").hidden = true;
    $("data-site-math").hidden = true;
    $("data-site-toggle").hidden = true;
    return;
  }

  $("data-site-toggle").hidden = false;
  $("data-site-say").textContent = "Prikazane su samo firme bez sajta.";

  const hiding = !view.withSite;
  $("data-site-band").classList.toggle("is-hiding", hiding);
  $("data-site-say").hidden = !hiding;
  $("data-site-chip").hidden = !hiding || hiddenCount === 0;
  $("data-site-chip").textContent = `${hiddenCount} sakriveno`;
  $("data-site-math").hidden = hiding;
  $("data-site-math").textContent =
    `${areaTotal} = ${areaWithoutSite} bez sajta + ${hiddenCount} sa sajtom`;

  $("data-site-toggle").classList.toggle("is-on", view.withSite);
  $("data-site-toggle-chip").hidden = !hiding || hiddenCount === 0;
  $("data-site-toggle-chip").textContent = `+${hiddenCount}`;
}

// --- the website search -----------------------------------------------------

/* OSM's `website` tag is written by whoever mapped the shop, not by the shop, so
 * a business with a perfectly good site still lands in the "no site" pile and
 * costs a phone call to find that out. This band searches the web for them.
 *
 * It is slow by design - every search engine worth asking rate-limits a burst -
 * so it runs in capped passes, reports progress, and can be stopped. */

function renderCheckBand(body) {
  const {
    unchecked, unchecked_here: here, found_sites: found, maybe_sites: maybe,
  } = body.counts;
  const progress = body.enrich || { status: "idle" };
  const band = $("data-check-band");

  // Nothing to check and nothing found means there is nothing to say. The area
  // count decides, not the filtered one, so the band does not vanish the moment
  // a filter excludes everything - that is exactly when it has something to say.
  band.hidden = unchecked === 0 && found === 0 && maybe === 0;
  if (band.hidden) return;

  check.running = progress.status === "running";
  band.classList.toggle("is-running", check.running);
  $("data-check-sweep").hidden = !check.running;
  $("data-check-cancel").hidden = !check.running;
  $("data-check-run").disabled = check.running || here === 0;
  // One pass is capped, so the button names what it will actually get through
  // rather than the whole queue.
  const willDo = Math.min(here, CHECK_LIMIT);
  $("data-check-run").textContent = willDo > 0 ? `Proveri ovih ${willDo}` : "Proveri sajtove";

  $("data-check-say").textContent = checkSentence(progress, unchecked, here);
  $("data-check-count").hidden = !check.running;
  $("data-check-count").textContent = `${progress.checked} / ${progress.total}`;

  const anyFound = found > 0;
  $("data-check-foot").hidden = !anyFound;
  $("data-check-toggle").classList.toggle("is-on", $("data-filter-hide-found").checked);
  $("data-check-toggle-chip").hidden = !anyFound;
  $("data-check-toggle-chip").textContent = `${found}`;
  $("data-check-note").textContent = maybe
    ? `${maybe} ${plural(maybe, "je za proveru", "su za proveru", "je za proveru")} — oznaceni su u koloni Sajt`
    : "";

  renderReadLine(body);
}

/* Reading the sites of rows that have one. A separate pass over a separate set
 * of rows: the search looks for businesses *without* a site, this reads the
 * ones that have one for the email and phone the map almost never carries. */

function renderReadLine(body) {
  const {
    unread, unread_here: here, found_emails: emails, found_phones: phones,
    dead_sites: dead,
  } = body.counts;
  const progress = body.contacts || { status: "idle" };

  const line = $("data-read-line");
  line.hidden = unread === 0 && emails === 0 && phones === 0 && dead === 0;
  if (line.hidden) return;

  read.running = progress.status === "running";
  $("data-read-sweep").hidden = !read.running;
  $("data-read-cancel").hidden = !read.running;
  $("data-read-run").disabled = read.running || here === 0;
  const willDo = Math.min(here, READ_LIMIT);
  $("data-read-run").textContent = willDo > 0 ? `Procitaj ovih ${willDo}` : "Procitaj sajtove";

  $("data-read-count").hidden = !read.running;
  $("data-read-count").textContent = `${progress.checked} / ${progress.total}`;
  $("data-read-say").textContent = readSentence(progress, unread, here, { emails, phones, dead });

  const chip = $("data-dead-chip");
  chip.hidden = dead === 0;
  chip.textContent = view.deadOnly
    ? `${dead} sa mrtvim sajtom — prikazi sve`
    : `${dead} ${plural(dead, "mrtav sajt", "mrtva sajta", "mrtvih sajtova")}`;
  chip.setAttribute("aria-pressed", String(view.deadOnly));
  chip.title = "Firme koje su imale sajt pa vise ne odgovara — verovatno ne znaju";
}

/** Businesses whose site has gone: they had one, so they wanted one. */
function toggleDeadOnly() {
  view.deadOnly = !view.deadOnly;
  view.page = 1;
  loadResults();
}

$("data-dead-chip").addEventListener("click", toggleDeadOnly);

function readSentence(progress, unread, here, found) {
  if (progress.status === "running") return "Citam sajtove. Ovo ide brzo.";
  if (progress.status === "error") return progress.message || "Citanje je puklo.";

  const tail = readRemainder(unread, here);
  const haul = [];
  if (found.emails) haul.push(`${found.emails} email`);
  if (found.phones) haul.push(`${found.phones} telefona`);
  if (found.dead) haul.push(`${found.dead} mrtvih sajtova`);
  const got = haul.length ? `Sa sajtova: ${haul.join(", ")}.` : "";

  if (progress.status === "cancelled") return `Prekinuto. ${got} ${tail}`.replace(/\s+/g, " ").trim();
  if (progress.status === "done") return `${got} ${tail}`.replace(/\s+/g, " ").trim();
  return tail;
}

function readRemainder(unread, here) {
  if (unread === 0) return "Svi sajtovi procitani.";
  if (here === 0) return `Filteri ne ostavljaju sajt za citanje. U oblasti jos ${unread}.`;
  if (here < unread) return `Filtrirano: ${here} neprocitano, u celoj oblasti jos ${unread}.`;
  return `${unread} ${plural(unread, "sajt nije procitan", "sajta nisu procitana", "sajtova nije procitano")}.`;
}

async function startRead() {
  if (read.running || !state.jobId) return;
  const params = filterParams();
  params.delete("hide_found");
  try {
    const response = await fetch(`/api/jobs/${state.jobId}/contacts?${params}`, { method: "POST" });
    if (!response.ok) throw new Error(await messageOf(response));
  } catch (error) {
    showError(humanError(error));
    return;
  }
  read.running = true;
  $("data-read-sweep").hidden = false;
  $("data-read-cancel").hidden = false;
  $("data-read-run").disabled = true;
  $("data-read-say").textContent = "Citam sajtove. Ovo ide brzo.";
  pollRead();
}

function pollRead() {
  clearTimeout(state.readTimer);
  state.readTimer = setTimeout(async () => {
    if (!state.jobId) return;
    let progress;
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/contacts`);
      if (!response.ok) return;
      progress = await response.json();
    } catch {
      return;  // a dropped poll is not worth an error band; the next one retries
    }
    $("data-read-count").textContent = `${progress.checked} / ${progress.total}`;
    if (progress.status === "running") {
      pollRead();
      return;
    }
    read.running = false;
    loadResults();
  }, POLL_INTERVAL_MS);
}

async function cancelRead() {
  clearTimeout(state.readTimer);
  if (!state.jobId) return;
  try {
    await fetch(`/api/jobs/${state.jobId}/contacts`, { method: "DELETE" });
  } catch {
    // Cancelling is cooperative anyway; the next load reports what happened.
  }
  read.running = false;
  loadResults();
}

$("data-read-run").addEventListener("click", startRead);
$("data-read-cancel").addEventListener("click", cancelRead);

function checkSentence(progress, unchecked, here) {
  if (progress.status === "running") return "Trazim sajtove. Ide polako, oko 2 s po firmi.";
  if (progress.status === "error") return progress.message || "Provera je pukla.";
  // The summary is labelled because it outlives the filters that produced it:
  // check nine bakeries, then narrow to hairdressers, and a bare "Provereno 9"
  // reads as nine hairdressers.
  if (progress.status === "cancelled") return `Prekinuto. ${remainderSentence(unchecked, here)}`;
  if (progress.status === "done") {
    return `Poslednji prolaz: ${progress.message} ${remainderSentence(unchecked, here)}`.trim();
  }
  if (unchecked === 0) return "Sve provereno.";
  return remainderSentence(unchecked, here);
}

/** The two numbers only differ when a filter is narrowing, and that is the whole
 *  point of saying both: the button works on `here`, the area holds `unchecked`. */
function remainderSentence(unchecked, here) {
  if (unchecked === 0) return "Sve provereno.";
  if (here === 0) return `Filteri ne ostavljaju nista za proveru. U oblasti jos ${unchecked}.`;
  if (here < unchecked) return `Filtrirano: ${here} neprovereno, u celoj oblasti jos ${unchecked}.`;
  return `${unchecked} ${plural(unchecked, "firma nije proverena", "firme nisu proverene", "firmi nije provereno")}.`;
}

async function startCheck() {
  if (check.running || !state.jobId) return;
  const params = filterParams();
  params.delete("hide_found");  // the pass works on everything, not on what is shown
  try {
    const response = await fetch(`/api/jobs/${state.jobId}/enrich?${params}`, { method: "POST" });
    if (!response.ok) throw new Error(await messageOf(response));
  } catch (error) {
    showError(humanError(error));
    return;
  }
  check.running = true;
  renderCheckRunning();
  pollCheck();
}

/** Paint the running state at once rather than waiting a poll for it to show. */
function renderCheckRunning() {
  $("data-check-band").classList.add("is-running");
  $("data-check-sweep").hidden = false;
  $("data-check-cancel").hidden = false;
  $("data-check-run").disabled = true;
  $("data-check-say").textContent = "Trazim sajtove. Ide polako, oko 2 s po firmi.";
}

function pollCheck() {
  clearTimeout(state.checkTimer);
  state.checkTimer = setTimeout(async () => {
    if (!state.jobId) return;
    let progress;
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/enrich`);
      if (!response.ok) return;
      progress = await response.json();
    } catch {
      return;  // a dropped poll is not worth an error band; the next one retries
    }

    $("data-check-count").textContent = `${progress.checked} / ${progress.total}`;
    if (progress.status === "running") {
      pollCheck();
      return;
    }
    check.running = false;
    // Reloading brings the findings down with the rows and repaints the band.
    if (progress.found > 0 && !check.autoHidden) {
      check.autoHidden = true;
      $("data-filter-hide-found").checked = true;
      view.page = 1;
    }
    loadResults();
  }, POLL_INTERVAL_MS);
}

async function cancelCheck() {
  clearTimeout(state.checkTimer);
  if (!state.jobId) return;
  try {
    await fetch(`/api/jobs/${state.jobId}/enrich`, { method: "DELETE" });
  } catch {
    // Cancelling is cooperative anyway; the next poll reports what happened.
  }
  check.running = false;
  loadResults();
}

$("data-check-run").addEventListener("click", startCheck);
$("data-check-cancel").addEventListener("click", cancelCheck);
$("data-filter-hide-found").addEventListener("change", () => {
  view.page = 1;
  loadResults();
});

/** Deviations from the default view, in either direction: unticking `Sazmi
 *  lance` shows more rows, but it is still the user having changed something. */
function activeFilterCount() {
  let count = 0;
  if ($("data-filter-q").value.trim()) count += 1;
  if ($("data-filter-contact").checked) count += 1;
  if (view.categories.size > 0) count += 1;
  if (view.deadOnly) count += 1;
  if (!$("data-filter-collapse").checked) count += 1;
  if (!$("data-filter-commercial").checked) count += 1;
  if ($("data-filter-hide-found").checked) count += 1;
  return count;
}

function renderFilterNote() {
  const count = activeFilterCount();
  $("data-filter-q").classList.toggle("is-set", Boolean($("data-filter-q").value.trim()));
  if (count > 0) {
    $("data-filter-note").textContent =
      `${count} ${plural(count, "aktivan filter", "aktivna filtera", "aktivnih filtera")}`;
  } else {
    $("data-filter-note").textContent = view.withSite ? "bez filtera" : "podrazumevani filter aktivan";
  }
}

// --- facets -----------------------------------------------------------------

function toggleFacet(value, on) {
  if (on) view.categories.add(value);
  else view.categories.delete(value);
  view.page = 1;
  loadResults();
}

function renderFacets() {
  $("data-facet-total").textContent = String(view.facets.length);

  // Ticked ones first, so a selection never scrolls out of the strip.
  const ordered = [...view.facets].sort((a, b) => {
    const selected = Number(view.categories.has(b.value)) - Number(view.categories.has(a.value));
    return selected || b.count - a.count;
  });

  const chips = $("data-facet-chips");
  clear(chips);
  for (const facet of ordered.slice(0, VISIBLE_CHIPS)) {
    chips.appendChild(facetChip(facet));
  }
  const rest = Math.max(0, ordered.length - VISIBLE_CHIPS);
  $("data-facet-rest").textContent = rest ? `+ ${rest}` : "";

  renderFacetMenu();
}

function facetChip(facet) {
  const on = view.categories.has(facet.value);
  const label = element("label", `facet-chip${on ? " is-on" : ""}`);
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = on;
  input.addEventListener("change", () => toggleFacet(facet.value, input.checked));
  label.append(
    input,
    element("span", "facet-key", facet.value),
    element("span", "facet-count", String(facet.count)),
  );
  return label;
}

function renderFacetMenu() {
  const needle = view.facetFilter.trim().toLowerCase();
  const shown = view.facets.filter(
    (facet) => !needle
      || facet.value.includes(needle)
      || (GLOSSES[facet.value] || "").includes(needle),
  );

  const list = $("data-facet-list");
  clear(list);
  for (const facet of shown) {
    const label = element("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = view.categories.has(facet.value);
    input.addEventListener("change", () => toggleFacet(facet.value, input.checked));
    label.append(
      input,
      element("span", "facet-key", facet.value),
      element("span", "facet-gloss", GLOSSES[facet.value] || ""),
      element("span", "facet-count", String(facet.count)),
    );
    list.appendChild(label);
  }

  $("data-facet-foot").textContent = needle
    ? `${shown.length} od ${view.facets.length} ${plural(view.facets.length, "kategorije", "kategorije", "kategorija")}`
    : `${view.facets.length} ${plural(view.facets.length, "kategorija", "kategorije", "kategorija")} — skroluj ili filtriraj`;
}

function openFacetMenu() {
  $("data-facet-menu").hidden = false;
  $("data-facet-open").setAttribute("aria-expanded", "true");
  $("data-facet-filter").focus();
}

function closeFacetMenu() {
  $("data-facet-menu").hidden = true;
  $("data-facet-open").setAttribute("aria-expanded", "false");
}

$("data-facet-open").addEventListener("click", () => {
  if ($("data-facet-menu").hidden) openFacetMenu();
  else closeFacetMenu();
});

$("data-facet-filter").addEventListener("input", (event) => {
  view.facetFilter = event.target.value;
  renderFacetMenu();
});

document.addEventListener("click", (event) => {
  if (!$("data-facet-menu").hidden && !event.target.closest(".facet-bar")) closeFacetMenu();
  if (state.candidatesOpen && !event.target.closest(".search-wrap")) {
    closeCandidates();
    renderArea();
  }
});

// --- rows -------------------------------------------------------------------

function renderRows(rows) {
  const empty = rows.length === 0;
  $("data-empty").hidden = !empty;
  $("data-table-body").hidden = empty;
  $("data-cards").hidden = empty;
  if (empty) {
    renderEmptyState();
    return;
  }
  renderTable(rows);
  renderCards(rows);
}

function addressOf(row) {
  return [row.street, row.housenumber].filter(Boolean).join(" ");
}

function siteLink(url, className) {
  const link = element("a", className);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = url.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "");
  return link;
}

/** Two kinds of site, never blended: the one OSM carries, and the one the search
 *  turned up. A found site is marked so the user knows nobody vouched for it. */
function siteCell(row) {
  const cell = element("div", "cell cell-mono cell-site");
  if (row.website) {
    // A site the map carries that no longer answers puts the business back in
    // the pile: they need one again, they just do not look like it.
    if (row.contact_status === "dead") cell.classList.add("cell-dead");
    cell.appendChild(siteLink(row.website));
    return cell;
  }
  if (!row.found_website) return cell;
  const sure = row.found_confidence === "strong";
  cell.classList.add(sure ? "is-found" : "is-maybe");
  cell.append(
    siteLink(row.found_website, "found-link"),
    element("span", "found-tag", sure ? "pretraga" : "proveri"),
  );
  return cell;
}

/** The address OSM carries, or the one read off the business's own site. */
function mailCell(row) {
  const cell = element("div", "cell cell-mono cell-mail");
  const address = row.email || row.found_email;
  if (!address) return cell;
  const link = element("a", row.email ? "" : "found-link");
  link.href = `mailto:${address}`;
  link.textContent = address;
  cell.appendChild(link);
  return cell;
}

function phoneCell(row) {
  const cell = element("div", "cell cell-mono");
  if (row.phone) {
    cell.textContent = row.phone;
    return cell;
  }
  if (row.found_phone) {
    cell.classList.add("cell-found");
    cell.textContent = row.found_phone;
  }
  return cell;
}

function renderTable(rows) {
  const body = $("data-table-body");
  clear(body);
  for (const row of rows) {
    const line = element("div", "grid-row");
    line.append(
      element("div", "cell", row.name),
      element("div", "cell cell-mono cell-muted", row.category),
      element("div", "cell", addressOf(row)),
      phoneCell(row),
      mailCell(row),
      siteCell(row),
      element("div", "cell cell-hours", row.opening_hours),
    );
    body.appendChild(line);
  }
}

function renderCards(rows) {
  const cards = $("data-cards");
  clear(cards);
  for (const row of rows) {
    const card = element("div", "card");
    const top = element("div", "card-top");
    top.append(element("div", "card-name", row.name), element("div", "card-key", row.category));
    const contact = element("div", "card-contact");
    const phone = row.phone || row.found_phone;
    const mail = row.email || row.found_email;
    if (phone) contact.appendChild(element("div", "card-phone", phone));
    if (mail) contact.appendChild(element("div", "card-mail", mail));
    if (row.opening_hours) contact.appendChild(element("div", "card-hours", row.opening_hours));
    card.append(top, element("div", "card-address", addressOf(row)));
    if (contact.childElementCount) card.appendChild(contact);
    cards.appendChild(card);
  }
}

function renderEmptyState() {
  const counts = state.results.counts;
  if (view.deadOnly) {
    // Reached by filtering the dead ones down to nothing; offering "show those
    // with a site" here would answer a question nobody asked.
    $("data-empty-say").textContent =
      "Nijedna firma sa mrtvim sajtom ne odgovara ostalim filterima.";
    $("data-empty-note").textContent =
      `${counts.dead_sites} ${plural(counts.dead_sites, "firmi je sajt crkao", "firmama je sajt crkao", "firmama je sajt crkao")} u oblasti`;
    $("data-show-with-site").hidden = true;
    $("data-reset-filters").disabled = false;
    return;
  }
  const scope = view.withSite ? counts.area_total : counts.area_without_site;
  const suffix = view.withSite ? "" : " Prikazane su samo firme bez sajta.";
  $("data-empty-say").textContent = `Nijedna firma ne odgovara filterima.${suffix}`;
  $("data-empty-note").textContent =
    `${scope} ${plural(scope, "firma je", "firme su", "firmi je")} u oblasti — filteri ih sve iskljucuju`;
  $("data-show-with-site").hidden = view.withSite;
  $("data-reset-filters").disabled = activeFilterCount() === 0;
}

$("data-reset-filters").addEventListener("click", () => {
  $("data-filter-q").value = "";
  $("data-filter-contact").checked = false;
  $("data-filter-collapse").checked = true;
  $("data-filter-commercial").checked = true;
  $("data-filter-hide-found").checked = false;
  view.categories.clear();
  view.deadOnly = false;
  view.facetFilter = "";
  $("data-facet-filter").value = "";
  view.page = 1;
  loadResults();
});

$("data-show-with-site").addEventListener("click", () => {
  $("data-filter-with-site").checked = true;
  setWithSite(true);
});

// --- sorting, paging, download ----------------------------------------------

const ARROWS = { asc: "▲", desc: "▼" };

function renderPager(body) {
  const pages = Math.max(1, Math.ceil(body.total / body.page_size));
  $("data-page-info").textContent = `Strana ${body.page} od ${pages}`;
  $("data-prev").disabled = body.page <= 1;
  $("data-next").disabled = body.page >= pages;

  $("data-download").disabled = body.total === 0;
  $("data-download-note").textContent = body.total === 0
    ? "nema sta da se skine"
    : `skida tacno ${body.total} ${plural(body.total, "filtriran red", "filtrirana reda", "filtriranih redova")}`;
  $("data-download-name").textContent = body.export_filename;

  for (const button of $("data-table-head").querySelectorAll("[data-sort]")) {
    const sorted = button.dataset.sort === view.sort;
    button.classList.toggle("is-sorted", sorted);
    button.querySelector(".arrow").textContent = sorted ? ARROWS[view.order] : "";
  }
}

function renderStep3(body) {
  const counts = body.counts;
  if (body.total === 0) {
    const scope = view.withSite ? counts.area_total : counts.area_without_site;
    setStep3(`0 prikazano, ${scope} u oblasti`, true);
    return;
  }
  setStep3(`${body.total} od ${counts.area_total} firmi${state.cached ? " — iz kesa" : ""}`, true);
}

$("data-prev").addEventListener("click", () => { view.page -= 1; loadResults(); });
$("data-next").addEventListener("click", () => { view.page += 1; loadResults(); });

$("data-table-head").addEventListener("click", (event) => {
  const column = event.target.closest("[data-sort]")?.dataset.sort;
  if (!column) return;
  view.order = view.sort === column && view.order === "asc" ? "desc" : "asc";
  view.sort = column;
  view.page = 1;
  loadResults();
});

function setWithSite(on) {
  view.withSite = on;
  view.deadOnly = false;  // same axis: asking for one is dropping the other
  view.page = 1;
  loadResults();
}

$("data-filter-with-site").addEventListener("change", (event) => setWithSite(event.target.checked));
$("data-filter-contact").addEventListener("change", () => { view.page = 1; loadResults(); });
for (const attribute of ["data-filter-collapse", "data-filter-commercial"]) {
  $(attribute).addEventListener("change", () => { view.page = 1; loadResults(); });
}

$("data-filter-q").addEventListener("input", () => {
  clearTimeout(state.filterTimer);
  state.filterTimer = setTimeout(() => { view.page = 1; loadResults(); }, FILTER_DEBOUNCE_MS);
});

$("data-download").addEventListener("click", () => {
  window.location.href = `/api/jobs/${state.jobId}/export.xlsx?${filterParams()}`;
});

// --- mobile: tabs and the map drawer ----------------------------------------

function selectTab(name) {
  // Tab 3 is the results zone; picking it reopens a panel the user had closed.
  if (name === "3" && state.results && $("data-panel").hidden) setPanelOpen(true);
  app.dataset.tab = name;
  for (const button of $("data-tabs").querySelectorAll("[data-tab-to]")) {
    button.setAttribute("aria-selected", String(button.dataset.tabTo === name));
  }
  relayoutMap();
}

$("data-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab-to]");
  if (button && !button.disabled) selectTab(button.dataset.tabTo);
});

$("data-map-expand").addEventListener("click", () => {
  const expanded = app.classList.toggle("map-expanded");
  $("data-map-expand").textContent = expanded ? "Skupi mapu" : "Razvuci mapu";
  relayoutMap();
});

// --- start ------------------------------------------------------------------

renderArea();
refreshRunButton();
