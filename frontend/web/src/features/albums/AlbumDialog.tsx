import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, RotateCcw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { Album, PhotoSummary } from "../../api/types";
import { useDurableMutation } from "../../api/mutations";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import { useToast } from "../../components/Toast";

export function AlbumDialog({
  album,
  knownPhotos,
  onClose,
  onSaved,
}: {
  album: Album | null;
  knownPhotos: PhotoSummary[];
  onClose: () => void;
  onSaved?: (album: Album) => void;
}) {
  const [name, setName] = useState(album?.name ?? "");
  const [description, setDescription] = useState(album?.description ?? "");
  const [members, setMembers] = useState(album?.assetIds ?? []);
  const [error, setError] = useState("");
  const { mutate, saving } = useDurableMutation();
  const queryClient = useQueryClient();
  const toast = useToast();
  const deleted = Boolean(album?.deletedAt);

  useEffect(() => {
    setName(album?.name ?? "");
    setDescription(album?.description ?? "");
    setMembers(album?.assetIds ?? []);
  }, [album]);

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["albums"] });
    await queryClient.invalidateQueries({ queryKey: ["library"] });
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const result = await mutate<Album>(
        album ? `/albums/${album.albumId}` : "/albums",
        album ? "PATCH" : "POST",
        {
          name: name.trim(),
          description,
          assetIds: members,
          ...(album ? { expectedRevision: album.revision } : {}),
        },
      );
      await refresh();
      toast.show(album ? "Album updated" : "Album created");
      onSaved?.(result);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Album could not be saved");
    }
  };

  const toggleDeleted = async () => {
    if (!album) return;
    setError("");
    try {
      await mutate<Album>(
        `/albums/${album.albumId}${deleted ? "/restore" : ""}`,
        deleted ? "POST" : "DELETE",
        { expectedRevision: album.revision },
      );
      await refresh();
      toast.show(deleted ? "Album restored" : "Album moved to trash");
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Album could not be changed");
    }
  };

  const move = (index: number, direction: -1 | 1) => {
    setMembers((current) => {
      const next = [...current];
      [next[index], next[index + direction]] = [next[index + direction], next[index]];
      return next;
    });
  };

  return (
    <Modal title={album ? (deleted ? "Deleted album" : "Edit album") : "Create album"} onClose={onClose} wide>
      <form className="album-form" onSubmit={save}>
        <label className="field">
          <span>Name</span>
          <input value={name} maxLength={200} required disabled={deleted || saving} autoFocus onChange={(event) => setName(event.target.value)} />
        </label>
        <label className="field">
          <span>Description</span>
          <textarea value={description} maxLength={10000} rows={3} disabled={deleted || saving} onChange={(event) => setDescription(event.target.value)} />
        </label>
        {members.length > 0 && (
          <section className="album-members">
            <header>
              <h3>Photographs</h3>
              <span>{members.length}</span>
            </header>
            <ol>
              {members.map((assetId, index) => {
                const photo = knownPhotos.find((item) => item.assetId === assetId);
                return (
                  <li key={assetId}>
                    <span title={assetId}>{photo?.originalFilename ?? assetId}</span>
                    {!deleted && (
                      <span className="album-members__actions">
                        <button type="button" disabled={index === 0 || saving} onClick={() => move(index, -1)} aria-label="Move photograph up"><ArrowUp size={14} /></button>
                        <button type="button" disabled={index === members.length - 1 || saving} onClick={() => move(index, 1)} aria-label="Move photograph down"><ArrowDown size={14} /></button>
                        <button type="button" disabled={saving} onClick={() => setMembers((current) => current.filter((id) => id !== assetId))} aria-label="Remove photograph"><X size={14} /></button>
                      </span>
                    )}
                  </li>
                );
              })}
            </ol>
          </section>
        )}
        {error && <p className="form-error" role="alert">{error}</p>}
        <footer className="modal__actions">
          {album && (
            <Button type="button" tone={deleted ? "default" : "danger"} disabled={saving} onClick={toggleDeleted}>
              {deleted ? <RotateCcw size={15} /> : <Trash2 size={15} />}
              {deleted ? "Restore album" : "Move to trash"}
            </Button>
          )}
          <span className="modal__spacer" />
          <Button type="button" tone="ghost" onClick={onClose}>Cancel</Button>
          {!deleted && <Button type="submit" tone="primary" disabled={saving || !name.trim()}>{saving ? "Saving…" : "Save album"}</Button>}
        </footer>
      </form>
    </Modal>
  );
}
