export type MediaType = "RAW" | "JPEG" | "HEIF";
export type PreviewStatus = "missing" | "pending" | "ready" | "failed" | "unavailable";

export interface Health {
  status: string;
  libraryId: string;
  assets: number;
  blobs: number;
  [key: string]: unknown;
}

export interface LocationValue {
  name: string;
  latitude: number | null;
  longitude: number | null;
}

export interface UserState {
  rating: number;
  favorite: boolean;
  caption: string;
  keywords: string[];
  location: LocationValue | null;
}

export interface PhotoSummary {
  assetId: string;
  originalFilename: string;
  mediaType: MediaType;
  timelineTime: string;
  dateSource: "capture" | "import";
  captureTime: string | null;
  importedAt: string;
  width: number | null;
  height: number | null;
  cameraMake: string | null;
  cameraModel: string | null;
  lens: string | null;
  technical: Record<string, unknown>;
  sizeBytes: number;
  rating: number;
  favorite: boolean;
  caption: string;
  deletedAt: string | null;
  revision: number;
  preview: { status: PreviewStatus; error: string | null };
  thumbnailUrl: string;
  previewUrl: string;
}

export interface BrowsePage {
  items: PhotoSummary[];
  total: number;
  nextCursor: string | null;
}

export interface BlobInfo {
  blobId: string;
  role: "ORIGINAL_RAW" | "ORIGINAL_JPEG" | "ORIGINAL_HEIF" | "SIDECAR";
  originalFilename: string;
  objectKey: string;
  sha256: string;
  sizeBytes: number;
  mimeType: string;
}

export interface PhotoDetail {
  schemaVersion: number;
  libraryId: string;
  assetId: string;
  revision: number;
  primaryBlobId: string;
  blobs: BlobInfo[];
  importedAt: string;
  captureTime: string | null;
  metadata: Record<string, unknown>;
  technical: Record<string, unknown>;
  userState: UserState;
  deletedAt: string | null;
  preview: { status: PreviewStatus; error?: string | null };
}

export interface Album {
  schemaVersion: number;
  libraryId: string;
  albumId: string;
  revision: number;
  previousRevision: number | null;
  operationId: string;
  name: string;
  description: string;
  assetIds: string[];
  deletedAt: string | null;
}

export interface MutationResult extends UserState {
  assetId: string;
  operationId: string;
  revision: number;
  deletedAt: string | null;
}

export interface LibraryFilters {
  q: string;
  dateFrom: string;
  dateTo: string;
  mediaType: "" | MediaType;
  ratingMin: number;
  sort: "newest" | "oldest";
  view: "all" | "favorites" | "trash";
  albumId: string | null;
}

export type MutationMethod = "POST" | "PATCH" | "DELETE";

export interface PendingMutation {
  path: string;
  method: MutationMethod;
  body: Record<string, unknown> & { operationId: string };
}
