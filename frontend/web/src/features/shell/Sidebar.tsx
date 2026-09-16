import { Aperture, Folder, Heart, Image, MoreHorizontal, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { Album, LibraryFilters, PhotoSummary } from "../../api/types";
import { writeFilters } from "../../domain/library";
import { useAlbums } from "../../hooks/useAlbums";
import { useLayoutStore } from "../../state/layout";
import { AlbumDialog } from "../albums/AlbumDialog";

export function Sidebar({ filters, knownPhotos }: { filters: LibraryFilters; knownPhotos: PhotoSummary[] }) {
  const { active, deleted } = useAlbums();
  const [editingAlbum, setEditingAlbum] = useState<Album | null | undefined>();
  const navigate = useNavigate();
  const location = useLocation();
  const open = useLayoutStore((state) => state.leftPanelOpen);

  const choose = (next: Partial<LibraryFilters>) => {
    const value = { ...filters, view: "all" as const, albumId: null, ...next };
    navigate({ pathname: "/", search: writeFilters(value).toString() });
  };

  return (
    <>
      <aside className={`sidebar ${open ? "" : "sidebar--closed"}`} aria-label="Library navigation">
        <div className="brand">
          <span className="brand__mark"><Aperture size={19} strokeWidth={1.6} /></span>
          <span>Photo Library</span>
        </div>
        <nav className="sidebar__nav">
          <p className="sidebar__label">Library</p>
          <button className={location.pathname === "/" && filters.view === "all" && !filters.albumId ? "is-active" : ""} onClick={() => choose({ view: "all", albumId: null })}>
            <Image size={16} /> <span>All photographs</span>
          </button>
          <button className={filters.view === "favorites" ? "is-active" : ""} onClick={() => choose({ view: "favorites", albumId: null })}>
            <Heart size={16} /> <span>Favorites</span>
          </button>
          <button className={filters.view === "trash" ? "is-active" : ""} onClick={() => choose({ view: "trash", albumId: null })}>
            <Trash2 size={16} /> <span>Recently deleted</span>
          </button>
        </nav>

        <nav className="sidebar__nav sidebar__albums">
          <div className="sidebar__label-row">
            <p className="sidebar__label">Albums</p>
            <button type="button" onClick={() => setEditingAlbum(null)} aria-label="Create album"><Plus size={15} /></button>
          </div>
          {active.length === 0 && <p className="sidebar__empty">No albums yet</p>}
          {active.map((album) => (
            <div className={`sidebar__album ${filters.albumId === album.albumId ? "is-active" : ""}`} key={album.albumId}>
              <button onClick={() => choose({ albumId: album.albumId })}><Folder size={15} /><span>{album.name}</span><small>{album.assetIds.length}</small></button>
              <button className="sidebar__album-menu" onClick={() => setEditingAlbum(album)} aria-label={`Edit ${album.name}`}><MoreHorizontal size={15} /></button>
            </div>
          ))}
          {deleted.length > 0 && (
            <details className="deleted-albums">
              <summary>Deleted albums ({deleted.length})</summary>
              {deleted.map((album) => (
                <button key={album.albumId} onClick={() => setEditingAlbum(album)}><Trash2 size={14} /><span>{album.name}</span></button>
              ))}
            </details>
          )}
        </nav>
        <div className="sidebar__footer">
          <span className="connection-dot" />
          <span>Your originals, at home</span>
        </div>
      </aside>
      {editingAlbum !== undefined && (
        <AlbumDialog album={editingAlbum} knownPhotos={knownPhotos} onClose={() => setEditingAlbum(undefined)} />
      )}
    </>
  );
}
