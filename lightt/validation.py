from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

import numpy as np

from .models import FisheyeConfig, ImageFrame


def assess_image_input(
    frame: ImageFrame,
    *,
    role: str,
    fisheye: FisheyeConfig | None = None,
    sample_max_pixels: int | None = None,
) -> dict[str, Any]:
    """Return a non-fatal, machine-readable input quality assessment.

    The assessment separates blockers from recoverable limitations.  Callers
    can keep running with conservative fallbacks instead of treating every
    missing metadata field as an opaque server error.
    """
    warnings: list[str] = []
    actions: list[str] = []
    recoveries: list[str] = []
    checks: dict[str, Any] = {}
    metadata = frame.metadata
    array = np.asarray(frame.intensity)
    sample = array
    sample_step = 1
    if sample_max_pixels is not None and sample_max_pixels > 0 and array.size > sample_max_pixels:
        sample_step = max(1, int(math.ceil(math.sqrt(array.size / sample_max_pixels))))
        sample = array[::sample_step, ::sample_step]

    finite = np.isfinite(sample)
    finite_fraction = float(np.mean(finite)) if sample.size else 0.0
    checks["finite_fraction"] = finite_fraction
    checks["inspection_sample_step"] = sample_step
    checks["shape"] = [int(metadata.height), int(metadata.width)]
    checks["source_type"] = metadata.source_type
    if array.ndim != 2 or min(array.shape, default=0) < 32:
        actions.append("영상은 최소 32×32의 2차원 밝기 평면이어야 합니다.")
    if finite_fraction < 0.90:
        actions.append("NaN/Inf가 10% 이상이라 유효 픽셀을 확보할 수 없습니다.")
    elif finite_fraction < 0.999:
        warnings.append("일부 NaN/Inf 픽셀은 자동 제외합니다.")

    finite_values = np.asarray(sample[finite], dtype=np.float64)
    if finite_values.size:
        p01, p50, p99 = np.percentile(finite_values, [1.0, 50.0, 99.0])
        checks.update({"p01": float(p01), "median": float(p50), "p99": float(p99)})
        if not math.isfinite(float(p99 - p01)) or p99 <= p01:
            actions.append("영상 밝기 범위가 없어 하늘 배경을 구분할 수 없습니다.")

    if metadata.source_type == "rendered":
        warnings.append("JPG/PNG/TIFF는 내부 톤커브 가능성이 있어 절대 ADU 계산을 진단용으로 낮춥니다.")
        recoveries.append("방향별 상대 밝기와 미리보기 분석만 유지")
    if metadata.exposure_sec is None:
        actions.append("노출시간 메타데이터가 없습니다. 분석 전에 노출시간을 입력해야 합니다.")
    exposure_provenance = metadata.extra.get("exposure_provenance")
    if isinstance(exposure_provenance, dict):
        checks["exposure_provenance"] = exposure_provenance
        exposure_confidence = str(exposure_provenance.get("confidence") or "none")
        conflicts = [str(item) for item in (exposure_provenance.get("conflicts") or [])]
        if conflicts:
            warnings.append(
                "노출시간 헤더 항목들이 서로 다릅니다: " + " ".join(conflicts[:3])
            )
            recoveries.append(
                "표준 EXPTIME/EXPOSURE를 우선 선택하고 충돌 내역을 결과에 보존"
            )
        elif exposure_confidence == "medium":
            warnings.append(
                "노출시간을 표준 EXPTIME 대신 detector DIT/NDIT 관계에서 유도했습니다."
            )
            recoveries.append("DIT×NDIT로 영상의 총 적분시간을 계산")
    if not metadata.date_obs:
        warnings.append("촬영시각이 없어 Stellarium과 영상 시각 일치를 자동 검증할 수 없습니다.")
    if not metadata.camera:
        warnings.append("카메라 식별자가 없어 전용 보정 대신 영상 기반 자동 판정을 사용합니다.")

    if role == "allsky" and fisheye is not None:
        checks["fisheye"] = asdict(fisheye)
        if fisheye.selection_source == "inferred_circular_footprint":
            recoveries.append("원형 하늘 영역의 중심과 반경을 영상에서 자동 감지")
        elif fisheye.selection_source == "centered_fallback":
            warnings.append("원형 하늘 경계를 확정하지 못해 중앙 equidistant 형상을 진단용으로 가정합니다.")
            recoveries.append("카메라 전용 형상 대신 보수적 중앙 형상 사용")
        if fisheye.orientation_confidence in {"unknown", "low"}:
            warnings.append("카메라별 북쪽 방향 보정이 없어 목표 방향 배경을 직접 조회하지 않습니다.")
            recoveries.append("잘못된 방향 추정 대신 전천 중앙 배경으로 대체")

    if actions:
        status = "needs_input"
    elif warnings:
        status = "usable_with_fallbacks"
    else:
        status = "ready"
    return {
        "status": status,
        "warnings": list(dict.fromkeys(warnings)),
        "required_actions": list(dict.fromkeys(actions)),
        "automatic_recoveries": list(dict.fromkeys(recoveries)),
        "checks": checks,
    }
