from __future__ import annotations

import gc
import json
import math
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__
from .equipment import EquipmentProfile, airmass_from_altitude
from .geometry import (
    estimate_masked_outer_field_pedestal,
    select_fisheye_config,
    validate_fisheye_directional_calibration,
)
from .io import apply_calibration, load_image
from .models import AnalysisSettings, CalibrationSet, ImageMetadata
from .planning import (
    _exposure_efficiency,
    _exposure_efficiency_time,
    _friendly_round_up,
    _safe_round_down,
)
from .sky import build_sky_map, prepare_sky_analysis_frame
from .visualization import save_exposure_snr_curve
from .time_utils import observation_time_difference_minutes
from .reference_sky import fetch_target_structure, survey_for_filter
from .evidence import exposure_evidence_prior


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


def _filter_uses_v_band_directly(name: str | None) -> bool | None:
    text = (name or "").strip().casefold().replace("-", "_")
    if not text:
        return None
    compatible = (
        text in {"v", "v_bess", "v_bessel", "johnson_v", "bessell_v"}
        or "johnson v" in text
        or "bessell v" in text
        or "bessel v" in text
    )
    return compatible


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


def _location_alignment(
    target: dict[str, Any],
    metadata: ImageMetadata,
    *,
    tracking_site_latitude_deg: float | None = None,
) -> list[str]:
    """Reject a fixed/tracking camera solution used at the wrong observing site."""
    notes: list[str] = []
    target_lat = _finite(target.get("latitude"))
    target_lon = _finite(target.get("longitude"))
    extra = metadata.extra if isinstance(metadata.extra, dict) else {}
    image_lat = _finite(extra.get("site_latitude_deg"))
    image_lon = _finite(extra.get("site_longitude_deg"))
    if target_lat is not None and tracking_site_latitude_deg is not None:
        delta = abs(target_lat - tracking_site_latitude_deg)
        if delta > 0.5:
            raise ValueError(
                f"Stellarium 위도와 고정식 전천 카메라 보정 관측소 위도가 {delta:.2f}° 다릅니다. "
                "Stellarium 관측 위치 또는 전천 영상을 확인하세요."
            )
    if None not in (target_lat, target_lon, image_lat, image_lon):
        assert target_lat is not None and target_lon is not None
        assert image_lat is not None and image_lon is not None
        latitude_delta = abs(target_lat - image_lat)
        longitude_delta = abs((target_lon - image_lon + 180.0) % 360.0 - 180.0)
        separation = math.hypot(latitude_delta, longitude_delta * math.cos(math.radians(target_lat)))
        if separation > 1.0:
            raise ValueError(
                f"Stellarium 관측 위치와 전천 FITS 관측소가 약 {separation:.2f}° 다릅니다. "
                "같은 관측 위치의 자료를 사용하세요."
            )
        if separation > 0.1:
            notes.append(f"Stellarium과 전천 영상 관측 위치가 약 {separation:.2f}° 차이납니다.")
    return notes

