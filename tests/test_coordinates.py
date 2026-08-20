from __future__ import annotations

import pytest

from lightt.coordinates import resolve_target_altaz
from lightt.models import AnalysisSettings


def test_altaz_is_passed_through() -> None:
    settings = AnalysisSettings(
        current_exposure_sec=30,
        target_coordinate_mode="altaz",
        target_alt_deg=42.5,
        target_az_deg=123.4,
    )
    alt, az, info = resolve_target_altaz(settings)
    assert alt == 42.5
    assert az == 123.4
    assert info["mode"] == "altaz"


def test_radec_resolves_to_finite_altaz() -> None:
    pytest.importorskip("astropy")
    settings = AnalysisSettings(
        current_exposure_sec=30,
        target_coordinate_mode="radec",
        target_ra_deg=0.0,
        target_dec_deg=89.0,
        latitude=37.7,
        longitude=128.26,
        height_m=50,
        observation_time="2026-07-20T22:00:00",
        timezone="KST",
    )
    alt, az, info = resolve_target_altaz(settings)
    assert 0 <= alt <= 90
    assert 0 <= az < 360
    assert info["mode"] == "radec"
