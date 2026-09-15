"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  items: [], cards: new Map(), nextCursor: null, total: 0, loading: false,
  generation: 0, request: null, previews: null, favorites: false,
  selected: null, detail: null, detailRequest: null, viewerPreviews: null,
  mutations: new Set(), dirty: false, returnFocus: null,
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail :
        Array.isArray(body.detail) ? body.detail.map((error) => error.msg).join("; ") : message;
    } catch { /* A proxy may return a non-JSON error. */ }
    throw new Error(message);
  }
  return response.json();
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $("#toast").hidden = true; }, 5000);
}

function formatDate(value, withTime = false) {
  if (!value) return "Not recorded";
  // A camera's wall clock must not change days with the browser's timezone.
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?/);
  if (!match) return value;
  const [, year, month, day, hour = "00", minute = "00", second = "00"] = match;
  const date = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}Z`);
  if (Number.isNaN(date.getTime())) return value;
  const options = { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  if (withTime) Object.assign(options, { hour: "2-digit", minute: "2-digit" });
  const offset = withTime ? value.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0] : null;
  return new Intl.DateTimeFormat(undefined, options).format(date) +
    (withTime ? offset ? ` (${offset === "Z" ? "UTC" : `UTC${offset}`})` : " (timezone unknown)" : "");
}

function sizeLabel(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

class PreviewLoader {
  constructor(concurrency = 4) {
    this.controller = new AbortController();
    this.queue = [];
    this.active = 0;
    this.concurrency = concurrency;
    this.urls = new Set();
    this.timers = new Set();
    this.observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          this.observer.unobserve(entry.target);
          this.enqueue(entry.target.previewTask);
        }
      }
    }, { rootMargin: "300px" });
  }

  stop() {
    this.controller.abort();
    this.observer.disconnect();
    this.queue = [];
    this.timers.forEach(clearTimeout);
    this.urls.forEach((url) => URL.revokeObjectURL(url));
  }

  add(container, url, status, alt, immediate = false) {
    const placeholder = element("div", "image-placeholder");
    placeholder.append(element("span", "placeholder-icon", "▧"));
    const label = element("span", "", "Loading preview…");
    placeholder.append(label);
    container.append(placeholder);
    const task = { container, url, label, placeholder, alt, attempts: 0, immediate };
    if (status === "unavailable" || status === "failed") {
      this.problem(task, status);
    } else if (immediate) {
      this.enqueue(task);
    } else {
      container.previewTask = task;
      this.observer.observe(container);
    }
  }

  problem(task, status) {
    task.label.textContent = status === "unavailable" ? "Preview unavailable" : "Preview could not be loaded";
    if (!task.immediate) return;
    const explanation = element("span", "", status === "unavailable" ?
      "No usable embedded preview. You can still download the original." :
      "The original is still available. Try generating the preview again.");
    const retry = element("button", "button secondary", "Retry preview");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        await api(task.url + "/retry", { method: "POST", signal: this.controller.signal });
        retry.remove();
        explanation.remove();
        task.label.textContent = "Preparing preview…";
        this.enqueue(task);
      } catch (error) {
        if (error.name !== "AbortError") task.label.textContent = error.message;
        retry.disabled = false;
      }
    });
    task.placeholder.append(explanation, retry);
  }

  enqueue(task) {
    if (this.controller.signal.aborted) return;
    this.queue.push(task);
    this.pump();
  }

  pump() {
    while (this.active < this.concurrency && this.queue.length) {
      const task = this.queue.shift();
      this.active++;
      this.load(task).finally(() => { this.active--; this.pump(); });
    }
  }

  async load(task) {
    const signal = this.controller.signal;
    try {
      const response = await fetch(task.url, { signal });
      if (response.status === 202) {
        task.label.textContent = task.attempts > 10 ? "Waiting for preview worker…" : "Preparing preview…";
        const wait = Math.min(30000, 2000 + task.attempts++ * 1000);
        const timer = setTimeout(() => { this.timers.delete(timer); this.enqueue(task); }, wait);
        this.timers.add(timer);
        return;
      }
      if (!response.ok) {
        this.problem(task, response.status === 404 ? "unavailable" : "failed");
        return;
      }
      const blob = await response.blob();
      if (signal.aborted) return;
      const url = URL.createObjectURL(blob);
      this.urls.add(url);
      const img = element("img");
      img.alt = task.alt;
      img.decoding = "async";
      img.src = url;
      await img.decode();
      if (signal.aborted) return;
      task.container.prepend(img);
      task.placeholder.remove();
    } catch (error) {
      if (!signal.aborted) this.problem(task, "failed");
    }
  }
}

function filterParams() {
  const params = new URLSearchParams();
  const fields = { q: "#search", date_from: "#date-from", date_to: "#date-to", media_type: "#media-type", rating_min: "#rating-min", sort: "#sort" };
  for (const [key, selector] of Object.entries(fields)) {
    const value = $(selector).value.trim();
    if (value && value !== "0" && !(key === "sort" && value === "newest")) params.set(key, value);
  }
  if (state.favorites) params.set("favorite", "true");
  return params;
}

function syncFilterUI() {
  const params = filterParams();
  const count = ["date_from", "date_to", "media_type", "rating_min"].filter((key) => params.has(key)).length;
  $("#filter-count").textContent = count || "";
  $("#page-title").textContent = state.favorites ? "Favorites" : "All photos";
  for (const [id, active] of [["#nav-all", !state.favorites], ["#nav-favorites", state.favorites]]) {
    $(id).classList.toggle("active", active);
    if (active) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
  const query = params.toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
}

async function loadLibrary(reset = false) {
  if (!reset && (state.loading || !state.nextCursor)) return;
  if (reset) {
    state.generation++;
    state.request?.abort();
    state.previews?.stop();
    state.request = new AbortController();
    state.previews = new PreviewLoader();
    state.items = [];
    state.cards.clear();
    state.nextCursor = null;
    state.dirty = false;
    $("#timeline").replaceChildren();
    $("#empty").hidden = true;
    $("#collection-count").textContent = "Loading your library…";
    syncFilterUI();
  }
  const generation = state.generation;
  state.loading = true;
  $("#load-more").disabled = true;
  $("#library-error").hidden = true;
  $("#page-status").textContent = "Loading photos…";
  $("#timeline").setAttribute("aria-busy", "true");
  const params = filterParams();
  if (state.nextCursor) params.set("cursor", state.nextCursor);
  try {
    const page = await api(`/library/assets?${params}`, { signal: state.request.signal });
    if (generation !== state.generation) return;
    state.nextCursor = page.nextCursor;
    state.total = page.total;
    const ids = new Set(state.items.map((item) => item.assetId));
    for (const item of page.items) {
      if (!ids.has(item.assetId)) {
        state.items.push(item);
        appendCard(item);
      }
    }
    $("#collection-count").textContent = `${page.total.toLocaleString()} ${page.total === 1 ? "photo" : "photos"}${filterParams().toString() ? " in this view" : " in your collection"}`;
    $("#load-more").hidden = !page.nextCursor;
    $("#page-status").textContent = state.items.length ? `${state.items.length.toLocaleString()} of ${page.total.toLocaleString()} photos` : "";
    if (!state.items.length) showEmpty();
    updateNavigation();
  } catch (error) {
    if (generation !== state.generation || error.name === "AbortError") return;
    $("#library-error").textContent = `${error.message} Use Refresh to try again.`;
    $("#library-error").hidden = false;
    $("#collection-count").textContent = "Library could not be loaded";
    $("#page-status").textContent = "";
  } finally {
    if (generation === state.generation) {
      state.loading = false;
      $("#load-more").disabled = false;
      $("#timeline").setAttribute("aria-busy", "false");
    }
  }
}

function showEmpty() {
  const params = filterParams();
  params.delete("sort");
  const filtered = params.size > 0;
  $("#empty-title").textContent = filtered ? "No photos in this view" : "Your library starts here";
  $("#empty-message").textContent = filtered ? "Try another search or clear your filters to see more of your library." :
    "Upload your first photos using the photo-upload client. Your originals stay safely in your own storage.";
  $("#empty-link").hidden = filtered;
  $("#empty-clear").hidden = !filtered;
  $("#empty").hidden = false;
}

function appendCard(item) {
  const month = item.timelineTime.slice(0, 7);
  let section = document.getElementById(`month-${month}`);
  if (!section) {
    section = element("section", "month-section");
    section.id = `month-${month}`;
    const label = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" })
      .format(new Date(`${month}-01T12:00:00Z`));
    const heading = element("h2", "month-heading", label);
    heading.id = `heading-${month}`;
    section.setAttribute("aria-labelledby", heading.id);
    section.append(heading, element("div", "photo-grid"));
    $("#timeline").append(section);
  }
  const card = element("article", "photo-card");
  const open = element("button", "photo-open");
  open.type = "button";
  open.setAttribute("aria-label", `Open ${item.originalFilename}`);
  open.addEventListener("click", () => openViewer(item.assetId));
  const frame = element("div", "photo-frame");
  state.previews.add(frame, item.thumbnailUrl, item.preview.status, "");
  frame.append(element("span", "format-badge", item.mediaType));
  open.append(frame, element("span", "card-caption", item.originalFilename));
  const bottom = element("div", "card-bottom");
  const description = element("span", "card-description", item.cameraModel ||
    `${formatDate(item.timelineTime)}${item.dateSource === "import" ? " · imported" : ""}`);
  description.title = description.textContent;
  const actions = element("div", "card-actions");
  const rating = element("span", "card-rating");
  rating.setAttribute("role", "img");
  const favorite = element("button", "card-favorite");
  favorite.type = "button";
  favorite.addEventListener("click", () => saveState(item.assetId, { favorite: !item.favorite }));
  actions.append(rating, favorite);
  bottom.append(description, actions);
  card.append(open, bottom);
  section.querySelector(".photo-grid").append(card);
  state.cards.set(item.assetId, { card, open, favorite, rating });
  updateCard(item);
}

function updateCard(item) {
  const refs = state.cards.get(item.assetId);
  if (!refs) return;
  refs.rating.textContent = "★".repeat(item.rating);
  refs.rating.setAttribute("aria-label", item.rating ? `${item.rating} stars` : "Unrated");
  refs.favorite.textContent = item.favorite ? "♥" : "♡";
  refs.favorite.setAttribute("aria-label", `${item.favorite ? "Remove" : "Add"} ${item.originalFilename} ${item.favorite ? "from" : "to"} favorites`);
  refs.favorite.setAttribute("aria-pressed", String(item.favorite));
  refs.favorite.disabled = state.mutations.has(item.assetId);
}

function updateNavigation() {
  const index = state.items.findIndex((item) => item.assetId === state.selected);
  $("#previous-photo").disabled = index <= 0;
  $("#next-photo").disabled = index < 0 || (index === state.items.length - 1 && !state.nextCursor);
  $("#viewer-position").textContent = index < 0 ? "YOUR LIBRARY" : `${index + 1} / ${state.total}`;
}

async function navigatePhoto(direction) {
  const selected = state.selected;
  let index = state.items.findIndex((item) => item.assetId === selected);
  if (index < 0) return;
  if (index + direction >= state.items.length && state.nextCursor) {
    $("#next-photo").disabled = true;
    await loadLibrary();
  }
  if (state.selected !== selected) return;
  index = state.items.findIndex((item) => item.assetId === selected);
  const next = state.items[index + direction];
  if (next) openViewer(next.assetId, true);
  updateNavigation();
}

async function openViewer(assetId, replace = false, fromURL = false) {
  if (state.selected === assetId && $("#viewer").open) return;
  if (!$("#viewer").open) {
    state.returnFocus = document.activeElement;
    $("#viewer").showModal();
  }
  state.selected = assetId;
  state.detail = null;
  state.detailRequest?.abort();
  state.detailRequest = new AbortController();
  state.viewerPreviews?.stop();
  state.viewerPreviews = new PreviewLoader(1);
  $("#viewer-image").replaceChildren();
  $("#metadata").replaceChildren();
  $("#exif").replaceChildren();
  $("#exif-details").open = false;
  $("#viewer-title").textContent = state.items.find((item) => item.assetId === assetId)?.originalFilename || "Loading photo…";
  $("#viewer-format").textContent = "";
  $("#download-original").hidden = true;
  $("#save-status").textContent = "Loading details…";
  $("#save-status").classList.remove("error");
  updateViewerControls();
  updateNavigation();
  if (!fromURL) {
    const url = `${location.pathname}${location.search}#photo/${assetId}`;
    if (replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
  }
  try {
    const detail = await api(`/assets/${assetId}`, { signal: state.detailRequest.signal });
    if (state.selected !== assetId) return;
    state.detail = detail;
    const primary = detail.blobs.find((blob) => blob.blobId === detail.primaryBlobId);
    $("#viewer-title").textContent = primary.originalFilename;
    $("#viewer-format").textContent = primary.role.replace("ORIGINAL_", "");
    $("#save-status").textContent = "";
    $("#download-original").href = `/assets/${assetId}/original`;
    $("#download-original").hidden = false;
    state.viewerPreviews.add($("#viewer-image"), `/assets/${assetId}/preview`, detail.preview.status, primary.originalFilename, true);
    renderMetadata(detail, primary);
    updateViewerControls();
  } catch (error) {
    if (error.name !== "AbortError" && state.selected === assetId) {
      $("#viewer-title").textContent = "Photo could not be loaded";
      $("#save-status").textContent = error.message;
      $("#save-status").classList.add("error");
    }
  }
}

