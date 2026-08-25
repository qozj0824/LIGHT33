from __future__ import annotations

import math

from lightt.equipment import EquipmentProfile, airmass_from_altitude
from lightt.models import ImageMetadata
from lightt.session import _background_rate, _build_plan, _signal_model


def profile(**overrides):
    base = dict(
        profile_id="abc123",
        name="test",
        created_at="2026-08-18T00:00:00+00:00",
        gain_e_per_adu=1.0,
        read_noise_e=3.0,
        dark_current_e_per_pix_sec=0.01,
        bias_offset_adu=0.0,
        sensor_clip_adu=65535.0,
        pixel_scale_arcsec=2.0,
        extinction_k_mag_per_airmass=0.2,
        reference_airmass=1.2,
        reference_background_adu_per_pix_sec=10.0,
        reference_peak_e_per_sec=200.0,
        reference_aperture_pixels=25,
        photometric_zero_point_mag=20.0,
        zero_point_quality="good",
        c_sys=2.0,
        c_sys_quality="good",
        reference_allsky_camera="Canon EOS R",
        reference_allsky_gain_setting=400.0,
        reference_allsky_source_type="raw",
        reference_allsky_dtype="uint16",
        reference_allsky_width=3360,
        reference_allsky_height=2240,
        reference_allsky_flat_applied=False,
        confidence="high",
    )
    base.update(overrides)
    return EquipmentProfile(**base)


def allsky_metadata(**overrides):
    base = dict(
        filename="sky.cr3",
        source_type="raw",
        width=3360,
        height=2240,
        dtype="uint16",
        exposure_sec=30.0,
        camera="Canon EOS R",
        gain_setting=400.0,
    )
    base.update(overrides)
    return ImageMetadata(**base)


def test_airmass_decreases_with_altitude():
    low = airmass_from_altitude(20)
    high = airmass_from_altitude(70)
    assert low is not None and high is not None
    assert low > high > 1


def test_csys_background_conversion_uses_current_allsky_rate():
    p = profile(c_sys=2.5)
    rate, method, warnings = _background_rate(
        p, allsky_target_adu=1200, allsky_median_adu=1000, allsky_exposure_sec=30,
        metadata=allsky_metadata(), current_flat_applied=False
    )
    assert method == "c_sys"
    assert warnings == []
    assert math.isclose(rate, 100.0)


def test_relative_background_fallback_is_flagged():
    p = profile(c_sys=None, reference_background_adu_per_pix_sec=20)
    rate, method, warnings = _background_rate(
        p, allsky_target_adu=1500, allsky_median_adu=1000, allsky_exposure_sec=30,
        metadata=allsky_metadata(), current_flat_applied=False
    )
    assert method == "relative_fallback"
    assert math.isclose(rate, 30.0)
    assert warnings


def test_point_target_signal_uses_catalog_magnitude_and_zero_point():
    p = profile(photometric_zero_point_mag=20.0, reference_airmass=1.0, extinction_k_mag_per_airmass=0.0)
    target = {"target_mode": "point", "vmag": 15.0, "size_deg": 0.0, "alt_deg": 60.0}
    total, per_pixel, source, warnings, diag = _signal_model(p, target, 25, None, None)
    assert source == "catalog_magnitude"
    assert warnings == []
    assert total is not None and math.isclose(total, 100.0, rel_tol=1e-8)
    assert per_pixel is not None and math.isclose(per_pixel, 4.0, rel_tol=1e-8)
    assert diag["target_mag"] == 15.0


def test_extended_target_uses_integrated_mag_and_size():
    p = profile(photometric_zero_point_mag=20.0, pixel_scale_arcsec=2.0, reference_airmass=1.0, extinction_k_mag_per_airmass=0.0)
    target = {"target_mode": "extended", "vmag": 10.0, "size_deg": 1.0, "alt_deg": 60.0}
    total, per_pixel, source, warnings, diag = _signal_model(p, target, 100, None, None)
    assert source == "integrated_mag_plus_size"
    assert total is not None and total > 0
    assert per_pixel is not None and per_pixel > 0
    assert warnings
    assert "mean_surface_brightness_mag_arcsec2" in diag


