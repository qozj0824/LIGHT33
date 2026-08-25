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
        focal_length_px=_optional_float(data.get("focal_length_px")),
        rotation_vector=[float(v) for v in data.get("rotation_vector", [])],
        radial_theta_coefficients=[float(v) for v in data.get("radial_theta_coefficients", [])],
        mirror_x=bool(data.get("mirror_x", False)),
        tracking_mode=str(data.get("tracking_mode") or "") or None,
        tracking_reference_lst_sec=_optional_float(data.get("tracking_reference_lst_sec")),
        tracking_site_latitude_deg=_optional_float(data.get("tracking_site_latitude_deg")),
        fit_star_count=int(data.get("fit_star_count", 0) or 0),
        fit_rms_deg=_optional_float(data.get("fit_rms_deg")),
        fit_edge_rms_deg=_optional_float(data.get("fit_edge_rms_deg")),
        validation_star_count=int(data.get("validation_star_count", 0) or 0),
        validation_max_error_px=_optional_float(data.get("validation_max_error_px")),
        validation_basis=str(data.get("validation_basis") or "") or None,
        calibration_date=str(data.get("calibration_date") or "") or None,
        camera_lens_id=str(data.get("camera_lens_id") or "") or None,
        selection_source=str(data.get("selection_source") or "configuration_file"),
        geometry_confidence=str(data.get("geometry_confidence") or "high"),
        orientation_confidence=str(data.get("orientation_confidence") or "high"),
        geometry_diagnostics=(
            dict(data.get("geometry_diagnostics"))
            if isinstance(data.get("geometry_diagnostics"), dict)
            else {}
        ),
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



def select_fisheye_config(
    project_root: Path,
    *,
    camera_name: str | None = None,
    filename: str | None = None,
    width: int | None = None,
    height: int | None = None,
    image: np.ndarray | None = None,
) -> FisheyeConfig:
    """Select only a positively matched camera calibration.

    Unknown cameras must never inherit the bundled Canon or APICAM geometry.
    For them, a circular footprint is inferred when the frame itself supports
    that inference; otherwise a centered equidistant diagnostic model is used.
    The automatic model intentionally has low orientation confidence because a
    single un-plate-solved image cannot reveal true north reliably.
    """
    identity = " ".join([str(camera_name or ""), str(filename or "")]).upper()
    if "APICAM" in identity:
        candidate = project_root / "config" / "fisheye_apicam.json"
        if candidate.exists():
            config = load_fisheye_config(candidate)
            config.selection_source = "camera_identity:apicam"
            config.geometry_confidence = "high"
            config.orientation_confidence = "planning"
            if width is not None and height is not None:
                # Keep the identity decision separate from the size check.  The
                # normal sky-map geometry validator gives a precise error later.
                _ = (width, height)
            return config
    shape = None
    if image is not None and np.asarray(image).ndim == 2:
        array_shape = np.asarray(image).shape
        shape = (int(array_shape[0]), int(array_shape[1]))
    elif width and height:
        shape = (int(height), int(width))
    if shape is None:
        shape = (max(int(height or 1), 1), max(int(width or 1), 1))
    center_x, center_y, radius, diagnostics = infer_circular_fisheye_geometry(image, shape)
    return FisheyeConfig(
        mode="auto_equidistant",
        center_x=center_x,
        center_y=center_y,
        horizon_radius=radius,
        sensor_width=shape[1],
        sensor_height=shape[0],
        north_offset_deg=0.0,
        selection_source=str(diagnostics.get("source", "centered_fallback")),
        geometry_confidence=str(diagnostics.get("confidence", "low")),
        orientation_confidence="unknown",
        geometry_diagnostics=diagnostics,
    )


