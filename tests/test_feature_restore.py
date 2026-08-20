from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import app as app_module


def test_basic_and_restored_feature_ui_tokens() -> None:
    html = (Path(app_module.ROOT) / "index.html").read_text(encoding="utf-8")
    javascript = (Path(app_module.STATIC_ROOT) / "app.js").read_text(encoding="utf-8")
    for token in [
        'id="equipmentProfile"',
        'id="openProfileManager"',
        'id="createProfile"',
        'id="importReferenceTarget"',
        'id="importTarget"',
        'id="allskyPreview"',
        'id="skyGallery"',
        'id="overviewGallery"',
        'id="minimumSkyAltitude"',
        'id="analyzeButton"',
    ]:
        assert token in html
    for token in [
        "/api/equipment/profiles",
        "/api/session/analyze",
        "/api/stellarium/normalize",
        "allsky_coordinate_overlay",
        "sky_polar_map",
        "sky_reliability",
        "exposure_snr_curve",
        "upload_token",
    ]:
        assert token in javascript


def test_inspect_returns_role_specific_preview_and_token() -> None:
    image = Image.new("I;16", (64, 48), color=1024)
    buffer = io.BytesIO()
    image.save(buffer, format="TIFF")
    client = TestClient(app_module.app)
    response = client.post(
        "/api/inspect",
        files={"file": ("scope.tiff", buffer.getvalue(), "image/tiff")},
        data={"role": "scope", "sensor_clip_adu": "65535", "safety": "0.8"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["preview_url"].endswith("/scope_preview.png")
    assert len(payload["upload_token"]) == 24
    result_path = app_module.ROOT / payload["preview_url"].lstrip("/")
    assert result_path.exists()


def test_stellarium_ping_endpoint_uses_service(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "stellarium_ping_service",
        lambda base_url: {"ok": True, "base_url": base_url, "status_keys": ["time"]},
    )
    client = TestClient(app_module.app)
    response = client.get(
        "/api/stellarium/ping",
        params={"base_url": "http://127.0.0.1:8090"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
