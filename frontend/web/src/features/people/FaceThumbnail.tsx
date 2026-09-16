import type { FaceReference } from "../../api/types";
import { PreviewImage } from "../../components/PreviewImage";

export function FaceThumbnail({ face, alt = "" }: { face: FaceReference; alt?: string }) {
  return (
    <div className="face-thumbnail">
      <PreviewImage src={face.thumbnailUrl} status="ready" alt={alt} />
    </div>
  );
}
