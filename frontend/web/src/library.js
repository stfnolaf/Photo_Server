"use strict";

const $ = (selector) => document.querySelector(selector);
const API_ROOT = "/api";
const apiUrl = (path) => `${API_ROOT}${path.startsWith("/") ? path : `/${path}`}`;
const state = {
  items: [], cards: new Map(), nextCursor: null, total: 0, loading: false,
  generation: 0, request: null, previews: null, favorites: false,
  selected: null, detail: null, detailRequest: null, viewerPreviews: null,
  mutations: new Set(), dirty: false, returnFocus: null,
  trash: false, albumId: null, albums: [], editingAlbum: null, albumMembers: [],
  pending: null, saving: false, journalKey: null,
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(url, options = {}) {
  const response = await fetch(apiUrl(url), options);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail :
        Array.isArray(body.detail) ? body.detail.map((error) => error.msg).join("; ") : message;
    } catch { /* A proxy may return a non-JSON error. */ }
    const error = new Error(message);
    error.status = response.status;
    throw error;
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
      const response = await fetch(apiUrl(task.url), { signal });
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
  if (state.trash) params.set("deleted", "true");
  if (state.albumId) params.set("album_id", state.albumId);
  return params;
}

function syncFilterUI() {
  const params = filterParams();
  const count = ["date_from", "date_to", "media_type", "rating_min"].filter((key) => params.has(key)).length;
  $("#filter-count").textContent = count || "";
  $("#page-title").textContent = state.albumId ? state.albums.find((album) => album.albumId === state.albumId)?.name || "Album" : state.trash ? "Trash" : state.favorites ? "Favorites" : "All photos";
  $("#edit-album").hidden = !state.albumId;
  for (const [id, active] of [["#nav-all", !state.favorites && !state.trash && !state.albumId], ["#nav-favorites", state.favorites], ["#nav-trash", state.trash]]) {
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
  refs.favorite.disabled = state.mutations.has(item.assetId) || !!item.deletedAt;
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
    $("#download-original").href = apiUrl(`/assets/${assetId}/original`);
    $("#download-original").hidden = false;
    state.viewerPreviews.add($("#viewer-image"), `/assets/${assetId}/preview`, detail.preview.status, primary.originalFilename, true);
    renderMetadata(detail, primary);
    fillMetadataEditor(detail.userState);
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
  const busy = !data || state.mutations.has(state.selected) || state.saving;
  const deleted = !!state.detail?.deletedAt;
  $("#trash-photo").textContent = deleted ? "Restore photo" : "Move to trash";
  $("#trash-photo").disabled = busy;
  for (const input of $("#metadata-editor").elements) input.disabled = busy || deleted;
  $("#photo-album").disabled = busy || deleted;
  updateMembershipButton();
  const favorite = $("#viewer-favorite");
  favorite.disabled = busy || deleted;
  favorite.setAttribute("aria-pressed", String(data?.favorite || false));
  favorite.textContent = data?.favorite ? "♥ In favorites" : "♡ Add to favorites";
  for (const button of $("#viewer-rating").querySelectorAll("button")) {
    const rating = Number(button.dataset.rating);
    button.disabled = busy || deleted;
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
    const data = await durableRequest(`/assets/${assetId}/user-state`, "PATCH", changes);
    if (item) Object.assign(item, data);
    if (state.selected === assetId && state.detail) {
      state.detail.userState = data;
      state.detail.revision = data.revision;
      $("#save-status").textContent = "Saved";
    }
    if (state.favorites || $("#search").value || Number($("#rating-min").value) > 0) {
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
  state.trash = params.get("deleted") === "true";
  state.albumId = params.get("album_id");
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
  state.trash = false;
  state.albumId = null;
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
$("#nav-all").addEventListener("click", () => selectCollection());
$("#nav-favorites").addEventListener("click", () => selectCollection("favorites"));
$("#nav-trash").addEventListener("click", () => selectCollection("trash"));
$("#refresh").addEventListener("click", async () => { await loadAlbums(); loadLibrary(true); });
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
  } else if (state.detail && !state.detail.deletedAt && /^[0-5]$/.test(event.key)) {
    event.preventDefault();
    saveState(state.selected, { rating: Number(event.key) });
  } else if (state.detail && !state.detail.deletedAt && event.key.toLowerCase() === "f") {
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
initializeLibrary();

function operationId() {
  // getRandomValues also works on trusted LAN HTTP origins.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function pendingNotice() {
  $("#pending-mutation").hidden = !state.pending;
  $("#retry-mutation").disabled = state.saving;
  $("#discard-mutation").disabled = state.saving;
  $("#pending-message").textContent = state.saving ? "Saving your change…" : "A change is awaiting retry. Retry it before making another change.";
}

async function durableRequest(path, method, changes = {}) {
  if (!state.journalKey) throw new Error("Library is still connecting. Refresh and try again.");
  if (state.pending) throw new Error("Retry or discard the pending request first.");
  const pending = { path, method, body: { ...changes, operationId: operationId() } };
  // Do not send a mutation unless its retry ID can survive a tab reload.
  localStorage.setItem(state.journalKey, JSON.stringify(pending));
  state.pending = pending;
  return sendPending();
}

async function sendPending() {
  if (state.saving || !state.pending) throw new Error("A save is already in progress.");
  state.saving = true;
  pendingNotice();
  const pending = state.pending;
  try {
    const result = await api(pending.path, {
      method: pending.method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pending.body),
    });
    localStorage.removeItem(state.journalKey);
    state.pending = null;
    return result;
  } finally {
    state.saving = false;
    pendingNotice();
  }
}

async function reloadAfterMutation() {
  await loadAlbums();
  await loadLibrary(true);
  if (state.selected) {
    const selected = state.selected;
    state.selected = null;
    await openViewer(selected, true, true);
  }
}

$("#retry-mutation").addEventListener("click", async () => {
  try { await sendPending(); await reloadAfterMutation(); toast("Change saved"); }
  catch (error) { toast(error.message); }
});
$("#discard-mutation").addEventListener("click", () => {
  if (!confirm("Discard this pending request? A change that already reached storage remains saved. Refresh to see its current state.")) return;
  localStorage.removeItem(state.journalKey);
  state.pending = null;
  pendingNotice();
  reloadAfterMutation().catch((error) => toast(error.message));
});

function selectCollection(kind = "all", albumId = null) {
  state.favorites = kind === "favorites";
  state.trash = kind === "trash";
  state.albumId = albumId;
  loadLibrary(true);
}

async function loadAlbums() {
  const [active, deleted] = await Promise.all([api("/albums"), api("/albums?deleted=true")]);
  state.albums = [...active, ...deleted];
  $("#album-list").replaceChildren();
  for (const album of state.albums) {
    const button = element("button", "nav-button", `${album.deletedAt ? "♲ " : ""}${album.name}`);
    button.addEventListener("click", () => album.deletedAt ? editAlbum(album) : selectCollection("album", album.albumId));
    $("#album-list").append(button);
  }
  const selected = $("#photo-album").value;
  $("#photo-album").replaceChildren(new Option("Choose an album", ""));
  for (const album of active) $("#photo-album").add(new Option(album.name, album.albumId));
  $("#photo-album").value = selected;
  updateMembershipButton();
}

function fillMetadataEditor(data) {
  $("#edit-caption").value = data.caption;
  $("#edit-keywords").value = data.keywords.join("\n");
  $("#edit-location").value = data.location?.name || "";
  $("#edit-latitude").value = data.location?.latitude ?? "";
  $("#edit-longitude").value = data.location?.longitude ?? "";
}

$("#metadata-editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.detail) return;
  const latitude = $("#edit-latitude").value;
  const longitude = $("#edit-longitude").value;
  if (!!latitude !== !!longitude) { toast("Enter both latitude and longitude."); return; }
  const name = $("#edit-location").value.trim();
  await saveState(state.selected, {
    caption: $("#edit-caption").value,
    keywords: [...new Set($("#edit-keywords").value.split("\n").map((word) => word.trim()).filter(Boolean))],
    location: name || latitude ? { name, latitude: latitude ? Number(latitude) : null, longitude: longitude ? Number(longitude) : null } : null,
  });
});

function updateMembershipButton() {
  const album = state.albums.find((entry) => entry.albumId === $("#photo-album").value);
  $("#toggle-membership").disabled = !album || !state.detail || !!state.detail.deletedAt || state.saving;
  $("#toggle-membership").textContent = album?.assetIds.includes(state.selected) ? "Remove from album" : "Add to album";
}
$("#photo-album").addEventListener("change", updateMembershipButton);
$("#toggle-membership").addEventListener("click", async () => {
  const album = state.albums.find((entry) => entry.albumId === $("#photo-album").value);
  if (!album || !state.selected) return;
  const assetIds = album.assetIds.includes(state.selected) ? album.assetIds.filter((id) => id !== state.selected) : [...album.assetIds, state.selected];
  try {
    await durableRequest(`/albums/${album.albumId}`, "PATCH", { assetIds, expectedRevision: album.revision });
    await loadAlbums();
    state.dirty = true;
    toast("Album saved");
  } catch (error) { toast(error.message); }
});
$("#trash-photo").addEventListener("click", async () => {
  if (!state.detail) return;
  const restore = !!state.detail.deletedAt;
  const id = state.selected;
  try {
    await durableRequest(`/assets/${id}${restore ? "/restore" : ""}`, restore ? "POST" : "DELETE");
    state.dirty = true;
    closeViewer();
    toast(restore ? "Photo restored" : "Photo moved to trash");
  } catch (error) { toast(error.message); }
});

function editAlbum(album = null) {
  state.editingAlbum = album;
  state.albumMembers = [...(album?.assetIds || [])];
  $("#album-editor-title").textContent = album ? album.deletedAt ? "Trashed album" : "Edit album" : "New album";
  $("#album-name").value = album?.name || "";
  $("#album-description").value = album?.description || "";
  $("#album-name").disabled = $("#album-description").disabled = !!album?.deletedAt;
  $("#save-album").hidden = !!album?.deletedAt;
  $("#trash-album").hidden = !album;
  $("#trash-album").textContent = album?.deletedAt ? "Restore album" : "Move album to trash";
  $("#album-status").textContent = "";
  renderAlbumMembers();
  $("#album-editor").showModal();
}
function renderAlbumMembers() {
  $("#album-members").replaceChildren();
  state.albumMembers.forEach((id, index) => {
    const row = element("li");
    const name = state.items.find((item) => item.assetId === id)?.originalFilename || id;
    row.append(element("span", "member-name", name));
    if (!state.editingAlbum?.deletedAt) {
      for (const [label, change, disabled] of [
        ["Move up", -1, index === 0], ["Move down", 1, index === state.albumMembers.length - 1], ["Remove", 0, false],
      ]) {
        const button = element("button", "button secondary", label);
        button.type = "button";
        button.disabled = disabled;
        button.setAttribute("aria-label", `${label}: ${name}`);
        button.addEventListener("click", () => {
          if (change) [state.albumMembers[index], state.albumMembers[index + change]] = [state.albumMembers[index + change], state.albumMembers[index]];
          else state.albumMembers.splice(index, 1);
          renderAlbumMembers();
        });
        row.append(button);
      }
    }
    $("#album-members").append(row);
  });
}
$("#new-album").addEventListener("click", () => editAlbum());
$("#edit-album").addEventListener("click", () => editAlbum(state.albums.find((album) => album.albumId === state.albumId)));
$("#close-album").addEventListener("click", () => $("#album-editor").close());
$("#album-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const album = state.editingAlbum;
  try {
    const result = await durableRequest(album ? `/albums/${album.albumId}` : "/albums", album ? "PATCH" : "POST", {
      name: $("#album-name").value.trim(), description: $("#album-description").value,
      assetIds: state.albumMembers, ...(album ? { expectedRevision: album.revision } : {}),
    });
    $("#album-editor").close();
    await loadAlbums();
    selectCollection("album", result.albumId);
  } catch (error) { $("#album-status").textContent = error.message; }
});
$("#trash-album").addEventListener("click", async () => {
  const album = state.editingAlbum;
  const restore = !!album.deletedAt;
  try {
    await durableRequest(`/albums/${album.albumId}${restore ? "/restore" : ""}`, restore ? "POST" : "DELETE");
    $("#album-editor").close();
    await loadAlbums();
    selectCollection();
  } catch (error) { $("#album-status").textContent = error.message; }
});

async function initializeLibrary() {
  try {
    const health = await api("/health");
    state.journalKey = `photo-library-pending:${health.libraryId}`;
    state.pending = JSON.parse(localStorage.getItem(state.journalKey) || "null");
    pendingNotice();
    await loadAlbums();
    readLocation();
    await loadLibrary(true);
    routePhoto();
  } catch (error) {
    $("#library-error").textContent = error.message;
    $("#library-error").hidden = false;
    $("#collection-count").textContent = "Library could not be loaded. Reload to reconnect.";
  }
}
