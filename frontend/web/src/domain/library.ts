import type { Album, LibraryFilters, PhotoDetail, PhotoSummary } from "../api/types";

export const defaultFilters: LibraryFilters = {
  q: "",
  dateFrom: "",
  dateTo: "",
  mediaType: "",
  ratingMin: 0,
  sort: "newest",
  view: "all",
  albumId: null,
};

export function readFilters(params: URLSearchParams): LibraryFilters {
  const mediaType = params.get("media_type");
  const sort = params.get("sort");
  const view = params.get("view");
  return {
    q: params.get("q") ?? "",
    dateFrom: params.get("date_from") ?? "",
    dateTo: params.get("date_to") ?? "",
    mediaType: mediaType === "RAW" || mediaType === "JPEG" || mediaType === "HEIF" ? mediaType : "",
    ratingMin: Math.min(5, Math.max(0, Number(params.get("rating_min")) || 0)),
    sort: sort === "oldest" ? "oldest" : "newest",
    view:
      view === "favorites" || params.get("favorite") === "true"
        ? "favorites"
        : view === "trash" || params.get("deleted") === "true"
          ? "trash"
          : "all",
    albumId: params.get("album") ?? params.get("album_id"),
  };
}

export function writeFilters(filters: LibraryFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  if (filters.mediaType) params.set("media_type", filters.mediaType);
  if (filters.ratingMin) params.set("rating_min", String(filters.ratingMin));
  if (filters.sort !== "newest") params.set("sort", filters.sort);
  if (filters.view !== "all") params.set("view", filters.view);
  if (filters.albumId) params.set("album", filters.albumId);
  return params;
}

export function collectionTitle(filters: LibraryFilters, albums: Album[]): string {
  if (filters.albumId) return albums.find((album) => album.albumId === filters.albumId)?.name ?? "Album";
  if (filters.view === "favorites") return "Favorites";
  if (filters.view === "trash") return "Recently deleted";
  return "All photographs";
}

export function formatDate(value: string | null, withTime = false): string {
  if (!value) return "Not recorded";
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?/);
  if (!match) return value;
  const [, year, month, day, hour = "00", minute = "00", second = "00"] = match;
  const date = new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}Z`);
  if (Number.isNaN(date.getTime())) return value;
  const options: Intl.DateTimeFormatOptions = {
    year: "numeric",
    month: withTime ? "short" : "long",
    day: "numeric",
    timeZone: "UTC",
  };
  if (withTime) Object.assign(options, { hour: "2-digit", minute: "2-digit" });
  const rendered = new Intl.DateTimeFormat(undefined, options).format(date);
  if (!withTime) return rendered;
  const offset = value.match(/(Z|[+-]\d{2}:\d{2})$/)?.[0];
  return `${rendered} (${offset === "Z" ? "UTC" : offset ? `UTC${offset}` : "timezone unknown"})`;
}

export function monthKey(photo: PhotoSummary): string {
  return photo.timelineTime.slice(0, 7);
}

export function monthLabel(key: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${key}-01T12:00:00Z`),
  );
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function primaryBlob(detail: PhotoDetail) {
  return detail.blobs.find((blob) => blob.blobId === detail.primaryBlobId) ?? detail.blobs[0];
}

export function displayValue(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function technicalRows(detail: PhotoDetail): Array<[string, string | null]> {
  const m = detail.metadata;
  const t = detail.technical;
  const exposure = Number(t.exposureTime ?? m.ExposureTime);
  const shutter = exposure
    ? exposure < 1
      ? `1/${Math.round(1 / exposure)} s`
      : `${exposure} s`
    : displayValue(t.shutterSpeed ?? m.ShutterSpeed);
  return [
    ["Camera", [m.Make, m.Model].filter(Boolean).join(" · ") || null],
    ["Lens", displayValue(t.lens ?? m.lensDisplay ?? m.LensModel ?? m.LensID)],
    ["Dimensions", m.ImageWidth && m.ImageHeight ? `${m.ImageWidth} × ${m.ImageHeight}` : null],
    ["Shutter", shutter],
    ["Aperture", t.aperture ? `ƒ/${t.aperture}` : null],
    ["ISO", displayValue(t.iso)],
    ["Focal length", t.focalLength ? `${t.focalLength} mm` : null],
    ["Exposure comp.", t.exposureCompensation != null ? `${t.exposureCompensation} EV` : null],
    ["White balance", displayValue(t.whiteBalance)],
  ];
}

export function createOperationId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
