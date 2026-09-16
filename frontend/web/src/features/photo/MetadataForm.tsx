import { useEffect, useState } from "react";
import type { LocationValue, PhotoDetail } from "../../api/types";
import { Button } from "../../components/Button";

export function MetadataForm({
  detail,
  disabled,
  onSave,
}: {
  detail: PhotoDetail;
  disabled: boolean;
  onSave: (changes: { caption: string; keywords: string[]; location: LocationValue | null }) => Promise<void>;
}) {
  const [caption, setCaption] = useState(detail.userState.caption);
  const [keywords, setKeywords] = useState(detail.userState.keywords.join("\n"));
  const [locationName, setLocationName] = useState(detail.userState.location?.name ?? "");
  const [latitude, setLatitude] = useState(detail.userState.location?.latitude?.toString() ?? "");
  const [longitude, setLongitude] = useState(detail.userState.location?.longitude?.toString() ?? "");
  const [error, setError] = useState("");

  useEffect(() => {
    setCaption(detail.userState.caption);
    setKeywords(detail.userState.keywords.join("\n"));
    setLocationName(detail.userState.location?.name ?? "");
    setLatitude(detail.userState.location?.latitude?.toString() ?? "");
    setLongitude(detail.userState.location?.longitude?.toString() ?? "");
  }, [detail.assetId, detail.revision, detail.userState]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (Boolean(latitude) !== Boolean(longitude)) {
      setError("Enter both latitude and longitude.");
      return;
    }
    setError("");
    const name = locationName.trim();
    await onSave({
      caption,
      keywords: [...new Set(keywords.split("\n").map((word) => word.trim()).filter(Boolean))],
      location: name || latitude
        ? { name, latitude: latitude ? Number(latitude) : null, longitude: longitude ? Number(longitude) : null }
        : null,
    });
  };

  return (
    <form className="metadata-form" onSubmit={submit}>
      <label className="field"><span>Caption</span><textarea rows={3} maxLength={10000} value={caption} disabled={disabled} placeholder="Add a caption" onChange={(event) => setCaption(event.target.value)} /></label>
      <label className="field"><span>Keywords <small>one per line</small></span><textarea rows={3} value={keywords} disabled={disabled} placeholder="portrait&#10;holiday" onChange={(event) => setKeywords(event.target.value)} /></label>
      <label className="field"><span>Location</span><input maxLength={500} value={locationName} disabled={disabled} placeholder="Location name" onChange={(event) => setLocationName(event.target.value)} /></label>
      <div className="coordinate-fields">
        <label className="field"><span>Latitude</span><input type="number" min="-90" max="90" step="any" value={latitude} disabled={disabled} onChange={(event) => setLatitude(event.target.value)} /></label>
        <label className="field"><span>Longitude</span><input type="number" min="-180" max="180" step="any" value={longitude} disabled={disabled} onChange={(event) => setLongitude(event.target.value)} /></label>
      </div>
      {error && <p className="form-error">{error}</p>}
      <Button compact type="submit" disabled={disabled}>Save metadata</Button>
    </form>
  );
}
