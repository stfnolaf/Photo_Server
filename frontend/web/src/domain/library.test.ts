import { describe, expect, it } from "vitest";
import { createOperationId, defaultFilters, readFilters, writeFilters } from "./library";

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
    expect(readFilters(new URLSearchParams("deleted=true"))).toMatchObject({ view: "trash" });
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
