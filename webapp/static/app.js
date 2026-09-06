"use strict";

const state = {
  area: null,      // { kind, area_id?, label?, bbox?, center?, radius_m? }
  jobId: null,
  pollTimer: null,
};

const $ = (attribute) => document.querySelector(`[${attribute}]`);

const map = L.map($("data-map")).setView([44.0, 21.0], 7);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

let areaLayer = null;

function showArea(layer, summary) {
  if (areaLayer) map.removeLayer(areaLayer);
  areaLayer = layer;
  if (layer) {
    layer.addTo(map);
    map.fitBounds(layer.getBounds(), { padding: [20, 20] });
  }
  $("data-area-summary").textContent = summary;
}

function clearArea() {
  state.area = null;
  showArea(null, "Nije izabrana nijedna oblast.");
  refreshRunButton();
}

// --- search ---------------------------------------------------------------

let searchTimer = null;

$("data-search-input").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const query = event.target.value.trim();
  if (query.length < 2) {
    $("data-search-results").hidden = true;
    return;
  }
  searchTimer = setTimeout(() => runSearch(query), 400);
});

async function runSearch(query) {
  const list = $("data-search-results");
  try {
    const response = await fetch(`/api/places?q=${encodeURIComponent(query)}&country=RS`);
    if (!response.ok) throw new Error(await messageOf(response));
    const candidates = await response.json();
    renderCandidates(candidates);
  } catch (error) {
    list.hidden = false;
    list.innerHTML = "";
    const item = document.createElement("li");
    item.textContent = error.message;
    list.appendChild(item);
  }
}

function renderCandidates(candidates) {
  const list = $("data-search-results");
  list.hidden = false;
  list.innerHTML = "";
  if (candidates.length === 0) {
    const item = document.createElement("li");
    item.textContent = "Nema rezultata. Probaj drugaciji naziv ili nacrtaj oblast na mapi.";
    list.appendChild(item);
    return;
  }
  for (const candidate of candidates) {
    const item = document.createElement("li");
    item.textContent = candidate.display_name;
    item.addEventListener("click", () => pickCandidate(candidate));
    list.appendChild(item);
  }
}

async function pickCandidate(candidate) {
  state.area = { kind: "area", area_id: candidate.area_id, label: candidate.display_name };
  $("data-search-results").hidden = true;
  $("data-search-input").value = candidate.display_name;
  $("data-area-summary").textContent = candidate.display_name;
  refreshRunButton();

  try {
    const response = await fetch(`/api/places/${candidate.osm_type}/${candidate.osm_id}/geometry`);
    if (!response.ok) return;  // the boundary is a nicety, not a requirement
    const { geojson } = await response.json();
    showArea(
      L.geoJSON(geojson, { style: { color: "#0b6", weight: 2, fillOpacity: 0.08 } }),
      candidate.display_name,
    );
  } catch (error) {
    console.warn("boundary unavailable", error);
  }
}

// --- drawing --------------------------------------------------------------

map.pm.setGlobalOptions({ snappable: false });

$("data-draw-rect").addEventListener("click", () => map.pm.enableDraw("Rectangle"));
$("data-draw-circle").addEventListener("click", () => map.pm.enableDraw("Circle"));
$("data-clear-area").addEventListener("click", () => {
  map.pm.disableDraw();
  $("data-search-input").value = "";
  clearArea();
});

map.on("pm:create", (event) => {
  map.pm.disableDraw();
  const layer = event.layer;
  // A drawn shape replaces whatever the search box found, so stop showing that name.
  $("data-search-input").value = "";

  if (event.shape === "Rectangle") {
    const bounds = layer.getBounds();
    state.area = {
      kind: "bbox",
      bbox: [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()],
    };
    showArea(layer, `Pravougaonik: ${formatBounds(bounds)}`);
  } else {
    const center = layer.getLatLng();
    const radius = Math.round(layer.getRadius());
    state.area = { kind: "circle", center: [center.lat, center.lng], radius_m: radius };
    showArea(layer, `Krug: ${radius} m oko ${center.lat.toFixed(4)}, ${center.lng.toFixed(4)}`);
  }
  refreshRunButton();
});

// --- helpers --------------------------------------------------------------

function formatBounds(bounds) {
  return `${bounds.getSouth().toFixed(3)}, ${bounds.getWest().toFixed(3)} - ` +
         `${bounds.getNorth().toFixed(3)}, ${bounds.getEast().toFixed(3)}`;
}

async function messageOf(response) {
  try {
    const body = await response.json();
    return body.detail || `Greska ${response.status}`;
  } catch {
    return `Greska ${response.status}`;
  }
}

// --- running a job --------------------------------------------------------

const POLL_INTERVAL_MS = 1000;
const MIN_RADIUS_M = 50;
const MAX_RADIUS_M = 50000;

function selectedCategories() {
  return [...$("data-categories").querySelectorAll("input:checked")].map((input) => input.value);
}

function refreshRunButton() {
  const ready = state.area !== null && selectedCategories().length > 0;
  $("data-run").disabled = !ready;
}

$("data-categories").addEventListener("change", refreshRunButton);

$("data-run").addEventListener("click", startJob);

$("data-cancel").addEventListener("click", async () => {
  if (!state.jobId) return;
  await fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" });
});

function validArea() {
  if (state.area?.kind === "circle") {
    const radius = state.area.radius_m;
    if (radius < MIN_RADIUS_M || radius > MAX_RADIUS_M) {
      showProgress(`Krug mora biti izmedju ${MIN_RADIUS_M} m i ${MAX_RADIUS_M / 1000} km. Nacrtaj drugi.`);
      return false;
    }
  }
  return true;
}

