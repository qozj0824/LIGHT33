from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .equipment import EquipmentProfile, airmass_from_altitude
from .geometry import load_fisheye_config, validate_fisheye_directional_calibration
from .io import apply_calibration, load_image
from .models import AnalysisSettings, CalibrationSet, ImageMetadata
from .planning import _safe_round_down
from .sky import build_sky_map
from .visualization import save_exposure_snr_curve
from .time_utils import observation_time_difference_minutes


def _finite(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _target_payload(target: dict[str, Any]) -> dict[str, Any]:
    mode = str(target.get("target_mode") or "extended")
    if mode not in {"point", "extended"}:
        mode = "extended"
    return {
        "name": str(target.get("name") or "선택 천체"),
        "object_type": str(target.get("object_type") or "unknown"),
        "target_mode": mode,
        "vmag": _finite(target.get("vmag")),
        "vmage": _finite(target.get("vmage")),
        "size_deg": _finite(target.get("size_deg")),
        "alt_deg": _finite(target.get("alt_deg")),
        "az_deg": _finite(target.get("az_deg")),
        "ra_deg": _finite(target.get("ra_deg")),
        "dec_deg": _finite(target.get("dec_deg")),
        "observation_time_utc": str(target.get("observation_time_utc") or "") or None,
        "observation_time_local": str(target.get("observation_time_local") or "") or None,
        "latitude": _finite(target.get("latitude")),
        "longitude": _finite(target.get("longitude")),
    }




def _is_narrowband_filter(name: str | None) -> bool:
    text = (name or "").strip().casefold().replace("α", "a")
    if not text:
        return False
    tokens = (
        "ha", "h-alpha", "halpha", "oiii", "o3", "sii", "s2",
        "narrow", "dual band", "dualband", "tri band", "triband",
        "l-extreme", "l-ultimate", "nbz", "uhc",
    )
    return any(token in text for token in tokens)


def _target_scope_flags(target: dict[str, Any]) -> tuple[bool, bool]:
    """Return (unsupported, limited_validation) for the report-aligned model.

    The research and validation material is centered on deep-sky imaging.  The Sun is
    deliberately rejected; planets/Moon/comets may still be diagnosed but are marked
    outside the validated deep-sky signal model.
    """
    text = f"{target.get('name', '')} {target.get('object_type', '')}".casefold()
    if "sun" in text or "태양" in text:
        return True, True
    limited_tokens = ("planet", "moon", "comet", "asteroid", "minor planet", "행성", "달", "혜성")
    return False, any(token in text for token in limited_tokens)


def _time_alignment(
    *,
    target_time_utc: str | None,
    allsky_date_obs: str | None,
    allsky_source_type: str,
) -> tuple[float | None, list[str]]:
    """Validate that the Stellarium Alt/Az refers to the all-sky capture epoch."""
    notes: list[str] = []
    delta_min = observation_time_difference_minutes(
        target_time_utc,
        allsky_date_obs,
        first_assume_utc_if_naive=True,
        second_assume_utc_if_naive=(allsky_source_type == "fits"),
    )
    if delta_min is None:
        if not target_time_utc:
            notes.append("Stellarium 기준 시각을 확인하지 못해 선택 천체 좌표와 전천 촬영시각의 일치 여부를 검증하지 못했습니다.")
        else:
            notes.append("전천 영상의 촬영시각 또는 시간대를 확인하지 못해 Stellarium 좌표와의 시간 일치 여부를 검증하지 못했습니다.")
        return None, notes
    if delta_min > 30.0:
        raise ValueError(
            f"Stellarium 기준 시각과 전천 영상 촬영시각이 {delta_min:.1f}분 차이납니다(허용 기준 30분). "
            "Stellarium 시각을 전천 영상 촬영시각에 맞춘 뒤 천체를 다시 가져오세요."
        )
    if delta_min > 10.0:
        notes.append(
            f"Stellarium 기준 시각과 전천 촬영시각이 {delta_min:.1f}분 차이납니다. "
            "천체의 고도·방위각이 빠르게 변하는 경우 방향별 배경 조회 오차가 커질 수 있습니다."
        )
    return delta_min, notes

def _allsky_profile_compatibility(
    profile: EquipmentProfile,
    metadata: ImageMetadata,
    *,
    current_flat_applied: bool,
) -> tuple[bool, bool, list[str]]:
    """Check whether a stored Csys is transferable to the current all-sky image."""
    incompatible: list[str] = []
    unknown: list[str] = []
    if profile.reference_allsky_camera and metadata.camera:
        if profile.reference_allsky_camera.strip().casefold() != metadata.camera.strip().casefold():
            incompatible.append("전천 카메라가 장비 프로필의 Csys 기준 카메라와 다릅니다.")
    elif profile.reference_allsky_camera or metadata.camera:
        unknown.append("전천 카메라 일치 여부를 완전히 확인하지 못했습니다.")

    ref_gain = profile.reference_allsky_gain_setting
    cur_gain = metadata.gain_setting
    if ref_gain is not None and cur_gain is not None and ref_gain > 0:
        if abs(cur_gain - ref_gain) / ref_gain > 0.02:
            incompatible.append("전천 영상의 ISO/Gain이 Csys 기준 영상과 다릅니다.")
    elif ref_gain is not None or cur_gain is not None:
        unknown.append("전천 ISO/Gain 일치 여부를 완전히 확인하지 못했습니다.")

    if profile.reference_allsky_width and profile.reference_allsky_height:
        width_ratio = abs(metadata.width - profile.reference_allsky_width) / max(profile.reference_allsky_width, 1)
        height_ratio = abs(metadata.height - profile.reference_allsky_height) / max(profile.reference_allsky_height, 1)
        if width_ratio > 0.02 or height_ratio > 0.02:
            incompatible.append("전천 영상의 센서 크기/크롭이 Csys 기준 영상과 다릅니다.")
    else:
        unknown.append("Csys 기준 전천 영상의 크기 정보가 없습니다.")

    if bool(profile.reference_allsky_flat_applied) != bool(current_flat_applied):
        incompatible.append("현재 전천 영상과 Csys 기준 전천 영상의 Flat 적용 상태가 다릅니다.")

    return not incompatible, not unknown, incompatible + unknown


def _background_rate(
    profile: EquipmentProfile,
    allsky_target_adu: float,
    allsky_median_adu: float,
    allsky_exposure_sec: float,
    metadata: ImageMetadata,
    *,
    current_flat_applied: bool,
) -> tuple[float, str, list[str]]:
    warnings: list[str] = []
    allsky_rate = max(allsky_target_adu, 0.0) / allsky_exposure_sec
    compatible, fully_verified, compatibility_notes = _allsky_profile_compatibility(
        profile, metadata, current_flat_applied=current_flat_applied
    )
    if profile.c_sys is not None and profile.c_sys > 0 and compatible:
        if compatibility_notes:
            warnings.extend(compatibility_notes)
        method = "c_sys" if (fully_verified and profile.c_sys_quality == "good") else "c_sys_planning"
        if method != "c_sys":
            warnings.append("Csys는 적용했지만 전천 장비 설정의 일치 여부가 완전히 검증되지 않아 계획용으로 표시합니다.")
        return float(profile.c_sys * allsky_rate), method, warnings

    if profile.c_sys is not None and profile.c_sys > 0 and not compatible:
        warnings.extend(compatibility_notes)
        warnings.append("현재 전천 영상 조건이 Csys 기준과 달라 절대 Csys 환산을 사용하지 않았습니다.")

    reference = profile.reference_background_adu_per_pix_sec
    relative = allsky_target_adu / allsky_median_adu if allsky_median_adu > 0 else 1.0
    if reference is None or reference <= 0:
        raise ValueError("장비 프로필에 망원경 기준 배경률이 없어 현재 하늘 배경을 환산할 수 없습니다.")
    warnings.append(
        "Csys 대신 현재 전천 영상 내부의 방향별 상대 밝기를 저장된 망원경 기준 배경률에 적용했습니다. "
        "이 값은 관측 계획용 근사값입니다."
    )
    return float(reference * relative), "relative_fallback", warnings


def _signal_model(
    profile: EquipmentProfile,
    target: dict[str, Any],
    effective_pixels: int,
    manual_target_mag: float | None,
    manual_surface_brightness_mag_arcsec2: float | None,
) -> tuple[float | None, float | None, str, list[str], dict[str, Any]]:
    warnings: list[str] = []
    narrowband = _is_narrowband_filter(profile.filter_name)
    if narrowband:
        warnings.append(
            "장비 프로필의 필터가 협대역/다중대역으로 보입니다. Stellarium V등급은 해당 통과대역의 "
            "실제 광자율을 직접 나타내지 않으므로 대상 신호와 SNR은 계획용 근사로만 해석합니다."
        )
    zero_point = profile.photometric_zero_point_mag
    if zero_point is None:
        return None, None, "unavailable", [
            "장비 프로필에 기기영점이 없어 선택 천체의 절대 신호를 예측할 수 없습니다. "
            "기준 천체 등급이 있는 영상으로 장비 프로필을 다시 보정하세요."
        ], {}
    current_airmass = airmass_from_altitude(target["alt_deg"])
    reference_airmass = profile.reference_airmass
    extinction_factor = 1.0
    if current_airmass is not None and reference_airmass is not None:
        delta_x = current_airmass - reference_airmass
        extinction_factor = 10 ** (-0.4 * profile.extinction_k_mag_per_airmass * delta_x)
    elif current_airmass is None:
        warnings.append("대상 고도가 없어 대기소광 차이를 적용하지 않았습니다.")

    mode = target["target_mode"]
    magnitude = manual_target_mag if manual_target_mag is not None else target["vmag"]
    magnitude_source = "manual" if manual_target_mag is not None else "vmag"
    if magnitude is None and target.get("vmage") is not None and current_airmass is not None:
        # Stellarium's extincted magnitude can be converted back to an approximate
        # catalogue-like magnitude using the profile extinction coefficient.
        magnitude = float(target["vmage"]) - profile.extinction_k_mag_per_airmass * current_airmass
        magnitude_source = "vmage_extinction_reversed"
        warnings.append(
            "카탈로그 V등급이 없어 Stellarium의 대기소광 후 등급에서 장비 프로필 kX를 되돌린 근사 등급을 사용했습니다."
        )
    size_deg = target["size_deg"]
    diagnostics: dict[str, Any] = {
        "zero_point_mag": zero_point,
        "current_airmass": current_airmass,
        "reference_airmass": reference_airmass,
        "extinction_factor": extinction_factor,
        "magnitude_source": magnitude_source,
    }
    if mode == "point":
        if magnitude is None:
            return None, None, "unavailable", warnings + [
                "Stellarium에서 선택 천체의 V등급을 받지 못했습니다. 상세 설정에서 대상 등급을 입력하세요."
            ], diagnostics
        total_rate = 10 ** (0.4 * (zero_point - magnitude)) * extinction_factor
        n_pix = max(1, effective_pixels or profile.reference_aperture_pixels or 25)
        per_pixel = total_rate / n_pix
        diagnostics.update({"target_mag": magnitude, "total_signal_e_per_sec": total_rate, "n_pix": n_pix})
        source_name = "catalog_magnitude_narrowband_approximation" if narrowband else "catalog_magnitude"
        return float(total_rate), float(per_pixel), source_name, warnings, diagnostics

    # Extended targets are best modeled with surface brightness.  If the user supplies
    # mu directly, use it.  Otherwise approximate a mean surface brightness from the
    # integrated V magnitude and Stellarium angular diameter.
    mu = manual_surface_brightness_mag_arcsec2
    source = "manual_surface_brightness"
    if mu is None:
        if magnitude is None or size_deg is None or size_deg <= 0:
            return None, None, "unavailable", warnings + [
                "확산천체의 평균 표면밝기를 계산하려면 V등급과 각크기가 모두 필요합니다. "
                "Stellarium 정보가 부족하면 상세 설정에 표면밝기를 입력하세요."
            ], diagnostics
        area_arcsec2 = math.pi * (size_deg * 3600.0 / 2.0) ** 2
        mu = magnitude + 2.5 * math.log10(max(area_arcsec2, 1e-12))
        source = "integrated_mag_plus_size_narrowband_approximation" if narrowband else "integrated_mag_plus_size"
        warnings.append(
            "확산천체는 카탈로그 통합등급과 원형 각크기로 평균 표면밝기를 근사했습니다. "
            "실제 구조·방출선·필터 특성 때문에 국소 SNR은 달라질 수 있습니다."
        )
    pixel_scale = profile.pixel_scale_arcsec
    if pixel_scale is None or pixel_scale <= 0:
        return None, None, "unavailable", warnings + [
            "확산천체 신호를 픽셀당 전자수로 환산하려면 장비 프로필의 pixel scale(arcsec/pixel)이 필요합니다."
        ], diagnostics
    e_per_sec_per_arcsec2 = 10 ** (0.4 * (zero_point - mu)) * extinction_factor
    per_pixel = e_per_sec_per_arcsec2 * pixel_scale**2
    total_rate = per_pixel * max(1, effective_pixels)
    diagnostics.update({
        "target_mag": magnitude,
        "mean_surface_brightness_mag_arcsec2": mu,
        "pixel_scale_arcsec": pixel_scale,
        "effective_pixels": effective_pixels,
        "signal_e_per_sec_per_pixel": per_pixel,
        "signal_e_per_sec": total_rate,
    })
    return float(total_rate), float(per_pixel), source, warnings, diagnostics


def _snr_for_exposure(
    t: float,
    signal_rate_e: float,
    background_rate_e_per_pix: float,
    dark_current: float,
    read_noise: float,
    n_pix: int,
) -> float:
    if t <= 0 or signal_rate_e <= 0 or n_pix <= 0:
        return 0.0
    signal = signal_rate_e * t
    variance = (
        signal
        + (background_rate_e_per_pix + dark_current) * t * n_pix
        + read_noise**2 * n_pix
    )
    return signal / math.sqrt(variance) if variance > 0 else 0.0


def _build_plan(
    *,
    profile: EquipmentProfile,
    target: dict[str, Any],
    background_rate_adu_per_pix: float,
    target_signal_rate_e: float | None,
    effective_pixels: int,
    target_snr: float,
    min_sub_exposure_sec: float,
    max_sub_exposure_sec: float,
    tracking_limit_sec: float,
    background_limit_fraction: float,
    saturation_safety_fraction: float,
    stack_efficiency: float,
    max_frames: int,
    frame_overhead_sec: float,
) -> dict[str, Any]:
    warnings: list[str] = []
    gain = profile.gain_e_per_adu
    bg_rate_e = max(background_rate_adu_per_pix, 0.0) * gain
    dark = max(profile.dark_current_e_per_pix_sec, 0.0)
    rn = max(profile.read_noise_e, 0.0)
    sky_lower = 5.0 * rn**2 / max(bg_rate_e + dark, 1e-12)

    upper_candidates: list[tuple[str, float]] = [("user_max", max_sub_exposure_sec)]
    if tracking_limit_sec > 0:
        upper_candidates.append(("tracking", tracking_limit_sec))

    fullwell_e: float | None = None
    background_upper: float | None = None
    saturation_upper: float | None = None
    target_saturation_upper: float | None = None
    if profile.sensor_clip_adu is not None and profile.sensor_clip_adu > profile.bias_offset_adu:
        fullwell_e = (profile.sensor_clip_adu - profile.bias_offset_adu) * gain
        bg_threshold_e = fullwell_e * background_limit_fraction
        if bg_rate_e + dark > 0:
            background_upper = bg_threshold_e / (bg_rate_e + dark)
            upper_candidates.append(("background", background_upper))
        if profile.reference_peak_e_per_sec is not None and profile.reference_peak_e_per_sec > 0:
            safe_e = fullwell_e * saturation_safety_fraction
            rate = profile.reference_peak_e_per_sec + bg_rate_e + dark
            saturation_upper = safe_e / rate
            warnings.append(
                "장비 프로필의 기준 영상에서 측정한 대표 별 포화시간은 과거 시야의 밝기 진단값입니다. "
                "오늘 선택한 시야의 별 밝기는 다를 수 있으므로 단일노출의 강제 상한으로 사용하지 않습니다."
            )
        else:
            warnings.append("장비 기준 영상에서 대표 비포화 별의 peak rate를 얻지 못해 과거 별 포화 진단값을 계산하지 못했습니다.")

        if (
            target["target_mode"] == "point"
            and target_signal_rate_e is not None
            and target_signal_rate_e > 0
            and profile.reference_psf_peak_fraction is not None
            and profile.reference_psf_peak_fraction > 0
        ):
            target_peak_rate = target_signal_rate_e * profile.reference_psf_peak_fraction
            target_saturation_upper = (fullwell_e * saturation_safety_fraction) / max(
                target_peak_rate + bg_rate_e + dark, 1e-12
            )
            upper_candidates.append(("target_saturation", target_saturation_upper))
        elif target["target_mode"] == "point":
            warnings.append("점광원 대상의 PSF peak 비율이 없어 대상 자체의 포화 상한은 별도로 계산하지 못했습니다.")
        else:
            warnings.append(
                "확산천체의 국소 최고 표면밝기와 오늘 시야의 가장 밝은 별은 카탈로그 통합등급만으로 "
                "확정할 수 없습니다. 배경 포화 상한은 적용하지만 대상/별 포화는 별도 확인이 필요한 계획값입니다."
            )
    else:
        warnings.append("장비 프로필에 센서 포화 ADU가 없어 포화 기반 상한을 적용하지 못했습니다.")

    limiting, practical_upper = min(upper_candidates, key=lambda item: item[1])
    if practical_upper < min_sub_exposure_sec:
        return {
            "status": "invalid",
            "recommended_sub_exposure_sec": None,
            "predicted_snr_per_sub": None,
            "frames": None,
            "total_integration_sec": None,
            "total_elapsed_sec": None,
            "sky_limited_lower_sec": sky_lower,
            "background_upper_sec": background_upper,
            "saturation_upper_sec": saturation_upper,
            "reference_star_saturation_diagnostic_sec": saturation_upper,
            "target_saturation_upper_sec": target_saturation_upper,
            "practical_upper_sec": practical_upper,
            "limiting_constraint": limiting,
            "confidence": "none",
            "warnings": warnings + ["안전 상한이 설정된 최소 단일노출보다 짧습니다."],
        }

    # The report separates single-sub selection from the total-SNR target.  Choose a
    # sub exposure that is read-noise efficient while retaining 15% headroom to the
    # tightest practical upper bound.  Total SNR is then reached by stacking.
    desired = min(practical_upper * 0.85, max(sky_lower, min_sub_exposure_sec))
    if sky_lower > practical_upper:
        desired = practical_upper * 0.85
        warnings.append("포화/추적 상한이 읽기잡음 효율 하한보다 짧아 포화 여유를 우선했습니다.")
    recommended = min(
        practical_upper,
        _safe_round_down(max(desired, min_sub_exposure_sec), min_sub_exposure_sec),
    )
    recommended = max(min_sub_exposure_sec, recommended)

    snr_sub: float | None = None
    frames: int | None = None
    total: float | None = None
    elapsed: float | None = None
    if target_signal_rate_e is not None and target_signal_rate_e > 0:
        snr_sub = _snr_for_exposure(
            recommended,
            target_signal_rate_e,
            bg_rate_e,
            dark,
            rn,
            effective_pixels,
        )
        if snr_sub > 0:
            frames = max(1, int(math.ceil((target_snr / max(snr_sub * stack_efficiency, 1e-12)) ** 2)))
            if frames > max_frames:
                warnings.append(
                    f"목표 SNR에 필요한 프레임 수 {frames:,}장이 설정 한계 {max_frames:,}장을 초과합니다."
                )
                frames = None
            else:
                total = frames * recommended
                elapsed = frames * (recommended + frame_overhead_sec)
    else:
        warnings.append("대상 신호 모델이 없어 단일노출 후보만 계산하고 목표 SNR 기반 촬영 장수는 표시하지 않습니다.")

    confidence = "high"
    if profile.confidence == "low":
        confidence = "low"
    elif profile.confidence != "high" or profile.c_sys_quality != "good":
        confidence = "medium"
    if target["target_mode"] == "extended" and confidence == "high":
        confidence = "medium"
    if target_signal_rate_e is None:
        confidence = "low"
    if profile.c_sys is None:
        confidence = "low"
    if profile.sensor_clip_adu is None:
        confidence = "low"
    return {
        "status": "ok" if recommended > 0 else "invalid",
        "recommended_sub_exposure_sec": float(recommended),
        "predicted_snr_per_sub": None if snr_sub is None else float(snr_sub),
        "frames": frames,
        "total_integration_sec": None if total is None else float(total),
        "total_elapsed_sec": None if elapsed is None else float(elapsed),
        "sky_limited_lower_sec": float(sky_lower),
        "background_upper_sec": None if background_upper is None else float(background_upper),
        "saturation_upper_sec": None if saturation_upper is None else float(saturation_upper),
        "reference_star_saturation_diagnostic_sec": None if saturation_upper is None else float(saturation_upper),
        "target_saturation_upper_sec": None if target_saturation_upper is None else float(target_saturation_upper),
        "practical_upper_sec": float(practical_upper),
        "limiting_constraint": limiting,
        "confidence": confidence,
        "warnings": warnings,
    }


def run_session_analysis(
    *,
    allsky_path: Path,
    profile: EquipmentProfile,
    target_payload: dict[str, Any],
    project_root: Path,
    result_root: Path,
    allsky_calibration: CalibrationSet | None = None,
    allsky_exposure_sec: float | None = None,
    allsky_bias_offset_adu: float | None = None,
    target_snr: float = 100.0,
    min_sub_exposure_sec: float = 1.0,
    max_sub_exposure_sec: float = 600.0,
    tracking_limit_sec: float = 0.0,
    background_limit_fraction: float = 0.30,
    saturation_safety_fraction: float = 0.80,
    stack_efficiency: float = 0.90,
    max_frames: int = 2000,
    frame_overhead_sec: float = 2.0,
    effective_pixels: int = 100,
    minimum_sky_altitude_deg: float = 15.0,
    az_bins: int = 72,
    alt_bins: int = 18,
    manual_target_mag: float | None = None,
    manual_surface_brightness_mag_arcsec2: float | None = None,
) -> dict[str, Any]:
    target = _target_payload(target_payload)
    unsupported_target, limited_target_model = _target_scope_flags(target)
    if unsupported_target:
        raise ValueError("태양은 본 연구의 심우주 촬영 노출 모델 적용 대상이 아닙니다.")
    if target["alt_deg"] is None or target["az_deg"] is None:
        raise ValueError("Stellarium에서 선택 천체의 현재 고도·방위각을 가져와야 합니다.")
    if target["alt_deg"] < 0:
        raise ValueError(f"선택 천체는 현재 고도 {target['alt_deg']:.2f}°로 지평선 아래에 있습니다.")
    if target["alt_deg"] < minimum_sky_altitude_deg:
        raise ValueError(
            f"선택 천체 고도 {target['alt_deg']:.2f}°가 최저 분석 고도 {minimum_sky_altitude_deg:.1f}°보다 낮습니다."
        )

    job_id = uuid.uuid4().hex[:12]
    result_dir = result_root / job_id
    result_dir.mkdir(parents=True, exist_ok=False)
    warnings: list[str] = []
    if limited_target_model:
        warnings.append(
            "선택 대상은 행성·달·혜성 등 태양계 천체로 분류됩니다. 보고서의 심우주 대상 신호/SNR 검증 범위를 벗어나므로 결과를 계획용으로 낮춥니다."
        )
    allsky_original = load_image(allsky_path)
    time_difference_min, time_notes = _time_alignment(
        target_time_utc=target.get("observation_time_utc"),
        allsky_date_obs=allsky_original.metadata.date_obs,
        allsky_source_type=allsky_original.metadata.source_type,
    )
    warnings.extend(time_notes)
    exposure = allsky_exposure_sec or allsky_original.metadata.exposure_sec
    if exposure is None or exposure <= 0:
        raise ValueError("전천 영상의 노출시간을 헤더에서 읽지 못했습니다. 전천 노출시간을 입력하세요.")
    allsky_calibration = allsky_calibration or CalibrationSet()
    allsky_frame, allsky_cal_report = apply_calibration(
        allsky_original,
        allsky_calibration,
        light_exposure_sec=exposure,
    )
    if isinstance(allsky_cal_report.get("warnings"), list):
        warnings.extend(str(item) for item in allsky_cal_report["warnings"])

    if allsky_original.metadata.source_type == "raw" or bool(allsky_cal_report.get("offset_removed")):
        current_allsky_offset = 0.0
        allsky_offset_known = True
    elif allsky_bias_offset_adu is not None and math.isfinite(allsky_bias_offset_adu):
        current_allsky_offset = float(allsky_bias_offset_adu)
        allsky_offset_known = True
    elif allsky_original.metadata.offset_setting is not None:
        current_allsky_offset = float(allsky_original.metadata.offset_setting)
        allsky_offset_known = True
    else:
        current_allsky_offset = 0.0
        allsky_offset_known = False
        warnings.append(
            "전천 FITS의 Bias/offset을 확인하지 못했습니다. 방향비는 계산하지만 단위시간 절대 배경률의 신뢰도를 낮춥니다."
        )

    sky_settings = AnalysisSettings(
        current_exposure_sec=1.0,
        target_mode=target["target_mode"],  # type: ignore[arg-type]
        target_name=target["name"],
        target_alt_deg=float(target["alt_deg"]),
        target_az_deg=float(target["az_deg"]),
        allsky_exposure_sec=float(exposure),
        minimum_sky_altitude_deg=minimum_sky_altitude_deg,
        az_bins=az_bins,
        alt_bins=alt_bins,
    )
    fisheye = load_fisheye_config(project_root / "config" / "fisheye.json")
    fisheye_errors = validate_fisheye_directional_calibration(fisheye)
    sky = build_sky_map(
        allsky_frame,
        sky_settings,
        fisheye,
        result_dir,
        flat_applied=bool(allsky_cal_report.get("flat_frames")),
    )
    warnings.extend(sky.notes)
    if sky.target_background_adu is None:
        raise ValueError("선택 천체 방향의 신뢰 가능한 전천 배경값을 계산하지 못했습니다.")

    corrected_target_background = max(float(sky.target_background_adu) - current_allsky_offset, 0.0)
    corrected_sky_median = max(float(sky.sky_median_adu) - current_allsky_offset, 0.0)
    if corrected_target_background <= 0 or corrected_sky_median <= 0:
        raise ValueError("Bias/offset 보정 후 전천 배경값이 0 이하입니다. 전천 offset과 보정 프레임을 확인하세요.")
    bg_rate_adu, bg_method, bg_warnings = _background_rate(
        profile,
        corrected_target_background,
        corrected_sky_median,
        exposure,
        allsky_original.metadata,
        current_flat_applied=bool(allsky_cal_report.get("flat_frames")),
    )
    warnings.extend(bg_warnings)

    signal_rate, signal_per_pixel, signal_source, signal_warnings, signal_diag = _signal_model(
        profile,
        target,
        max(1, effective_pixels),
        manual_target_mag,
        manual_surface_brightness_mag_arcsec2,
    )
    warnings.extend(signal_warnings)
    if target["target_mode"] == "point":
        n_pix = max(1, profile.reference_aperture_pixels or effective_pixels)
    else:
        n_pix = max(1, effective_pixels)

    plan = _build_plan(
        profile=profile,
        target=target,
        background_rate_adu_per_pix=bg_rate_adu,
        target_signal_rate_e=signal_rate,
        effective_pixels=n_pix,
        target_snr=target_snr,
        min_sub_exposure_sec=min_sub_exposure_sec,
        max_sub_exposure_sec=max_sub_exposure_sec,
        tracking_limit_sec=tracking_limit_sec,
        background_limit_fraction=background_limit_fraction,
        saturation_safety_fraction=saturation_safety_fraction,
        stack_efficiency=stack_efficiency,
        max_frames=max_frames,
        frame_overhead_sec=frame_overhead_sec,
    )
    warnings.extend(plan["warnings"])

    curve_path = result_dir / "exposure_snr_curve.png"
    curve_min = max(0.05, min_sub_exposure_sec / 3.0)
    curve_max = max(curve_min * 10, min(max_sub_exposure_sec, max(plan.get("practical_upper_sec") or 0, 10.0)))
    xs = np.geomspace(curve_min, curve_max, 180)
    if signal_rate is not None and signal_rate > 0:
        bg_e = bg_rate_adu * profile.gain_e_per_adu
        ys = np.array([
            _snr_for_exposure(x, signal_rate, bg_e, profile.dark_current_e_per_pix_sec, profile.read_noise_e, n_pix)
            for x in xs
        ])
        save_exposure_snr_curve(
            xs,
            ys,
            curve_path,
            current_exposure_sec=plan["recommended_sub_exposure_sec"] or 1.0,
            current_snr=plan["predicted_snr_per_sub"] or 0.0,
            target_snr=target_snr,
            recommended_exposure_sec=plan["recommended_sub_exposure_sec"],
            practical_upper_sec=plan["practical_upper_sec"],
        )
    else:
        # A zero line still documents that the sub exposure is constrained by sky/
        # saturation but the target SNR model is unavailable.
        save_exposure_snr_curve(
            xs,
            np.zeros_like(xs),
            curve_path,
            current_exposure_sec=1.0,
            current_snr=0.0,
            target_snr=target_snr,
            recommended_exposure_sec=plan["recommended_sub_exposure_sec"],
            practical_upper_sec=plan["practical_upper_sec"],
        )

    confidence = plan["confidence"]
    validity = "quantitative_candidate"
    validity_reasons: list[str] = []
    if fisheye_errors:
        validity = "planning_only"
        validity_reasons.append("어안 보정 계수는 적용했지만 독립 각도 RMS 검증 자료가 없어 방향값을 관측 계획용으로 취급합니다.")
    if bg_method != "c_sys":
        validity = "planning_only"
        if bg_method == "relative_fallback":
            validity_reasons.append("현재 전천 조건에 직접 적용 가능한 Csys가 없어 방향별 상대 배경 환산을 사용했습니다.")
        else:
            validity_reasons.append("Csys는 적용했지만 전천 장비 설정 일치 여부가 완전히 검증되지 않았습니다.")
    approximate_signal = signal_source in {
        "integrated_mag_plus_size",
        "integrated_mag_plus_size_narrowband_approximation",
        "catalog_magnitude_narrowband_approximation",
    }
    if signal_source == "unavailable" or approximate_signal:
        validity = "planning_only"
        if signal_source == "unavailable":
            validity_reasons.append("선택 천체의 절대 신호 모델이 없어 촬영 장수 계산이 제한됩니다.")
        elif "narrowband" in signal_source:
            validity_reasons.append("협대역/다중대역 필터에서 V등급 기반 신호 환산을 사용해 결과를 계획용 근사로 취급합니다.")
        else:
            validity_reasons.append("확산천체 신호는 통합등급과 각크기로 평균 표면밝기를 근사했습니다.")
    if profile.zero_point_quality not in {"good"} and signal_source != "unavailable":
        validity = "planning_only"
        validity_reasons.append(f"장비 프로필의 기기영점 품질이 {profile.zero_point_quality}이므로 절대 신호 예측을 정밀 측광값으로 취급하지 않습니다.")
    if limited_target_model:
        validity = "planning_only"
        validity_reasons.append("선택 대상이 보고서의 심우주 검증 범위를 벗어납니다.")
    if not bool(allsky_cal_report.get("flat_frames")):
        validity = "planning_only"
        validity_reasons.append("전천 flat이 없어 렌즈 비네팅이 방향별 배경에 일부 포함될 수 있습니다.")
    if not validity_reasons:
        validity_reasons.append("보고서의 기본 계산 조건을 충족했으나 실제 관측 결과에 따른 추가 검증은 필요합니다.")

    if bg_method == "relative_fallback":
        confidence = "low"
    elif bg_method == "c_sys_planning" and confidence == "high":
        confidence = "medium"

    artifacts = {
        "allsky_preview": f"/results/{job_id}/{sky.preview_path}" if sky.preview_path else "",
        "allsky_coordinate_overlay": f"/results/{job_id}/{sky.coordinate_overlay_path}" if sky.coordinate_overlay_path else "",
        "sky_map": f"/results/{job_id}/{sky.map_path}" if sky.map_path else "",
        "sky_relative_map": f"/results/{job_id}/{sky.relative_map_path}" if sky.relative_map_path else "",
        "sky_polar_map": f"/results/{job_id}/{sky.polar_map_path}" if sky.polar_map_path else "",
        "sky_reliability": f"/results/{job_id}/{sky.reliability_path}" if sky.reliability_path else "",
        "sky_altitude_profiles": f"/results/{job_id}/{sky.horizon_profile_path}" if sky.horizon_profile_path else "",
        "sky_distribution": f"/results/{job_id}/{sky.distribution_path}" if sky.distribution_path else "",
        "sky_table": f"/results/{job_id}/{sky.table_path}" if sky.table_path else "",
        "exposure_snr_curve": f"/results/{job_id}/{curve_path.name}",
        "result_json": f"/results/{job_id}/result.json",
    }
    result = {
        "job_id": job_id,
        "version": "34.2.0",
        "validity": validity,
        "validity_reasons": validity_reasons,
        "target": target,
        "equipment_profile": {
            "profile_id": profile.profile_id,
            "name": profile.name,
            "telescope_name": profile.telescope_name,
            "camera_name": profile.camera_name,
            "filter_name": profile.filter_name,
            "confidence": profile.confidence,
            "zero_point_quality": profile.zero_point_quality,
            "c_sys_quality": profile.c_sys_quality,
        },
        "sky": asdict(sky),
        "background_model": {
            "method": bg_method,
            "allsky_exposure_sec": exposure,
            "target_allsky_background_adu_raw": sky.target_background_adu,
            "allsky_median_adu_raw": sky.sky_median_adu,
            "allsky_offset_adu": current_allsky_offset,
            "allsky_offset_known": allsky_offset_known,
            "target_allsky_background_adu": corrected_target_background,
            "allsky_median_adu": corrected_sky_median,
            "telescope_background_adu_per_sec_per_pixel": bg_rate_adu,
            "telescope_background_e_per_sec_per_pixel": bg_rate_adu * profile.gain_e_per_adu,
        },
        "target_signal_model": {
            "source": signal_source,
            "signal_e_per_sec": signal_rate,
            "signal_e_per_sec_per_pixel": signal_per_pixel,
            "effective_pixels": n_pix,
            **signal_diag,
        },
        "plan": plan,
        "confidence": confidence,
        "warnings": list(dict.fromkeys(warnings)),
        "diagnostics": {
            "fisheye_validation_errors": fisheye_errors,
            "allsky_metadata": asdict(allsky_original.metadata),
            "allsky_calibration": allsky_cal_report,
            "observation_context": {
                "stellarium_time_utc": target.get("observation_time_utc"),
                "stellarium_time_local": target.get("observation_time_local"),
                "stellarium_latitude": target.get("latitude"),
                "stellarium_longitude": target.get("longitude"),
                "allsky_date_obs": allsky_original.metadata.date_obs,
                "time_difference_min": time_difference_min,
            },
        },
        "artifacts": artifacts,
    }
    (result_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return result
