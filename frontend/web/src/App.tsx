import { useQuery } from "@tanstack/react-query";
import { Aperture, LoaderCircle, RefreshCw, LockKeyhole } from "lucide-react";
import { FormEvent, useState } from "react";
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
  const session = useQuery({ queryKey: ["session"], queryFn: () => api.session(), retry: false });
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loggingIn, setLoggingIn] = useState(false);
  const [filters, setFilters] = useLibraryFilters();
  const albums = useAlbums();
  const library = usePhotoLibrary(filters);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    staleTime: 30_000,
    enabled: session.data?.authenticated === true,
  });

  if (session.isLoading || (session.data?.authenticated && health.isLoading)) {
    return (
      <div className="boot-screen">
        <Aperture size={38} strokeWidth={1.25} />
        <LoaderCircle className="spin" size={17} />
        <span>Opening your library</span>
      </div>
    );
  }
  if (!session.data?.authenticated) {
    const submit = async (event: FormEvent) => {
      event.preventDefault();
      setLoggingIn(true);
      setLoginError("");
      try {
        await api.login(password);
        setPassword("");
        await session.refetch();
      } catch {
        setLoginError("That password did not unlock the library.");
      } finally {
        setLoggingIn(false);
      }
    };
    return (
      <main className="login-screen">
        <form className="login-panel" onSubmit={submit}>
          <LockKeyhole size={28} strokeWidth={1.2} />
          <p className="login-panel__eyebrow">Private library</p>
          <h1>Unlock your archive</h1>
          <label htmlFor="library-password">Password</label>
          <input id="library-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} autoFocus />
          {loginError && <p className="login-panel__error" role="alert">{loginError}</p>}
          <Button type="submit" disabled={loggingIn || !password}>{loggingIn ? "Checking…" : "Continue"}</Button>
        </form>
      </main>
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
        <AppShell health={health.data} filters={filters} knownPhotos={library.photos} albums={albums.active}>
          <Routes>
            <Route
              path="/"
              element={<LibraryPage filters={filters} setFilters={setFilters} albums={albums.active} library={library} />}
            />
            <Route
              path="/photo/:assetId"
              element={<PhotoPage filters={filters} albums={albums.active} library={library} health={health.data} />}
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