function renderMetadata(detail, primary) {
  const m = detail.metadata;
  const exposure = Number(m.ExposureTime);
  const fields = [
    ["Captured", formatDate(detail.captureTime, true)],
    ["Imported", formatDate(detail.importedAt, true)],
    ["Camera", [m.Make, m.Model].filter(Boolean).join(" · ")],
    ["Lens", m.LensModel || m.LensID],
    ["Dimensions", m.ImageWidth && m.ImageHeight ? `${m.ImageWidth} × ${m.ImageHeight}` : null],
    ["Exposure", exposure ? exposure < 1 ? `1/${Math.round(1 / exposure)} s` : `${exposure} s` : null],
    ["Aperture", m.FNumber ? `ƒ/${m.FNumber}` : null],
    ["ISO", m.ISO],
    ["Focal length", m.FocalLength ? `${m.FocalLength} mm` : null],
    ["Location", m.GPSLatitude != null && m.GPSLongitude != null ? `${m.GPSLatitude}, ${m.GPSLongitude}` : null],
    ["File size", sizeLabel(primary.sizeBytes)],
    ["Sidecars", detail.blobs.filter((blob) => blob.role === "SIDECAR").map((blob) => blob.originalFilename).join(", ")],
  ];
  for (const [key, value] of fields) {
    if (value !== null && value !== undefined && value !== "") {
      $("#metadata").append(element("dt", "", key), element("dd", "", String(value)));
    }
  }
  for (const [key, value] of Object.entries(m).sort(([a], [b]) => a.localeCompare(b))) {
    $("#exif").append(element("dt", "", key), element("dd", "", typeof value === "object" ? JSON.stringify(value) : String(value)));
  }
  $("#exif-details").hidden = !Object.keys(m).length;
}

