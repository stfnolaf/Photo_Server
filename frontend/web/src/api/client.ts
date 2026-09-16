import type {
  Album,
  BrowsePage,
  Health,
  LibraryFilters,
  PeoplePage,
  PersonDetail,
  PhotoDetail,
  PendingMutation,
  PreviewStatus,
  UploadBatch,
  UploadQueueStatus,
} from "./types";

const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api";

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

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(apiUrl(path), options);
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: string | Array<{ msg?: string }> };
      if (typeof body.detail === "string") message = body.detail;
      if (Array.isArray(body.detail)) {
        message = body.detail.map((item) => item.msg ?? "Invalid value").join("; ");
      }
    } catch {
      // A reverse proxy can return a non-JSON error page.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
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
  health: (signal?: AbortSignal) => request<Health>("/health", { signal }),

  albums: (deleted = false, signal?: AbortSignal) =>
    request<Album[]>(`/albums${deleted ? "?deleted=true" : ""}`, { signal }),

  photo: (assetId: string, signal?: AbortSignal) =>
    request<PhotoDetail>(`/assets/${assetId}`, { signal }),

  people: (q = "", signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: "1000" });
    if (q) params.set("q", q);
    return request<PeoplePage>(`/people?${params}`, { signal });
  },

  person: (personId: string, signal?: AbortSignal) =>
    request<PersonDetail>(`/people/${personId}`, { signal }),

  browse: (filters: LibraryFilters, cursor?: string | null, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (filters.q) params.set("q", filters.q);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.mediaType) params.set("media_type", filters.mediaType);
    if (filters.ratingMin) params.set("rating_min", String(filters.ratingMin));
    if (filters.sort !== "newest") params.set("sort", filters.sort);
    if (filters.view === "favorites") params.set("favorite", "true");
    if (filters.view === "trash") params.set("deleted", "true");
    if (filters.albumId) params.set("album_id", filters.albumId);
    if (cursor) params.set("cursor", cursor);
    return request<BrowsePage>(`/library/assets?${params}`, { signal });
  },

  createUploadBatch: (
    batchId: string,
    files: Array<{ path: string; sizeBytes: number; mimeType: string }>,
  ) =>
    request<UploadBatch>("/upload-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batchId, files }),
    }),

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
    request<UploadBatch>(`/upload-batches/${batchId}`, { signal }),

  activeUploadBatches: (signal?: AbortSignal) =>
    request<UploadBatch[]>("/upload-batches", { signal }),

  uploadQueue: (signal?: AbortSignal) =>
    request<UploadQueueStatus>("/upload-queue", { signal }),

  sealUploadBatch: (batchId: string) =>
    request<UploadBatch>(`/upload-batches/${batchId}/seal`, { method: "POST" }),

  retryUploadBatch: (batchId: string) =>
    request<UploadBatch>(`/upload-batches/${batchId}/retry`, { method: "POST" }),

  abandonUploadBatch: (batchId: string) =>
    request<{ batchId: string; status: "deleted" }>(`/upload-batches/${batchId}`, {
      method: "DELETE",
    }),

  sendMutation: <T>(pending: PendingMutation) =>
    request<T>(pending.path, {
      method: pending.method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(pending.body),
    }),

  retryPreview: (assetId: string) =>
    request<{ status: PreviewStatus }>(`/assets/${assetId}/preview/retry`, {
      method: "POST",
    }),

  reanalyze: (assetId: string) =>
    request<{ assets: number; jobsQueued: number; jobsAlreadyQueued: number; jobsAlreadyRunning: number }>(`/assets/${assetId}/analysis/retry`, {
      method: "POST",
    }),
};
