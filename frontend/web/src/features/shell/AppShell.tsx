import { useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, CircleHelp, PanelLeftClose, PanelLeftOpen, RotateCw } from "lucide-react";
import { type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { Album, Health, LibraryFilters, PhotoSummary } from "../../api/types";
import { useDurableMutation } from "../../api/mutations";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { writeFilters } from "../../domain/library";
import { useLayoutStore } from "../../state/layout";
import { UploadQueue } from "../uploads/UploadQueue";
import { Sidebar } from "./Sidebar";

export function AppShell({
  health,
  filters,
  knownPhotos,
  albums,
  children,
}: {
  health: Health;
  filters: LibraryFilters;
  knownPhotos: PhotoSummary[];
  albums: Album[];
  children: ReactNode;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const { pending, saving, retry, discard } = useDurableMutation();
  const leftOpen = useLayoutStore((state) => state.leftPanelOpen);
  const toggleLeft = useLayoutStore((state) => state.toggleLeftPanel);
  const isPhoto = location.pathname.startsWith("/photo/");
  const isPeople = location.pathname === "/people";

  const refresh = async () => {
    await queryClient.invalidateQueries();
    toast.show("Library refreshed");
  };

  const retryPending = async () => {
    try {
      await retry();
      await queryClient.invalidateQueries();
      toast.show("Pending change saved");
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Change could not be saved", "error");
    }
  };

  const discardPending = async () => {
    const confirmed = window.confirm(
      "Discard this pending request? A request that already reached the server may still have been saved.",
    );
    if (!confirmed) return;
    discard();
    await queryClient.invalidateQueries();
  };

  return (
    <div className={`app ${leftOpen ? "" : "app--sidebar-closed"}`}>
      <a className="skip-link" href="#workspace">Skip to workspace</a>
      <Sidebar filters={filters} knownPhotos={knownPhotos} />
      <header className="topbar">
        <div className="topbar__left">
          <button type="button" className="icon-button panel-toggle" onClick={toggleLeft} aria-label={leftOpen ? "Hide library panel" : "Show library panel"}>
            {leftOpen ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
          </button>
          {isPhoto && (
            <button className="back-to-library" type="button" onClick={() => navigate({ pathname: "/", search: writeFilters(filters).toString() })}>
              <ChevronLeft size={16} /> Library
            </button>
          )}
          <div className="module-switcher" aria-label="Workspace">
            <Link to={{ pathname: "/", search: writeFilters(filters).toString() }} className={!isPhoto && !isPeople ? "is-active" : ""}>Library</Link>
            <Link to="/people" className={isPeople ? "is-active" : ""}>People</Link>
            <span className={isPhoto ? "is-active" : ""}>Photo</span>
          </div>
        </div>
        <div className="topbar__right">
          <span className="library-count" title={`${health.blobs} stored files`}>{health.assets.toLocaleString()} originals</span>
          <UploadQueue albums={albums} />
          <button className="icon-button refresh-button" type="button" onClick={refresh} aria-label="Refresh library"><RotateCw size={16} /></button>
          <a className="icon-button" href="/docs" target="_blank" rel="noreferrer" aria-label="Open API documentation"><CircleHelp size={17} /></a>
        </div>
      </header>
      {pending && (
        <div className="pending-bar" role="status">
          <span>{saving ? "Saving pending change…" : "A change is waiting to be retried."}</span>
          <Button compact onClick={retryPending} disabled={saving}>Retry</Button>
          <Button compact tone="ghost" onClick={discardPending} disabled={saving}>Discard</Button>
        </div>
      )}
      <main id="workspace" className="workspace" tabIndex={-1}>{children}</main>
      <nav className="mobile-nav" aria-label="Mobile navigation">
        <button onClick={() => navigate({ pathname: "/", search: writeFilters({ ...filters, view: "all", albumId: null }).toString() })}>Library</button>
        <button onClick={() => navigate({ pathname: "/", search: writeFilters({ ...filters, view: "favorites", albumId: null }).toString() })}>Favorites</button>
        <button onClick={() => navigate("/people")}>People</button>
        <button onClick={() => navigate({ pathname: "/", search: writeFilters({ ...filters, view: "hidden", albumId: null }).toString() })}>Hidden</button>
      </nav>
    </div>
  );
}