function resetResultsView() {
  clearTimeout(filterTimer);
  view.page = 1;
  view.sort = "name";
  view.order = "asc";
  view.categories.clear();
  $("data-filter-q").value = "";
  $("data-filter-contact").checked = false;
  $("data-facets").innerHTML = "";
  $("data-table-body").innerHTML = "";
  $("data-results").hidden = true;
}

async function startJob() {
  if (!state.area || !validArea()) return;

  resetResultsView();
  $("data-run").disabled = true;
  showProgress("Pokrecem pretragu...");

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ area: state.area, categories: selectedCategories() }),
    });
    if (!response.ok) throw new Error(await messageOf(response));
    const { job_id } = await response.json();
    state.jobId = job_id;
    pollJob();
  } catch (error) {
    showProgress(error.message);
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
      renderProgress(job);
      if (job.status === "running" || job.status === "pending") {
        pollJob();
      } else {
        onJobFinished(job);
      }
    } catch (error) {
      showProgress(error.message);
      refreshRunButton();
    }
  }, POLL_INTERVAL_MS);
}

function renderProgress(job) {
  $("data-progress-message").textContent = job.message || "Radim...";
  $("data-progress-elapsed").textContent = `Proteklo: ${Math.round(job.elapsed_s)} s`;
}

function onJobFinished(job) {
  clearTimeout(state.pollTimer);
  refreshRunButton();

  if (job.status === "error") {
    showProgress(job.error || "Nesto je poslo naopako.");
    return;
  }
  if (job.status === "cancelled") {
    showProgress("Otkazano.");
    return;
  }
  if (job.rows === 0) {
    showProgress("Nije pronadjena nijedna firma u ovoj oblasti. Probaj vecu oblast ili vise kategorija.");
    return;
  }
  hideProgress();
  loadResults();
}

function showProgress(message) {
  $("data-progress").hidden = false;
  $("data-progress-message").textContent = message;
  $("data-progress-elapsed").textContent = "";
}

function hideProgress() {
  $("data-progress").hidden = true;
}

// --- results --------------------------------------------------------------

const PAGE_SIZE = 50;

const view = { page: 1, sort: "name", order: "asc", categories: new Set() };

function filterParams() {
  const params = new URLSearchParams();
  for (const category of view.categories) params.append("categories", category);
  if ($("data-filter-contact").checked) params.set("require_contact", "true");
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

  const response = await fetch(`/api/jobs/${state.jobId}/results?${params}`);
  if (!response.ok) {
    showProgress(await messageOf(response));
    return;
  }
  const body = await response.json();
  $("data-results").hidden = false;
  renderCount(body);
  renderFacets(body.facets);
  renderTable(body.rows);
  renderPager(body);
}

function renderCount(body) {
  $("data-results-count").textContent =
    body.total === 0
      ? "Nijedna firma ne odgovara filterima."
      : `Pronadjeno: ${body.total} firmi u oblasti ${body.area_label}.`;
}

function renderFacets(facets) {
  const container = $("data-facets");
  container.innerHTML = "";
  for (const { value, count } of facets) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    input.checked = view.categories.has(value);
    input.addEventListener("change", () => {
      if (input.checked) {
        view.categories.add(value);
      } else {
        view.categories.delete(value);
      }
      view.page = 1;
      loadResults();
    });
    label.append(input, document.createTextNode(` ${value} (${count})`));
    container.appendChild(label);
  }
}

function renderTable(rows) {
  const body = $("data-table-body");
  body.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    const address = [row.street, row.housenumber].filter(Boolean).join(" ");
    appendCell(tr, row.name);
    appendCell(tr, row.category);
    appendCell(tr, address);
    appendCell(tr, row.phone);
    appendLinkCell(tr, row.website);
    appendCell(tr, row.opening_hours);
    body.appendChild(tr);
  }
}

function appendCell(tr, text) {
  const td = document.createElement("td");
  td.textContent = text || "";
  tr.appendChild(td);
}

function appendLinkCell(tr, url) {
  const td = document.createElement("td");
  if (url) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url.replace(/^https?:\/\/(www\.)?/, "");
    td.appendChild(link);
  }
  tr.appendChild(td);
}

function renderPager(body) {
  const pages = Math.max(1, Math.ceil(body.total / body.page_size));
  $("data-page-info").textContent = `Strana ${body.page} od ${pages}`;
  $("data-prev").disabled = body.page <= 1;
  $("data-next").disabled = body.page >= pages;
}

$("data-prev").addEventListener("click", () => { view.page -= 1; loadResults(); });
$("data-next").addEventListener("click", () => { view.page += 1; loadResults(); });

$("data-filter-contact").addEventListener("change", () => { view.page = 1; loadResults(); });

let filterTimer = null;
$("data-filter-q").addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => { view.page = 1; loadResults(); }, 300);
});

$("data-table-head").addEventListener("click", (event) => {
  const column = event.target.closest("[data-sort]")?.dataset.sort;
  if (!column) return;
  view.order = view.sort === column && view.order === "asc" ? "desc" : "asc";
  view.sort = column;
  view.page = 1;
  loadResults();
});

$("data-download").addEventListener("click", () => {
  window.location.href = `/api/jobs/${state.jobId}/export.xlsx?${filterParams()}`;
});

refreshRunButton();
