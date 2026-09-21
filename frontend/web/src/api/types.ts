/**
 * Client-facing type shims over the OpenAPI-generated contract
 * (`./generated/types.gen.ts`, generated from `openapi/openapi.json` per
 * `docs/openapi-codegen-plan.md` phase 5a).
 *
 * Every wire type below derives from the generated module — no hand-written
 * contract definitions remain in this file. The only hand-written types are
 * the client-internal ones (`LibraryFilters`, `MutationMethod`,
 * `PendingMutation`), which name no wire field of their own.
 *
 * `PhotoSummary`, `BrowsePage`, and `BurstDetail` are pure aliases now: the
 * backend models the summary URLs as non-nullable (`PhotoSummaryOut` in
 * `backend/src/photo_server/api_schemas.py` — `asset_summary()` always emits
 * both as f-strings), so the spec says `string` and the generated types
 * carry the invariant. A producer regression failing validation is a 500
 * (a caught bug, per the plan's decision 3), not a null the client must
 * defend against.
 *
 * One shim is deliberately not a plain alias:
 *
 * - `PhotoDetail` aliases the schema-v2 member of the `AssetDetailOut`
 *   union (`AssetDetailV2Out`). The photo feature page is a v2-shaped
 *   consumer: it reads `deletedAt` (absent on v1 documents, where the runtime
 *   value is `undefined` — identical to `null` under its only uses,
 *   `Boolean(...)` / `!detail.deletedAt`) and it writes v2 documents back
 *   after every mutation (any asset mutation upgrades the document to v2).
 *   Typing the query-cache slot as the v2 member also keeps the TanStack
 *   `setQueryData` updater assignable, because the v1 member pins
 *   `revision: 1` as a literal while post-mutation revisions are numbers.
 *   Every field the client reads exists on both variants.
 */
import type * as Generated from "./generated/types.gen";

export type MediaType = Generated.PhotoSummaryOut["mediaType"];
export type PreviewStatus = Generated.PreviewStatusOut["status"];

export type Health = Generated.HealthOut;
export type LocationValue = Generated.LocationOut;
export type UserState = Generated.UserStateOut;

export type PhotoSummary = Generated.PhotoSummaryOut;
export type BurstDetail = Generated.BurstDetailOut;
export type BrowsePage = Generated.BrowsePageOut;

export type BlobInfo = Generated.BlobOut;

/** See the module header: the schema-v2 member of the `AssetDetailOut` union. */
export type PhotoDetail = Generated.AssetDetailV2Out;

export type AnalysisObject = Generated.AnalysisObjectOut;
export type AnalysisResult = Generated.AnalysisResultOut;
export type PhotoAnalysis = Generated.AnalysisStatusOut;
export type FaceReference = Generated.FaceRefOut;
export type PersonSummary = Generated.PersonSummaryOut;
export type PeoplePage = Generated.PeoplePageOut;
export type PersonDetail = Generated.PersonDetailOut;
/**
 * The three face-mutation endpoints return different result shapes; this
 * intersection covers every field the client reads from any of them.
 */
export type FaceMutationResult = Generated.PersonRenameOut &
  Generated.PersonMergeOut &
  Generated.FaceMoveOut;

export type Album = Generated.AlbumOut;
export type MutationResult = Generated.MutationResultOut;

// --- Client-internal types (no wire shape of their own) ---------------------

export interface LibraryFilters {
  q: string;
  dateFrom: string;
  dateTo: string;
  mediaType: "" | MediaType;
  ratingMin: number;
  sort: "newest" | "oldest";
  view: "all" | "favorites" | "hidden";
  albumId: string | null;
}

export type MutationMethod = "POST" | "PATCH" | "DELETE";

export interface PendingMutation {
  path: string;
  method: MutationMethod;
  body: Record<string, unknown> & { operationId: string };
}

// --- Upload queue ------------------------------------------------------------

export type UploadBatchStatus = Generated.UploadBatchOut["status"];
export type UploadFileStatus = Generated.UploadFileOut["status"];
export type UploadBatchFile = Generated.UploadFileOut;
export type UploadBatchJob = Generated.UploadJobOut;
export type UploadBatch = Generated.UploadBatchOut;
export type UploadQueueStatus = Generated.UploadQueueStatusOut;
