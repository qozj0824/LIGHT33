from __future__ import annotations

from lightt.models import (
    AnalysisSettings,
    IntensityDomain,
    SaturationReport,
    SignalMeasurement,
)
from lightt.planning import build_exposure_plan


def domain(clip: float = 65535, *, confirmed: bool = True) -> IntensityDomain:
    return IntensityDomain(
        source_kind="fits",
        dtype="uint16",
        observed_min=0,
        observed_median=1676,
        observed_p999=12000,
        observed_max=min(clip, 64000),
        sensor_clip_adu=clip,
        saturation_threshold_adu=clip * 0.995,
        is_rendered=False,
        quantitative_saturation_supported=confirmed,
        clip_source="user_confirmed" if confirmed else "unverified_placeholder",
        clip_confidence="high" if confirmed else "unknown",
        requires_user_confirmation=not confirmed,
        warnings=[] if confirmed else ["센서 포화 ADU 확인 필요"],
    )


def measurement(current_snr: float = 70.64, signal: float = 316) -> SignalMeasurement:
    return SignalMeasurement(
        target_mode="extended",
        current_snr=current_snr,
        model_snr=max(current_snr, 0.01),
        signal_adu_per_pixel=signal,
        background_adu_per_pixel=1676,
        sensor_background_adu_per_pixel=1676,
        background_std_adu=214,
        target_std_adu=220,
        background_estimator_std_adu=2.5,
        effective_pixels=100,
        target_pixels=10_000,
        background_pixels=10_000,
    )


def saturation(reference: float = 57153.2, exact: bool = True) -> SaturationReport:
    return SaturationReport(
        threshold_adu=65207,
        saturated_pixel_count=4351,
        saturated_pixel_fraction=0.000168,
        connected_components=100,
        star_like_components=20,
        isolated_components=80,
        streak_components=0,
        largest_component_pixels=20,
        usable_unsaturated_star_count=245 if exact else 0,
        reference_peak_quantile=0.95 if exact else None,
        reference_peak_total_adu=reference if exact else None,
        exact_limit_available=exact,
        reason="ok" if exact else "비포화 기준별 부족",
    )


def test_realistic_16bit_plan_is_not_point_one_second() -> None:
    settings = AnalysisSettings(current_exposure_sec=180, target_snr=150)
    plan = build_exposure_plan(measurement(), saturation(), domain(), settings, 180)
    assert plan.status == "ok"
    assert plan.recommended_sub_exposure_sec is not None
    assert 100 <= plan.recommended_sub_exposure_sec <= 170
    assert plan.frames is not None and plan.frames < 30
    assert plan.confidence == "medium"
    assert any("Gain·읽기잡음" in warning for warning in plan.warnings)


def test_impossible_limit_returns_invalid_not_minimum_floor() -> None:
    settings = AnalysisSettings(current_exposure_sec=180, target_snr=150, min_sub_exposure_sec=1)
    plan = build_exposure_plan(
        measurement(), saturation(reference=64010), domain(255), settings, 180
    )
    assert plan.status == "invalid"
    assert plan.recommended_sub_exposure_sec is None
    assert plan.frames is None


def test_too_many_frames_are_not_presented() -> None:
    weak = measurement(current_snr=0.01, signal=0.001)
    settings = AnalysisSettings(current_exposure_sec=30, target_snr=300, max_recommended_frames=100)
    plan = build_exposure_plan(weak, saturation(), domain(), settings, 30)
    assert plan.status == "invalid"
    assert plan.frames is None


def test_unconfirmed_sensor_clip_blocks_plan() -> None:
    settings = AnalysisSettings(current_exposure_sec=30)
    plan = build_exposure_plan(measurement(), saturation(), domain(3000, confirmed=False), settings, 30)
    assert plan.status == "invalid"
    assert plan.limiting_constraint == "sensor_clip_unconfirmed"


def test_no_unsaturated_reference_stars_blocks_plan() -> None:
    settings = AnalysisSettings(current_exposure_sec=30)
    plan = build_exposure_plan(measurement(), saturation(exact=False), domain(), settings, 30)
    assert plan.status == "invalid"
    assert plan.limiting_constraint == "saturation_unresolved"


def test_confirmed_noise_parameters_still_downgrade_when_reference_frame_is_saturated() -> None:
    settings = AnalysisSettings(
        current_exposure_sec=180, target_snr=150, noise_parameters_confirmed=True
    )
    plan = build_exposure_plan(measurement(), saturation(), domain(), settings, 180)
    assert plan.status == "ok"
    assert plan.confidence == "medium"
    assert any("포화 픽셀" in warning for warning in plan.warnings)
