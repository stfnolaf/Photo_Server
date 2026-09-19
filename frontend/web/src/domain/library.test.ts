import { describe, expect, it } from "vitest";
import type { PhotoSummary } from "../api/types";
import { collapseBursts, createOperationId, defaultFilters, isBurst, readFilters, writeFilters } from "./library";

let randomSeed = 0;
Object.defineProperty(globalThis, "crypto", {
  configurable: true,
  value: {
    getRandomValues: (bytes: Uint8Array) => {
      randomSeed += 1;
      bytes.forEach((_, index) => { bytes[index] = index + randomSeed; });
      return bytes;
    },
  },
});

describe("library filter URLs", () => {
  it("round-trips every supported filter", () => {
    const filters = {
      ...defaultFilters,
      q: "Sony 100%",
      dateFrom: "2024-01-02",
      dateTo: "2024-03-04",
      mediaType: "RAW" as const,
      ratingMin: 3,
      sort: "oldest" as const,
      view: "favorites" as const,
      albumId: "0563bd46-40ff-4673-88a1-3ca4e0ec1c82",
    };
    expect(readFilters(writeFilters(filters))).toEqual(filters);
  });

  it("understands URLs from the previous web client", () => {
    expect(readFilters(new URLSearchParams("favorite=true&album_id=old-album"))).toMatchObject({
      view: "favorites",
      albumId: "old-album",
    });
    expect(readFilters(new URLSearchParams("deleted=true"))).toMatchObject({ view: "hidden" });
  });

  it("normalizes invalid values", () => {
    expect(readFilters(new URLSearchParams("rating_min=99&media_type=GIF&sort=random"))).toEqual({
      ...defaultFilters,
      ratingMin: 5,
    });
  });
});

describe("durable operation IDs", () => {
  it("creates distinct UUIDs", () => {
    const first = createOperationId();
    const second = createOperationId();
    expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(second).not.toBe(first);
  });
});

function photo(overrides: Partial<PhotoSummary> & { assetId: string }): PhotoSummary {
  return {
    originalFilename: `${overrides.assetId}.jpg`,
    mediaType: "JPEG",
    timelineTime: "2024-06-15T10:00:00Z",
    dateSource: "capture",
    captureTime: "2024-06-15T10:00:00Z",
    importedAt: "2024-06-15T10:00:00Z",
    width: 4000,
    height: 3000,
    cameraMake: null,
    cameraModel: null,
    lens: null,
    technical: {},
    sizeBytes: 1000,
    rating: 0,
    favorite: false,
    caption: "",
    deletedAt: null,
    revision: 1,
    burstId: null,
    burstSize: null,
    burstRepresentativeAssetId: null,
    preview: { status: "ready", error: null },
    thumbnailUrl: `/assets/${overrides.assetId}/thumbnail`,
    previewUrl: `/assets/${overrides.assetId}/preview`,
    ...overrides,
  };
}

describe("isBurst", () => {
  it("is true only for clustered frames with more than one member", () => {
    expect(isBurst(photo({ assetId: "a", burstId: "c1", burstSize: 3, burstRepresentativeAssetId: "b" }))).toBe(true);
    expect(isBurst(photo({ assetId: "a" }))).toBe(false);
    expect(isBurst(photo({ assetId: "a", burstId: "c1", burstSize: 1, burstRepresentativeAssetId: "a" }))).toBe(false);
  });
});

describe("collapseBursts", () => {
  it("keeps non-burst frames and collapses each burst to one item", () => {
    const frames = [
      photo({ assetId: "a", burstId: "c1", burstSize: 3, burstRepresentativeAssetId: "b" }),
      photo({ assetId: "x" }),
      photo({ assetId: "b", burstId: "c1", burstSize: 3, burstRepresentativeAssetId: "b" }),
      photo({ assetId: "c", burstId: "c1", burstSize: 3, burstRepresentativeAssetId: "b" }),
    ];
    const collapsed = collapseBursts(frames);
    expect(collapsed.map((item) => item.assetId)).toEqual(["a", "x"]);
  });

  it("displays the representative image even when it is not the first member", () => {
    const frames = [
      photo({ assetId: "a", burstId: "c1", burstSize: 2, burstRepresentativeAssetId: "b" }),
      photo({ assetId: "b", burstId: "c1", burstSize: 2, burstRepresentativeAssetId: "b" }),
    ];
    const [cell] = collapseBursts(frames);
    expect(cell.assetId).toBe("a");
    expect(cell.thumbnailUrl).toBe("/assets/b/thumbnail");
    expect(cell.previewUrl).toBe("/assets/b/preview");
  });

  it("falls back to the member's own image when no representative is set", () => {
    const frames = [photo({ assetId: "a", burstId: "c1", burstSize: 2 })];
    const [cell] = collapseBursts(frames);
    expect(cell.thumbnailUrl).toBe("/assets/a/thumbnail");
  });

  it("preserves the first member's revision and identity for the collapsed cell", () => {
    const frames = [
      photo({ assetId: "a", burstId: "c1", burstSize: 2, burstRepresentativeAssetId: "b", revision: 7, rating: 4 }),
      photo({ assetId: "b", burstId: "c1", burstSize: 2, burstRepresentativeAssetId: "b", revision: 3 }),
    ];
    const [cell] = collapseBursts(frames);
    expect(cell.assetId).toBe("a");
    expect(cell.revision).toBe(7);
    expect(cell.rating).toBe(4);
  });
});
