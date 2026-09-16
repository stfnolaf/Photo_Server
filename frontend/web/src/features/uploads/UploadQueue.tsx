import { useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  CircleX,
  Clock3,
  ListChecks,
  LoaderCircle,
  MinusCircle,
  RotateCw,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { UploadBatch, UploadFileStatus, UploadQueueStatus } from "../../api/types";
import { Button } from "../../components/Button";
import { createOperationId } from "../../domain/library";

const ACCEPTED_FILES = [
  ".arw", ".cr2", ".cr3", ".nef", ".nrw", ".dng", ".raf", ".rw2", ".orf", ".pef", ".srw",
  ".jpg", ".jpeg", ".heif", ".heic", ".hif", ".xmp",
].join(",");
const ACTIVE_BATCH_STATUSES = new Set(["preparing", "uploading", "processing"]);

type QueuePhase = "preparing" | "uploading" | "processing" | "complete" | "failed";

interface QueueFile {
  path: string;
  source: File | null;
  sizeBytes: number;
  mimeType: string;
  fileId: string | null;
  uploadUrl: string | null;
  required: boolean;
  status: UploadFileStatus;
  progress: number;
  reason: string | null;
  error: string | null;
}

interface QueueBatch {
  id: string;
  phase: QueuePhase;
  serverStatus: UploadBatch["status"] | null;
  files: QueueFile[];
  error: string | null;
}

const wait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

function filePath(file: File): string {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${unit}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The upload could not be completed.";
}

function humanReason(reason: string | null): string {
  if (reason === "raw_preferred") return "RAW selected instead";
  if (reason === "unassigned_sidecar") return "Sidecar not matched";
  return reason ? reason.replaceAll("_", " ") : "Not needed";
}

function statusLabel(file: QueueFile): string {
  if (file.status === "uploading") return `${Math.round(file.progress * 100)}%`;
  if (file.status === "waiting") return "Waiting";
  if (file.status === "uploaded") return "Uploaded";
  if (file.status === "queued") return "Queued";
  if (file.status === "processing") return "Processing";
  if (file.status === "imported") return "Added";
  if (file.status === "duplicate") return "Already in library";
  if (file.status === "skipped") return humanReason(file.reason);
  return "Failed";
}

function batchLabel(batch: QueueBatch): string {
  const required = batch.files.filter((file) => file.required);
  const complete = required.filter((file) => ["uploaded", "queued", "processing", "imported", "duplicate"].includes(file.status)).length;
  if (batch.phase === "preparing") return "Preparing files";
  if (batch.phase === "uploading") return `Uploading ${complete} of ${required.length}`;
  if (batch.phase === "processing") return "Adding to library";
  if (batch.phase === "complete") return `${required.length} file${required.length === 1 ? "" : "s"} finished`;
  return "Needs attention";
}

function batchProgress(batch: QueueBatch): number {
  const required = batch.files.filter((file) => file.required);
  if (batch.phase === "processing") {
    if (!required.length) return 0;
    return required.filter((file) => ["imported", "duplicate", "failed"].includes(file.status)).length / required.length;
  }
  const total = required.reduce((sum, file) => sum + file.sizeBytes, 0);
  if (!total) return batch.phase === "complete" ? 1 : 0;
  return required.reduce((sum, file) => {
    const settled = ["uploaded", "queued", "processing", "imported", "duplicate"].includes(file.status);
    return sum + file.sizeBytes * (settled ? 1 : file.progress);
  }, 0) / total;
}

function restoredBatch(server: UploadBatch): QueueBatch {
  const interrupted = server.status === "accepting";
  return {
    id: server.batchId,
    phase: interrupted ? "failed" : server.status === "failed" ? "failed" : "processing",
    serverStatus: server.status,
    error: interrupted
      ? "This upload was interrupted before it was queued. Dismiss it and select the files again."
      : server.status === "failed"
        ? server.jobs.find((job) => job.error)?.error ?? "One or more files could not be added."
        : null,
    files: server.files.map((file) => ({
      path: file.path,
      source: null,
      sizeBytes: file.sizeBytes,
      mimeType: file.mimeType || "application/octet-stream",
      fileId: file.fileId,
      uploadUrl: file.uploadUrl,
      required: file.required,
      status: file.status,
      progress: ["waiting", "uploading"].includes(file.status) ? 0 : 1,
      reason: file.reason,
      error: file.error,
    })),
  };
}

function StatusIcon({ status }: { status: UploadFileStatus }) {
  if (["imported", "duplicate", "uploaded"].includes(status)) return <CheckCircle2 size={15} />;
  if (status === "failed") return <CircleX size={15} />;
  if (status === "skipped") return <MinusCircle size={15} />;
  if (["uploading", "processing"].includes(status)) return <LoaderCircle className="spin" size={15} />;
  return <Clock3 size={15} />;
}

async function inParallel<T>(items: T[], concurrency: number, task: (item: T) => Promise<void>) {
  let next = 0;
  const errors: unknown[] = [];
  const worker = async () => {
    while (next < items.length) {
      const item = items[next];
      next += 1;
      try {
        await task(item);
      } catch (error) {
        errors.push(error);
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  if (errors.length) throw errors[0];
}

export function UploadQueue() {
  const inputRef = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const queryClient = useQueryClient();
  const [batches, setBatches] = useState<QueueBatch[]>([]);
  const [open, setOpen] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [queueStatus, setQueueStatus] = useState<UploadQueueStatus | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const changeBatch = (id: string, change: (batch: QueueBatch) => QueueBatch) => {
    if (!mounted.current) return;
    setBatches((current) => current.map((batch) => batch.id === id ? change(batch) : batch));
  };

  const applyServerState = (id: string, server: UploadBatch, phase?: QueuePhase) => {
    changeBatch(id, (batch) => ({
      ...batch,
      phase: phase ?? (server.status === "complete" ? "complete" : server.status === "failed" ? "failed" : server.status === "accepting" ? "uploading" : "processing"),
      serverStatus: server.status,
      error: server.status === "failed" ? server.jobs.find((job) => job.error)?.error ?? "One or more files could not be added." : null,
      files: batch.files.map((local) => {
        const remote = server.files.find((file) => file.path === local.path);
        if (!remote) return local;
        return {
          ...local,
          fileId: remote.fileId,
          uploadUrl: remote.uploadUrl,
          mimeType: remote.mimeType || "application/octet-stream",
          required: remote.required,
          status: remote.status,
          progress: remote.status === "waiting" ? 0 : ["uploading", "failed"].includes(remote.status) ? local.progress : 1,
          reason: remote.reason,
          error: remote.error,
        };
      }),
    }));
  };

  const pollBatch = async (id: string) => {
    try {
      while (mounted.current) {
        await wait(1200);
        const server = await api.uploadBatch(id);
        applyServerState(id, server);
        if (server.status === "complete") {
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["library"] }),
            queryClient.invalidateQueries({ queryKey: ["health"] }),
          ]);
          return;
        }
        if (server.status === "failed") return;
      }
    } catch (error) {
      changeBatch(id, (batch) => ({ ...batch, phase: "failed", error: errorMessage(error) }));
    }
  };

  useEffect(() => {
    let cancelled = false;
    const restore = async () => {
      try {
        const serverBatches = await api.activeUploadBatches();
        if (cancelled) return;
        setBatches((current) => {
          const known = new Set(current.map((batch) => batch.id));
          return [...current, ...serverBatches.filter((batch) => !known.has(batch.batchId)).map(restoredBatch)];
        });
        for (const batch of serverBatches) {
          if (batch.status === "queued" || batch.status === "processing") void pollBatch(batch.batchId);
        }
      } catch {
        // The activity summary below still exposes server availability problems.
      } finally {
        if (!cancelled) setRestoring(false);
      }
    };
    const refreshStatus = async () => {
      try {
        const status = await api.uploadQueue();
        if (!cancelled) setQueueStatus(status);
      } catch {
        if (!cancelled) setQueueStatus(null);
      }
    };
    void restore();
    void refreshStatus();
    const timer = window.setInterval(() => { void refreshStatus(); }, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const runBatch = async (id: string, localFiles: QueueFile[]) => {
    try {
      changeBatch(id, (batch) => ({ ...batch, phase: "preparing", error: null }));
      let server = await api.createUploadBatch(
        id,
        localFiles.map((file) => ({
          path: file.path,
          sizeBytes: file.sizeBytes,
          mimeType: file.mimeType,
        })),
      );
      applyServerState(id, server);

      if (server.status === "accepting") {
        const pending = server.files.filter((file) => file.required && file.status !== "uploaded");
        const sources = new Map(localFiles.map((file) => [file.path, file.source]));
        await inParallel(pending, 4, async (remote) => {
          const source = sources.get(remote.path);
          if (!source || !remote.uploadUrl) throw new Error(`The selected file is no longer available: ${remote.path}`);
          changeBatch(id, (batch) => ({
            ...batch,
            phase: "uploading",
            files: batch.files.map((file) => file.path === remote.path ? { ...file, status: "uploading", error: null } : file),
          }));
          try {
            await api.uploadFile(remote.uploadUrl, source, (progress) => {
              changeBatch(id, (batch) => ({
                ...batch,
                files: batch.files.map((file) => file.path === remote.path ? { ...file, progress } : file),
              }));
            });
            changeBatch(id, (batch) => ({
              ...batch,
              files: batch.files.map((file) => file.path === remote.path ? { ...file, status: "uploaded", progress: 1 } : file),
            }));
          } catch (error) {
            changeBatch(id, (batch) => ({
              ...batch,
              files: batch.files.map((file) => file.path === remote.path ? { ...file, status: "failed", error: errorMessage(error) } : file),
            }));
            throw error;
          }
        });
        server = await api.sealUploadBatch(id);
        applyServerState(id, server, "processing");
      }

      if (server.status === "queued" || server.status === "processing") await pollBatch(id);
      else applyServerState(id, server);
    } catch (error) {
      changeBatch(id, (batch) => ({ ...batch, phase: "failed", error: errorMessage(error) }));
    }
  };

  const selectFiles = (files: FileList | null) => {
    if (!files?.length) return;
    const selected = Array.from(files);
    const paths = selected.map(filePath);
    const duplicate = paths.find((path, index) => paths.indexOf(path) !== index);
    const id = createOperationId();
    const queueFiles: QueueFile[] = selected.map((source) => ({
      path: filePath(source),
      source,
      sizeBytes: source.size,
      mimeType: source.type || "application/octet-stream",
      fileId: null,
      uploadUrl: null,
      required: true,
      status: "waiting",
      progress: 0,
      reason: null,
      error: null,
    }));
    setBatches((current) => [{
      id,
      phase: duplicate ? "failed" : "preparing",
      serverStatus: null,
      files: queueFiles,
      error: duplicate ? `Two selected files have the same name: ${duplicate}` : null,
    }, ...current]);
    setOpen(true);
    if (!duplicate) void runBatch(id, queueFiles);
  };

  const retry = async (batch: QueueBatch) => {
    changeBatch(batch.id, (value) => ({ ...value, phase: "preparing", error: null }));
    try {
      const server = await api.uploadBatch(batch.id);
      if (server.status === "failed") {
        const retried = await api.retryUploadBatch(batch.id);
        applyServerState(batch.id, retried, "processing");
        await pollBatch(batch.id);
      } else {
        await runBatch(batch.id, batch.files);
      }
    } catch {
      await runBatch(batch.id, batch.files);
    }
  };

  const dismiss = async (batch: QueueBatch) => {
    if (batch.serverStatus === null || batch.serverStatus === "accepting") {
      try {
        await api.abandonUploadBatch(batch.id);
      } catch {
        // The worker will clean up an unreachable or still-active unsealed batch after its TTL.
      }
    }
    setBatches((current) => current.filter((item) => item.id !== batch.id));
  };

  const backgroundRows = queueStatus ? [
    { label: "File transfers", pending: queueStatus.uploadsWaiting, running: queueStatus.uploadsActive, failed: 0 },
    { label: "Library imports", pending: queueStatus.onboardingPending, running: queueStatus.onboardingRunning, failed: queueStatus.onboardingFailed },
    { label: "Metadata", pending: queueStatus.processingPending, running: queueStatus.processingRunning, failed: queueStatus.processingFailed },
    { label: "Previews", pending: queueStatus.previewPending, running: queueStatus.previewRunning, failed: queueStatus.previewFailed },
    { label: "AI analysis", pending: queueStatus.analysisPending, running: queueStatus.analysisRunning, failed: queueStatus.analysisFailed },
  ].filter((row) => row.pending || row.running || row.failed) : [];
  const backgroundActive = backgroundRows.reduce((sum, row) => sum + row.pending + row.running, 0);
  const backgroundFailed = backgroundRows.reduce((sum, row) => sum + row.failed, 0);
  const activeCount = batches.filter((batch) => ACTIVE_BATCH_STATUSES.has(batch.phase)).length;
  const failedCount = batches.filter((batch) => batch.phase === "failed").length;
  const unfinishedCount = batches.filter((batch) => batch.phase !== "complete").length;
  const badgeCount = unfinishedCount || backgroundActive;

  return (
    <>
      <input
        ref={inputRef}
        className="sr-only"
        type="file"
        accept={ACCEPTED_FILES}
        multiple
        tabIndex={-1}
        onChange={(event) => {
          selectFiles(event.currentTarget.files);
          event.currentTarget.value = "";
        }}
      />
      <Button className="upload-trigger" compact tone="primary" onClick={() => inputRef.current?.click()}>
        <Upload size={14} /> <span>Upload</span>
      </Button>
      <button
        className="icon-button queue-trigger"
        type="button"
        aria-label={`Open upload queue${activeCount ? `, ${activeCount} active` : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ListChecks size={17} />
        {badgeCount > 0 && <span className="queue-trigger__count">{badgeCount > 99 ? "99+" : badgeCount}</span>}
      </button>

      {open && (
        <aside className="upload-queue" role="dialog" aria-label="Upload queue" aria-live="polite">
          <header className="upload-queue__header">
            <div>
              <h2>Upload queue</h2>
              <span>{activeCount ? `${activeCount} active upload${activeCount === 1 ? "" : "s"}` : backgroundActive ? `${backgroundActive} background job${backgroundActive === 1 ? "" : "s"}` : failedCount || backgroundFailed ? `${failedCount + backgroundFailed} need attention` : batches.length ? "Up to date" : "No uploads yet"}</span>
            </div>
            <button className="icon-button" type="button" onClick={() => setOpen(false)} aria-label="Close upload queue"><X size={17} /></button>
          </header>
          <div className="upload-queue__body">
            {backgroundRows.length > 0 && (
              <section className="background-work">
                <header>Background work</header>
                {backgroundRows.map((row) => (
                  <div className="background-work__row" key={row.label}>
                    <span>{row.label}</span>
                    <small>
                      {row.running > 0 && `${row.running} running`}
                      {row.running > 0 && row.pending > 0 && " · "}
                      {row.pending > 0 && `${row.pending} queued`}
                      {(row.running > 0 || row.pending > 0) && row.failed > 0 && " · "}
                      {row.failed > 0 && <strong>{row.failed} failed</strong>}
                    </small>
                  </div>
                ))}
              </section>
            )}
            {restoring && batches.length === 0 && (
              <div className="upload-queue__loading"><LoaderCircle className="spin" size={17} /> Checking server queue…</div>
            )}
            {!restoring && batches.length === 0 && backgroundRows.length === 0 && (
              <div className="upload-queue__empty">
                <Upload size={25} />
                <p>Select RAW, JPEG, HEIF, or XMP files to add them to your library.</p>
                <Button compact onClick={() => inputRef.current?.click()}>Choose files</Button>
              </div>
            )}
            {batches.map((batch) => {
              const progress = batchProgress(batch);
              const canRetry = batch.serverStatus === "failed" || batch.files.every((file) => file.source !== null);
              return (
                <section className={`upload-batch upload-batch--${batch.phase}`} key={batch.id}>
                  <div className="upload-batch__summary">
                    <div>
                      <strong>{batchLabel(batch)}</strong>
                      <span>{batch.files.length} selected · {formatBytes(batch.files.reduce((sum, file) => sum + file.sizeBytes, 0))}</span>
                    </div>
                    <span>{Math.round(progress * 100)}%</span>
                  </div>
                  <div className="upload-progress" role="progressbar" aria-label="Batch progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
                    <span style={{ width: `${progress * 100}%` }} />
                  </div>
                  {batch.error && <p className="upload-batch__error">{batch.error}</p>}
                  <ol className="upload-files">
                    {batch.files.map((file) => (
                      <li key={file.path} className={`upload-file upload-file--${file.status}`} title={file.error ?? undefined}>
                        <StatusIcon status={file.status} />
                        <div>
                          <strong>{file.path}</strong>
                          <span>{statusLabel(file)} · {formatBytes(file.sizeBytes)}</span>
                        </div>
                      </li>
                    ))}
                  </ol>
                  {batch.phase === "failed" && (
                    <div className="upload-batch__actions">
                      {canRetry && <Button compact onClick={() => void retry(batch)}><RotateCw size={13} /> Retry</Button>}
                      <Button compact tone="ghost" onClick={() => void dismiss(batch)}>Dismiss</Button>
                    </div>
                  )}
                </section>
              );
            })}
          </div>
          {batches.some((batch) => batch.phase === "complete") && (
            <footer className="upload-queue__footer">
              <button type="button" onClick={() => setBatches((current) => current.filter((batch) => batch.phase !== "complete"))}>Clear completed</button>
            </footer>
          )}
        </aside>
      )}
    </>
  );
}
