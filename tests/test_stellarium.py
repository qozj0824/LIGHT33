from __future__ import annotations

import pytest

from lightt.stellarium import normalize_base_url, normalize_selected_object


def test_stellarium_url_allows_local_and_private_hosts() -> None:
    assert normalize_base_url("localhost:8090") == "http://localhost:8090"
    assert normalize_base_url("http://127.0.0.1:8090") == "http://127.0.0.1:8090"
    assert normalize_base_url("http://192.168.0.15:8090") == "http://192.168.0.15:8090"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/test",
        "http://8.8.8.8:8090",
        "http://169.254.169.254:80",
        "http://example.com:8090",
        "http://127.0.0.1:8090/api/main/status",
        "http://user:pass@127.0.0.1:8090",
    ],
)
def test_stellarium_url_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(url)


def test_stellarium_selected_object_normalization() -> None:
    info = {
        "name": "M 42",
        "type": "Nebula",
        "ra": "05h 35m 17.3s",
        "dec": "-05° 23' 28\"",
        "altitude": "41.5°",
        "azimuth": "182.25°",
    }
    status = {
        "location": {
            "name": "School roof",
            "latitude": 37.7,
            "longitude": 128.26,
            "altitude": 50,
        },
        "time": {
            "local": "2026-07-20T21:15:00+09:00",
            "utc": "2026-07-20T12:15:00Z",
            "timeZone": "Asia/Seoul",
            "gmtShift": 9,
        },
    }
    result = normalize_selected_object(info, status)
    assert result["name"] == "M 42"
    assert result["target_mode"] == "extended"
    assert result["ra_deg"] == pytest.approx(83.822083, rel=1e-5)
    assert result["dec_deg"] == pytest.approx(-5.391111, rel=1e-5)
    assert result["alt_deg"] == pytest.approx(41.5)
    assert result["az_deg"] == pytest.approx(182.25)
    assert result["location"]["latitude"] == pytest.approx(37.7)
    assert result["time"]["time_zone"] == "Asia/Seoul"


def test_selected_object_includes_magnitude_and_size():
    from lightt.stellarium import normalize_selected_object

    payload = {
        "name": "Test Nebula",
        "object-type": "nebula",
        "vmag": 8.5,
        "size-dd": 1.25,
        "altitude": 30.0,
        "azimuth": 120.0,
    }
    result = normalize_selected_object(payload, {})
    assert result["target_mode"] == "extended"
    assert result["vmag"] == 8.5
    assert result["size_deg"] == 1.25


def test_target_mode_distinguishes_planet_and_asteroid():
    planet = normalize_selected_object({"name": "Jupiter", "type": "Planet", "altitude": 30, "azimuth": 180}, {})
    asteroid = normalize_selected_object({"name": "Ceres", "type": "Minor planet", "altitude": 30, "azimuth": 180}, {})
    assert planet["target_mode"] == "extended"
    assert asteroid["target_mode"] == "point"


def test_set_simulation_time_posts_julian_day(monkeypatch):
    from lightt import stellarium

    captured = {}

    def fake_post(base_url, endpoint, fields, timeout=3.0):
        captured.update({"base_url": base_url, "endpoint": endpoint, "fields": fields})
        return "ok"

    monkeypatch.setattr(stellarium, "post_form", fake_post)
    result = stellarium.set_simulation_time(
        "http://127.0.0.1:8090", "2026-08-20T07:00:00+00:00", pause=True
    )
    assert result["ok"] is True
    assert captured["endpoint"] == "/api/main/time"
    assert float(captured["fields"]["time"]) > 2_400_000
    assert float(captured["fields"]["timerate"]) == 0.0
