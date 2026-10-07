from photo_server.metrics import Metrics, safe_metrics


def test_metrics_render_stable_labels_and_values():
    metrics = Metrics()
    metrics.inc("photo_upload_batches")
    metrics.inc("photo_uploaded_bytes", 2048)
    metrics.inc("photo_preview_cache_hits", labels={"kind": "preview"})
    metrics.observe("photo_job_duration_seconds", 0.25, {"queue": "preview"})

    output = metrics.render()
    assert "photo_upload_batches_total 1" in output
    assert "photo_uploaded_bytes_total 2048" in output
    assert 'photo_preview_cache_hits_total{kind="preview"} 1' in output
    assert 'photo_job_duration_seconds_count{queue="preview"} 1' in output
    assert "asset_id" not in output


def test_metrics_failure_isolated_from_primary_result(monkeypatch):
    metrics = Metrics()
    monkeypatch.setattr(metrics, "inc", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    result = {"status": "uploaded", "value": 7}

    safe_metrics(metrics, "inc", "photo_uploaded_bytes", 7)

    assert result == {"status": "uploaded", "value": 7}
