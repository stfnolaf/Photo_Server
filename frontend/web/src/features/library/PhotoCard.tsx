import { Check, Heart, Layers } from "lucide-react";
import type { MouseEvent } from "react";
import type { PhotoSummary } from "../../api/types";
import { PreviewImage } from "../../components/PreviewImage";
import { Rating } from "../../components/Rating";
import { formatDate, isBurst } from "../../domain/library";

export function PhotoCard({
  photo,
  selected,
  onOpen,
  onSelect,
  onFavorite,
  onBurst,
  busy,
}: {
  photo: PhotoSummary;
  selected: boolean;
  onOpen: () => void;
  onSelect: (range: boolean) => void;
  onFavorite: () => void;
  onBurst?: () => void;
  busy: boolean;
}) {
  const select = (event: MouseEvent) => {
    event.stopPropagation();
    onSelect(event.shiftKey);
  };
  const equipment = [photo.cameraModel, photo.lens].filter(Boolean).join(" · ");
  const burst = isBurst(photo);
  return (
    <article className={`photo-card ${selected ? "is-selected" : ""} ${burst ? "has-burst" : ""}`} data-asset-id={photo.assetId}>
      <button className="photo-card__open" type="button" onClick={onOpen} aria-label={`Open ${photo.originalFilename}`}>
        <div className="photo-card__frame">
          <PreviewImage src={photo.thumbnailUrl} status={photo.preview.status} alt="" />
          <span className="format-chip">{photo.mediaType}</span>
          {photo.dateSource === "import" && <span className="import-chip">Import date</span>}
          {burst && (
            <span className="burst-badge">
              <Layers size={12} />
              {photo.burstSize}
            </span>
          )}
        </div>
      </button>
      <button
        className="photo-card__select"
        type="button"
        aria-label={`${selected ? "Deselect" : "Select"} ${photo.originalFilename}`}
        aria-pressed={selected}
        onClick={select}
      >
        {selected && <Check size={13} strokeWidth={3} />}
      </button>
      {burst && onBurst && (
        <button
          className="burst-picker"
          type="button"
          title="Choose the best frame"
          aria-label={`Choose the best frame in this burst of ${photo.burstSize}`}
          onClick={onBurst}
        >
          <Layers size={14} />
        </button>
      )}
      <div className="photo-card__meta">
        <div className="photo-card__copy">
          <strong title={photo.originalFilename}>{photo.originalFilename}</strong>
          <span title={equipment || formatDate(photo.timelineTime)}>{equipment || formatDate(photo.timelineTime)}</span>
        </div>
        <div className="photo-card__marks">
          <Rating value={photo.rating} compact />
          <button
            type="button"
            className={`favorite-mark ${photo.favorite ? "is-active" : ""}`}
            aria-label={photo.favorite ? "Remove from favorites" : "Add to favorites"}
            aria-pressed={photo.favorite}
            disabled={busy || Boolean(photo.deletedAt)}
            onClick={onFavorite}
          >
            <Heart size={14} fill={photo.favorite ? "currentColor" : "none"} />
          </button>
        </div>
      </div>
    </article>
  );
}
