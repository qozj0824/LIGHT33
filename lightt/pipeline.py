from __future__ import annotations

import json
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np

from .coordinates import resolve_target_altaz
from .geometry import select_fisheye_config, validate_fisheye_calibration
from .io import apply_calibration, infer_intensity_domain, load_image, resolve_exposure
from .models import AnalysisResult, AnalysisSettings, CalibrationSet
from .photometry import (
    analyze_saturation,
    measure_extended_source,
    measure_point_source,
    measure_stars,
    sensor_background_for_measurement,
)
from .planning import build_exposure_plan, predict_single_snr
from .sky import build_sky_map
from .standard import (
    load_standard_config,
    sky_surface_brightness_mag_arcsec2,
    telescope_background_adu_per_sec_per_pixel,
    validate_standard_config,
)
from .visualization import (
    save_adu_histogram,
    save_exposure_snr_curve,
    save_saturation_diagnostic,
    save_scope_overlay,
    save_scope_preview,
)


def _public_artifact(job_id: str, filename: str) -> str:
    return f"/results/{job_id}/{filename}" if filename else ""


def _validity(
    *,
    scope_rendered: bool,
    allsky_rendered: bool,
    plan_status: str,
    calibration_applied: bool,
    standard_errors: list[str],
    sky_good_fraction: float,
    auto_roi: bool,
    auto_roi_confirmed: bool,
    target_mode: str,
    point_target_selected: bool,
    fisheye_errors: list[str],
    clip_confirmed: bool,
    noise_parameters_confirmed: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if plan_status != "ok":
        return "invalid", ["안전한 촬영 계획을 확정할 수 없습니다."]
    if scope_rendered:
        return "diagnostic_only", ["망원경 JPG/PNG는 선형 센서 ADU를 보존하지 않아 진단용입니다."]
    if not clip_confirmed:
        return "diagnostic_only", ["센서 포화 ADU가 확인되지 않았습니다."]
    if not noise_parameters_confirmed:
        reasons.append("Gain·읽기잡음이 장비 사양으로 확인되지 않아 SNR은 참고값입니다.")
    if allsky_rendered:
        reasons.append("전천 영상이 JPG/PNG라 방향별 배경은 상대 비교용입니다.")
    if target_mode == "extended" and auto_roi and not auto_roi_confirmed:
        reasons.append("자동 ROI를 아직 사용자가 확인하지 않았습니다.")
    if target_mode == "point" and not point_target_selected:
        reasons.append("목표별 ROI가 없어 대표 별을 사용했습니다.")
    reasons.extend(fisheye_errors)
    if sky_good_fraction < 0.25:
        reasons.append("전천지도에서 신뢰도 '좋음' 셀 비율이 낮습니다.")
    if not calibration_applied:
        reasons.append("두 카메라 모두에 bias/dark/flat 보정이 적용된 상태가 아닙니다.")
    if standard_errors:
        reasons.append("표준광도 보정은 비활성 또는 미검증 상태입니다.")
    if reasons:
        return "planning_only", reasons
    return "quantitative_candidate", ["필수 조건은 충족했지만 독립 실관측 검증은 여전히 필요합니다."]


def _beginner_summary(
    settings: AnalysisSettings,
    plan_status: str,
    recommended: float | None,
    frames: int | None,
    total: float | None,
    confidence: str,
    limiting_constraint: str,
    warnings: list[str],
    sky_factor: float | None,
) -> dict[str, Any]:
    constraint_names = {
        "saturation": "밝은 별 포화",
        "background": "하늘 배경 밝기",
        "tracking": "추적 가능 시간",
        "user_max": "사용자가 정한 최대 노출",
        "sensor_clip_unconfirmed": "센서 포화값 미확인",
        "saturation_unresolved": "포화 기준별 부족",
    }
    if plan_status == "ok" and recommended is not None and frames is not None:
        headline = f"권장 계획: 프레임당 {recommended:g}초, 최소 {frames}장"
        detail = (
            f"예상 총 적분시간은 {total/60:.1f}분입니다. "
            f"주요 제한 조건은 '{constraint_names.get(limiting_constraint, limiting_constraint)}'입니다."
            if total is not None
            else ""
        )
        next_steps = [
            "초기 시험 촬영에서 밝은 별의 포화 여부를 확인합니다.",
            "구름·달빛·대기 투명도가 변한 경우 전천 영상을 갱신하여 재분석합니다.",
            "자동 ROI를 사용한 경우 결과 오버레이에서 대상·배경 영역의 적절성을 검토합니다.",
        ]
    else:
        headline = "권장 노출시간을 확정할 수 없음"
        detail = warnings[0] if warnings else "입력값과 시험 영상의 유효성을 검토해야 합니다."
        next_steps = [
            "카메라의 실제 포화 ADU를 확인하여 입력해야 합니다.",
            "포화되지 않은 별을 포함하는 더 짧은 시험 영상이 필요합니다.",
            "대상 ROI와 대표 배경 ROI를 직접 지정해야 합니다.",
        ]
    if sky_factor is None:
        sky_text = "목표 방향의 하늘 배경은 신뢰도 기준을 충족하지 않아 정량 비교에서 제외되었습니다."
    elif sky_factor > 1.15:
        sky_text = "목표 방향의 하늘 배경은 전천 중앙값보다 밝으며, 광해·달빛·구름 등의 영향 가능성이 큽니다."
    elif sky_factor < 0.85:
        sky_text = "목표 방향의 하늘 배경은 전천 중앙값보다 상대적으로 낮습니다."
    else:
        sky_text = "목표 방향의 하늘 배경은 전천 중앙값과 유사합니다."
    return {
        "headline": headline,
        "detail": detail,
        "confidence": confidence,
        "sky_text": sky_text,
        "next_steps": next_steps,
        "target_name": settings.target_name or "이름 미입력",
    }


def run_analysis(
    *,
    allsky_path: Path,
    scope_path: Path,
    settings: AnalysisSettings,
    allsky_calibration: CalibrationSet,
    scope_calibration: CalibrationSet,
    project_root: Path,
    result_root: Path,
) -> AnalysisResult:
    job_id = uuid.uuid4().hex[:12]
    result_dir = result_root / job_id
    result_dir.mkdir(parents=True, exist_ok=False)
    warnings: list[str] = []

    resolved_alt, resolved_az, coordinate_diagnostics = resolve_target_altaz(settings)
    settings.target_alt_deg = resolved_alt
    settings.target_az_deg = resolved_az

    allsky_original = load_image(allsky_path)
    scope_original = load_image(scope_path)
    current_exposure, exposure_source = resolve_exposure(
        scope_original, settings.current_exposure_sec, settings.exposure_mode
    )
    allsky_exposure_for_calibration = settings.allsky_exposure_sec or allsky_original.metadata.exposure_sec
    allsky_frame, allsky_cal = apply_calibration(
        allsky_original,
        allsky_calibration,
        light_exposure_sec=allsky_exposure_for_calibration,
    )
    scope_frame, scope_cal = apply_calibration(
        scope_original,
        scope_calibration,
        light_exposure_sec=current_exposure,
    )
    for report in (allsky_cal, scope_cal):
        report_warnings = report.get("warnings")
        if isinstance(report_warnings, list):
            warnings.extend(str(item) for item in report_warnings)

    photometry_settings = replace(
        settings,
        bias_offset_adu=0.0 if bool(scope_cal.get("offset_removed")) else settings.bias_offset_adu,
    )
    domain = infer_intensity_domain(
        scope_original,
        settings.sensor_clip_adu,
        settings.saturation_safety_fraction,
    )
    warnings.extend(domain.warnings)

    stars = measure_stars(
        scope_frame.intensity,
        domain,
        photometry_settings,
        current_exposure_sec=current_exposure,
    )
    saturation_source = (
        scope_original.saturation_intensity
        if scope_original.saturation_intensity is not None
        else scope_original.raw_intensity
        if scope_original.raw_intensity is not None
        else scope_original.intensity
    )
    saturation = analyze_saturation(
        saturation_source,
        domain,
        stars,
        settings.saturation_policy,
    )
    if settings.target_mode == "point":
        measurement = measure_point_source(scope_frame.intensity, stars, photometry_settings)
    else:
        measurement = measure_extended_source(
            scope_frame.intensity,
            photometry_settings,
            current_exposure_sec=current_exposure,
        )
    raw_background_source = (
        scope_original.raw_intensity
        if scope_original.raw_intensity is not None
        else scope_original.intensity
    )
    sensor_background = sensor_background_for_measurement(raw_background_source, measurement)
    measurement = replace(measurement, sensor_background_adu_per_pixel=sensor_background)

    plan = build_exposure_plan(
        measurement,
        saturation,
        domain,
        photometry_settings,
        current_exposure,
        sensor_bias_offset_adu=settings.bias_offset_adu,
    )
    warnings.extend(plan.warnings)

    fisheye = select_fisheye_config(
        project_root,
        camera_name=allsky_original.metadata.camera,
        filename=allsky_original.metadata.filename,
        width=allsky_original.metadata.width,
        height=allsky_original.metadata.height,
        image=allsky_original.intensity,
    )
    fisheye_errors = validate_fisheye_calibration(fisheye)
    sky = build_sky_map(
        allsky_frame,
        settings,
        fisheye,
        result_dir,
        flat_applied=bool(allsky_cal.get("flat_frames")),
    )
    warnings.extend(sky.notes)

    standard_cfg = load_standard_config(project_root / "config" / "standard_photometry.json")
    standard_errors = validate_standard_config(standard_cfg)
    standard_diagnostics: dict[str, Any] = {
        "enabled": standard_cfg.enabled,
        "apply_background_scenario": standard_cfg.apply_background_scenario,
        "validation_errors": standard_errors,
        "note": "표준 시나리오는 측정 대상 신호를 변경하지 않으며 별도 참고값으로만 표시합니다.",
    }
    if standard_cfg.enabled and standard_errors:
        warnings.append("표준광도 설정 오류: " + "; ".join(standard_errors))
    elif standard_cfg.enabled:
        allsky_exposure = settings.allsky_exposure_sec or allsky_original.metadata.exposure_sec
        if allsky_exposure is None or allsky_exposure <= 0:
            standard_diagnostics.update(
                status="unavailable",
                reason="전천 영상 노출시간이 없어 1초로 가정하지 않았습니다.",
            )
        elif sky.target_background_adu is None or sky.target_solid_angle_arcsec2 is None:
            standard_diagnostics.update(
                status="unavailable",
                reason="목표 방향 ADU 또는 픽셀 입체각을 계산하지 못했습니다.",
            )
        else:
            sky_mag = sky_surface_brightness_mag_arcsec2(
                sky.target_background_adu,
                allsky_exposure,
                sky.target_solid_angle_arcsec2,
                settings.target_alt_deg,
                standard_cfg,
            )
            standard_diagnostics.update(
                status="calculated",
                allsky_exposure_sec=allsky_exposure,
                target_sky_v_mag_arcsec2=sky_mag,
                target_solid_angle_arcsec2=sky.target_solid_angle_arcsec2,
            )
            if standard_cfg.apply_background_scenario:
                standard_diagnostics["telescope_background_adu_per_sec_per_pixel"] = (
                    telescope_background_adu_per_sec_per_pixel(
                        sky_mag,
                        settings.target_alt_deg,
                        standard_cfg,
                    )
                )

    scope_preview = result_dir / "scope_preview.png"
    scope_overlay = result_dir / "scope_overlay.png"
    adu_histogram = result_dir / "scope_adu_histogram.png"
    saturation_diagnostic = result_dir / "scope_saturation_diagnostic.png"
    exposure_curve = result_dir / "exposure_snr_curve.png"
    save_scope_preview(scope_frame.intensity, scope_preview)
    save_scope_overlay(scope_frame.intensity, measurement, stars, scope_overlay)
    save_adu_histogram(
        scope_frame.intensity,
        adu_histogram,
        sensor_clip_adu=domain.sensor_clip_adu if domain.quantitative_saturation_supported else None,
        saturation_threshold_adu=domain.saturation_threshold_adu if domain.quantitative_saturation_supported else None,
    )
    save_saturation_diagnostic(
        saturation_source,
        domain.saturation_threshold_adu,
        saturation_diagnostic,
    )
    curve_min = max(0.001, min(settings.min_sub_exposure_sec, current_exposure / 20.0))
    curve_max_candidates = [
        settings.max_sub_exposure_sec,
        current_exposure * 4.0,
        plan.practical_upper_sec or 0.0,
        plan.exposure_for_single_frame_target_snr_sec or 0.0,
    ]
    curve_max = max(curve_min * 10.0, min(max(curve_max_candidates), 86400.0))
    exposure_seconds = np.geomspace(curve_min, curve_max, 180)
    snr_values = np.array(
        [predict_single_snr(value, measurement, photometry_settings, current_exposure) for value in exposure_seconds],
        dtype=np.float64,
    )
    save_exposure_snr_curve(
        exposure_seconds,
        snr_values,
        exposure_curve,
        current_exposure_sec=current_exposure,
        current_snr=measurement.current_snr,
        target_snr=settings.target_snr,
        recommended_exposure_sec=plan.recommended_sub_exposure_sec,
        practical_upper_sec=plan.practical_upper_sec,
    )

    validity, validity_reasons = _validity(
        scope_rendered=domain.is_rendered,
        allsky_rendered=allsky_original.metadata.source_type == "rendered",
        plan_status=plan.status,
        calibration_applied=bool(allsky_cal.get("applied") and scope_cal.get("applied")),
        standard_errors=standard_errors if standard_cfg.enabled else ["disabled"],
        sky_good_fraction=sky.good_fraction,
        auto_roi=settings.auto_roi,
        auto_roi_confirmed=settings.auto_roi_confirmed,
        target_mode=settings.target_mode,
        point_target_selected=bool(measurement.target_roi),
        fisheye_errors=fisheye_errors,
        clip_confirmed=domain.quantitative_saturation_supported,
        noise_parameters_confirmed=settings.noise_parameters_confirmed,
    )
    beginner_summary = _beginner_summary(
        settings,
        plan.status,
        plan.recommended_sub_exposure_sec,
        plan.frames,
        plan.total_integration_sec,
        plan.confidence,
        plan.limiting_constraint,
        [*plan.warnings, *([f"어안 보정 검증: {msg}" for msg in fisheye_errors] if fisheye_errors else [])],
        None if fisheye_errors else sky.target_relative_factor,
    )
    diagnostics: dict[str, Any] = {
        "current_exposure_sec": current_exposure,
        "exposure_source": exposure_source,
        "target_name": settings.target_name,
        "star_candidates_accepted": len(stars),
        "unsaturated_star_count": sum(not star.saturated for star in stars),
        "median_star_fwhm_px": float(np.median([star.fwhm_px for star in stars])) if stars else None,
        "allsky_calibration": allsky_cal,
        "scope_calibration": scope_cal,
        "target_coordinates": coordinate_diagnostics,
        "fisheye_validation_errors": fisheye_errors,
        "standard_photometry": standard_diagnostics,
        "sky_target_is_context_only": True,
        "settings": asdict(settings),
        "effective_photometry_bias_offset_adu": photometry_settings.bias_offset_adu,
    }
    artifacts = {
        "scope_preview": _public_artifact(job_id, scope_preview.name),
        "scope_overlay": _public_artifact(job_id, scope_overlay.name),
        "scope_adu_histogram": _public_artifact(job_id, adu_histogram.name),
        "scope_saturation": _public_artifact(job_id, saturation_diagnostic.name),
        "exposure_snr_curve": _public_artifact(job_id, exposure_curve.name),
        "allsky_preview": _public_artifact(job_id, sky.preview_path or ""),
        "allsky_coordinate_overlay": _public_artifact(job_id, sky.coordinate_overlay_path or ""),
        "sky_map": _public_artifact(job_id, sky.map_path or ""),
        "sky_relative_map": _public_artifact(job_id, sky.relative_map_path or ""),
        "sky_polar_map": _public_artifact(job_id, sky.polar_map_path or ""),
        "sky_reliability": _public_artifact(job_id, sky.reliability_path or ""),
        "sky_altitude_profiles": _public_artifact(job_id, sky.horizon_profile_path or ""),
        "sky_distribution": _public_artifact(job_id, sky.distribution_path or ""),
        "sky_table": _public_artifact(job_id, sky.table_path or ""),
    }
    result = AnalysisResult(
        job_id=job_id,
        validity=validity,  # type: ignore[arg-type]
        validity_reasons=validity_reasons,
        beginner_summary=beginner_summary,
        scope_metadata=scope_original.metadata,
        allsky_metadata=allsky_original.metadata,
        intensity_domain=domain,
        measurement=measurement,
        saturation=saturation,
        plan=plan,
        sky=sky,
        artifacts=artifacts,
        diagnostics=diagnostics,
        warnings=list(dict.fromkeys(warnings)),
    )
    artifacts["result_json"] = _public_artifact(job_id, "result.json")
    (result_dir / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return result
