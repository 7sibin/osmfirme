"use strict";

/* OSM firme — the map is a permanent surface, the sidebar is the control column
 * and results are a third zone beside them. Nothing navigates away: every state
 * (searching, drawing, working, failed, listing) is a band that appears in place.
 */

const POLL_INTERVAL_MS = 1000;
const SEARCH_DEBOUNCE_MS = 400;
const FILTER_DEBOUNCE_MS = 300;
const PAGE_SIZE = 50;
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
};

const view = {
  page: 1,
  sort: "name",
  order: "asc",
  categories: new Set(),
  withSite: false,
  facets: [],
  facetFilter: "",
};

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

function hidePanel() {
  $("data-panel").hidden = true;
  app.classList.remove("has-panel");
  $("data-tabs").querySelector('[data-tab-to="3"]').disabled = true;
  pointLayer.clearLayers();
  lastPoints = [];
  relayoutMap();
}

function resetResultsView() {
  clearTimeout(state.filterTimer);
  view.page = 1;
  view.sort = "name";
  view.order = "asc";
  view.categories.clear();
  view.withSite = false;
  view.facets = [];
  view.facetFilter = "";
  state.results = null;
  $("data-filter-q").value = "";
  $("data-filter-contact").checked = false;
  $("data-filter-with-site").checked = false;
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
 *  The default is the useful one: whoever is missing a site. */
function websiteFilter() {
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
  $("data-panel").hidden = false;
  app.classList.add("has-panel");
  $("data-tabs").querySelector('[data-tab-to="3"]').disabled = false;
  if (wasHidden) {
    selectTab("3");
    relayoutMap();
  }

  renderHead(body);
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
  const hiddenCount = areaTotal - areaWithoutSite;
  const headline = view.withSite ? areaTotal : areaWithoutSite;

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

  $("data-top-note-m").textContent =
    `${body.total} ${plural(body.total, "firma", "firme", "firmi")}`;
}

function activeFilterCount() {
  let count = 0;
  if ($("data-filter-q").value.trim()) count += 1;
  if ($("data-filter-contact").checked) count += 1;
  if (view.categories.size > 0) count += 1;
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

function siteCell(row) {
  const cell = element("div", "cell cell-mono cell-site");
  if (row.website) {
    const link = document.createElement("a");
    link.href = row.website;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = row.website.replace(/^https?:\/\/(www\.)?/, "");
    cell.appendChild(link);
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
      element("div", "cell cell-mono", row.phone),
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
    if (row.phone) contact.appendChild(element("div", "card-phone", row.phone));
    if (row.opening_hours) contact.appendChild(element("div", "card-hours", row.opening_hours));
    card.append(top, element("div", "card-address", addressOf(row)));
    if (contact.childElementCount) card.appendChild(contact);
    cards.appendChild(card);
  }
}

function renderEmptyState() {
  const counts = state.results.counts;
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
  view.categories.clear();
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
  view.page = 1;
  loadResults();
}

$("data-filter-with-site").addEventListener("change", (event) => setWithSite(event.target.checked));
$("data-filter-contact").addEventListener("change", () => { view.page = 1; loadResults(); });

$("data-filter-q").addEventListener("input", () => {
  clearTimeout(state.filterTimer);
  state.filterTimer = setTimeout(() => { view.page = 1; loadResults(); }, FILTER_DEBOUNCE_MS);
});

$("data-download").addEventListener("click", () => {
  window.location.href = `/api/jobs/${state.jobId}/export.xlsx?${filterParams()}`;
});

// --- mobile: tabs and the map drawer ----------------------------------------

function selectTab(name) {
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
