import { Download, Heart, RotateCcw, Trash2 } from "lucide-react";
import type { Album, LocationValue, MutationResult, PhotoDetail } from "../../api/types";
import { apiUrl } from "../../api/client";
import { Button } from "../../components/Button";
import { PanelSection } from "../../components/PanelSection";
import { Rating } from "../../components/Rating";
import { displayValue, formatBytes, formatDate, primaryBlob, technicalRows } from "../../domain/library";
import { MetadataForm } from "./MetadataForm";

export function PhotoInspector({
  detail,
  albums,
  busy,
  onState,
  onMetadata,
  onToggleAlbum,
  onDelete,
}: {
  detail: PhotoDetail;
  albums: Album[];
  busy: boolean;
  onState: (changes: Partial<MutationResult>) => Promise<void>;
  onMetadata: (changes: { caption: string; keywords: string[]; location: LocationValue | null }) => Promise<void>;
  onToggleAlbum: (album: Album) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const primary = primaryBlob(detail);
  const deleted = Boolean(detail.deletedAt);
  const metadataRows = technicalRows(detail).filter((row): row is [string, string] => Boolean(row[1]));
  return (
    <aside className="photo-inspector" aria-label="Photo information and organization">
      <PanelSection title="Quick actions">
        <div className="quick-actions">
          <Rating value={detail.userState.rating} disabled={busy || deleted} onChange={(rating) => onState({ rating })} />
          <button className={`favorite-action ${detail.userState.favorite ? "is-active" : ""}`} disabled={busy || deleted} onClick={() => onState({ favorite: !detail.userState.favorite })}>
            <Heart size={16} fill={detail.userState.favorite ? "currentColor" : "none"} />
            {detail.userState.favorite ? "Favorite" : "Add favorite"}
          </button>
        </div>
      </PanelSection>

      <PanelSection title="Description">
        <MetadataForm detail={detail} disabled={busy || deleted} onSave={onMetadata} />
      </PanelSection>

      <PanelSection title="Albums" defaultOpen={false}>
        <div className="album-checklist">
          {albums.length === 0 && <p className="inspector-note">Create an album from the Library panel.</p>}
          {albums.map((album) => {
            const checked = album.assetIds.includes(detail.assetId);
            return <label key={album.albumId}><input type="checkbox" checked={checked} disabled={busy || deleted} onChange={() => onToggleAlbum(album)} /><span>{album.name}</span><small>{album.assetIds.length}</small></label>;
          })}
        </div>
      </PanelSection>

      <PanelSection title="Camera and exposure">
        <dl className="properties">
          {metadataRows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
      </PanelSection>

      <PanelSection title="File information" defaultOpen={false}>
        <dl className="properties">
          <div><dt>Captured</dt><dd>{formatDate(detail.captureTime, true)}</dd></div>
          <div><dt>Imported</dt><dd>{formatDate(detail.importedAt, true)}</dd></div>
          <div><dt>Format</dt><dd>{primary.role.replace("ORIGINAL_", "")}</dd></div>
          <div><dt>File size</dt><dd>{formatBytes(primary.sizeBytes)}</dd></div>
          {detail.blobs.filter((blob) => blob.role === "SIDECAR").map((blob) => <div key={blob.blobId}><dt>Sidecar</dt><dd>{blob.originalFilename}</dd></div>)}
        </dl>
        <a className="button button--default download-button" href={apiUrl(`/assets/${detail.assetId}/original`)} download><Download size={14} /> Download original</a>
      </PanelSection>

      <PanelSection title="All recorded metadata" defaultOpen={false}>
        <dl className="properties properties--raw">
          {Object.entries(detail.metadata).sort(([a], [b]) => a.localeCompare(b)).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{displayValue(value)}</dd></div>)}
        </dl>
      </PanelSection>

      <div className="inspector-danger">
        <Button tone={deleted ? "default" : "danger"} disabled={busy} onClick={onDelete}>
          {deleted ? <RotateCcw size={14} /> : <Trash2 size={14} />}
          {deleted ? "Restore photograph" : "Move to trash"}
        </Button>
      </div>
    </aside>
  );
}
