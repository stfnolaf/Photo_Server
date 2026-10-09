import { ImageOff, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { apiUrl } from "../api/client";
import type { PreviewStatus } from "../api/types";
import { Button } from "./Button";

type LoadState = "idle" | "loading" | "ready" | "unavailable" | "failed";

class FetchPool {
  private active = 0;
  private readonly queue: Array<{
    signal: AbortSignal;
    run: () => Promise<Response>;
    resolve: (response: Response) => void;
    reject: (error: unknown) => void;
  }> = [];

  constructor(private readonly concurrency: number) {}

  request(run: () => Promise<Response>, signal: AbortSignal): Promise<Response> {
    return new Promise((resolve, reject) => {
      this.queue.push({ signal, run, resolve, reject });
      this.pump();
    });
  }

  private pump() {
    while (this.active < this.concurrency && this.queue.length > 0) {
      const task = this.queue.shift()!;
      if (task.signal.aborted) {
        task.reject(new DOMException("Aborted", "AbortError"));
        continue;
      }
      this.active += 1;
      task.run().then(task.resolve, task.reject).finally(() => {
        this.active -= 1;
        this.pump();
      });
    }
  }
}

const derivativeFetches = new FetchPool(5);

async function waitForDerivative(path: string, signal: AbortSignal): Promise<void> {
  let attempt = 0;
  while (!signal.aborted) {
    const response = await derivativeFetches.request(() => fetch(apiUrl(path), { signal }), signal);
    if (response.status === 202) {
      const seconds = Number(response.headers.get("Retry-After")) || Math.min(20, 2 + attempt++);
      await new Promise<void>((resolve, reject) => {
        const abort = () => {
          window.clearTimeout(timer);
          reject(new DOMException("Aborted", "AbortError"));
        };
        const timer = window.setTimeout(() => {
          signal.removeEventListener("abort", abort);
          resolve();
        }, seconds * 1000);
        signal.addEventListener("abort", abort, { once: true });
      });
      continue;
    }
    if (response.status === 404) throw new Error("unavailable");
    if (!response.ok) throw new Error("failed");
    return;
  }
  throw new DOMException("Aborted", "AbortError");
}

export function PreviewImage({
  src,
  status,
  alt,
  eager = false,
  contain = false,
  onRetry,
  onImageLoad,
}: {
  src: string;
  status: PreviewStatus;
  alt: string;
  eager?: boolean;
  contain?: boolean;
  onRetry?: () => Promise<unknown>;
  onImageLoad?: (width: number, height: number) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(eager);
  const [loadState, setLoadState] = useState<LoadState>(
    status === "unavailable" ? "unavailable" : status === "failed" ? "failed" : "idle",
  );
  const [imageKey, setImageKey] = useState(0);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (eager || active || !root.current) return;
    const observer = new IntersectionObserver(
      ([entry]) => entry.isIntersecting && setActive(true),
      { rootMargin: "400px" },
    );
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [active, eager]);

  useEffect(() => {
    if (!active || status === "unavailable") return;
    const controller = new AbortController();
    if (status === "failed" && attempt === 0) {
      setLoadState("failed");
      return () => controller.abort();
    }
    setLoadState("loading");
    // A ready job can still have an absent cache file: preview files are
    // disposable and may have been evicted since the catalog was projected.
    // Probe the endpoint after the first direct image attempt so a 202 can
    // regenerate the derivative instead of being treated as a decode error.
    if (status === "ready" && attempt === 0) {
      setImageKey((value) => value + 1);
      return () => controller.abort();
    }
    waitForDerivative(src, controller.signal)
      .then(() => {
        setImageKey((value) => value + 1);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setLoadState(error instanceof Error && error.message === "unavailable" ? "unavailable" : "failed");
      });
    return () => {
      controller.abort();
    };
  }, [active, attempt, src, status]);

  const handleImageError = () => {
    // Preserve the browser's decoded-image and HTTP caches on the first try.
    // If a ready derivative was evicted, the follow-up probe sees 202 and
    // waits for regeneration; a second decode failure is treated as real.
    if (status === "ready" && attempt > 0) {
      setLoadState("failed");
      return;
    }
    setLoadState("loading");
    setAttempt((value) => value + 1);
  };

  return (
    <div ref={root} className={`preview-image ${contain ? "preview-image--contain" : ""}`}>
      {active && loadState !== "unavailable" && loadState !== "failed" && (
        <img
          key={`${src}-${imageKey}`}
          src={apiUrl(src)}
          alt={alt}
          draggable={false}
          onLoad={(event) => {
            setLoadState("ready");
            onImageLoad?.(event.currentTarget.naturalWidth, event.currentTarget.naturalHeight);
          }}
          onError={handleImageError}
        />
      )}
      {loadState !== "ready" && (
        <div className="preview-image__state">
          {loadState === "loading" || loadState === "idle" ? (
            <>
              <LoaderCircle className="spin" size={eager ? 28 : 20} />
              {eager && <span>Preparing preview…</span>}
            </>
          ) : (
            <>
              <ImageOff size={eager ? 34 : 24} />
              <span>{loadState === "unavailable" ? "Preview unavailable" : "Preview could not be loaded"}</span>
              {eager && loadState === "failed" && (
                <Button compact tone="ghost" onClick={async () => {
                  try { await onRetry?.(); } catch { /* The next fetch keeps the failure visible. */ }
                  setAttempt((value) => value + 1);
                }}>
                  <RefreshCw size={13} /> Retry
                </Button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
