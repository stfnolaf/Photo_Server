import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { EyeOff, FolderOpen, Grid2X2, ImageOff, LoaderCircle, Pencil, RefreshCw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Album, LibraryFilters, MutationResult, PhotoSummary } from "../../api/types";
import { useDurableMutation } from "../../api/mutations";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { collectionTitle, collapseBursts, isBurst, monthKey, monthLabel, writeFilters } from "../../domain/library";
import { usePhotoLibrary } from "../../hooks/usePhotoLibrary";
import { useLayoutStore } from "../../state/layout";
import { AlbumDialog } from "../albums/AlbumDialog";
import { BurstStrip } from "./BurstStrip";
import { LibraryToolbar } from "./LibraryToolbar";
import { PhotoCard } from "./PhotoCard";

type VirtualRow =
  | { type: "heading"; key: string; label: string; count: number }
  | { type: "photos"; key: string; photos: PhotoSummary[] };

function buildRows(photos: PhotoSummary[], columns: number): VirtualRow[] {
  const groups = new Map<string, PhotoSummary[]>();
  for (const photo of photos) {
    const key = monthKey(photo);
    groups.set(key, [...(groups.get(key) ?? []), photo]);
  }
  const rows: VirtualRow[] = [];
  for (const [key, items] of groups) {
    rows.push({ type: "heading", key: `heading-${key}`, label: monthLabel(key), count: items.length });
    for (let index = 0; index < items.length; index += columns) {
      rows.push({ type: "photos", key: `${key}-${index}`, photos: items.slice(index, index + columns) });
    }
  }
  return rows;
}

