/**
 * Hand-written facade over the OpenAPI-generated SDK
 * (`./generated`, per `docs/openapi-codegen-plan.md` phase 5a).
 *
 * Feature code keeps calling the `api` methods with exactly the signatures
 * they have always had; this module is the only place that knows the
 * generated operation names exist. It also owns the two behaviors the
 * generated SDK deliberately leaves to the caller:
 *
 * - API prefixing: the SDK client below is created with `baseUrl: API_ROOT`.
 *   The spec defines no servers, so every generated URL stays relative and
 *   flows through the Vite dev proxy / nginx `/api` proxy (or a full-URL
 *   `VITE_API_ROOT` for cross-origin deployments).
 * - Error normalization: operations run with the SDK's default
 *   `throwOnError: false`, and `call()` turns a failed result into the app's
 *   `ApiError` with the same message extraction the old hand-rolled
 *   `request()` had (FastAPI `{detail: string}` bodies, `{detail: [...]}`
 *   422 arrays, non-JSON proxy pages, network failures).
 *
 * The one endpoint that stays outside the generated SDK is `uploadFile`:
 * multipart upload with XHR progress reporting. Its request/response shapes
 * come from the generated types; only the transport is hand-rolled.
 */
import { createClient } from "./generated/client";
import {
  abandonUploadBatch,
  browseAssets,
  createUploadBatch,
  getAssetDetail,
  getBurst,
  getHealth,
  getPerson,
  getUploadBatch,
  getUploadQueue,
  listAlbums,
  listPeople,
  listUploadBatches,
  retryAnalysis,
  retryPreview,
  retryUploadBatch,
  sealUploadBatch,
} from "./generated/sdk.gen";
import type {
  BatchAbandonedOut,
  BrowseAssetsData,
  PreviewStatusOut,
  QueueResultOut,
  UploadBatchOut,
} from "./generated/types.gen";
import type {
  Album,
  BrowsePage,
  BurstDetail,
  Health,
  LibraryFilters,
  PeoplePage,
  PersonDetail,
  PhotoDetail,
  PendingMutation,
  UploadBatch,
  UploadQueueStatus,
} from "./types";

const API_ROOT: string = import.meta.env.VITE_API_ROOT ?? "/api";

/**
 * Shared client instance for every generated SDK operation. Passing
 * `API_ROOT` as the SDK's `baseUrl` is how the facade applies the API
 * prefix: request URLs resolve against it here, while the generated
 * `client.gen.ts` default stays prefix-agnostic.
 */
const client = createClient({ baseUrl: API_ROOT });

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const apiUrl = (path: string) =>
  `${API_ROOT}${path.startsWith("/") ? path : `/${path}`}`;

/**
 * Convert a failed SDK result into the app's `ApiError`, with the message
 * extraction the previous hand-rolled `request()` used: `{detail: string}`
 * bodies become that string; `{detail: [...]}` (422) bodies become the
 * joined `msg` values; anything else (raw proxy pages, non-JSON bodies)
 * falls back to the status-line message. A missing `Response` means the
 * network itself failed.
 */
function normalizeApiError(error: unknown, response: Response): ApiError {
  let message = `Request failed (${response.status}).`;
  if (error !== null && typeof error === "object") {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") message = detail;
    if (Array.isArray(detail)) {
      message = detail
        .map((item) =>
          item !== null && typeof item === "object"
            ? ((item as { msg?: string }).msg ?? "Invalid value")
            : "Invalid value",
        )
        .join("; ");
    }
  }
  return new ApiError(message, response.status);
}

/** Shape the generated SDK reports for a finished request (any operation). */
type GeneratedResult = { data?: unknown; error?: unknown; response?: Response };

/**
 * Run a generated operation with the SDK's default `throwOnError: false`
 * and convert the result into the app's promise conventions: reject with
 * `ApiError` on failure, resolve with the response data on success.
 */
async function call<T>(run: () => Promise<GeneratedResult>): Promise<T> {
  const { data, error, response } = await run();
  if (response === undefined) {
    // The network itself failed before a response arrived. The previous
    // request() let that raw fetch error (TypeError/AbortError) propagate
    // untouched; TanStack Query relies on the AbortError identity to treat
    // aborted queries as cancellations rather than failures, so rethrow it.
    throw error ?? new ApiError("Network error", 0);
  }
  if (error !== undefined) throw normalizeApiError(error, response);
  return data as T;
}

function uploadError(status: number, responseText: string): ApiError {
  let message = status ? `Upload failed (${status}).` : "The upload connection was interrupted.";
  try {
    const body = JSON.parse(responseText) as { detail?: string | Array<{ msg?: string }> };
    if (typeof body.detail === "string") message = body.detail;
    if (Array.isArray(body.detail)) {
      message = body.detail.map((item) => item.msg ?? "Invalid value").join("; ");
    }
  } catch {
    // Keep the useful transport-level fallback for non-JSON proxy responses.
  }
  return new ApiError(message, status);
}