def test_target_snr_changes_frames_not_single_exposure_when_constraints_same():
    p = profile()
    target = {"target_mode": "point"}
    low = _build_plan(
        profile=p,
        target=target,
        background_rate_adu_per_pix=10.0,
        target_signal_rate_e=50.0,
        effective_pixels=25,
        target_snr=50.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=600.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    high = _build_plan(
        profile=p,
        target=target,
        background_rate_adu_per_pix=10.0,
        target_signal_rate_e=50.0,
        effective_pixels=25,
        target_snr=150.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=600.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    assert low["recommended_sub_exposure_sec"] == high["recommended_sub_exposure_sec"]
    assert low["frames"] is not None and high["frames"] is not None
    assert high["frames"] > low["frames"]


def test_session_plan_honors_110_second_user_max_above_sky_lower():
    p = profile(
        gain_e_per_adu=1.25,
        read_noise_e=2.7,
        dark_current_e_per_pix_sec=0.0,
        reference_peak_e_per_sec=521.0,
    )
    result = _build_plan(
        profile=p,
        target={"target_mode": "extended"},
        background_rate_adu_per_pix=0.595452380173163,
        target_signal_rate_e=100.0,
        effective_pixels=100,
        target_snr=150.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=110.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=2000,
        frame_overhead_sec=2.0,
    )
    assert 48.0 < result["sky_limited_lower_sec"] < 50.0
    assert result["recommended_sub_exposure_sec"] == 110.0
    assert result["practical_upper_sec"] == 110.0
    assert result["limiting_constraint"] == "user_max"
    assert result["constraint_inputs"]["max_sub_exposure_sec"] == 110.0
    assert result["constraint_status"] == "sky_limited"


def test_session_plan_changes_when_user_max_changes():
    p = profile(reference_peak_e_per_sec=None)
    common = dict(
        profile=p,
        target={"target_mode": "extended"},
        background_rate_adu_per_pix=1.0,
        target_signal_rate_e=30.0,
        effective_pixels=100,
        target_snr=100.0,
        min_sub_exposure_sec=1.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    short = _build_plan(max_sub_exposure_sec=110.0, **common)
    long = _build_plan(max_sub_exposure_sec=600.0, **common)
    assert short["recommended_sub_exposure_sec"] == 110.0
    assert long["recommended_sub_exposure_sec"] == 600.0


def test_session_plan_reports_read_noise_compromise_without_crashing():
    p = profile(read_noise_e=12.0, reference_peak_e_per_sec=None)
    result = _build_plan(
        profile=p,
        target={"target_mode": "extended"},
        background_rate_adu_per_pix=0.05,
        target_signal_rate_e=10.0,
        effective_pixels=100,
        target_snr=50.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=30.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    assert result["recommended_sub_exposure_sec"] == 30.0
    assert result["constraint_status"] == "upper_bound_compromise"
    assert result["sky_limited_feasible"] is False


def test_non_v_broadband_filter_is_marked_approximate():
    p = profile(filter_name="I_BESS", photometric_zero_point_mag=20.0, reference_airmass=1.0)
    target = {"target_mode": "point", "vmag": 12.0, "size_deg": 0.0, "alt_deg": 60.0}
    total, per_pixel, source, warnings, diagnostics = _signal_model(p, target, 25, None, None)
    assert total is not None and per_pixel is not None
    assert source == "catalog_magnitude_filter_mismatch_approximation"
    assert diagnostics["filter_v_band_match"] is False
    assert any("같은 대역" in item for item in warnings)


def test_csys_is_not_used_when_current_allsky_gain_differs():
    p = profile(c_sys=2.5, reference_background_adu_per_pix_sec=20)
    rate, method, warnings = _background_rate(
        p, allsky_target_adu=1500, allsky_median_adu=1000, allsky_exposure_sec=30,
        metadata=allsky_metadata(gain_setting=800.0), current_flat_applied=False
    )
    assert method == "relative_fallback"
    assert math.isclose(rate, 30.0)
    assert any("ISO/Gain" in item for item in warnings)


def test_point_target_saturation_uses_psf_peak_fraction():
    p = profile(reference_psf_peak_fraction=0.2, reference_peak_e_per_sec=None)
    target = {"target_mode": "point"}
    result = _build_plan(
        profile=p, target=target, background_rate_adu_per_pix=10.0,
        target_signal_rate_e=1000.0, effective_pixels=25, target_snr=50.0,
        min_sub_exposure_sec=1.0, max_sub_exposure_sec=600.0,
        tracking_limit_sec=0.0, background_limit_fraction=0.30,
        saturation_safety_fraction=0.80, stack_efficiency=0.90,
        max_frames=100000, frame_overhead_sec=2.0,
    )
    assert result["target_saturation_upper_sec"] is not None
    assert result["limiting_constraint"] in {"target_saturation", "background", "user_max"}


def test_equipment_profile_persists_reference_image(tmp_path):
    import numpy as np
    from PIL import Image
    from lightt.equipment import create_equipment_profile, load_profile

    yy, xx = np.indices((192, 192))
    data = np.full((192, 192), 1000.0)
    for cx, cy, amp in [(96, 96, 10000), (50, 60, 5000), (150, 140, 6000)]:
        data += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.0**2))
    path = tmp_path / "reference.tiff"
    Image.fromarray(np.clip(data, 0, 65535).astype(np.uint16)).save(path)

    profile = create_equipment_profile(
        profile_root=tmp_path / "profiles",
        profile_name="saved rig",
        scope_path=path,
        reference_target={
            "name": "reference star", "object_type": "star", "target_mode": "point",
            "vmag": 10.0, "alt_deg": 60.0, "az_deg": 100.0,
        },
        project_root=tmp_path,
        gain_e_per_adu=1.0,
        read_noise_e=3.0,
        sensor_clip_adu=65535.0,
        scope_exposure_sec=30.0,
    )
    loaded = load_profile(tmp_path / "profiles", profile.profile_id)
    assert loaded.name == "saved rig"
    assert loaded.photometric_zero_point_mag is not None
    assert loaded.confidence == "low"
    assert any("센서 물성값" in item for item in loaded.warnings)
    assert (tmp_path / "profiles" / profile.profile_id / "reference_scope.tiff").exists()


def test_plan_confidence_never_upgrades_low_profile():
    p = profile(confidence="low", c_sys_quality="planning")
    result = _build_plan(
        profile=p,
        target={"target_mode": "point"},
        background_rate_adu_per_pix=10.0,
        target_signal_rate_e=50.0,
        effective_pixels=25,
        target_snr=50.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=600.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    assert result["confidence"] == "low"


def test_saved_profile_validation_disables_bad_optional_values():
    from lightt.equipment import validate_equipment_profile

    p = profile(
        c_sys=-1.0,
        c_sys_quality="good",
        sensor_clip_adu=-5.0,
        pixel_scale_arcsec=0.0,
    )
    validated = validate_equipment_profile(p)
    assert validated.c_sys is None
    assert validated.c_sys_quality == "unavailable"
    assert validated.sensor_clip_adu is None
    assert validated.pixel_scale_arcsec is None
    assert validated.warnings


def test_saved_profile_validation_repairs_corrupt_schema_and_bounded_values():
    from lightt.equipment import PROFILE_SCHEMA_VERSION, validate_equipment_profile

    p = profile(
        schema_version="broken",
        extinction_k_mag_per_airmass=-2.0,
        reference_psf_peak_fraction=1.4,
    )
    validated = validate_equipment_profile(p)
    assert validated.schema_version == PROFILE_SCHEMA_VERSION
    assert math.isclose(validated.extinction_k_mag_per_airmass, 0.20)
    assert validated.reference_psf_peak_fraction is None
    assert any("형식 번호" in warning for warning in validated.warnings)
    assert any("PSF peak" in warning for warning in validated.warnings)


def test_session_time_alignment_accepts_close_times():
    from lightt.session import _time_alignment

    delta, notes = _time_alignment(
        target_time_utc="2026-08-18T07:00:00Z",
        allsky_date_obs="2026-08-18T07:05:00",
        allsky_source_type="fits",
    )
    assert delta == 5.0
    assert notes == []


def test_session_time_alignment_blocks_large_mismatch():
    from lightt.session import _time_alignment
    import pytest

    with pytest.raises(ValueError, match="30"):
        _time_alignment(
            target_time_utc="2026-08-18T07:00:00Z",
            allsky_date_obs="2026-08-18T08:00:01Z",
            allsky_source_type="raw",
        )


def test_session_time_alignment_does_not_guess_rendered_exif_timezone():
    from lightt.session import _time_alignment

    delta, notes = _time_alignment(
        target_time_utc="2026-08-18T07:00:00Z",
        allsky_date_obs="2026:08:18 16:00:00",
        allsky_source_type="rendered",
    )
    assert delta is None
    assert notes


def test_fixed_allsky_calibration_rejects_wrong_stellarium_location():
    import pytest
    from lightt.session import _location_alignment

    target = {"latitude": 37.5, "longitude": 127.0}
    with pytest.raises(ValueError, match="관측소 위도"):
        _location_alignment(
            target,
            allsky_metadata(),
            tracking_site_latitude_deg=-24.6276,
        )


def test_reference_image_star_peak_is_diagnostic_not_future_scene_hard_limit():
    p = profile(reference_peak_e_per_sec=50000.0, reference_psf_peak_fraction=None)
    result = _build_plan(
        profile=p,
        target={"target_mode": "extended"},
        background_rate_adu_per_pix=2.0,
        target_signal_rate_e=20.0,
        effective_pixels=100,
        target_snr=50.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=600.0,
        tracking_limit_sec=0.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=100000,
        frame_overhead_sec=2.0,
    )
    assert result["reference_star_saturation_diagnostic_sec"] is not None
    assert result["limiting_constraint"] != "reference_star_saturation"
    assert result["recommended_sub_exposure_sec"] is not None
    assert any("강제 상한" in item for item in result["warnings"])


def test_narrowband_catalog_signal_is_explicitly_marked_approximate():
    p = profile(filter_name="H-alpha 7nm", photometric_zero_point_mag=20.0, reference_airmass=1.0)
    target = {"target_mode": "point", "vmag": 12.0, "size_deg": 0.0, "alt_deg": 60.0}
    total, per_pixel, source, warnings, _ = _signal_model(p, target, 25, None, None)
    assert total is not None and per_pixel is not None
    assert source == "catalog_magnitude_narrowband_approximation"
    assert any("협대역" in item for item in warnings)


def test_reference_epoch_mismatch_no_longer_blocks_basic_profile(monkeypatch):
    from lightt.equipment import _prepare_reference_target_for_capture
    from lightt.models import ImageFrame, ImageMetadata
    import numpy as np

    frame = ImageFrame(
        intensity=np.ones((8, 8), dtype=np.float32),
        metadata=ImageMetadata(
            filename="old.fit",
            source_type="fits",
            width=8,
            height=8,
            dtype="float32",
            date_obs="2025-04-15T12:00:00",
        ),
    )
    target = {
        "name": "old target",
        "object_type": "star",
        "target_mode": "point",
        "alt_deg": 70.0,
        "az_deg": 100.0,
        "ra_deg": None,
        "dec_deg": None,
        "observation_time_utc": "2026-08-20T07:00:00Z",
        "latitude": None,
        "longitude": None,
    }
    warnings = []
    prepared, delta, source, capture = _prepare_reference_target_for_capture(target, frame, warnings)
    assert delta is not None and delta > 30
    assert source == "stale_altaz_discarded"
    assert prepared["alt_deg"] is None
    assert prepared["az_deg"] is None
    assert capture is not None
    assert any("프로필은 저장" in item for item in warnings)


def test_airmass_deneb_validation_value():
    # Validation sample from the NØXIS UI: altitude 41.37032161363435 deg.
    value = airmass_from_altitude(41.37032161363435)
    assert math.isclose(value, 1.51095, abs_tol=5e-5)