function updateViewerControls() {
  const data = state.detail?.userState;
  const busy = !data || state.mutations.has(state.selected);
  const favorite = $("#viewer-favorite");
  favorite.disabled = busy;
  favorite.setAttribute("aria-pressed", String(data?.favorite || false));
  favorite.textContent = data?.favorite ? "♥ In favorites" : "♡ Add to favorites";
  for (const button of $("#viewer-rating").querySelectorAll("button")) {
    const rating = Number(button.dataset.rating);
    button.disabled = busy;
    button.classList.toggle("filled", rating > 0 && rating <= (data?.rating || 0));
    button.setAttribute("aria-pressed", String(rating === (data?.rating || 0)));
  }
}

async function saveState(assetId, changes) {
  if (state.mutations.has(assetId)) return;
  state.mutations.add(assetId);
  const item = state.items.find((entry) => entry.assetId === assetId);
  if (item) updateCard(item);
  if (state.selected === assetId) {
    updateViewerControls();
    $("#save-status").textContent = "Saving…";
    $("#save-status").classList.remove("error");
  }
  try {
    const data = await api(`/assets/${assetId}/user-state`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    });
    if (item) Object.assign(item, data);
    if (state.selected === assetId && state.detail) {
      state.detail.userState = data;
      $("#save-status").textContent = "Saved";
    }
    if (state.favorites || Number($("#rating-min").value) > 0) {
      state.dirty = true;
      if (!$("#viewer").open) await loadLibrary(true);
    }
  } catch (error) {
    if (state.selected === assetId) {
      $("#save-status").textContent = `Could not save: ${error.message}`;
      $("#save-status").classList.add("error");
    } else toast(`Could not save: ${error.message}`);
  } finally {
    state.mutations.delete(assetId);
    if (item) updateCard(item);
    if (state.selected === assetId) updateViewerControls();
  }
}

