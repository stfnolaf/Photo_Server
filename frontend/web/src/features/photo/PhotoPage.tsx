import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, LoaderCircle, Minus, PanelRightClose, PanelRightOpen, Plus, Scan, Sparkles } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../../api/client";
import { useDurableMutation } from "../../api/mutations";
import type { Album, Health, LibraryFilters, LocationValue, MutationResult, PhotoDetail } from "../../api/types";
import { PreviewImage } from "../../components/PreviewImage";
import { useToast } from "../../components/Toast";
import { primaryBlob, writeFilters } from "../../domain/library";
import { usePhotoLibrary } from "../../hooks/usePhotoLibrary";
import { useLayoutStore } from "../../state/layout";
import { PhotoInspector } from "./PhotoInspector";

function NamedFaceOverlays({ faces, imageAspectRatio }: { faces: PhotoDetail["analysis"]["faces"]; imageAspectRatio: number | null }) {
  const root = useRef<HTMLDivElement>(null);
  const [imageRect, setImageRect] = useState({ left: 0, top: 0, width: 100, height: 100 });
  useLayoutEffect(() => {
    const parent = root.current?.parentElement;
    if (!parent || !imageAspectRatio) return;
    const update = () => {
      const { width, height } = parent.getBoundingClientRect();
      if (!width || !height) return;
      const containerRatio = width / height;
      if (containerRatio > imageAspectRatio) {
        const displayedWidth = height * imageAspectRatio;
        setImageRect({ left: ((width - displayedWidth) / width) * 50, top: 0, width: (displayedWidth / width) * 100, height: 100 });
      } else {
        const displayedHeight = width / imageAspectRatio;
        setImageRect({ left: 0, top: ((height - displayedHeight) / height) * 50, width: 100, height: (displayedHeight / height) * 100 });
      }
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(parent);
    return () => observer.disconnect();
  }, [imageAspectRatio]);

  return (
    <div ref={root} className="photo-face-overlays" aria-label="Named people in this photograph">
      <div className="photo-face-overlays__image" style={{ left: `${imageRect.left}%`, top: `${imageRect.top}%`, width: `${imageRect.width}%`, height: `${imageRect.height}%` }}>
      {faces.map((face) => {
        const name = face.personName?.trim();
        if (!name || face.box.length < 4) return null;
        // Face-service boxes are normalized [x, y, width, height].
        const [left, top, width, height] = face.box;
        return (
          <span
            key={`${face.personId}-${face.faceIndex}`}
            className="photo-face-overlay"
            style={{
              left: `${left * 100}%`,
              top: `${top * 100}%`,
              width: `${width * 100}%`,
              height: `${height * 100}%`,
            }}
            aria-label={name}
            data-name={name}
            tabIndex={0}
          />
        );
      })}
      </div>
    </div>
  );
}

export function PhotoPage({
  filters,
  albums,
  library,
  health,
}: {
  filters: LibraryFilters;
  albums: Album[];
  library: ReturnType<typeof usePhotoLibrary>;
  health: Health;
}) {
  const { assetId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const { mutate } = useDurableMutation();
  const [busy, setBusy] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [previewAspectRatio, setPreviewAspectRatio] = useState<number | null>(null);
  const inspectorOpen = useLayoutStore((state) => state.inspectorOpen);
  const toggleInspector = useLayoutStore((state) => state.toggleInspector);
  const detailQuery = useQuery({
    queryKey: ["photo", assetId],
    queryFn: ({ signal }) => api.photo(assetId, signal),
    enabled: Boolean(assetId),
    refetchInterval: (query) => {
      const status = query.state.data?.analysis.status;
      return status === "pending" || status === "running" ? 2000 : false;
    },
  });
  const { photos } = library;
  const index = photos.findIndex((photo) => photo.assetId === assetId);
  const previous = index > 0 ? photos[index - 1] : null;
  const next = index >= 0 && index < photos.length - 1 ? photos[index + 1] : null;

  useEffect(() => {
    setZoom(1);
    setPreviewAspectRatio(null);
  }, [assetId]);

  const open = useCallback(
    (id: string) => navigate({ pathname: `/photo/${id}`, search: writeFilters(filters).toString() }),
    [filters, navigate],
  );

  const go = useCallback(
    async (direction: -1 | 1) => {
      if (direction < 0 && previous) open(previous.assetId);
      if (direction > 0) {
        if (next) open(next.assetId);
        else if (library.hasNextPage && !library.isFetchingNextPage) {
          const result = await library.fetchNextPage();
          const all = result.data?.pages.flatMap((page) => page.items) ?? [];
          const current = all.findIndex((photo) => photo.assetId === assetId);
          if (all[current + 1]) open(all[current + 1].assetId);
        }
      }
    },
    [assetId, library, next, open, previous],
  );

  const updateCachedDetail = (result: MutationResult) => {
    queryClient.setQueryData<PhotoDetail>(["photo", assetId], (current) =>
      current
        ? {
            ...current,
            revision: result.revision,
            deletedAt: result.deletedAt,
            userState: {
              rating: result.rating,
              favorite: result.favorite,
              caption: result.caption,
              keywords: result.keywords,
              location: result.location,
            },
          }
        : current,
    );
  };

  const saveState = useCallback(
    async (changes: Record<string, unknown>) => {
      const detail = queryClient.getQueryData<PhotoDetail>(["photo", assetId]);
      if (!detail) return;
      setBusy(true);
      try {
        const result = await mutate<MutationResult>(`/assets/${assetId}/user-state`, "PATCH", {
          ...changes,
          expectedRevision: detail.revision,
        });
        updateCachedDetail(result);
        await queryClient.invalidateQueries({ queryKey: ["library"] });
        toast.show("Changes saved");
      } catch (error) {
        toast.show(error instanceof Error ? error.message : "Changes could not be saved", "error");
      } finally {
        setBusy(false);
      }
    },
    [assetId, mutate, queryClient, toast],
  );

  const toggleAlbum = async (album: Album) => {
    setBusy(true);
    try {
      const assetIds = album.assetIds.includes(assetId)
        ? album.assetIds.filter((id) => id !== assetId)
        : [...album.assetIds, assetId];
      await mutate(`/albums/${album.albumId}`, "PATCH", {
        assetIds,
        expectedRevision: album.revision,
      });
      await queryClient.invalidateQueries({ queryKey: ["albums"] });
      toast.show(album.assetIds.includes(assetId) ? "Removed from album" : "Added to album");
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Album could not be changed", "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleDeleted = async () => {
    const detail = detailQuery.data;
    if (!detail) return;
    const restore = Boolean(detail.deletedAt);
    setBusy(true);
    try {
      await mutate(`/assets/${assetId}${restore ? "/restore" : ""}`, restore ? "POST" : "DELETE", {
        expectedRevision: detail.revision,
      });
      await queryClient.invalidateQueries({ queryKey: ["library"] });
      toast.show(restore ? "Photograph unhidden" : "Photograph hidden");
      navigate({ pathname: "/", search: writeFilters(filters).toString() });
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Photograph could not be changed", "error");
    } finally {
      setBusy(false);
    }
  };

  const reanalyze = async () => {
    setBusy(true);
    try {
      await api.reanalyze(assetId);
      await queryClient.invalidateQueries({ queryKey: ["photo", assetId] });
      toast.show("Local analysis queued");
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Analysis could not be queued", "error");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.altKey || event.metaKey || /INPUT|TEXTAREA|SELECT/.test((event.target as HTMLElement)?.tagName)) return;
      if (event.key === "ArrowLeft") void go(-1);
      else if (event.key === "ArrowRight") void go(1);
      else if (event.key === "Escape") navigate({ pathname: "/", search: writeFilters(filters).toString() });
      else if (/^[0-5]$/.test(event.key) && detailQuery.data && !detailQuery.data.deletedAt) void saveState({ rating: Number(event.key) });
      else if (event.key.toLowerCase() === "f" && detailQuery.data && !detailQuery.data.deletedAt) void saveState({ favorite: !detailQuery.data.userState.favorite });
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [detailQuery.data, filters, go, navigate, saveState]);

  const filmstrip = useMemo(() => photos.slice(Math.max(0, index - 15), Math.max(30, index + 16)), [index, photos]);

  if (detailQuery.isLoading) return <div className="photo-loading"><LoaderCircle className="spin" /><span>Loading photograph…</span></div>;
  if (detailQuery.isError || !detailQuery.data) return <div className="photo-loading photo-loading--error"><h1>Photograph unavailable</h1><p>{detailQuery.error instanceof Error ? detailQuery.error.message : "The photograph could not be loaded."}</p></div>;

  const detail = detailQuery.data;
  const primary = primaryBlob(detail);

  return (
    <section className={`photo-workspace ${inspectorOpen ? "" : "photo-workspace--inspector-closed"}`}>
      <header className="photo-toolbar">
        <div className="photo-toolbar__identity"><strong title={primary.originalFilename}>{primary.originalFilename}</strong><span>{primary.role.replace("ORIGINAL_", "")}</span></div>
        <div className="photo-toolbar__zoom">
          <button onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))} aria-label="Zoom out"><Minus size={15} /></button>
          <button onClick={() => setZoom(1)} aria-label="Fit photograph"><Scan size={15} /><span>{Math.round(zoom * 100)}%</span></button>
          <button onClick={() => setZoom((value) => Math.min(4, value + 0.25))} aria-label="Zoom in"><Plus size={15} /></button>
        </div>
        <button type="button" className="icon-button" onClick={toggleInspector} aria-label={inspectorOpen ? "Hide inspector" : "Show inspector"}>{inspectorOpen ? <PanelRightClose size={18} /> : <PanelRightOpen size={18} />}</button>
      </header>
      <div className="photo-stage">
        <div className="photo-stage__canvas">
          <div className="photo-stage__scaled" style={{ transform: `scale(${zoom})` }}>
            <PreviewImage src={`/assets/${assetId}/preview`} status={detail.preview.status} alt={primary.originalFilename} eager contain onRetry={() => api.retryPreview(assetId)} onImageLoad={(width, height) => setPreviewAspectRatio(width / height)} />
            {detail.analysis.status === "ready" && <NamedFaceOverlays faces={detail.analysis.faces} imageAspectRatio={previewAspectRatio} />}
          </div>
        </div>
        {(detail.analysis.status === "pending" || detail.analysis.status === "running") && (
          <div className="photo-analysis-badge" role="status">
            {detail.analysis.status === "pending"
              ? <Sparkles size={13} />
              : <LoaderCircle className="spin" size={13} />}
            {detail.analysis.status === "pending" ? "Queued for GPU analysis" : "GPU analysis in progress"}
          </div>
        )}
        <button className="stage-nav stage-nav--previous" disabled={!previous} onClick={() => go(-1)} aria-label="Previous photograph"><ChevronLeft size={27} /></button>
        <button className="stage-nav stage-nav--next" disabled={!next && !library.hasNextPage} onClick={() => go(1)} aria-label="Next photograph"><ChevronRight size={27} /></button>
      </div>
      <div className="filmstrip" aria-label="Photograph filmstrip">
        {filmstrip.map((photo) => (
          <button key={photo.assetId} className={photo.assetId === assetId ? "is-active" : ""} onClick={() => open(photo.assetId)} title={photo.originalFilename}>
            <PreviewImage src={photo.thumbnailUrl} status={photo.preview.status} alt="" />
          </button>
        ))}
      </div>
      {inspectorOpen && (
        <PhotoInspector
          detail={detail}
          albums={albums}
          health={health}
          busy={busy}
          onState={(changes) => saveState(changes)}
          onMetadata={(changes: { caption: string; keywords: string[]; location: LocationValue | null }) => saveState(changes)}
          onToggleAlbum={toggleAlbum}
          onReanalyze={reanalyze}
          onDelete={toggleDeleted}
        />
      )}
    </section>
  );
}
