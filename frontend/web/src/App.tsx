import { useQuery } from "@tanstack/react-query";
import { Aperture, LoaderCircle, RefreshCw } from "lucide-react";
import { Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import { DurableMutationProvider } from "./api/mutations";
import { Button } from "./components/Button";
import { ToastProvider } from "./components/Toast";
import { LibraryPage } from "./features/library/LibraryPage";
import { PeoplePage } from "./features/people/PeoplePage";
import { PhotoPage } from "./features/photo/PhotoPage";
import { AppShell } from "./features/shell/AppShell";
import { useAlbums } from "./hooks/useAlbums";
import { useLibraryFilters } from "./hooks/useLibraryFilters";
import { usePhotoLibrary } from "./hooks/usePhotoLibrary";
import { writeFilters } from "./domain/library";

function ConnectedApp() {
  const [filters, setFilters] = useLibraryFilters();
  const albums = useAlbums();
  const library = usePhotoLibrary(filters);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    staleTime: 30_000,
  });

  if (health.isLoading) {
    return (
      <div className="boot-screen">
        <Aperture size={38} strokeWidth={1.25} />
        <LoaderCircle className="spin" size={17} />
        <span>Opening your library</span>
      </div>
    );
  }
  if (health.isError || !health.data) {
    return (
      <div className="boot-screen boot-screen--error">
        <Aperture size={38} strokeWidth={1.25} />
        <h1>Library unavailable</h1>
        <p>{health.error instanceof Error ? health.error.message : "The photo server could not be reached."}</p>
        <Button onClick={() => health.refetch()}><RefreshCw size={14} /> Reconnect</Button>
      </div>
    );
  }

  return (
    <DurableMutationProvider libraryId={health.data.libraryId}>
      <ToastProvider>
        <AppShell health={health.data} filters={filters} knownPhotos={library.photos}>
          <Routes>
            <Route
              path="/"
              element={<LibraryPage filters={filters} setFilters={setFilters} albums={albums.active} library={library} />}
            />
            <Route
              path="/photo/:assetId"
              element={<PhotoPage filters={filters} albums={albums.active} library={library} />}
            />
            <Route path="/people" element={<PeoplePage filters={filters} />} />
            <Route path="*" element={<Navigate to={{ pathname: "/", search: writeFilters(filters).toString() }} replace />} />
          </Routes>
        </AppShell>
      </ToastProvider>
    </DurableMutationProvider>
  );
}

export default function App() {
  return <ConnectedApp />;
}
