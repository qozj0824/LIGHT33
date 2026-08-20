from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .models import FisheyeConfig


def load_fisheye_config(path: Path) -> FisheyeConfig:
    import json

    if not path.exists():
        return FisheyeConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    return FisheyeConfig(
        mode=str(data.get("mode", "auto_equidistant")),
        center_x=_optional_float(data.get("center_x")),
        center_y=_optional_float(data.get("center_y")),
        horizon_radius=_optional_float(data.get("horizon_radius")),
        sensor_width=_optional_int(data.get("sensor_width")),
        sensor_height=_optional_int(data.get("sensor_height")),
        north_offset_deg=float(data.get("north_offset_deg", 0.0)),
        E=_optional_float(data.get("E")),
        a0=_optional_float(data.get("a0")),
        eps=_optional_float(data.get("eps")),
        coefficients=[float(v) for v in data.get("coefficients", [])],
        fit_star_count=int(data.get("fit_star_count", 0) or 0),
        fit_rms_deg=_optional_float(data.get("fit_rms_deg")),
        fit_edge_rms_deg=_optional_float(data.get("fit_edge_rms_deg")),
        validation_star_count=int(data.get("validation_star_count", 0) or 0),
        validation_max_error_px=_optional_float(data.get("validation_max_error_px")),
        validation_basis=str(data.get("validation_basis") or "") or None,
        calibration_date=str(data.get("calibration_date") or "") or None,
        camera_lens_id=str(data.get("camera_lens_id") or "") or None,
    )


def _optional_int(value: object) -> int | None:
    try:
        result = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _optional_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None



def validate_fisheye_calibration(config: FisheyeConfig) -> list[str]:
    """Return reasons why a fisheye model should not be treated as quantitative."""
    errors: list[str] = []
    if config.mode != "calibrated_kannala_brandt":
        errors.append("표준별로 검증된 Kannala–Brandt 보정이 아닙니다.")
        return errors
    if any(value is None for value in (config.center_x, config.center_y, config.E, config.a0, config.eps)):
        errors.append("어안 중심·자세 파라미터가 완전하지 않습니다.")
    if not config.coefficients:
        errors.append("어안 왜곡계수가 없습니다.")
    if config.fit_star_count < 15:
        errors.append("어안 보정에 사용한 표준별 수가 15개 미만이거나 기록되지 않았습니다.")
    if config.fit_rms_deg is None or config.fit_rms_deg > 1.0:
        errors.append("어안 보정 RMS 각오차가 없거나 1.0°보다 큽니다.")
    if config.fit_edge_rms_deg is None or config.fit_edge_rms_deg > 2.0:
        errors.append("어안 가장자리 RMS 각오차가 없거나 2.0°보다 큽니다.")
    if not config.camera_lens_id:
        errors.append("보정에 사용한 카메라·렌즈 식별자가 기록되지 않았습니다.")
    return errors


def validate_fisheye_directional_calibration(config: FisheyeConfig) -> list[str]:
    """Validate the fisheye solution for azimuth/altitude direction lookup.

    The research report records a 30-star fit and independent grid-point checks
    within 5 detector pixels.  That evidence is appropriate for selecting a
    directional sky-background cell.  It is deliberately kept separate from
    ``validate_fisheye_calibration`` because a pixel residual alone does not
    establish an angular RMS or the accuracy of per-pixel solid angle.
    """
    errors: list[str] = []
    if config.mode != "calibrated_kannala_brandt":
        errors.append("보고서의 기준별 보정 Kannala–Brandt 모델이 아닙니다.")
        return errors
    if any(value is None for value in (config.center_x, config.center_y, config.E, config.a0, config.eps)):
        errors.append("어안 중심·자세 파라미터가 완전하지 않습니다.")
    if not config.coefficients:
        errors.append("어안 왜곡계수가 없습니다.")
    stars = max(config.fit_star_count, config.validation_star_count)
    if stars < 20:
        errors.append("방향 보정에 사용한 기준별 수가 20개 미만이거나 기록되지 않았습니다.")
    if config.validation_max_error_px is None or config.validation_max_error_px > 5.0:
        errors.append("방향 보정의 독립 검증 최대오차가 없거나 5 px보다 큽니다.")
    if not config.camera_lens_id:
        errors.append("보정에 사용한 카메라·렌즈 식별자가 기록되지 않았습니다.")
    return errors

def odd_polynomial_theta(radius_pixels: np.ndarray, coefficients: list[float]) -> np.ndarray:
    radius = np.asarray(radius_pixels, dtype=np.float64)
    theta = np.zeros_like(radius)
    for index, coefficient in enumerate(coefficients):
        theta += float(coefficient) * np.power(radius, 2 * index + 1)
    return theta


def odd_polynomial_derivative(radius_pixels: np.ndarray, coefficients: list[float]) -> np.ndarray:
    radius = np.asarray(radius_pixels, dtype=np.float64)
    derivative = np.zeros_like(radius)
    for index, coefficient in enumerate(coefficients):
        power = 2 * index + 1
        derivative += power * float(coefficient) * np.power(radius, power - 1)
    return derivative


def auto_geometry(shape: tuple[int, int]) -> tuple[float, float, float]:
    height, width = shape
    center_x = (width - 1) / 2.0
    center_y = (height - 1) / 2.0
    horizon_radius = min(width, height) * 0.48
    return center_x, center_y, horizon_radius


