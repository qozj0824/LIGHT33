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


def test_profile_snapshot_recovers_after_server_storage_loss(tmp_path: Path, monkeypatch) -> None:
    import json
    from lightt.equipment import EquipmentProfile

    monkeypatch.setattr(app_module, "PROFILE_ROOT", tmp_path / "profiles")
    profile = EquipmentProfile(
        profile_id="a1b2c3d4",
        name="Browser recovery profile",
        created_at="2026-08-24T00:00:00+00:00",
        telescope_name="Synthetic Scope",
        camera_name="Synthetic Camera",
        gain_e_per_adu=1.0,
        read_noise_e=4.0,
    )
    loaded, recovered = app_module._load_profile_or_snapshot(
        profile.profile_id,
        json.dumps(profile.to_dict()),
    )
    assert recovered is True
    assert loaded.profile_id == profile.profile_id
    assert loaded.camera_name == "Synthetic Camera"


def test_health_exposes_instance_id() -> None:
    client = TestClient(app_module.app)
    payload = client.get("/health").json()
    assert isinstance(payload.get("instance_id"), str)
    assert payload["instance_id"]