export const api = {
  health: (signal?: AbortSignal) =>
    call<Health>(() => getHealth({ client, signal })),

  albums: (deleted = false, signal?: AbortSignal) =>
    call<Album[]>(() =>
      listAlbums({ client, signal, ...(deleted ? { query: { deleted: true } } : {}) }),
    ),

  photo: (assetId: string, signal?: AbortSignal) =>
    call<PhotoDetail>(() => getAssetDetail({ client, signal, path: { asset_id: assetId } })),

  burst: (assetId: string, signal?: AbortSignal) =>
    call<BurstDetail>(() => getBurst({ client, signal, path: { asset_id: assetId } })),

  people: (q = "", signal?: AbortSignal) =>
    call<PeoplePage>(() =>
      listPeople({ client, signal, query: { limit: 1000, ...(q ? { q } : {}) } }),
    ),

  person: (personId: string, signal?: AbortSignal) =>
    call<PersonDetail>(() => getPerson({ client, signal, path: { person_id: personId } })),

  browse: (filters: LibraryFilters, cursor?: string | null, signal?: AbortSignal) => {
    // Parameter set and order mirror the previous request exactly; the SDK's
    // query serializer omits undefined keys, so unset filters stay off the wire.
    const query: BrowseAssetsData["query"] = {};
    if (filters.q) query.q = filters.q;
    if (filters.dateFrom) query.date_from = filters.dateFrom;
    if (filters.dateTo) query.date_to = filters.dateTo;
    if (filters.mediaType) query.media_type = filters.mediaType;
    if (filters.ratingMin) query.rating_min = filters.ratingMin;
    if (filters.sort !== "newest") query.sort = filters.sort;
    if (filters.view === "favorites") query.favorite = true;
    if (filters.view === "hidden") query.deleted = true;
    if (filters.albumId) query.album_id = filters.albumId;
    if (cursor) query.cursor = cursor;
    return call<BrowsePage>(() => browseAssets({ client, signal, query }));
  },

  createUploadBatch: (
    batchId: string,
    files: Array<{ path: string; sizeBytes: number; mimeType: string; sha256?: string }>,
    album?: { albumId?: string; albumName?: string },
  ) =>
    call<UploadBatch>(() =>
      createUploadBatch({ client, body: { batchId, files, ...album } }),
    ),

  uploadFile: (
    path: string,
    file: File,
    onProgress: (progress: number) => void,
  ) => new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", apiUrl(path));
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.addEventListener("progress", (event) => {
      onProgress(Math.min(1, event.loaded / file.size));
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else {
        reject(uploadError(xhr.status, xhr.responseText));
      }
    });
    xhr.addEventListener("error", () => reject(uploadError(xhr.status, xhr.responseText)));
    xhr.addEventListener("abort", () => reject(new ApiError("Upload was cancelled.", 0)));
    xhr.send(file);
  }),

  uploadBatch: (batchId: string, signal?: AbortSignal) =>
    call<UploadBatch>(() => getUploadBatch({ client, signal, path: { batch_id: batchId } })),

  activeUploadBatches: (signal?: AbortSignal) =>
    call<UploadBatch[]>(() => listUploadBatches({ client, signal })),

  uploadQueue: (signal?: AbortSignal) =>
    call<UploadQueueStatus>(() => getUploadQueue({ client, signal })),

  sealUploadBatch: (batchId: string) =>
    call<UploadBatchOut>(() => sealUploadBatch({ client, path: { batch_id: batchId } })),

  retryUploadBatch: (batchId: string) =>
    call<UploadBatchOut>(() => retryUploadBatch({ client, path: { batch_id: batchId } })),

  abandonUploadBatch: (batchId: string) =>
    call<BatchAbandonedOut>(() => abandonUploadBatch({ client, path: { batch_id: batchId } })),

  // The durable-mutation journal protocol sends a dynamic (path, method)
  // pair, so this one path goes through the generated client's raw
  // request() rather than a named operation; the body always carries the
  // journal's operationId (and optionally expectedRevision). This is the
  // only raw-client call site in the facade: `pending.method` passes
  // through unconverted because the journal's MutationMethod values are
  // already the uppercase HTTP tokens the SDK's method enum expects in
  // this hey-api version (the wire bytes are unchanged from the old
  // fetch-based client). If a future SDK pin changes that enum, tsc will
  // surface it here.
  sendMutation: async <T>(pending: PendingMutation): Promise<T> => {
    const { data, error, response } = await client.request({
      url: pending.path,
      method: pending.method,
      body: pending.body,
    });
    if (response === undefined) throw error ?? new ApiError("Network error", 0);
    if (error !== undefined) throw normalizeApiError(error, response);
    return data as T;
  },

  retryPreview: (assetId: string) =>
    call<PreviewStatusOut>(() => retryPreview({ client, path: { asset_id: assetId } })),

  reanalyze: (assetId: string) =>
    call<QueueResultOut>(() => retryAnalysis({ client, path: { asset_id: assetId } })),
};