def infer_circular_fisheye_geometry(
    image: np.ndarray | None,
    shape: tuple[int, int],
) -> tuple[float, float, float, dict[str, float | int | str]]:
    """Infer a circular sky footprint without assuming a camera brand.

    The estimator looks for a stable outer detector field surrounding a more
    variable illuminated circle.  It rejects weak/non-circular evidence and
    falls back to centered geometry instead of fabricating a precise solution.
    """
    height, width = shape
    fallback_x, fallback_y, fallback_radius = auto_geometry(shape)
    diagnostics: dict[str, float | int | str] = {
        "source": "centered_fallback",
        "confidence": "low",
        "center_x_px": float(fallback_x),
        "center_y_px": float(fallback_y),
        "horizon_radius_px": float(fallback_radius),
    }
    if image is None:
        diagnostics["reason"] = "pixel_data_unavailable"
        return fallback_x, fallback_y, fallback_radius, diagnostics
    arr = np.asarray(image)
    if arr.ndim != 2 or min(arr.shape) < 64:
        diagnostics["reason"] = "image_too_small_or_not_2d"
        return fallback_x, fallback_y, fallback_radius, diagnostics

    from scipy import ndimage

    scale = max(arr.shape) / 512.0
    step = max(1, int(math.ceil(scale)))
    sample = np.asarray(arr[::step, ::step], dtype=np.float64)
    finite = np.isfinite(sample)
    if np.count_nonzero(finite) < sample.size * 0.90:
        diagnostics["reason"] = "too_many_nonfinite_pixels"
        return fallback_x, fallback_y, fallback_radius, diagnostics
    fill = float(np.nanmedian(sample[finite]))
    sample = np.where(finite, sample, fill)
    edge = max(2, int(round(min(sample.shape) * 0.025)))
    border = np.concatenate(
        [
            sample[:edge, :].ravel(),
            sample[-edge:, :].ravel(),
            sample[:, :edge].ravel(),
            sample[:, -edge:].ravel(),
        ]
    )
    border_median = float(np.median(border))
    border_mad = float(np.median(np.abs(border - border_median)))
    sample_percentiles = np.asarray(np.percentile(sample, [1.0, 99.0]), dtype=np.float64)
    epsilon = float(np.finfo(np.float64).eps)
    dynamic = max(float(sample_percentiles[1] - sample_percentiles[0]), epsilon)
    threshold = max(8.0 * 1.4826 * border_mad, 0.025 * dynamic)
    active = np.abs(sample - border_median) > threshold
    active = ndimage.binary_closing(active, iterations=max(1, min(sample.shape) // 128))
    active = ndimage.binary_fill_holes(active)
    labels, count = ndimage.label(active)
    if count <= 0:
        diagnostics["reason"] = "no_illuminated_footprint"
        return fallback_x, fallback_y, fallback_radius, diagnostics
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    component = labels == int(np.argmax(sizes))
    yy, xx = np.nonzero(component)
    if xx.size < sample.size * 0.08:
        diagnostics["reason"] = "footprint_too_small"
        return fallback_x, fallback_y, fallback_radius, diagnostics
    x0, x1 = int(xx.min()), int(xx.max())
    y0, y1 = int(yy.min()), int(yy.max())
    box_width = x1 - x0 + 1
    box_height = y1 - y0 + 1
    aspect = box_width / max(box_height, 1)
    radius_small = 0.25 * (box_width + box_height)
    center_x_small = 0.5 * (x0 + x1)
    center_y_small = 0.5 * (y0 + y1)
    circle_area = math.pi * max(radius_small, 1.0) ** 2
    fill_ratio = float(xx.size / circle_area)
    outer_fraction = 1.0 - float(xx.size / sample.size)
    center_ok = (
        abs(center_x_small - (sample.shape[1] - 1) / 2.0) <= sample.shape[1] * 0.22
        and abs(center_y_small - (sample.shape[0] - 1) / 2.0) <= sample.shape[0] * 0.22
    )
    plausible = (
        0.78 <= aspect <= 1.28
        and 0.45 <= fill_ratio <= 1.30
        and 0.02 <= outer_fraction <= 0.75
        and center_ok
    )
    diagnostics.update(
        {
            "border_median_adu": border_median,
            "border_mad_adu": border_mad,
            "detection_threshold_adu": float(threshold),
            "footprint_aspect": float(aspect),
            "footprint_fill_ratio": float(fill_ratio),
            "outer_fraction": float(outer_fraction),
            "sample_step": int(step),
        }
    )
    if not plausible:
        diagnostics["reason"] = "circular_evidence_rejected"
        return fallback_x, fallback_y, fallback_radius, diagnostics

    scale_x = width / sample.shape[1]
    scale_y = height / sample.shape[0]
    center_x = (center_x_small + 0.5) * scale_x - 0.5
    center_y = (center_y_small + 0.5) * scale_y - 0.5
    radius = radius_small * math.sqrt(scale_x * scale_y)
    confidence = "medium" if border_mad <= 0.02 * dynamic else "low"
    diagnostics.update(
        {
            "source": "inferred_circular_footprint",
            "confidence": confidence,
            "center_x_px": float(center_x),
            "center_y_px": float(center_y),
            "horizon_radius_px": float(radius),
        }
    )
    return float(center_x), float(center_y), float(radius), diagnostics


def _rotation_matrix_from_rotvec(rotation_vector: list[float]) -> np.ndarray:
    if len(rotation_vector) != 3:
        raise ValueError("보정 카메라 회전벡터는 3개 값이어야 합니다.")
    vector = np.asarray(rotation_vector, dtype=np.float64)
    angle = float(np.linalg.norm(vector))
    if not math.isfinite(angle):
        raise ValueError("보정 카메라 회전벡터가 유한값이 아닙니다.")
    if angle <= 1e-14:
        return np.eye(3, dtype=np.float64)
    axis = vector / angle
    x, y, z = axis
    skew = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


def _advance_local_enu_for_sidereal_tracking(
    local_vectors: np.ndarray,
    *,
    site_latitude_deg: float,
    reference_lst_sec: float,
    observation_lst_sec: float,
) -> np.ndarray:
    """Advance reference-epoch ENU vectors to a new local sidereal time.

    A tracking all-sky camera keeps detector coordinates nearly fixed on the
    celestial sphere.  The fitted camera rotation therefore maps detector rays
    to *reference-epoch* local ENU, not to a permanently fixed local direction.
    Convert those rays to (declination, hour angle), add the LST difference, and
    transform back to local ENU at the observation epoch.
    """
    vectors = np.asarray(local_vectors, dtype=np.float64)
    phi = math.radians(float(site_latitude_deg))
    east = vectors[..., 0]
    north = vectors[..., 1]
    up = vectors[..., 2]
    norm = np.sqrt(east * east + north * north + up * up)
    east = np.divide(east, norm, out=np.zeros_like(east), where=norm > 1e-12)
    north = np.divide(north, norm, out=np.zeros_like(north), where=norm > 1e-12)
    up = np.divide(up, norm, out=np.zeros_like(up), where=norm > 1e-12)

    sin_dec = np.clip(north * math.cos(phi) + up * math.sin(phi), -1.0, 1.0)
    dec = np.arcsin(sin_dec)
    cos_dec_cos_h = up * math.cos(phi) - north * math.sin(phi)
    cos_dec_sin_h = -east
    hour_angle = np.arctan2(cos_dec_sin_h, cos_dec_cos_h)

    # FITS LST is expressed in sidereal seconds (24 h = 86400 s).
    delta_lst = (float(observation_lst_sec) - float(reference_lst_sec)) * (2.0 * math.pi / 86400.0)
    hour_angle = hour_angle + delta_lst
    sin_dec = np.sin(dec)
    cos_dec = np.cos(dec)
    sin_h = np.sin(hour_angle)
    cos_h = np.cos(hour_angle)
    current_east = -cos_dec * sin_h
    current_north = sin_dec * math.cos(phi) - cos_dec * cos_h * math.sin(phi)
    current_up = sin_dec * math.sin(phi) + cos_dec * cos_h * math.cos(phi)
    return np.stack([current_east, current_north, current_up], axis=-1)


def _theta_from_radial_radius(
    radius: np.ndarray,
    focal_length_px: float,
    coefficients: list[float],
) -> np.ndarray:
    """Invert r=f*theta*(1+k1 theta^2+k2 theta^4+...) by Newton iteration."""
    r = np.asarray(radius, dtype=np.float64)
    f = max(float(focal_length_px), 1e-12)
    theta = np.clip(r / f, 0.0, math.radians(110.0))
    for _ in range(10):
        poly = np.ones_like(theta)
        derivative_poly = np.ones_like(theta)
        for index, coefficient in enumerate(coefficients, start=1):
            power = 2 * index
            c = float(coefficient)
            poly += c * np.power(theta, power)
            derivative_poly += (power + 1) * c * np.power(theta, power)
        value = f * theta * poly - r
        derivative = f * derivative_poly
        step = np.divide(value, derivative, out=np.zeros_like(value), where=np.abs(derivative) > 1e-12)
        theta = np.clip(theta - step, 0.0, math.radians(110.0))
    return theta



def _configured_horizon_radius(config: FisheyeConfig) -> float | None:
    if config.horizon_radius is not None and config.horizon_radius > 0:
        return float(config.horizon_radius)
    if config.mode == "calibrated_camera_model" and config.focal_length_px:
        theta = math.pi / 2.0
        radial_factor = 1.0
        for index, coefficient in enumerate(config.radial_theta_coefficients, start=1):
            radial_factor += float(coefficient) * theta ** (2 * index)
        radius = float(config.focal_length_px) * theta * radial_factor
        return radius if math.isfinite(radius) and radius > 0 else None
    if config.mode == "calibrated_kannala_brandt" and config.coefficients:
        # theta(r) is monotonic over the calibrated 180-degree field.  Bisection
        # avoids coupling the pedestal fallback to a camera-specific inverse.
        low = 0.0
        high = float(max(config.sensor_width or 1, config.sensor_height or 1))
        target = math.pi / 2.0
        for _ in range(60):
            middle = (low + high) / 2.0
            theta = float(odd_polynomial_theta(np.asarray([middle]), config.coefficients)[0])
            if theta < target:
                low = middle
            else:
                high = middle
        radius = (low + high) / 2.0
        return radius if math.isfinite(radius) and radius > 0 else None
    return None


def estimate_masked_outer_field_pedestal(
    image: np.ndarray,
    config: FisheyeConfig,
) -> tuple[float | None, dict[str, float | int | str]]:
    """Estimate a pedestal only when an optically dark outer field is proven.

    This is camera-agnostic.  It needs a circular horizon geometry, sufficient
    detector area beyond that circle, a uniform outer distribution, and clear
    contrast between the sky circle and the outer field.  Rejection is the safe
    result for full-frame projections and weak evidence.
    """
    arr = np.asarray(image)
    diagnostics: dict[str, float | int | str] = {
        "method": "masked_outer_field",
        "status": "unavailable",
    }
    if arr.ndim != 2:
        diagnostics["reason"] = "not_2d"
        return None, diagnostics
    if config.center_x is None or config.center_y is None:
        diagnostics["reason"] = "incomplete_geometry"
        return None, diagnostics
    if config.sensor_width and config.sensor_height:
        if arr.shape != (int(config.sensor_height), int(config.sensor_width)):
            diagnostics["reason"] = "unexpected_dimensions"
            return None, diagnostics
    horizon_radius = _configured_horizon_radius(config)
    if horizon_radius is None:
        diagnostics["reason"] = "invalid_horizon_radius"
        return None, diagnostics

    margin = max(4.0, 0.035 * horizon_radius)
    cutoff = horizon_radius + margin
    # Pedestal statistics do not need every detector pixel.  Limit the working
    # grid to roughly 1.5M samples so 4k/8k inputs do not allocate multiple
    # full-frame float64 radius maps on small Render workers.
    sample_step = max(1, int(math.ceil(math.sqrt(arr.size / 1_500_000.0))))
    sampled = np.asarray(arr[::sample_step, ::sample_step])
    center_x = (float(config.center_x) + 0.5) / sample_step - 0.5
    center_y = (float(config.center_y) + 0.5) / sample_step - 0.5
    sampled_horizon_radius = horizon_radius / sample_step
    sampled_cutoff = cutoff / sample_step
    yy, xx = np.ogrid[: sampled.shape[0], : sampled.shape[1]]
    radius_squared = (xx - center_x) ** 2 + (yy - center_y) ** 2
    mask = radius_squared >= sampled_cutoff**2
    values = np.asarray(sampled[mask], dtype=np.float64)
    values = values[np.isfinite(values)]
    minimum_samples = max(5_000, int(sampled.size * 0.005))
    if values.size < minimum_samples:
        diagnostics["reason"] = "too_few_masked_pixels"
        diagnostics["sample_count"] = int(values.size)
        return None, diagnostics

    outer_percentiles = np.asarray(
        np.percentile(values, [1.0, 5.0, 50.0, 95.0, 99.0]),
        dtype=np.float64,
    )
    p01, p05, med, p95, p99 = (float(value) for value in outer_percentiles)
    width_90 = float(p95 - p05)
    finite_all = np.asarray(sampled[np.isfinite(sampled)], dtype=np.float64)
    global_percentiles = np.asarray(np.percentile(finite_all, [1.0, 99.0]), dtype=np.float64)
    epsilon = float(np.finfo(np.float64).eps)
    global_dynamic = max(float(global_percentiles[1] - global_percentiles[0]), epsilon)
    allowed_width = max(0.10 * global_dynamic, 0.20 * max(abs(float(med)), global_dynamic * 0.01))
    inner_mask = radius_squared <= (0.75 * sampled_horizon_radius) ** 2
    inner_values = np.asarray(sampled[inner_mask], dtype=np.float64)
    inner_values = inner_values[np.isfinite(inner_values)]
    inner_median = float(np.median(inner_values)) if inner_values.size else float("nan")
    contrast = inner_median - float(med)
    outer_sigma = max(width_90 / 3.29, epsilon)
    minimum_contrast = max(3.0 * outer_sigma, 0.01 * global_dynamic)
    diagnostics.update(
        {
            "status": "ok",
            "sample_count": int(values.size),
            "horizon_radius_px": float(horizon_radius),
            "margin_px": float(margin),
            "cutoff_radius_px": float(cutoff),
            "sample_step": int(sample_step),
            "p01_adu": float(p01),
            "p05_adu": float(p05),
            "median_adu": float(med),
            "p95_adu": float(p95),
            "p99_adu": float(p99),
            "p95_minus_p05_adu": width_90,
            "allowed_width_adu": float(allowed_width),
            "inner_median_adu": inner_median,
            "inner_minus_outer_adu": float(contrast),
            "minimum_contrast_adu": float(minimum_contrast),
            "geometry_source": config.selection_source,
        }
    )
    if not math.isfinite(float(med)) or width_90 > allowed_width:
        diagnostics["status"] = "rejected"
        diagnostics["reason"] = "outer_field_not_uniform"
        return None, diagnostics
    if not math.isfinite(inner_median) or contrast <= minimum_contrast:
        diagnostics["status"] = "rejected"
        diagnostics["reason"] = "outer_field_not_proven_dark"
        return None, diagnostics
    return float(med), diagnostics


def estimate_apicam_masked_pedestal(
    image: np.ndarray,
    config: FisheyeConfig,
    *,
    camera_name: str | None = None,
    filename: str | None = None,
) -> tuple[float | None, dict[str, float | int | str]]:
    """Backward-compatible APICAM wrapper around the generic estimator."""
    identity = " ".join([str(camera_name or ""), str(filename or "")]).upper()
    if "APICAM" not in identity:
        return None, {
            "method": "apicam_masked_outer_field",
            "status": "unavailable",
            "reason": "not_apicam",
        }
    value, diagnostics = estimate_masked_outer_field_pedestal(image, config)
    diagnostics["method"] = "apicam_masked_outer_field"
    return value, diagnostics

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
    """Validate a fisheye solution for azimuth/altitude direction lookup."""
    errors: list[str] = []
    if config.mode == "calibrated_kannala_brandt":
        if any(value is None for value in (config.center_x, config.center_y, config.E, config.a0, config.eps)):
            errors.append("어안 중심·자세 파라미터가 완전하지 않습니다.")
        if not config.coefficients:
            errors.append("어안 왜곡계수가 없습니다.")
        stars = max(config.fit_star_count, config.validation_star_count)
        if stars < 20:
            errors.append("방향 보정에 사용한 기준별 수가 20개 미만이거나 기록되지 않았습니다.")
        if config.validation_max_error_px is None or config.validation_max_error_px > 5.0:
            errors.append("방향 보정의 독립 검증 최대오차가 없거나 5 px보다 큽니다.")
    elif config.mode == "calibrated_camera_model":
        if any(value is None for value in (config.center_x, config.center_y, config.focal_length_px)):
            errors.append("APICAM 보정의 중심·초점 파라미터가 완전하지 않습니다.")
        if len(config.rotation_vector) != 3:
            errors.append("APICAM 보정 회전벡터가 완전하지 않습니다.")
        if config.fit_star_count < 15:
            errors.append("APICAM 보정에 사용한 기준별 수가 15개 미만이거나 기록되지 않았습니다.")
        if str(config.tracking_mode or "").lower() == "sidereal":
            if config.tracking_reference_lst_sec is None or config.tracking_site_latitude_deg is None:
                errors.append("APICAM 추적식 좌표변환의 기준 LST/관측소 위도가 기록되지 않았습니다.")
        # APICAM temporal hold-out validation is recorded in detector pixels.
        # intentionally allowed for planning/background lookup but is not
        # promoted to independently validated quantitative direction accuracy.
        if config.validation_star_count < 10 or config.validation_max_error_px is None:
            errors.append("APICAM 좌표해는 독립 hold-out 검증 전이므로 방향값을 계획용으로 취급합니다.")
        elif config.validation_max_error_px > 5.0:
            errors.append("APICAM 방향 보정의 독립 검증 최대오차가 5 px보다 큽니다.")
    else:
        errors.append("표준별로 보정된 어안 방향 모델이 아닙니다.")
        return errors
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
    observation_lst_sec: float | None = None,
    site_latitude_deg: float | None = None,
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

    if config.mode == "calibrated_camera_model":
        if config.center_x is None or config.center_y is None or config.focal_length_px is None:
            raise ValueError("보정 카메라 모델의 중심·초점 파라미터가 완전하지 않습니다.")
        if len(config.rotation_vector) != 3:
            raise ValueError("보정 카메라 모델의 회전벡터가 완전하지 않습니다.")
        original_x = (x + 0.5) * coordinate_scale_x - 0.5
        original_y = (y + 0.5) * coordinate_scale_y - 0.5
        dx = original_x - float(config.center_x)
        dy = original_y - float(config.center_y)
        radius = np.hypot(dx, dy)
        theta = _theta_from_radial_radius(
            radius,
            float(config.focal_length_px),
            config.radial_theta_coefficients,
        )
        radial_x = np.divide(dx, radius, out=np.zeros_like(dx), where=radius > 1e-12)
        radial_y = np.divide(dy, radius, out=np.zeros_like(dy), where=radius > 1e-12)
        if config.mirror_x:
            radial_x = -radial_x
        sin_theta = np.sin(theta)
        camera_vectors = np.stack(
            [radial_x * sin_theta, radial_y * sin_theta, np.cos(theta)],
            axis=-1,
        )
        rotation = _rotation_matrix_from_rotvec(config.rotation_vector)
        # Forward calibration is camera = R @ local_ENU, so invert with R.T.
        local_vectors = camera_vectors @ rotation
        if str(config.tracking_mode or "").lower() == "sidereal":
            reference_lst = config.tracking_reference_lst_sec
            latitude = site_latitude_deg if site_latitude_deg is not None else config.tracking_site_latitude_deg
            if reference_lst is not None and observation_lst_sec is not None and latitude is not None:
                local_vectors = _advance_local_enu_for_sidereal_tracking(
                    local_vectors,
                    site_latitude_deg=float(latitude),
                    reference_lst_sec=float(reference_lst),
                    observation_lst_sec=float(observation_lst_sec),
                )
        east = local_vectors[..., 0]
        north = local_vectors[..., 1]
        up = local_vectors[..., 2]
        norm = np.sqrt(east * east + north * north + up * up)
        up_normalized = np.divide(up, norm, out=np.zeros_like(up), where=norm > 1e-12)
        alt = np.rad2deg(np.arcsin(np.clip(up_normalized, -1.0, 1.0)))
        az = np.mod(np.rad2deg(np.arctan2(east, north)), 360.0)
        valid = (
            np.isfinite(alt)
            & np.isfinite(az)
            & np.isfinite(theta)
            & (theta <= math.radians(100.0))
            & (alt >= -5.0)
            & (alt <= 90.5)
        )
        return az, alt, valid

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
