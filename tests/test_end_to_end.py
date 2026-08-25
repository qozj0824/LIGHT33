from __future__ import annotations

import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app as app_module
from lightt.models import FisheyeConfig

fits = pytest.importorskip("astropy.io.fits")


def fits_bytes(data: np.ndarray, exposure: float) -> bytes:
    header = fits.Header()
    header["EXPTIME"] = exposure
    header["SATURATE"] = 65535
    buffer = io.BytesIO()
    fits.writeto(buffer, data.astype(np.uint16), header=header)
    return buffer.getvalue()


def gaussian_star(image: np.ndarray, x: int, y: int, peak: float, sigma: float = 2.0) -> None:
    yy, xx = np.indices(image.shape)
    image += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))


def test_full_api_generates_beginner_plan_and_polar_map(tmp_path, monkeypatch) -> None:
    rng = np.random.default_rng(31)
    allsky = rng.normal(1000, 8, (240, 320)).astype(np.float64)
    yy, xx = np.indices(allsky.shape)
    radius = np.hypot(xx - 159.5, yy - 119.5)
    allsky += np.clip(radius / 3, 0, 250)

    scope = rng.normal(1200, 5, (260, 360)).astype(np.float64)
    scope[85:175, 110:230] += 140
    for index in range(12):
        gaussian_star(scope, 28 + index * 27, 35 + (index % 4) * 55, 9000 + index * 900)
    scope = np.clip(scope, 0, 60000)

    monkeypatch.setattr(
        "lightt.pipeline.select_fisheye_config",
        lambda *args, **kwargs: FisheyeConfig(mode="auto_equidistant"),
    )
    client = TestClient(app_module.app)
    inspect_allsky = client.post(
        "/api/inspect",
        files={"file": ("allsky.fit", fits_bytes(allsky, 10), "application/fits")},
        data={"role": "allsky"},
    )
    inspect_scope = client.post(
        "/api/inspect",
        files={"file": ("scope.fit", fits_bytes(scope, 30), "application/fits")},
        data={"role": "scope", "sensor_clip_adu": "65535"},
    )
    assert inspect_allsky.status_code == 200
    assert inspect_scope.status_code == 200
    allsky_token = inspect_allsky.json()["upload_token"]
    scope_token = inspect_scope.json()["upload_token"]

    response = client.post(
        "/api/analyze",
        data={
            "allsky_token": allsky_token,
            "scope_token": scope_token,
            "current_exposure_sec": "30",
            "target_snr": "60",
            "target_mode": "extended",
            "target_name": "synthetic nebula",
            "gain_e_per_adu": "1",
            "read_noise_e": "3",
            "sensor_clip_adu": "65535",
            "smoothing_pixels": "100",
            "target_roi_json": json.dumps({"x": 0.31, "y": 0.33, "w": 0.32, "h": 0.34}),
            "background_roi_json": json.dumps({"x": 0.05, "y": 0.65, "w": 0.2, "h": 0.22}),
            "auto_roi": "false",
            "auto_roi_confirmed": "true",
            "target_alt_deg": "55",
            "target_az_deg": "140",
            "minimum_sky_altitude_deg": "15",
            "max_sub_exposure_sec": "300",
            "max_recommended_frames": "2000",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["beginner_summary"]["target_name"] == "synthetic nebula"
    assert payload["plan"]["status"] == "ok"
    assert payload["plan"]["recommended_sub_exposure_sec"] > 0
    assert payload["artifacts"]["sky_polar_map"].endswith(".png")
    assert payload["sky"]["map_label"].endswith("하늘 배경 ADU 지도")
