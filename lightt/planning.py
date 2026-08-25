from __future__ import annotations

import math

from .models import AnalysisSettings, ExposurePlan, IntensityDomain, SaturationReport, SignalMeasurement



def _signal_background_rates_e(
    measurement: SignalMeasurement,
    settings: AnalysisSettings,
    current_exposure_sec: float,
) -> tuple[float, float, float, int]:
    gain = settings.gain_e_per_adu
    n_pix = max(1, measurement.effective_pixels)
    if measurement.target_mode == "point":
        signal_adu = measurement.point_flux_adu or measurement.signal_adu_per_pixel
    else:
        signal_adu = measurement.signal_adu_per_pixel * n_pix
    signal_rate_e = max(signal_adu, 0.0) * gain / current_exposure_sec
    sky_rate_e = (
        max(measurement.background_adu_per_pixel - settings.bias_offset_adu, 0.0)
        * gain
        * n_pix
        / current_exposure_sec
    )
    dark_rate_e = settings.dark_current_e_per_pix_sec * n_pix
    # The uncertainty of the background mean is measured at the current exposure.
    # Treat its variance as exposure-proportional for planning. Spatial gradients are
    # reported separately and lower confidence; they are not detector shot noise.
    bg_estimator_variance_rate = (
        (measurement.background_estimator_std_adu * gain * n_pix) ** 2
        / max(current_exposure_sec, 1e-12)
    )
    return signal_rate_e, sky_rate_e + dark_rate_e, bg_estimator_variance_rate, n_pix



def _quality_factor(measurement: SignalMeasurement) -> float:
    if measurement.model_snr <= 0 or measurement.current_snr <= 0:
        return 1.0
    return float(min(1.0, max(0.05, measurement.current_snr / measurement.model_snr)))



def predict_single_snr(
    exposure_sec: float,
    measurement: SignalMeasurement,
    settings: AnalysisSettings,
    current_exposure_sec: float,
) -> float:
    if exposure_sec <= 0:
        return 0.0
    signal_rate, background_rate, bg_estimator_variance_rate, n_pix = _signal_background_rates_e(
        measurement, settings, current_exposure_sec
    )
    signal = signal_rate * exposure_sec
    variance = (
        signal
        + background_rate * exposure_sec
        + bg_estimator_variance_rate * exposure_sec
        + n_pix * settings.read_noise_e**2
    )
    ideal = signal / math.sqrt(variance) if variance > 0 else 0.0
    return ideal * _quality_factor(measurement)



def exposure_for_single_snr(
    target_snr: float,
    measurement: SignalMeasurement,
    settings: AnalysisSettings,
    current_exposure_sec: float,
) -> float | None:
    signal_rate, background_rate, bg_estimator_variance_rate, n_pix = _signal_background_rates_e(
        measurement, settings, current_exposure_sec
    )
    quality = _quality_factor(measurement)
    if signal_rate <= 0 or target_snr <= 0 or quality <= 0:
        return None
    ideal_target = target_snr / quality
    target2 = ideal_target**2
    total_linear_noise_rate = signal_rate + background_rate + bg_estimator_variance_rate
    a = signal_rate**2
    b = -target2 * total_linear_noise_rate
    c = -target2 * n_pix * settings.read_noise_e**2
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return None
    root = (-b + math.sqrt(discriminant)) / (2 * a)
    return float(root) if math.isfinite(root) and root > 0 else None



def _safe_round_down(seconds: float, minimum: float) -> float:
    if seconds <= minimum:
        return float(minimum)
    if seconds < 2:
        step = 0.1
    elif seconds < 10:
        step = 0.5
    elif seconds < 30:
        step = 1.0
    elif seconds < 120:
        step = 5.0
    elif seconds < 300:
        step = 10.0
    else:
        step = 30.0
    return float(max(minimum, math.floor(seconds / step + 1e-12) * step))


