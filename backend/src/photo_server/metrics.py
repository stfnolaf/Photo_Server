"""Small dependency-free Prometheus metrics registry.

The registry deliberately accepts only labels chosen by the application. Callers
must never pass asset, filename, URL, or operation identifiers as labels.
"""

from collections import defaultdict
from threading import Lock


class Metrics:
    def __init__(self):
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)

    @staticmethod
    def _key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))

    def inc(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        try:
            with self._lock:
                self._counters[(name, self._key(labels))] += value
        except Exception:
            pass

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        try:
            with self._lock:
                self._gauges[(name, self._key(labels))] = value
        except Exception:
            pass

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        try:
            with self._lock:
                self._histograms[(name, self._key(labels))].append(float(value))
        except Exception:
            pass

    def render(self) -> str:
        try:
            with self._lock:
                counters, gauges, histograms = (
                    dict(self._counters), dict(self._gauges), dict(self._histograms)
                )
            lines: list[str] = []

            def labels_text(labels):
                if not labels:
                    return ""
                return "{" + ",".join(f'{k}="{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in labels) + "}"

            for (name, labels), value in sorted(counters.items()):
                lines.append(f"{name}_total{labels_text(labels)} {value:g}")
            for (name, labels), value in sorted(gauges.items()):
                lines.append(f"{name}{labels_text(labels)} {value:g}")
            for (name, labels), values in sorted(histograms.items()):
                total = len(values)
                for bound in (0.01, 0.1, 1, 10, 60, float("inf")):
                    bucket_labels = labels + (("le", "+Inf" if bound == float("inf") else str(bound)),)
                    lines.append(
                        f"{name}_bucket{labels_text(bucket_labels)} "
                        f"{sum(1 for value in values if value <= bound)}"
                    )
                lines.append(f"{name}_count{labels_text(labels)} {total}")
                lines.append(f"{name}_sum{labels_text(labels)} {sum(values):g}")
            return "\n".join(lines) + ("\n" if lines else "")
        except Exception:
            return ""


def safe_metrics(metrics: Metrics, method: str, *args, **kwargs) -> None:
    """Call instrumentation without changing the primary operation result."""
    try:
        getattr(metrics, method)(*args, **kwargs)
    except Exception:
        pass
