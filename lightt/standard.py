from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .models import StandardPhotometryConfig


def load_standard_config(path: Path) -> StandardPhotometryConfig:
    if not path.exists():
        return StandardPhotometryConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    cfg = StandardPhotometryConfig(
        enabled=bool(data.get("enabled", False)),
        apply_background_scenario=bool(data.get("apply_background_scenario", False)),
        allsky_zero_point=_optional(data.get("allsky_zero_point")),
        allsky_extinction_k=_optional(data.get("allsky_extinction_k")),
        allsky_color_term=float(data.get("allsky_color_term", 0.0)),
        allsky_fit_star_count=int(data.get("allsky_fit_star_count", 0)),
        allsky_fit_rms_mag=_optional(data.get("allsky_fit_rms_mag")),
        allsky_fit_data_hash=str(data.get("allsky_fit_data_hash") or "") or None,
        telescope_zero_point=_optional(data.get("telescope_zero_point")),
        telescope_extinction_k=_optional(data.get("telescope_extinction_k")),
        telescope_fit_star_count=int(data.get("telescope_fit_star_count", 0)),
        telescope_fit_rms_mag=_optional(data.get("telescope_fit_rms_mag")),
        telescope_fit_data_hash=str(data.get("telescope_fit_data_hash") or "") or None,
        telescope_pixel_scale_arcsec=_optional(data.get("telescope_pixel_scale_arcsec")),
    )
    return cfg


def _optional(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_standard_config(cfg: StandardPhotometryConfig) -> list[str]:
    errors: list[str] = []
    if not cfg.enabled:
        return errors
    if cfg.allsky_zero_point is None or cfg.allsky_extinction_k is None:
        errors.append("전천카메라 zero point와 extinction k가 필요합니다.")
    if cfg.allsky_fit_star_count < 10:
        errors.append("전천카메라 회귀 표준성이 10개 미만입니다.")
    if cfg.allsky_fit_rms_mag is None or cfg.allsky_fit_rms_mag > 0.20:
        errors.append("전천카메라 회귀 RMS가 없거나 0.20 mag보다 큽니다.")
    if not cfg.allsky_fit_data_hash:
        errors.append("전천카메라 회귀 원본 자료의 해시가 기록되지 않았습니다.")
    if abs(cfg.allsky_color_term) > 1e-12:
        errors.append("별 색항을 확산 하늘 배경에 직접 적용할 수 없어 allsky_color_term은 0이어야 합니다.")
    if cfg.apply_background_scenario:
        if cfg.telescope_zero_point is None or cfg.telescope_extinction_k is None:
            errors.append(
                "망원경 background transfer에는 망원경 zero point와 extinction k가 필요합니다."
            )
        if cfg.telescope_fit_star_count < 10:
            errors.append("망원경 회귀 표준성이 10개 미만입니다.")
        if cfg.telescope_fit_rms_mag is None or cfg.telescope_fit_rms_mag > 0.20:
            errors.append("망원경 회귀 RMS가 없거나 0.20 mag보다 큽니다.")
        if not cfg.telescope_fit_data_hash:
            errors.append("망원경 회귀 원본 자료의 해시가 기록되지 않았습니다.")
        if cfg.telescope_pixel_scale_arcsec is None or cfg.telescope_pixel_scale_arcsec <= 0:
            errors.append("망원경 pixel scale이 필요합니다.")
    if cfg.allsky_extinction_k is not None and not 0 <= cfg.allsky_extinction_k <= 1.5:
        errors.append("전천카메라 extinction k가 물리적 범위를 벗어납니다.")
    if cfg.telescope_extinction_k is not None and not 0 <= cfg.telescope_extinction_k <= 1.5:
        errors.append("망원경 extinction k가 물리적 범위를 벗어납니다.")
    return errors


def airmass_kasten_young(altitude_deg: float) -> float:
    altitude = float(np.clip(altitude_deg, 0.1, 90.0))
    zenith = 90.0 - altitude
    return float(1.0 / (math.cos(math.radians(zenith)) + 0.50572 * (96.07995 - zenith) ** -1.6364))


def instrumental_magnitude(adu: float, exposure_sec: float) -> float:
    if adu <= 0 or exposure_sec <= 0:
        raise ValueError("Instrumental magnitude에는 양의 ADU와 노출시간이 필요합니다.")
    return float(-2.5 * math.log10(adu / exposure_sec))


def sky_surface_brightness_mag_arcsec2(
    adu_per_sample: float,
    exposure_sec: float,
    solid_angle_arcsec2: float,
    altitude_deg: float,
    cfg: StandardPhotometryConfig,
) -> float:
    if adu_per_sample <= 0 or exposure_sec <= 0:
        raise ValueError("표준광도 계산에는 양의 ADU와 전천 영상 노출시간이 필요합니다.")
    if solid_angle_arcsec2 <= 0:
        raise ValueError("픽셀 입체각은 양수여야 합니다.")
    errors = validate_standard_config(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    m_inst = instrumental_magnitude(adu_per_sample, exposure_sec)
    X = airmass_kasten_young(altitude_deg)
    assert cfg.allsky_extinction_k is not None
    assert cfg.allsky_zero_point is not None
    # V_cat - m_inst = -kX + Z, therefore calibrated V = m_inst - kX + Z.
    calibrated_sample_mag = (
        m_inst - float(cfg.allsky_extinction_k) * X + float(cfg.allsky_zero_point)
    )
    return float(calibrated_sample_mag + 2.5 * math.log10(solid_angle_arcsec2))


def telescope_background_adu_per_sec_per_pixel(
    sky_mag_arcsec2: float,
    altitude_deg: float,
    cfg: StandardPhotometryConfig,
) -> float:
    errors = validate_standard_config(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    if not cfg.apply_background_scenario:
        raise ValueError("표준광도 background transfer가 명시적으로 활성화되지 않았습니다.")
    assert cfg.telescope_pixel_scale_arcsec is not None
    assert cfg.telescope_extinction_k is not None
    assert cfg.telescope_zero_point is not None
    pixel_area = float(cfg.telescope_pixel_scale_arcsec) ** 2
    pixel_mag = sky_mag_arcsec2 - 2.5 * math.log10(pixel_area)
    X = airmass_kasten_young(altitude_deg)
    # m_inst,tel = V + kX - Z; ADU/s = 10^(-0.4*m_inst).
    m_inst_tel = pixel_mag + float(cfg.telescope_extinction_k) * X - float(cfg.telescope_zero_point)
    return float(10 ** (-0.4 * m_inst_tel))