def _friendly_round_up(seconds: float, minimum: float) -> float:
    """Round a physical lower bound up without violating it."""
    if seconds <= minimum:
        return float(minimum)
    if seconds < 2:
        step = 0.1
    elif seconds < 10:
        step = 0.5
    elif seconds < 30:
        step = 1.0
    elif seconds < 120:
        step = 5.0
    elif seconds < 300:
        step = 10.0
    else:
        step = 30.0
    return float(max(minimum, math.ceil(seconds / step - 1e-12) * step))


def _exposure_efficiency_time(
    *,
    background_rate_e_per_pix: float,
    dark_current_e_per_pix_sec: float,
    read_noise_e: float,
    frame_overhead_sec: float,
    target_efficiency: float = 0.90,
) -> tuple[float, float]:
    """Shortest sub exposure meeting the information-rate efficiency target.

    For background-limited planning the relative useful-information rate is
    ``1 / ((1 + tau_read/t) * (1 + overhead/t))`` with
    ``tau_read = RN² / (sky + dark)``. The same detector-noise equation is used
    for every camera, target, and upload; no reference result is fitted here.
    """
    linear_rate = max(background_rate_e_per_pix + dark_current_e_per_pix_sec, 1e-12)
    read_noise_time = max(read_noise_e, 0.0) ** 2 / linear_rate
    overhead = max(frame_overhead_sec, 0.0)
    efficiency = min(0.99, max(0.50, target_efficiency))
    loss = 1.0 / efficiency - 1.0
    linear = read_noise_time + overhead
    discriminant = linear**2 + 4.0 * loss * read_noise_time * overhead
    exposure = (linear + math.sqrt(max(discriminant, 0.0))) / max(2.0 * loss, 1e-12)
    return float(exposure), float(read_noise_time)


def _exposure_efficiency(
    exposure_sec: float,
    *,
    read_noise_time_sec: float,
    frame_overhead_sec: float,
) -> float:
    if exposure_sec <= 0:
        return 0.0
    return float(
        1.0
        / (
            (1.0 + max(read_noise_time_sec, 0.0) / exposure_sec)
            * (1.0 + max(frame_overhead_sec, 0.0) / exposure_sec)
        )
    )



def _invalid_plan(
    *,
    measurement: SignalMeasurement,
    settings: AnalysisSettings,
    current_exposure_sec: float,
    limiting_constraint: str,
    warnings: list[str],
    sky_lower: float | None = None,
    background_upper: float | None = None,
    saturation_upper: float | None = None,
    practical_upper: float | None = None,
) -> ExposurePlan:
    return ExposurePlan(
        recommended_sub_exposure_sec=None,
        predicted_snr_per_sub=None,
        frames=None,
        total_integration_sec=None,
        total_elapsed_sec=None,
        exposure_for_single_frame_target_snr_sec=exposure_for_single_snr(
            settings.target_snr, measurement, settings, current_exposure_sec
        ),
        sky_limited_lower_sec=sky_lower,
        background_upper_sec=background_upper,
        saturation_upper_sec=saturation_upper,
        practical_upper_sec=practical_upper,
        limiting_constraint=limiting_constraint,
        status="invalid",
        confidence="none",
        warnings=warnings,
    )



