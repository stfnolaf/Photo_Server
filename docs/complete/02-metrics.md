# Session 2: Operational metrics

Work in `/home/stephen/dev/photo_server`.

Implement metrics only. Do not implement authentication, backup replication,
restore verification, or storage cleanup in this session.

## Context

The project has durable queues, preview-cache accounting, optional AI stages,
and scheduled backups, but operators cannot easily see trends such as queue
age, cache hit rate, job latency, or backup freshness.

Review the existing health, catalog, worker, AI worker, cache, and backup code
before choosing instrumentation points. Reuse existing counters and queries
where practical; avoid adding an expensive database query to every request.

## Requirements

- Add a small metrics surface suitable for local operation and future
  Prometheus scraping. Keep it disabled or loopback/internal-only by default if
  exposing it would reveal private library information.
- Instrument at minimum:
  - upload batches and uploaded bytes;
  - pending/running/oldest queue jobs;
  - job failures, retries, and durations;
  - preview-cache hits, misses, bytes, evictions, and regeneration latency;
  - AI stage durations and provider failures;
  - backup age and failures.
- Use stable metric names and document labels carefully to prevent unbounded
  cardinality. Never use asset ID, filename, URL, or operation ID as a metric
  label.
- Ensure instrumentation cannot make ingestion or preview delivery fail.
- Add configuration for binding/enabling metrics if needed.

## Verification

- Add tests for metric output and representative counter increments.
- Test that instrumentation failures do not alter the primary operation result.
- Exercise queue and cache metrics with existing integration fixtures.
- Run backend tests, lint, and API/OpenAPI checks if routes or schemas change.
- Document how to scrape or inspect the metrics.

## Stop condition

Return the implementation, verification results, metric names, and any known
limitations. Do not add dashboards or authentication in this session.