def pixel_to_altaz(
    x: np.ndarray,
    y: np.ndarray,
    shape: tuple[int, int],
    config: FisheyeConfig,
    *,
    coordinate_scale_x: float = 1.0,
    coordinate_scale_y: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if config.mode == "calibrated_kannala_brandt":
        required = [config.center_x, config.center_y, config.E, config.a0, config.eps]
        if any(value is None for value in required) or len(config.coefficients) < 1:
            raise ValueError("보정 어안 모드의 중심·E·a0·eps·왜곡계수가 완전하지 않습니다.")
        assert config.center_x is not None
        assert config.center_y is not None
        assert config.E is not None
        assert config.a0 is not None
        assert config.eps is not None
        original_x = (x + 0.5) * coordinate_scale_x - 0.5
        original_y = (y + 0.5) * coordinate_scale_y - 0.5
        dx = original_x - float(config.center_x)
        dy = original_y - float(config.center_y)
        radius = np.hypot(dx, dy)
        u0 = odd_polynomial_theta(radius, config.coefficients)
        ca = np.arctan2(dx, dy) + np.pi
        E = float(config.E)
        eps = float(config.eps)
        X = float(config.a0) - E
        b = X + ca
        cos_ze = np.cos(u0) * np.cos(eps) - np.cos(b) * np.sin(u0) * np.sin(eps)
        ze = np.arccos(np.clip(cos_ze, -1.0, 1.0))
        amE = np.arctan2(
            np.sin(b) * np.sin(u0),
            np.cos(b) * np.sin(u0) * np.cos(eps) + np.cos(u0) * np.sin(eps),
        )
        az = np.mod(amE + E, 2 * np.pi)
        alt = np.pi / 2 - ze
        valid = (
            np.isfinite(alt)
            & np.isfinite(az)
            & np.isfinite(u0)
            & (alt >= -np.deg2rad(5.0))
            & (alt <= np.deg2rad(90.5))
            & (u0 >= 0)
            & (u0 <= np.deg2rad(100.0))
        )
        return np.rad2deg(az), np.rad2deg(alt), valid

    center_x, center_y, horizon_radius = auto_geometry(shape)
    if config.center_x is not None:
        center_x = config.center_x / coordinate_scale_x
    if config.center_y is not None:
        center_y = config.center_y / coordinate_scale_y
    if config.horizon_radius is not None:
        horizon_radius = config.horizon_radius / math.sqrt(coordinate_scale_x * coordinate_scale_y)
    dx = x - center_x
    dy = y - center_y
    radius = np.hypot(dx, dy)
    normalized = radius / max(horizon_radius, 1e-9)
    mode = config.mode
    if mode == "equisolid":
        theta = 2.0 * np.arcsin(np.clip(normalized * math.sin(math.pi / 4), 0, 1))
    elif mode == "stereographic":
        theta = 2.0 * np.arctan(normalized * math.tan(math.pi / 4))
    elif mode == "orthographic":
        theta = np.arcsin(np.clip(normalized, 0, 1))
    else:
        theta = normalized * math.pi / 2
    alt = 90.0 - np.rad2deg(theta)
    az = np.mod(np.rad2deg(np.arctan2(dx, -dy)) + config.north_offset_deg, 360.0)
    valid = np.isfinite(alt) & np.isfinite(az) & (normalized <= 1.02) & (alt >= -2.0)
    return az, alt, valid


def pixel_solid_angle_arcsec2(
    x: np.ndarray,
    y: np.ndarray,
    config: FisheyeConfig,
    *,
    coordinate_scale_x: float = 1.0,
    coordinate_scale_y: float = 1.0,
    area_multiplier: float = 1.0,
) -> np.ndarray:
    if (
        config.mode != "calibrated_kannala_brandt"
        or config.center_x is None
        or config.center_y is None
        or not config.coefficients
    ):
        raise ValueError("픽셀 입체각에는 보정 Kannala–Brandt 모델이 필요합니다.")
    original_x = (np.asarray(x, dtype=np.float64) + 0.5) * coordinate_scale_x - 0.5
    original_y = (np.asarray(y, dtype=np.float64) + 0.5) * coordinate_scale_y - 0.5
    radius = np.hypot(original_x - config.center_x, original_y - config.center_y)
    theta = odd_polynomial_theta(radius, config.coefficients)
    dtheta_dr = odd_polynomial_derivative(radius, config.coefficients)
    # Axisymmetric mapping: dOmega = sin(theta) dtheta dphi and detector area = r dr dphi.
    with np.errstate(divide="ignore", invalid="ignore"):
        omega_sr = np.where(
            radius > 1e-9,
            np.sin(theta) * dtheta_dr / radius,
            np.square(config.coefficients[0]),
        )
    if not math.isfinite(area_multiplier) or area_multiplier <= 0:
        raise ValueError("광도 면적 배수는 양의 유한값이어야 합니다.")
    # The coordinate scale maps sample centers to original pixels. The photometric area
    # multiplier describes how many detector photosites were averaged/summed into one
    # intensity sample (two for a two-green Bayer mean); interpolation does not enlarge it.
    omega_sr *= area_multiplier
    arcsec_per_rad = 206264.80624709636
    return omega_sr * arcsec_per_rad**2