export function LibraryPage({
  filters,
  setFilters,
  albums,
  library,
}: {
  filters: LibraryFilters;
  setFilters: (filters: LibraryFilters) => void;
  albums: Album[];
  library: ReturnType<typeof usePhotoLibrary>;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [lastSelected, setLastSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [editingAlbum, setEditingAlbum] = useState<Album | null | undefined>();
  const thumbnailSize = useLayoutStore((state) => state.thumbnailSize);
  const setThumbnailSize = useLayoutStore((state) => state.setThumbnailSize);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { mutate } = useDurableMutation();
  const toast = useToast();
  const { photos, total } = library;
  const currentAlbum = filters.albumId ? albums.find((album) => album.albumId === filters.albumId) : undefined;

  useEffect(() => {
    if (!scrollRef.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(scrollRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => setSelected(new Set()), [filters]);

  const [burstReview, setBurstReview] = useState<PhotoSummary | null>(null);
  const gap = 12;
  const columns = Math.max(1, Math.floor((width - 32 + gap) / (thumbnailSize + gap)));
  const cardWidth = (width - 32 - gap * (columns - 1)) / columns;
  const collapsed = useMemo(() => collapseBursts(photos), [photos]);
  const rows = useMemo(() => buildRows(collapsed, columns), [columns, collapsed]);
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => (rows[index]?.type === "heading" ? 50 : cardWidth * (2 / 3) + 58),
    overscan: 3,
  });
  const virtualRows = virtualizer.getVirtualItems();

  useEffect(() => virtualizer.measure(), [columns, virtualizer]);

  useEffect(() => {
    const last = virtualRows.at(-1);
    if (last && last.index >= rows.length - 3 && library.hasNextPage && !library.isFetchingNextPage) {
      void library.fetchNextPage();
    }
  }, [library, rows.length, virtualRows]);

  const choose = (assetId: string, range: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (range && lastSelected) {
        const start = collapsed.findIndex((photo) => photo.assetId === lastSelected);
        const end = collapsed.findIndex((photo) => photo.assetId === assetId);
        if (start >= 0 && end >= 0) {
          for (const photo of collapsed.slice(Math.min(start, end), Math.max(start, end) + 1)) next.add(photo.assetId);
        }
      } else if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
    setLastSelected(assetId);
  };

  const toggleFavorite = async (photo: PhotoSummary) => {
    setBusy((value) => new Set(value).add(photo.assetId));
    try {
      await mutate<MutationResult>(`/assets/${photo.assetId}/user-state`, "PATCH", {
        favorite: !photo.favorite,
        expectedRevision: photo.revision,
      });
      await queryClient.invalidateQueries({ queryKey: ["library"] });
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Favorite could not be saved", "error");
    } finally {
      setBusy((value) => {
        const next = new Set(value);
        next.delete(photo.assetId);
        return next;
      });
    }
  };

  const addSelectionToAlbum = async (albumId: string) => {
    if (!albumId) return;
    const album = albums.find((entry) => entry.albumId === albumId);
    if (!album) return;
    try {
      await mutate(`/albums/${album.albumId}`, "PATCH", {
        assetIds: [...new Set([...album.assetIds, ...selected])],
        expectedRevision: album.revision,
      });
      await queryClient.invalidateQueries({ queryKey: ["albums"] });
      toast.show(`${selected.size} photograph${selected.size === 1 ? "" : "s"} added to ${album.name}`);
      setSelected(new Set());
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Album could not be changed", "error");
    }
  };

  const removeSelectionFromCurrentAlbum = async () => {
    if (!currentAlbum) return;
    try {
      await mutate(`/albums/${currentAlbum.albumId}`, "PATCH", {
        assetIds: currentAlbum.assetIds.filter((id) => !selected.has(id)),
        expectedRevision: currentAlbum.revision,
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["albums"] }),
        queryClient.invalidateQueries({ queryKey: ["library"] }),
      ]);
      toast.show(`${selected.size} photograph${selected.size === 1 ? "" : "s"} removed from ${currentAlbum.name}`);
      setSelected(new Set());
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Album could not be changed", "error");
    }
  };

  const title = collectionTitle(filters, albums);
  const activeFilters = Boolean(filters.q || filters.dateFrom || filters.dateTo || filters.mediaType || filters.ratingMin);

  return (
    <section className="library-workspace">
      <header className="library-header">
        <div>
          <p className="eyebrow">{filters.albumId ? "Album" : filters.view === "hidden" ? "Hidden photographs" : "Photo library"}</p>
          <div className="library-header__title">
            <h1>{title}</h1>
            {currentAlbum && <button className="icon-button" type="button" onClick={() => setEditingAlbum(currentAlbum)} aria-label="Edit album"><Pencil size={15} /></button>}
          </div>
          <p>{library.isLoading ? "Reading your library…" : `${total.toLocaleString()} photograph${total === 1 ? "" : "s"}`}</p>
        </div>
      </header>
      <LibraryToolbar filters={filters} setFilters={setFilters} />

      <div className="photo-scroll" ref={scrollRef} aria-busy={library.isLoading}>
        {library.isLoading && <div className="center-state"><LoaderCircle className="spin" /><span>Loading photographs</span></div>}
        {library.isError && (
          <div className="center-state center-state--error">
            <ImageOff size={32} /><h2>Library unavailable</h2><p>{library.error instanceof Error ? library.error.message : "The library could not be loaded."}</p>
            <Button onClick={() => library.refetch()}><RefreshCw size={14} /> Try again</Button>
          </div>
        )}
        {!library.isLoading && !library.isError && photos.length === 0 && (
          <div className="center-state empty-library">
            {filters.view === "hidden" ? <EyeOff size={38} /> : filters.albumId ? <FolderOpen size={38} /> : <Grid2X2 size={38} />}
            <h2>{activeFilters ? "No matching photographs" : filters.view === "hidden" ? "No hidden photographs" : filters.albumId ? "This album is empty" : "Your library starts here"}</h2>
            <p>{activeFilters ? "Try widening the filters or using a different search." : "Imported photographs will appear here, ordered by capture date."}</p>
            {activeFilters && <Button onClick={() => setFilters({ ...filters, q: "", dateFrom: "", dateTo: "", mediaType: "", ratingMin: 0 })}>Clear filters</Button>}
          </div>
        )}
        {photos.length > 0 && (
          <div className="virtual-grid" style={{ height: virtualizer.getTotalSize() }}>
            {virtualRows.map((virtualRow) => {
              const row = rows[virtualRow.index];
              return (
                <div
                  key={row.key}
                  ref={virtualizer.measureElement}
                  data-index={virtualRow.index}
                  className={row.type === "heading" ? "month-row" : "photo-row"}
                  style={{ transform: `translateY(${virtualRow.start}px)` }}
                >
                  {row.type === "heading" ? (
                    <><h2>{row.label}</h2><span>{row.count}</span></>
                  ) : (
                    <div className="photo-row__grid" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
                      {row.photos.map((photo) => (
                        <PhotoCard
                          key={photo.assetId}
                          photo={photo}
                          selected={selected.has(photo.assetId)}
                          busy={busy.has(photo.assetId)}
                          onSelect={(range) => choose(photo.assetId, range)}
                          onFavorite={() => toggleFavorite(photo)}
                          onOpen={() => {
                            const target = isBurst(photo) ? (photo.burstRepresentativeAssetId ?? photo.assetId) : photo.assetId;
                            navigate({ pathname: `/photo/${target}`, search: writeFilters(filters).toString() });
                          }}
                          onBurst={isBurst(photo) ? () => setBurstReview(photo) : undefined}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {library.isFetchingNextPage && <div className="page-loading"><LoaderCircle className="spin" size={16} /> Loading more…</div>}
      </div>

      <footer className="library-statusbar">
        <div className="selection-actions">
          {selected.size > 0 ? <>
            <strong>{selected.size}</strong> selected
            {albums.length > 0 && (
              <select defaultValue="" aria-label="Add selected photographs to album" onChange={(event) => { void addSelectionToAlbum(event.target.value); event.target.value = ""; }}>
                <option value="" disabled>Add to album…</option>
                {albums.map((album) => <option key={album.albumId} value={album.albumId}>{album.name}</option>)}
              </select>
            )}
            {currentAlbum && <button onClick={removeSelectionFromCurrentAlbum}>Remove from album</button>}
            <button onClick={() => setSelected(new Set())}><X size={12} /> Clear</button>
          </> : `${photos.length.toLocaleString()} of ${total.toLocaleString()}`}
        </div>
        <label className="density-control"><Grid2X2 size={13} /><span className="sr-only">Thumbnail size</span><input type="range" min="130" max="280" step="10" value={thumbnailSize} onChange={(event) => setThumbnailSize(Number(event.target.value))} /></label>
      </footer>
      {editingAlbum !== undefined && <AlbumDialog album={editingAlbum} knownPhotos={photos} onClose={() => setEditingAlbum(undefined)} />}
      {burstReview && (
        <BurstStrip
          photo={burstReview}
          onClose={() => setBurstReview(null)}
          onRepresentative={async (assetId) => {
            try {
              await mutate(`/assets/${assetId}/burst/representative`, "POST", {});
              await queryClient.invalidateQueries({ queryKey: ["library"] });
              await queryClient.invalidateQueries({ queryKey: ["burst"] });
            } catch (error) {
              toast.show(error instanceof Error ? error.message : "The burst selection could not be saved", "error");
            }
          }}
        />
      )}
    </section>
  );
}
