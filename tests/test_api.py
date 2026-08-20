from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


def test_health() -> None:
    client = TestClient(app_module.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_upload_writer_preserves_bytes(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(app_module.app)
    # A non-image file with an allowed suffix reaches the decoder only after byte-perfect upload.
    payload = bytes((index * 37) % 256 for index in range(2_500_123))
    observed = {}

    def fake_load(path: Path):
        observed["bytes"] = path.read_bytes()
        raise ValueError("decoder stop")

    monkeypatch.setattr(app_module, "load_image", fake_load)
    response = client.post(
        "/api/inspect",
        files={"file": ("sample.fit", payload, "application/octet-stream")},
    )
    assert response.status_code == 400
    assert observed["bytes"] == payload


def test_stellarium_set_time_endpoint(monkeypatch) -> None:
    client = TestClient(app_module.app)
    monkeypatch.setattr(
        app_module,
        "stellarium_set_time_service",
        lambda base_url, observation_time_utc, pause=True: {
            "ok": True,
            "observation_time_utc": observation_time_utc,
            "paused": pause,
        },
    )
    response = client.post(
        "/api/stellarium/set-time",
        data={
            "base_url": "http://127.0.0.1:8090",
            "observation_time_utc": "2025-04-15T12:00:00+00:00",
            "pause": "true",
        },
    )
    assert response.status_code == 200
    assert response.json()["paused"] is True
