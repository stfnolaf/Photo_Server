import { useQuery } from "@tanstack/react-query";
import { Check, ImageOff, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { api } from "../../api/client";
import type { PhotoSummary } from "../../api/types";
import { Modal } from "../../components/Modal";
import { PreviewImage } from "../../components/PreviewImage";
import { formatDate } from "../../domain/library";

export function BurstStrip({
  photo,
  onClose,
  onRepresentative,
}: {
  photo: PhotoSummary;
  onClose: () => void;
  onRepresentative: (assetId: string) => void | Promise<void>;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const burst = useQuery({
    queryKey: ["burst", photo.assetId],
    queryFn: ({ signal }) => api.burst(photo.assetId, signal),
  });

  const frames = burst.data?.frames ?? [];
  const representativeId = burst.data?.representativeAssetId;

  const choose = (assetId: string) => {
    if (pending) return;
    setPending(assetId);
    Promise.resolve(onRepresentative(assetId)).finally(() => setPending(null));
  };

  return (
    <Modal title={`Choose the best frame · ${frames.length} shots`} onClose={onClose} wide>
      {burst.isPending && (
        <div className="burst-strip__state">
          <LoaderCircle className="spin" size={20} /> Loading frames…
        </div>
      )}
      {burst.isError && (
        <div className="burst-strip__state">
          <ImageOff size={20} />
          The burst frames could not be loaded.
        </div>
      )}
      {burst.isSuccess && frames.length === 0 && (
        <div className="burst-strip__state">
          <ImageOff size={20} />
          This burst has no frames.
        </div>
      )}
      {burst.isSuccess && frames.length > 0 && (
        <div className="burst-strip">
          {frames.map((frame) => {
            const isRepresentative = frame.assetId === representativeId;
            const isPending = pending === frame.assetId;
            return (
              <button
                key={frame.assetId}
                type="button"
                className={`burst-frame ${isRepresentative ? "is-best" : ""} ${isPending ? "is-busy" : ""}`}
                onClick={() => choose(frame.assetId)}
                aria-label={isRepresentative ? `${frame.originalFilename} is the current best frame` : `Make ${frame.originalFilename} the best frame`}
                aria-pressed={isRepresentative}
                title={isRepresentative ? "Current best frame" : "Make this the best frame"}
              >
                <div className="burst-frame__image">
                  <PreviewImage src={frame.previewUrl} status={frame.preview.status} alt="" eager />
                </div>
                <span className="burst-frame__caption">
                  <strong>{frame.originalFilename}</strong>
                  <span>{formatDate(frame.timelineTime, true)}</span>
                </span>
                {isRepresentative && (
                  <span className="burst-frame__best">
                    <Check size={12} strokeWidth={3} /> Best
                  </span>
                )}
                {isPending && <LoaderCircle className="spin" size={16} />}
              </button>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