def build_exposure_plan(
    measurement: SignalMeasurement,
    saturation: SaturationReport,
    domain: IntensityDomain,
    settings: AnalysisSettings,
    current_exposure_sec: float,
    *,
    sensor_bias_offset_adu: float | None = None,
) -> ExposurePlan:
    warnings: list[str] = []
    sensor_bias = settings.bias_offset_adu if sensor_bias_offset_adu is None else float(sensor_bias_offset_adu)
    if measurement.current_snr <= 0:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint="invalid_signal",
            warnings=["현재 SNR이 0 이하라 촬영 계획을 계산할 수 없습니다."],
        )

    if not domain.quantitative_saturation_supported and not settings.allow_unverified_saturation:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint="sensor_clip_unconfirmed",
            warnings=[
                "센서 포화 ADU가 확인되지 않았습니다. 카메라의 실제 clipping ADU를 입력하면 안전한 단일노출을 계산할 수 있습니다."
            ] + domain.warnings,
        )

    gain = settings.gain_e_per_adu
    calibrated_bg_rate_e_pix = (
        max(measurement.background_adu_per_pixel - settings.bias_offset_adu, 0.0)
        * gain
        / current_exposure_sec
    ) + settings.dark_current_e_per_pix_sec
    sky_lower = (
        5.0 * settings.read_noise_e**2 / calibrated_bg_rate_e_pix
        if calibrated_bg_rate_e_pix > 0
        else None
    )

    background_upper = None
    sensor_background = measurement.sensor_background_adu_per_pixel
    if sensor_background is not None and sensor_background > sensor_bias:
        raw_bg_rate = (sensor_background - sensor_bias) / current_exposure_sec
        background_threshold = domain.sensor_clip_adu * settings.background_limit_fraction
        if raw_bg_rate > 0 and background_threshold > sensor_bias:
            background_upper = (background_threshold - sensor_bias) / raw_bg_rate
    else:
        warnings.append(
            "원본 센서 ADU에서 배경을 측정하지 못해 배경 포화 상한은 사용하지 않았습니다."
        )

    saturation_upper = None
    if saturation.exact_limit_available and saturation.reference_peak_total_adu is not None:
        peak_rate = max(saturation.reference_peak_total_adu - sensor_bias, 0.0) / current_exposure_sec
        safe_threshold = domain.sensor_clip_adu * settings.saturation_safety_fraction
        if peak_rate > 0 and safe_threshold > sensor_bias:
            saturation_upper = (safe_threshold - sensor_bias) / peak_rate
    elif not settings.allow_unverified_saturation:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint="saturation_unresolved",
            warnings=[saturation.reason, "비포화 기준별을 충분히 확보한 더 짧은 시험 영상을 사용하세요."],
            sky_lower=sky_lower,
            background_upper=background_upper,
        )
    else:
        warnings.append("사용자 선택으로 검증되지 않은 포화 제한 없이 계산합니다.")
        warnings.append(saturation.reason)

    upper_candidates: list[tuple[str, float]] = [("user_max", settings.max_sub_exposure_sec)]
    if settings.tracking_limit_sec > 0:
        upper_candidates.append(("tracking", settings.tracking_limit_sec))
    if background_upper is not None and math.isfinite(background_upper) and background_upper > 0:
        upper_candidates.append(("background", background_upper))
    if saturation_upper is not None and math.isfinite(saturation_upper) and saturation_upper > 0:
        upper_candidates.append(("saturation", saturation_upper))
    limiting_constraint, practical_upper = min(upper_candidates, key=lambda pair: pair[1])

    if practical_upper < settings.min_sub_exposure_sec:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint=limiting_constraint,
            warnings=warnings + [
                f"안전 상한({practical_upper:.3f}초)이 최소 노출({settings.min_sub_exposure_sec:.3f}초)보다 짧습니다. "
                "ADU 단위, 포화값, 시험 영상의 밝기를 확인하세요."
            ],
            sky_lower=sky_lower,
            background_upper=background_upper,
            saturation_upper=saturation_upper,
            practical_upper=practical_upper,
        )

    efficiency_target = 0.90
    efficiency_lower, read_noise_time = _exposure_efficiency_time(
        background_rate_e_per_pix=calibrated_bg_rate_e_pix,
        dark_current_e_per_pix_sec=0.0,
        read_noise_e=settings.read_noise_e,
        frame_overhead_sec=settings.frame_overhead_sec,
        target_efficiency=efficiency_target,
    )
    physical_lower = max(sky_lower or settings.min_sub_exposure_sec, efficiency_lower)
    if physical_lower <= practical_upper:
        recommended = min(
            practical_upper,
            _friendly_round_up(physical_lower, settings.min_sub_exposure_sec),
        )
        limiting_constraint = "exposure_efficiency_target"
    else:
        recommended = min(
            practical_upper,
            _safe_round_down(practical_upper, settings.min_sub_exposure_sec),
        )
        warnings.append(
            "포화·추적·사용자 상한이 읽기잡음과 프레임 오버헤드를 합친 "
            "90% 정보효율 하한보다 짧아 안전 상한을 우선했습니다."
        )
    achieved_efficiency = _exposure_efficiency(
        recommended,
        read_noise_time_sec=read_noise_time,
        frame_overhead_sec=settings.frame_overhead_sec,
    )
    if achieved_efficiency >= efficiency_target:
        warnings.append(
            f"권장 노출은 배경·다크·읽기잡음·프레임 오버헤드 모델의 "
            f"장노출 대비 정보효율 {achieved_efficiency:.1%}를 만족하는 가장 짧은 실용값입니다."
        )
    if recommended <= 0:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint=limiting_constraint,
            warnings=warnings + ["권장 노출 계산이 유효하지 않습니다."],
            sky_lower=sky_lower,
            background_upper=background_upper,
            saturation_upper=saturation_upper,
            practical_upper=practical_upper,
        )

    snr_sub = predict_single_snr(recommended, measurement, settings, current_exposure_sec)
    if snr_sub <= 0:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint=limiting_constraint,
            warnings=warnings + ["권장 노출의 예측 SNR이 0입니다."],
            sky_lower=sky_lower,
            background_upper=background_upper,
            saturation_upper=saturation_upper,
            practical_upper=practical_upper,
        )

    effective_snr_per_frame = snr_sub * settings.stack_efficiency
    frames = max(1, int(math.ceil((settings.target_snr / max(effective_snr_per_frame, 1e-12)) ** 2)))
    single_target = exposure_for_single_snr(
        settings.target_snr, measurement, settings, current_exposure_sec
    )
    if frames > settings.max_recommended_frames:
        return _invalid_plan(
            measurement=measurement,
            settings=settings,
            current_exposure_sec=current_exposure_sec,
            limiting_constraint=limiting_constraint,
            warnings=warnings + [
                f"필요 프레임 수가 {frames:,}장으로 설정 한계 {settings.max_recommended_frames:,}장을 초과합니다. "
                "목표 SNR, 측정 영역, 시험 영상 또는 장비 설정을 다시 확인하세요."
            ],
            sky_lower=sky_lower,
            background_upper=background_upper,
            saturation_upper=saturation_upper,
            practical_upper=practical_upper,
        )

    total_integration = frames * recommended
    total_elapsed = frames * (recommended + settings.frame_overhead_sec)
    confidence = "high"
    if domain.clip_confidence != "high" or saturation.usable_unsaturated_star_count < 10:
        confidence = "medium"
    if not settings.noise_parameters_confirmed and confidence == "high":
        confidence = "medium"
        warnings.append("Gain·읽기잡음이 기본값이므로 SNR과 필요 장수는 참고값입니다.")
    if settings.allow_unverified_saturation:
        confidence = "low"
    if measurement.spatial_contrast_snr is not None:
        if measurement.spatial_contrast_snr < 5:
            confidence = "low"
            warnings.append("대상과 배경의 공간적 대비가 불안정합니다. ROI 또는 배경 모델을 다시 확인하세요.")
        elif measurement.spatial_contrast_snr < 10 and confidence == "high":
            confidence = "medium"
    if saturation.saturated_pixel_fraction > 0:
        if confidence == "high":
            confidence = "medium"
        warnings.append(
            "기준 영상에 포화 픽셀이 있습니다. 비포화 별 분포로 상한을 추정했지만 더 짧은 시험 영상으로 확인하는 것이 안전합니다."
        )
    warnings.append(
        f"스택 계산은 프레임 독립성을 가정하고 실제 효율 {settings.stack_efficiency:.0%}를 적용했습니다."
    )
    return ExposurePlan(
        recommended_sub_exposure_sec=float(recommended),
        predicted_snr_per_sub=float(snr_sub),
        frames=frames,
        total_integration_sec=float(total_integration),
        total_elapsed_sec=float(total_elapsed),
        exposure_for_single_frame_target_snr_sec=single_target,
        sky_limited_lower_sec=sky_lower,
        background_upper_sec=background_upper,
        saturation_upper_sec=saturation_upper,
        practical_upper_sec=practical_upper,
        limiting_constraint=limiting_constraint,
        status="ok",
        confidence=confidence,
        warnings=warnings,
    )
