import type {
  Album,
  BrowsePage,
  Health,
  LibraryFilters,
  PhotoDetail,
  PendingMutation,
  PreviewStatus,
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

export const api = {
  health: (signal?: AbortSignal) => request<Health>("/health", { signal }),

  albums: (deleted = false, signal?: AbortSignal) =>
    request<Album[]>(`/albums${deleted ? "?deleted=true" : ""}`, { signal }),

  photo: (assetId: string, signal?: AbortSignal) =>
    request<PhotoDetail>(`/assets/${assetId}`, { signal }),

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
};