function closeViewer(fromURL = false) {
  state.detailRequest?.abort();
  state.viewerPreviews?.stop();
  state.selected = null;
  state.detail = null;
  $("#viewer").close();
  if (!fromURL) history.replaceState(null, "", location.pathname + location.search);
  if (state.returnFocus?.isConnected) state.returnFocus.focus();
  else $("#main").focus();
  if (state.dirty) loadLibrary(true);
}

function readLocation() {
  const params = new URLSearchParams(location.search);
  for (const [key, selector] of Object.entries({ q: "#search", date_from: "#date-from", date_to: "#date-to", media_type: "#media-type", rating_min: "#rating-min", sort: "#sort" })) {
    $(selector).value = params.get(key) || (key === "rating_min" ? "0" : key === "sort" ? "newest" : "");
  }
  state.favorites = params.get("favorite") === "true";
  if (["date_from", "date_to", "media_type", "rating_min"].some((key) => params.has(key))) {
    $("#filter-fields").hidden = false;
    $("#filter-toggle").setAttribute("aria-expanded", "true");
  }
}

function routePhoto() {
  const match = location.hash.match(/^#photo\/([0-9a-f-]{36})$/i);
  if (match) openViewer(match[1], false, true);
  else if ($("#viewer").open) closeViewer(true);
}

function clearFilters() {
  $("#filters").reset();
  state.favorites = false;
  clearTimeout(searchTimer);
  loadLibrary(true);
}

let searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadLibrary(true), 300);
});
$("#filters").addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(searchTimer); loadLibrary(true); });
for (const id of ["#sort", "#date-from", "#date-to", "#media-type", "#rating-min"]) {
  $(id).addEventListener("change", () => { clearTimeout(searchTimer); loadLibrary(true); });
}
$("#filter-toggle").addEventListener("click", () => {
  const open = $("#filter-fields").hidden;
  $("#filter-fields").hidden = !open;
  $("#filter-toggle").setAttribute("aria-expanded", String(open));
});
$("#clear-filters").addEventListener("click", clearFilters);
$("#empty-clear").addEventListener("click", clearFilters);
$("#nav-all").addEventListener("click", () => { state.favorites = false; loadLibrary(true); });
$("#nav-favorites").addEventListener("click", () => { state.favorites = true; loadLibrary(true); });
$("#refresh").addEventListener("click", () => loadLibrary(true));
$("#load-more").addEventListener("click", () => loadLibrary());
$("#close-viewer").addEventListener("click", () => closeViewer());
$("#viewer").addEventListener("cancel", (event) => { event.preventDefault(); closeViewer(); });
$("#previous-photo").addEventListener("click", () => navigatePhoto(-1));
$("#next-photo").addEventListener("click", () => navigatePhoto(1));
$("#viewer-favorite").addEventListener("click", () => {
  if (state.detail) saveState(state.selected, { favorite: !state.detail.userState.favorite });
});
$("#viewer-rating").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-rating]");
  if (button && !button.disabled) saveState(state.selected, { rating: Number(button.dataset.rating) });
});
$("#viewer").addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.altKey || event.metaKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    navigatePhoto(event.key === "ArrowLeft" ? -1 : 1);
  } else if (state.detail && /^[0-5]$/.test(event.key)) {
    event.preventDefault();
    saveState(state.selected, { rating: Number(event.key) });
  } else if (state.detail && event.key.toLowerCase() === "f") {
    event.preventDefault();
    saveState(state.selected, { favorite: !state.detail.userState.favorite });
  }
});
window.addEventListener("hashchange", routePhoto);
window.addEventListener("popstate", () => {
  const old = filterParams().toString();
  readLocation();
  if (old !== filterParams().toString()) loadLibrary(true);
  routePhoto();
});
readLocation();
loadLibrary(true).then(routePhoto);