def _allsky_profile_compatibility(
    profile: EquipmentProfile,
    metadata: ImageMetadata,
    *,
    current_flat_applied: bool,
) -> tuple[bool, bool, list[str]]:
    """Check whether a stored Csys is transferable to the current all-sky image."""
    incompatible: list[str] = []
    unknown: list[str] = []
    if metadata.source_type == "rendered":
        incompatible.append("현재 전천 영상이 렌더링 영상이라 저장된 절대 Csys의 선형 ADU 척도를 보장할 수 없습니다.")
    reference_source_type = profile.reference_allsky_source_type
    if reference_source_type and metadata.source_type:
        if reference_source_type != metadata.source_type:
            incompatible.append("현재 전천 영상과 Csys 기준 영상의 원본 형식/선형화 경로가 다릅니다.")
    else:
        unknown.append("Csys 기준 영상의 원본 형식 일치 여부를 확인하지 못했습니다.")

    if profile.reference_allsky_dtype and metadata.dtype:
        if profile.reference_allsky_dtype.strip().casefold() != metadata.dtype.strip().casefold():
            unknown.append("현재 전천 영상과 Csys 기준 영상의 저장 dtype이 달라 ADU 척도 일치를 추가 확인해야 합니다.")
    if profile.reference_allsky_bit_depth and metadata.bit_depth:
        if int(profile.reference_allsky_bit_depth) != int(metadata.bit_depth):
            incompatible.append("현재 전천 영상과 Csys 기준 영상의 bit depth가 다릅니다.")
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
    v_band_match = _filter_uses_v_band_directly(profile.filter_name)
    if narrowband:
        warnings.append(
            "장비 프로필의 필터가 협대역/다중대역으로 보입니다. Stellarium V등급은 해당 통과대역의 "
            "실제 광자율을 직접 나타내지 않으므로 대상 신호와 SNR은 계획용 근사로만 해석합니다."
        )
    elif v_band_match is False:
        warnings.append(
            f"장비 프로필 필터({profile.filter_name or '미상'})와 입력 천체등급(V)이 같은 대역이 아닙니다. "
            "기준 천체와 대상의 색이 다르면 영점 전이가 달라질 수 있어 SNR을 계획용 근사로 표시합니다."
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
        "magnitude_band": "V",
        "profile_filter": profile.filter_name or None,
        "filter_v_band_match": v_band_match,
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
        if narrowband:
            source_name = "catalog_magnitude_narrowband_approximation"
        elif v_band_match is False:
            source_name = "catalog_magnitude_filter_mismatch_approximation"
        else:
            source_name = "catalog_magnitude"
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
        if narrowband:
            source = "integrated_mag_plus_size_narrowband_approximation"
        elif v_band_match is False:
            source = "integrated_mag_plus_size_filter_mismatch_approximation"
        else:
            source = "integrated_mag_plus_size"
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


def _signal_model_uncertainty_fraction(
    source: str,
    profile: EquipmentProfile,
    *,
    limited_target_model: bool,
) -> float:
    """Assign a stated planning uncertainty from model provenance, not output fit."""
    if source == "unavailable":
        return 0.0
    uncertainty = 0.15
    if source == "manual_surface_brightness":
        uncertainty = 0.20
    if source.startswith("integrated_mag_plus_size"):
        uncertainty = max(uncertainty, 0.40)
    if "filter_mismatch" in source:
        uncertainty = max(uncertainty, 0.50)
    if "narrowband" in source:
        uncertainty = max(uncertainty, 0.75)
    if profile.zero_point_quality == "approximate":
        uncertainty = max(uncertainty, 0.30)
    elif profile.zero_point_quality not in {"good"}:
        uncertainty = max(uncertainty, 0.50)
    if limited_target_model:
        uncertainty = max(uncertainty, 0.75)
    return float(min(uncertainty, 0.95))


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
    background_uncertainty_fraction: float = 0.0,
    signal_uncertainty_fraction: float = 0.0,
    target_signal_rate_e_per_pixel: float | None = None,
    target_structure_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    gain = profile.gain_e_per_adu
    bg_rate_e = max(background_rate_adu_per_pix, 0.0) * gain
    background_uncertainty_fraction = min(0.90, max(0.0, background_uncertainty_fraction))
    signal_uncertainty_fraction = min(0.95, max(0.0, signal_uncertainty_fraction))
    bg_rate_e_low = bg_rate_e * (1.0 - background_uncertainty_fraction)
    bg_rate_e_high = bg_rate_e * (1.0 + background_uncertainty_fraction)
    dark = max(profile.dark_current_e_per_pix_sec, 0.0)
    rn = max(profile.read_noise_e, 0.0)
    # Low sky is the adverse case for read-noise efficiency; high sky is the
    # adverse case for detector-background and saturation headroom.
    sky_lower = 5.0 * rn**2 / max(bg_rate_e_low + dark, 1e-12)
    sky_lower_bright = 5.0 * rn**2 / max(bg_rate_e_high + dark, 1e-12)

    upper_candidates: list[tuple[str, float]] = [("user_max", max_sub_exposure_sec)]
    if tracking_limit_sec > 0:
        upper_candidates.append(("tracking", tracking_limit_sec))
    else:
        warnings.append(
            "추적 상한을 입력하지 않아 마운트 주기오차·극축정렬·바람·시잉은 "
            "강제 상한에 포함하지 않았습니다."
        )

    fullwell_e: float | None = None
    background_upper: float | None = None
    saturation_upper: float | None = None
    target_saturation_upper: float | None = None
    if profile.sensor_clip_adu is not None and profile.sensor_clip_adu > profile.bias_offset_adu:
        fullwell_e = (profile.sensor_clip_adu - profile.bias_offset_adu) * gain
        bg_threshold_e = fullwell_e * background_limit_fraction
        if bg_rate_e_high + dark > 0:
            background_upper = bg_threshold_e / (bg_rate_e_high + dark)
            upper_candidates.append(("background", background_upper))
        if profile.reference_peak_e_per_sec is not None and profile.reference_peak_e_per_sec > 0:
            safe_e = fullwell_e * saturation_safety_fraction
            rate = profile.reference_peak_e_per_sec + bg_rate_e_high + dark
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
            target_peak_rate = (
                target_signal_rate_e
                * (1.0 + signal_uncertainty_fraction)
                * profile.reference_psf_peak_fraction
            )
            target_saturation_upper = (fullwell_e * saturation_safety_fraction) / max(
                target_peak_rate + bg_rate_e_high + dark, 1e-12
            )
            upper_candidates.append(("target_saturation", target_saturation_upper))
        elif target["target_mode"] == "point":
            warnings.append("점광원 대상의 PSF peak 비율이 없어 대상 자체의 포화 상한은 별도로 계산하지 못했습니다.")
        elif target_signal_rate_e_per_pixel is not None and target_signal_rate_e_per_pixel > 0:
            _, limited_extended_target = _target_scope_flags(target)
            structure_factor = None
            structure_status = (target_structure_model or {}).get("status")
            structure_confidence = (target_structure_model or {}).get("confidence")
            structure_value = (target_structure_model or {}).get("bright_structure_factor")
            if structure_status == "ok" and isinstance(structure_value, (int, float)) and structure_value > 0:
                structure_factor = float(structure_value)
                warnings.append(
                    f"공개 참조 영상의 상대 밝기 분포에서 밝은 구조 계수 {structure_factor:.2f}×를 측정해 "
                    "확산 대상의 픽셀 포화 상한에 적용했습니다. 참조 영상의 절대 ADU/flux는 사용하지 않았습니다."
                )
            if structure_factor is None:
                structure_factor = 2.0 if limited_extended_target else 1.5
                warnings.append(
                    "대상 구조 참조 영상을 사용할 수 없어 평균 표면밝기에 보수적 구조 안전계수를 적용했습니다. "
                    "밝은 핵이 있는 대상은 시험 촬영으로 포화를 추가 확인하세요."
                )
            target_peak_rate = (
                target_signal_rate_e_per_pixel
                * (1.0 + signal_uncertainty_fraction)
                * structure_factor
            )
            target_saturation_upper = (fullwell_e * saturation_safety_fraction) / max(
                target_peak_rate + bg_rate_e_high + dark, 1e-12
            )
            upper_candidates.append(("target_saturation", target_saturation_upper))
        else:
            warnings.append(
                "확산천체의 국소 최고 표면밝기와 오늘 시야의 가장 밝은 별은 카탈로그 통합등급만으로 "
                "확정할 수 없습니다. 배경 포화 상한은 적용하지만 대상/별 포화는 별도 확인이 필요한 계획값입니다."
            )
    else:
        warnings.append("장비 프로필에 센서 포화 ADU가 없어 포화 기반 상한을 적용하지 못했습니다.")

    hard_upper_constraint, practical_upper = min(upper_candidates, key=lambda item: item[1])
    exposure_efficiency_target = 0.90
    efficiency_lower, read_noise_time = _exposure_efficiency_time(
        background_rate_e_per_pix=bg_rate_e_low,
        dark_current_e_per_pix_sec=dark,
        read_noise_e=rn,
        frame_overhead_sec=frame_overhead_sec,
        target_efficiency=exposure_efficiency_target,
    )
    efficiency_goal = max(sky_lower, efficiency_lower)
    efficiency_lower_bright, _ = _exposure_efficiency_time(
        background_rate_e_per_pix=bg_rate_e_high,
        dark_current_e_per_pix_sec=dark,
        read_noise_e=rn,
        frame_overhead_sec=frame_overhead_sec,
        target_efficiency=exposure_efficiency_target,
    )
    recommendation_range_lower = min(
        practical_upper,
        _friendly_round_up(
            max(sky_lower_bright, efficiency_lower_bright), min_sub_exposure_sec
        ),
    )
    constraint_inputs = {
        "target_snr": float(target_snr),
        "min_sub_exposure_sec": float(min_sub_exposure_sec),
        "max_sub_exposure_sec": float(max_sub_exposure_sec),
        "tracking_limit_sec": float(tracking_limit_sec),
        "background_limit_fraction": float(background_limit_fraction),
        "saturation_safety_fraction": float(saturation_safety_fraction),
        "stack_efficiency": float(stack_efficiency),
        "max_frames": int(max_frames),
        "frame_overhead_sec": float(frame_overhead_sec),
        "effective_pixels": int(effective_pixels),
        "background_uncertainty_fraction": float(background_uncertainty_fraction),
        "signal_uncertainty_fraction": float(signal_uncertainty_fraction),
        "target_signal_rate_e_per_pixel": (
            None
            if target_signal_rate_e_per_pixel is None
            else float(target_signal_rate_e_per_pixel)
        ),
        "target_structure_status": (target_structure_model or {}).get("status"),
        "target_structure_confidence": (target_structure_model or {}).get("confidence"),
        "target_structure_faint_factor": (target_structure_model or {}).get("faint_structure_factor"),
        "target_structure_bright_factor": (target_structure_model or {}).get("bright_structure_factor"),
    }
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
            "limiting_constraint": hard_upper_constraint,
            "hard_upper_constraint": hard_upper_constraint,
            "selection_basis": "no_feasible_interval",
            "constraint_status": "invalid",
            "constraint_inputs": constraint_inputs,
            "confidence": "none",
            "warnings": warnings + [
                "안전 상한이 설정된 최소 단일노출보다 짧습니다. "
                f"최소 단일노출을 {max(practical_upper, 0.001):.3g}초 이하로 낮추세요."
            ],
        }

    # The user maximum is a cap, not a desired exposure.  Select the shortest
    # exposure that reaches the fixed efficiency target.  A measured reference
    # field peak is not a universal hard limit, but it is useful as a conservative
    # advisory ceiling when the efficiency target would otherwise exceed it.
    selection_target = min(practical_upper, efficiency_goal)
    selection_basis = "efficiency_target"
    limiting_constraint = "exposure_efficiency_target"
    reference_advisory_applied = False
    if practical_upper < efficiency_goal:
        selection_basis = "hard_upper_before_efficiency_target"
        limiting_constraint = hard_upper_constraint
    if saturation_upper is not None and saturation_upper < selection_target:
        selection_target = max(min_sub_exposure_sec, saturation_upper)
        selection_basis = "reference_star_advisory_before_efficiency_target"
        limiting_constraint = "reference_star_advisory"
        reference_advisory_applied = True
        warnings.append(
            "기준 시야의 대표 별 포화 진단이 효율 목표보다 짧아 보수적인 권장값에 반영했습니다. "
            "이는 현재 시야의 절대 포화 보장이 아니라 장비 기준 영상에 근거한 권고입니다."
        )
    if selection_basis == "efficiency_target":
        recommended = min(practical_upper, _friendly_round_up(selection_target, min_sub_exposure_sec))
        if saturation_upper is not None and recommended > saturation_upper:
            recommended = _safe_round_down(max(min_sub_exposure_sec, saturation_upper), min_sub_exposure_sec)
            selection_basis = "reference_star_advisory_before_efficiency_target"
            limiting_constraint = "reference_star_advisory"
            reference_advisory_applied = True
            warnings.append(
                "효율 목표를 실용 간격으로 올림하면 기준 시야의 대표 별 포화 진단을 넘으므로 "
                "보수적인 기준별 권고값으로 내렸습니다."
            )
    else:
        recommended = _safe_round_down(selection_target, min_sub_exposure_sec)
        if recommended < selection_target * 0.95:
            precision = 10.0 if selection_target < 100.0 else 1.0
            recommended = math.floor(selection_target * precision) / precision
    recommended = min(practical_upper, max(min_sub_exposure_sec, recommended))
    recommendation_range = [
        float(min(recommendation_range_lower, recommended)),
        float(max(recommendation_range_lower, recommended)),
    ]
    sky_limited_feasible = sky_lower <= practical_upper
    sky_limited_achieved = recommended >= sky_lower
    efficiency_target_achieved = recommended >= efficiency_lower
    if not sky_limited_feasible:
        constraint_status = "upper_bound_compromise"
    elif reference_advisory_applied and not efficiency_target_achieved:
        constraint_status = "reference_saturation_compromise"
    elif efficiency_target_achieved:
        constraint_status = "efficiency_balanced"
    else:
        constraint_status = "bounded_below_efficiency_target"
    if not sky_limited_feasible:
        warnings.append("포화/추적 상한이 읽기잡음 효율 하한보다 짧아 포화 여유를 우선했습니다.")
    elif not efficiency_target_achieved:
        warnings.append(
            "안전/사용자 상한 때문에 읽기잡음과 프레임 오버헤드를 합친 90% 효율 목표에는 미달합니다."
        )
    elif practical_upper > recommended * 1.25:
        warnings.append(
            "최대 단일노출은 허용 상한이며 추천값 자체가 아닙니다. 더 긴 노출의 효율 증가는 작고 "
            "추적·포화·우주선 영향 위험은 커질 수 있어 효율 목표를 만족하는 짧은 값을 선택했습니다."
        )

    efficiency_at_recommendation = _exposure_efficiency(
        recommended,
        read_noise_time_sec=read_noise_time,
        frame_overhead_sec=frame_overhead_sec,
    )

    snr_sub_mean: float | None = None
    snr_sub_science: float | None = None
    snr_sub: float | None = None
    frames: int | None = None
    required_frames_unbounded: int | None = None
    required_frames_mean: int | None = None
    max_frames_exceeded = False
    achievable_snr_at_max_frames: float | None = None
    total: float | None = None
    elapsed: float | None = None
    required_frames_range: list[int] | None = None
    science_signal_rate_e: float | None = target_signal_rate_e
    structure_aware_integration = False
    science_zone_factor = 1.0
    science_zone_percentile: float | None = None
    structure_confidence = (target_structure_model or {}).get("confidence")
    faint_factor = (target_structure_model or {}).get("faint_structure_factor")
    if (
        target["target_mode"] == "extended"
        and (target_structure_model or {}).get("status") == "ok"
        and structure_confidence in {"high", "medium"}
        and isinstance(faint_factor, (int, float))
        and 0 < float(faint_factor) <= 1.0
        and target_signal_rate_e is not None
    ):
        science_zone_factor = float(faint_factor)
        science_zone_percentile = float((target_structure_model or {}).get("science_percentile") or 25.0)
        science_signal_rate_e = target_signal_rate_e * science_zone_factor
        structure_aware_integration = True
        warnings.append(
            f"총 적분시간은 평균 밝기가 아니라 검출된 확산 구조의 {science_zone_percentile:.0f}백분위 "
            f"희미한 구역({science_zone_factor:.2f}× 평균)을 목표 SNR에 도달시키도록 계산했습니다. "
            "희미한 구역 때문에 단일노출을 늘리지는 않습니다."
        )
    elif target["target_mode"] == "extended" and (target_structure_model or {}).get("status") == "ok":
        warnings.append(
            "대상 구조 영상은 확보했지만 구조 신뢰도가 낮아 총 적분시간을 강제로 늘리는 데 사용하지 않고 포화 진단/참고 정보로만 사용했습니다."
        )

    if target_signal_rate_e is not None and target_signal_rate_e > 0:
        snr_sub_mean = _snr_for_exposure(
            recommended, target_signal_rate_e, bg_rate_e, dark, rn, effective_pixels
        )
        if science_signal_rate_e is not None and science_signal_rate_e > 0:
            snr_sub_science = _snr_for_exposure(
                recommended, science_signal_rate_e, bg_rate_e, dark, rn, effective_pixels
            )
        snr_sub = snr_sub_science if structure_aware_integration else snr_sub_mean
        if snr_sub_mean and snr_sub_mean > 0:
            required_frames_mean = max(
                1, int(math.ceil((target_snr / max(snr_sub_mean * stack_efficiency, 1e-12)) ** 2))
            )
        if snr_sub is not None and snr_sub > 0:
            required_frames_unbounded = max(
                1,
                int(math.ceil((target_snr / max(snr_sub * stack_efficiency, 1e-12)) ** 2)),
            )
            basis_rate = science_signal_rate_e if structure_aware_integration else target_signal_rate_e
            assert basis_rate is not None
            low_signal = basis_rate * (1.0 - signal_uncertainty_fraction)
            high_signal = basis_rate * (1.0 + signal_uncertainty_fraction)
            optimistic_snr = _snr_for_exposure(
                recommended, high_signal, bg_rate_e_low, dark, rn, effective_pixels
            )
            pessimistic_snr = _snr_for_exposure(
                recommended, low_signal, bg_rate_e_high, dark, rn, effective_pixels
            )
            optimistic_frames = max(
                1,
                int(math.ceil((target_snr / max(optimistic_snr * stack_efficiency, 1e-12)) ** 2)),
            )
            pessimistic_frames = max(
                optimistic_frames,
                int(math.ceil((target_snr / max(pessimistic_snr * stack_efficiency, 1e-12)) ** 2)),
            )
            required_frames_range = [optimistic_frames, pessimistic_frames]
            frames = required_frames_unbounded
            achievable_snr_at_max_frames = snr_sub * stack_efficiency * math.sqrt(max_frames)
            if required_frames_unbounded > max_frames:
                max_frames_exceeded = True
                warnings.append(
                    f"목표 SNR에 필요한 프레임 수 {required_frames_unbounded:,}장이 설정 한계 {max_frames:,}장을 초과합니다. "
                    f"최대 장수에서 계획 기준 구역 예상 SNR은 약 {achievable_snr_at_max_frames:.1f}입니다."
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
        "predicted_snr_per_sub_mean": None if snr_sub_mean is None else float(snr_sub_mean),
        "predicted_snr_per_sub_science_zone": None if snr_sub_science is None else float(snr_sub_science),
        "snr_basis": "faint_structure_zone" if structure_aware_integration else "mean_target_signal",
        "structure_aware_integration": structure_aware_integration,
        "science_zone_factor": science_zone_factor,
        "science_zone_percentile": science_zone_percentile,
        "frames": frames,
        "required_frames_unbounded": required_frames_unbounded,
        "required_frames_mean_target": required_frames_mean,
        "required_frames_range": required_frames_range,
        "max_frames_exceeded": max_frames_exceeded,
        "achievable_snr_at_max_frames": achievable_snr_at_max_frames,
        "total_integration_sec": None if total is None else float(total),
        "total_elapsed_sec": None if elapsed is None else float(elapsed),
        "sky_limited_lower_sec": float(sky_lower),
        "background_upper_sec": None if background_upper is None else float(background_upper),
        "saturation_upper_sec": None if saturation_upper is None else float(saturation_upper),
        "reference_star_saturation_diagnostic_sec": None if saturation_upper is None else float(saturation_upper),
        "target_saturation_upper_sec": None if target_saturation_upper is None else float(target_saturation_upper),
        "practical_upper_sec": float(practical_upper),
        "limiting_constraint": limiting_constraint,
        "hard_upper_constraint": hard_upper_constraint,
        "selection_basis": selection_basis,
        "constraint_status": constraint_status,
        "sky_limited_feasible": bool(sky_limited_feasible),
        "sky_limited_achieved": bool(sky_limited_achieved),
        "exposure_efficiency_target": exposure_efficiency_target,
        "exposure_efficiency_lower_sec": float(efficiency_lower),
        "recommended_sub_exposure_range_sec": recommendation_range,
        "exposure_efficiency_at_recommendation": efficiency_at_recommendation,
        "read_noise_time_constant_sec": float(read_noise_time),
        "reference_star_advisory_applied": reference_advisory_applied,
        "constraint_inputs": constraint_inputs,
        "physics_model": {
            "snr_variance": "S*t + n_pix*(B*t + D*t + RN^2)",
            "information_efficiency": "1 / ((1 + RN^2/((B+D)*t)) * (1 + overhead/t))",
            "efficiency_target": exposure_efficiency_target,
            "selection_policy": "shortest practical exposure satisfying physical lower bounds, then hard safety caps",
            "uncertainty_policy": "low sky for read-noise lower bound; high sky and high signal for saturation limits",
            "structure_policy": "bright morphology constrains sub-exposure saturation; faint morphology constrains total integration, never by extending sub-exposure",
        },
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
    allsky_metadata = allsky_original.metadata
    time_difference_min, time_notes = _time_alignment(
        target_time_utc=target.get("observation_time_utc"),
        allsky_date_obs=allsky_metadata.date_obs,
        allsky_source_type=allsky_metadata.source_type,
    )
    warnings.extend(time_notes)
    exposure = allsky_exposure_sec or allsky_metadata.exposure_sec
    if exposure is None or exposure <= 0:
        raise ValueError("전천 영상의 노출시간을 헤더에서 읽지 못했습니다. 전천 노출시간을 입력하세요.")
    allsky_calibration = allsky_calibration or CalibrationSet()
    allsky_frame, allsky_cal_report = apply_calibration(
        allsky_original,
        allsky_calibration,
        light_exposure_sec=exposure,
    )
    allsky_cal_warnings = allsky_cal_report.get("warnings")
    if isinstance(allsky_cal_warnings, list):
        warnings.extend(str(item) for item in allsky_cal_warnings)

    fisheye = select_fisheye_config(
        project_root,
        camera_name=allsky_metadata.camera,
        filename=allsky_metadata.filename,
        width=allsky_metadata.width,
        height=allsky_metadata.height,
        image=allsky_original.intensity,
    )
    warnings.extend(
        _location_alignment(
            target,
            allsky_metadata,
            tracking_site_latitude_deg=fisheye.tracking_site_latitude_deg,
        )
    )
    allsky_offset_method = "unknown"
    allsky_offset_diagnostics: dict[str, Any] = {}
    if allsky_metadata.source_type == "raw" or bool(allsky_cal_report.get("offset_removed")):
        current_allsky_offset = 0.0
        allsky_offset_known = True
        allsky_offset_method = "calibration_or_raw_loader"
    elif allsky_bias_offset_adu is not None and math.isfinite(allsky_bias_offset_adu):
        current_allsky_offset = float(allsky_bias_offset_adu)
        allsky_offset_known = True
        allsky_offset_method = "user_supplied"
    elif allsky_metadata.offset_setting is not None:
        current_allsky_offset = float(allsky_metadata.offset_setting)
        allsky_offset_known = True
        allsky_offset_method = "fits_header"
    else:
        current_allsky_offset = 0.0
        allsky_offset_known = False
        if not bool(allsky_cal_report.get("flat_frames")):
            estimated_offset, offset_diag = estimate_masked_outer_field_pedestal(
                allsky_original.intensity,
                fisheye,
            )
            allsky_offset_diagnostics = offset_diag
            if estimated_offset is not None:
                current_allsky_offset = float(estimated_offset)
                allsky_offset_known = True
                allsky_offset_method = "masked_outer_field"
                warnings.append(
                    f"전천영상의 원형 하늘 영역 밖에서 균일한 비조명 detector 영역을 확인해 동일 프레임의 "
                    f"bias+dark pedestal를 {current_allsky_offset:.1f} ADU로 추정했습니다."
                )
        if not allsky_offset_known:
            warnings.append(
                "전천 FITS의 Bias/offset을 확인하지 못했습니다. 방향비는 계산하지만 단위시간 절대 배경률의 신뢰도를 낮춥니다."
            )

    sky_settings = AnalysisSettings(
        current_exposure_sec=1.0,
        target_mode=target["target_mode"],
        target_name=target["name"],
        target_alt_deg=float(target["alt_deg"]),
        target_az_deg=float(target["az_deg"]),
        allsky_exposure_sec=float(exposure),
        minimum_sky_altitude_deg=minimum_sky_altitude_deg,
        az_bins=az_bins,
        alt_bins=alt_bins,
    )
    fisheye_errors = validate_fisheye_directional_calibration(fisheye)
    compact_allsky_frame = prepare_sky_analysis_frame(allsky_frame)
    del allsky_frame
    del allsky_original
    gc.collect()
    sky = build_sky_map(
        compact_allsky_frame,
        sky_settings,
        fisheye,
        result_dir,
        flat_applied=bool(allsky_cal_report.get("flat_frames")),
    )
    warnings.extend(sky.notes)
    orientation_available = fisheye.orientation_confidence not in {"unknown", "low"}
    directional_lookup_used = bool(
        orientation_available
        and sky.target_background_source in {"directional_interpolation", "nearby_reliable_cells"}
    )
    if orientation_available and sky.target_background_adu is not None:
        target_background_raw = float(sky.target_background_adu)
        selected_background_source = sky.target_background_source
    else:
        target_background_raw = float(sky.sky_median_adu)
        selected_background_source = "allsky_median_orientation_fallback"
    if not orientation_available:
        warnings.append(
            "이 전천 카메라의 북쪽 방향 보정값을 확인하지 못해 임의 방향 셀을 사용하지 않고 "
            "검증 가능한 전천 중앙 배경값으로 자동 대체했습니다. 카메라별 어안 보정 전까지 방향 지도는 진단용입니다."
        )
    elif sky.target_background_fallback_used:
        warnings.append(
            f"목표 방향의 엄격 보간 조건을 충족하지 못해 "
            f"{sky.target_background_source} 단계로 배경값을 대체했습니다. "
            "값은 유지하되 불확실성과 결과 제한을 함께 올립니다."
        )
    corrected_target_background = max(target_background_raw - current_allsky_offset, 0.0)
    corrected_sky_median = max(float(sky.sky_median_adu) - current_allsky_offset, 0.0)
    if corrected_target_background <= 0 or corrected_sky_median <= 0:
        raise ValueError("Bias/offset 보정 후 전천 배경값이 0 이하입니다. 전천 offset과 보정 프레임을 확인하세요.")
    bg_rate_adu, bg_method, bg_warnings = _background_rate(
        profile,
        corrected_target_background,
        corrected_sky_median,
        exposure,
        allsky_metadata,
        current_flat_applied=bool(allsky_cal_report.get("flat_frames")),
    )
    warnings.extend(bg_warnings)
    background_uncertainty_fraction = 0.0
    if orientation_available and sky.target_uncertainty_adu is not None and target_background_raw > 0:
        background_uncertainty_fraction = max(
            background_uncertainty_fraction,
            min(float(sky.target_uncertainty_adu) / target_background_raw, 1.0),
        )
    if bg_method == "c_sys_planning":
        background_uncertainty_fraction = max(background_uncertainty_fraction, 0.15)
    elif bg_method == "relative_fallback":
        background_uncertainty_fraction = max(background_uncertainty_fraction, 0.30)
    if not bool(allsky_cal_report.get("flat_frames")):
        background_uncertainty_fraction = max(background_uncertainty_fraction, 0.15)
    if not allsky_offset_known:
        background_uncertainty_fraction = max(background_uncertainty_fraction, 0.10)
    background_rate_for_plan = bg_rate_adu
    if background_uncertainty_fraction > 0:
        warnings.append(
            f"전천 보정과 배경 환산의 불확실성 {background_uncertainty_fraction:.0%}를 분리해 "
            "어두운 배경 경계는 읽기잡음 하한에, 밝은 배경 경계는 포화 상한에 적용했습니다."
        )

    # Airmass is a geometric observing-condition value and does not depend on
    # photometric zero-point availability. Store it explicitly for UI/JSON
    # validation even when the absolute target signal model is unavailable.
    current_airmass = airmass_from_altitude(target.get("alt_deg"))
    target["airmass"] = current_airmass

    signal_rate, signal_per_pixel, signal_source, signal_warnings, signal_diag = _signal_model(
        profile,
        target,
        max(1, effective_pixels),
        manual_target_mag,
        manual_surface_brightness_mag_arcsec2,
    )
    warnings.extend(signal_warnings)
    signal_uncertainty_fraction = _signal_model_uncertainty_fraction(
        signal_source,
        profile,
        limited_target_model=limited_target_model,
    )
    if signal_rate is not None and signal_uncertainty_fraction > 0:
        warnings.append(
            f"대상 신호 모델의 근거({signal_source})에 따라 신호율 불확실성을 "
            f"±{signal_uncertainty_fraction:.0%}로 표시하고 필요 장수 범위에 전파했습니다."
        )
    if target["target_mode"] == "point":
        n_pix = max(1, profile.reference_aperture_pixels or effective_pixels)
    else:
        n_pix = max(1, effective_pixels)

    target_structure: dict[str, Any]
    target_structure_plot: str | None = None
    if target["target_mode"] == "extended" and not limited_target_model:
        target_structure, structure_warnings, target_structure_plot = fetch_target_structure(
            ra_deg=target.get("ra_deg"),
            dec_deg=target.get("dec_deg"),
            target_size_deg=target.get("size_deg"),
            target_mode=target["target_mode"],
            result_dir=result_dir,
            survey=survey_for_filter(profile.filter_name),
        )
        warnings.extend(structure_warnings)
        if target_structure.get("status") == "ok":
            target_structure["filter_name"] = profile.filter_name or None
            target_structure["passband_role"] = "relative_morphology_only"
            if _is_narrowband_filter(profile.filter_name):
                if target_structure.get("confidence") == "high":
                    target_structure["confidence"] = "medium"
                warnings.append(
                    "협대역/다중대역에서는 DSS2 참조 형태가 실제 필터의 방출선 구조와 완전히 같지 않습니다. "
                    "구조 계수는 상대 형태 보조값이며 신뢰도를 한 단계 낮췄습니다."
                )
            object_text = f"{target.get('name','')} {target.get('object_type','')}".casefold()
            if "dark nebula" in object_text or "암흑성운" in object_text:
                target_structure["confidence"] = "low"
                warnings.append(
                    "암흑성운은 주변보다 어두운 흡수 구조이므로 양의 diffuse surface-brightness 모델과 맞지 않습니다. "
                    "구조 기반 총 적분시간을 강제하지 않습니다."
                )
    else:
        target_structure = {
            "status": "unavailable",
            "confidence": "none",
            "source": "not_applicable",
            "notes": ["점광원 또는 이동 태양계 대상에는 고정 하늘 survey 형태 분석을 적용하지 않습니다."],
        }

    plan = _build_plan(
        profile=profile,
        target=target,
        background_rate_adu_per_pix=background_rate_for_plan,
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
        background_uncertainty_fraction=background_uncertainty_fraction,
        signal_uncertainty_fraction=signal_uncertainty_fraction,
        target_signal_rate_e_per_pixel=signal_per_pixel,
        target_structure_model=target_structure,
    )
    warnings.extend(plan["warnings"])
    evidence_prior, evidence_warnings = exposure_evidence_prior(target, plan.get("recommended_sub_exposure_sec"))
    warnings.extend(evidence_warnings)

    curve_path = result_dir / "exposure_snr_curve.png"
    curve_min = max(0.05, min_sub_exposure_sec / 3.0)
    curve_max = max(curve_min * 10, min(max_sub_exposure_sec, max(plan.get("practical_upper_sec") or 0, 10.0)))
    xs = np.geomspace(curve_min, curve_max, 180)
    if signal_rate is not None and signal_rate > 0:
        bg_e = background_rate_for_plan * profile.gain_e_per_adu
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
    if not orientation_available:
        validity = "planning_only"
        validity_reasons.append(
            "카메라별 북쪽 방향 보정이 없어 목표 방향을 추측하지 않고 전천 중앙 배경으로 자동 대체했습니다."
        )
    elif sky.target_background_fallback_used:
        validity = "planning_only"
        validity_reasons.append(
            f"목표 지점의 엄격 보간 조건을 충족하지 못해 "
            f"{sky.target_background_source} 배경 대체값과 확대된 불확실성을 사용했습니다."
        )
    elif fisheye_errors:
        validity = "planning_only"
        validity_reasons.append("어안 보정 계수는 적용했지만 독립 각도 RMS 검증 자료가 없어 방향값을 관측 계획용으로 취급합니다.")
    if bg_method != "c_sys":
        validity = "planning_only"
        if bg_method == "relative_fallback":
            validity_reasons.append("현재 전천 조건에 직접 적용 가능한 Csys가 없어 방향별 상대 배경 환산을 사용했습니다.")
        else:
            validity_reasons.append("Csys는 적용했지만 전천 장비 설정 일치 여부가 완전히 검증되지 않았습니다.")
    approximate_signal = (
        signal_source.startswith("integrated_mag_plus_size")
        or "approximation" in signal_source
    )
    if signal_source == "unavailable" or approximate_signal:
        validity = "planning_only"
        if signal_source == "unavailable":
            validity_reasons.append("선택 천체의 절대 신호 모델이 없어 촬영 장수 계산이 제한됩니다.")
        elif "narrowband" in signal_source:
            validity_reasons.append("협대역/다중대역 필터에서 V등급 기반 신호 환산을 사용해 결과를 계획용 근사로 취급합니다.")
        elif "filter_mismatch" in signal_source:
            validity_reasons.append("장비 필터와 V등급 대역이 달라 색항을 알 수 없으므로 절대 신호를 계획용 근사로 취급합니다.")
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
        "target_structure_profile": (
            f"/results/{job_id}/{target_structure_plot}" if target_structure_plot else ""
        ),
        "target_structure_reference_fits": (
            f"/results/{job_id}/target_reference_dss2_red.fits"
            if (result_dir / "target_reference_dss2_red.fits").exists() else ""
        ),
        "result_json": f"/results/{job_id}/result.json",
    }
    result = {
        "job_id": job_id,
        "version": __version__,
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
            "selected_background_adu_raw": target_background_raw,
            "selected_background_source": selected_background_source,
            "directional_lookup_used": directional_lookup_used,
            "allsky_median_adu_raw": sky.sky_median_adu,
            "allsky_offset_adu": current_allsky_offset,
            "allsky_offset_known": allsky_offset_known,
            "allsky_offset_method": allsky_offset_method,
            "allsky_offset_diagnostics": allsky_offset_diagnostics,
            "target_allsky_background_adu": corrected_target_background,
            "allsky_median_adu": corrected_sky_median,
            "telescope_background_adu_per_sec_per_pixel": bg_rate_adu,
            "telescope_background_e_per_sec_per_pixel": bg_rate_adu * profile.gain_e_per_adu,
            "uncertainty_fraction_for_plan": background_uncertainty_fraction,
            "planning_background_adu_per_sec_per_pixel": background_rate_for_plan,
            "planning_background_e_per_sec_per_pixel": background_rate_for_plan * profile.gain_e_per_adu,
            "background_rate_lower_e_per_sec_per_pixel": (
                background_rate_for_plan
                * profile.gain_e_per_adu
                * (1.0 - background_uncertainty_fraction)
            ),
            "background_rate_upper_e_per_sec_per_pixel": (
                background_rate_for_plan
                * profile.gain_e_per_adu
                * (1.0 + background_uncertainty_fraction)
            ),
        },
        "target_signal_model": {
            "source": signal_source,
            "signal_e_per_sec": signal_rate,
            "signal_e_per_sec_per_pixel": signal_per_pixel,
            "effective_pixels": n_pix,
            "uncertainty_fraction": signal_uncertainty_fraction,
            "structure_applied": bool(plan.get("structure_aware_integration")),
            **signal_diag,
        },
        "target_structure_model": target_structure,
        "exposure_evidence_prior": evidence_prior,
        "plan": plan,
        "confidence": confidence,
        "warnings": list(dict.fromkeys(warnings)),
        "diagnostics": {
            "fisheye_validation_errors": fisheye_errors,
            "fisheye_selection": asdict(fisheye),
            "allsky_metadata": asdict(allsky_metadata),
            "allsky_calibration": allsky_cal_report,
            "observation_context": {
                "stellarium_time_utc": target.get("observation_time_utc"),
                "stellarium_time_local": target.get("observation_time_local"),
                "stellarium_latitude": target.get("latitude"),
                "stellarium_longitude": target.get("longitude"),
                "allsky_date_obs": allsky_metadata.date_obs,
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
