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
import { StoragePage } from "./features/storage/StoragePage";
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
  const startup = useQuery({
    queryKey: ["startup-status"],
    queryFn: ({ signal }) => api.startupStatus(signal),
    enabled: session.data?.authenticated === true,
    refetchInterval: (query) => query.state.data?.state === "ready" ? false : 1000,
  });
  const ready = startup.data?.state === "ready";
  const albums = useAlbums(ready);
  const library = usePhotoLibrary(filters, ready);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    staleTime: 30_000,
    enabled: session.data?.authenticated === true && ready,
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
  if (session.isError) {
    return (
      <div className="boot-screen boot-screen--error">
        <Aperture size={38} strokeWidth={1.25} />
        <h1>Library is starting</h1>
        <p>The server is rebuilding or checking its catalog. Try again in a moment.</p>
        <Button onClick={() => session.refetch()}><RefreshCw size={14} /> Try again</Button>
      </div>
    );
  }
  if (session.data?.authenticated && (startup.isLoading || !ready)) {
    const status = startup.data;
    const failed = status?.state === "failed";
    const phaseLabel = status?.phase === "projection"
      ? "Applying the rebuilt catalog…"
      : status?.phase === "reconciliation"
        ? "Checking the restored catalog…"
        : "Rebuilding the searchable catalog from S3…";
    return (
      <div className={`boot-screen ${failed ? "boot-screen--error" : ""}`}>
        <Aperture size={38} strokeWidth={1.25} />
        <LoaderCircle className={failed ? "" : "spin"} size={17} />
        <h1>{failed ? "Library recovery failed" : "Preparing your library"}</h1>
        <p>{failed ? status.error : phaseLabel}</p>
        {status && status.total > 0 && (
          <div className="startup-progress" aria-label={`Recovery progress: ${status.percent}%`}>
            <div className="startup-progress__bar"><span style={{ width: `${status.percent}%` }} /></div>
            <span>{status.scanned.toLocaleString()} / {status.total.toLocaleString()} manifests · {status.percent.toFixed(1)}%</span>
          </div>
        )}
        {failed && <Button onClick={() => startup.refetch()}><RefreshCw size={14} /> Try again</Button>}
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
            <Route path="/storage" element={<StoragePage />} />
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
