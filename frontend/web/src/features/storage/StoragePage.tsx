import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Database, HardDrive, RefreshCw, ShieldCheck, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { api, type StorageReport } from "../../api/client";
import { Button } from "../../components/Button";

const labels: Record<string, string> = {
  canonical_objects: "Canonical objects",
  manifests: "Manifests & tombstones",
  processing_artifacts: "Processing artifacts",
  upload_staging: "Upload staging",
  recovery_checkpoints: "Recovery checkpoints",
  progress_records: "Progress records",
  unknown: "Unknown prefixes",
};

function bytes(value: number | null | undefined) {
  if (value == null) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let number = value;
  let unit = -1;
  while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit += 1; }
  return `${number.toFixed(number >= 10 ? 0 : 1)} ${units[unit]}`;
}

function age(seconds: number | null | undefined) {
  if (seconds == null) return "age unknown";
  const days = Math.floor(seconds / 86400);
  if (days) return `${days}d old`;
  const hours = Math.floor(seconds / 3600);
  return `${hours || 1}h old`;
}

function Status({ value }: { value: string }) {
  const tone = value === "ok" || value === "retained" ? "good" : value === "hard" || value === "review" ? "bad" : "warn";
  return <span className={`storage-status storage-status--${tone}`}><i />{value}</span>;
}

function Panel({ title, icon, children, className = "" }: { title: string; icon: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`storage-panel ${className}`}><header><span>{icon}</span><h2>{title}</h2></header>{children}</section>;
}

function IntegrityLine({ label, count, good = false }: { label: string; count: number; good?: boolean }) {
  return <div className="storage-integrity-line"><span>{good ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}{label}</span><strong className={count ? "is-alert" : ""}>{count || (good ? "Clear" : 0)}</strong></div>;
}

export function StoragePage() {
  const report = useQuery({ queryKey: ["storage-report"], queryFn: ({ signal }) => api.storageReport(signal), staleTime: 30_000 });
  if (report.isLoading) return <div className="storage-state"><RefreshCw className="spin" size={18} /><span>Scanning storage surfaces…</span></div>;
  if (report.isError || !report.data) return <div className="storage-state storage-state--error"><TriangleAlert size={20} /><h2>Storage report unavailable</h2><p>{report.error instanceof Error ? report.error.message : "The report could not be loaded."}</p><Button compact onClick={() => report.refetch()}><RefreshCw size={13} /> Retry</Button></div>;

  const data: StorageReport = report.data;
  const namespaces = Object.entries(data.s3.namespaces).sort(([, a], [, b]) => b.bytes - a.bytes);
  const maxBytes = Math.max(...namespaces.map(([, item]) => item.bytes), 1);
  const findings = data.integrity;
  const fs = data.local.filesystem;
  const freeRatio = fs.totalBytes && fs.freeBytes != null ? fs.freeBytes / fs.totalBytes : null;

  return <div className="storage-workspace">
    <header className="storage-header">
      <div><p className="eyebrow">Operations / capacity</p><div className="storage-title"><HardDrive size={21} /><h1>Storage telemetry</h1><Status value={data.status} /></div><p>Read-only accounting across the authoritative library and rebuildable local surfaces.</p></div>
      <div className="storage-header__meta"><span><i className="storage-live-dot" /> Report is read-only</span><span>As of {new Date(data.asOf).toLocaleString()}</span><Button compact tone="ghost" onClick={() => report.refetch()}><RefreshCw size={13} /> Refresh</Button></div>
    </header>

    <div className="storage-scroll">
      <div className="storage-summary">
        <div><span>S3 footprint</span><strong>{bytes(data.s3.totalBytes)}</strong><small>{data.s3.totalObjects.toLocaleString()} objects</small></div>
        <div><span>Canonical objects</span><strong>{bytes(data.s3.namespaces.canonical_objects?.bytes)}</strong><small>{(data.s3.namespaces.canonical_objects?.objects ?? 0).toLocaleString()} immutable blobs</small></div>
        <div><span>Local free space</span><strong>{bytes(fs.freeBytes)}</strong><small>{freeRatio == null ? "metric unavailable" : `${Math.round(freeRatio * 100)}% of filesystem free`}</small></div>
        <div><span>Review queue</span><strong>{findings.retentionReview.length}</strong><small>retention candidates · no deletes</small></div>
      </div>

      <div className="storage-grid storage-grid--primary">
        <Panel title="Namespace accounting" icon={<Database size={14} />} className="storage-panel--wide">
          <div className="storage-table storage-table--namespaces"><div className="storage-table__head"><span>Namespace</span><span>Footprint</span><span>Objects</span><span>Oldest</span></div>{namespaces.map(([key, item]) => <div className="storage-table__row" key={key}><span><b>{labels[key] ?? key}</b><em>{key}</em></span><span><strong>{bytes(item.bytes)}</strong><i className="storage-bar"><b style={{ width: `${Math.max(2, (item.bytes / maxBytes) * 100)}%` }} /></i></span><span>{item.objects.toLocaleString()}</span><span>{item.oldest ? age(Math.max(0, (Date.now() - Date.parse(item.oldest)) / 1000)) : "—"}</span></div>)}</div>
        </Panel>
        <Panel title="Integrity & references" icon={<ShieldCheck size={14} />}>
          <div className="storage-integrity"><IntegrityLine label="Missing references" count={findings.missingReferences.length} /><IntegrityLine label="Malformed manifests" count={findings.malformedManifests.length} /><IntegrityLine label="Unsupported versions" count={findings.unsupportedManifests.length} /><IntegrityLine label="Divergent / orphaned" count={findings.divergentOrOrphanedRecords.length} /><IntegrityLine label="Canonical namespace protected" count={0} good /></div>
          <p className="storage-note">Canonical objects and manifests are never presented as reclaimable from age alone.</p>
        </Panel>
      </div>

      <div className="storage-grid storage-grid--secondary">
        <Panel title="Local derived storage" icon={<HardDrive size={14} />}>
          <div className="storage-facts"><div><span>Preview files</span><strong>{data.local.previewCache.fileCount.toLocaleString()} · {bytes(data.local.previewCache.fileBytes)}</strong></div><div><span>Tracked rows</span><strong>{data.local.previewCache.rowCount ?? "Unavailable"}</strong></div><div><span>Orphan directories</span><strong className={data.local.previewCache.orphanedDirectories.length ? "is-alert" : ""}>{data.local.previewCache.orphanedDirectories.length}</strong></div><div><span>Cache limit</span><strong>{data.local.previewCache.configuredLimitBytes ? bytes(data.local.previewCache.configuredLimitBytes) : "Disabled"}</strong></div><div><span>Filesystem status</span><Status value={data.local.status} /></div></div>
        </Panel>
      </div>

      <Panel title={`Retention review · ${findings.retentionReview.length} candidates`} icon={<TriangleAlert size={14} />} className="storage-panel--retention">
        {findings.retentionReview.length === 0 ? <p className="storage-empty">No candidates currently meet a review rule. Nothing is eligible for automatic deletion.</p> : <div className="storage-table storage-table--retention"><div className="storage-table__head"><span>Object</span><span>Size</span><span>Rule</span><span>Recovery impact</span></div>{findings.retentionReview.map((item) => <div className="storage-table__row" key={item.key}><span><b>{item.key}</b><em>{item.namespace} · {age(item.ageSeconds)}</em></span><span>{bytes(item.sizeBytes)}</span><span>{item.retentionRule}</span><span>{item.recoveryImpact}</span></div>)}</div>}
      </Panel>
    </div>
  </div>;
}
