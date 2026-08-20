from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .models import (
    AnalysisSettings,
    IntensityDomain,
    SaturationReport,
    SignalMeasurement,
    StarMeasurement,
)


@dataclass(slots=True)
class RobustStats:
    median: float
    std: float
    mad: float
    mean: float
    count: int



def sigma_clipped_stats(
    values: np.ndarray,
    sigma: float = 3.0,
    iterations: int = 5,
) -> RobustStats:
    data = np.asarray(values, dtype=np.float64)
    data = data[np.isfinite(data)]
    if data.size == 0:
        return RobustStats(float("nan"), float("nan"), float("nan"), float("nan"), 0)
    keep = data
    for _ in range(iterations):
        median = float(np.median(keep))
        deviations = np.abs(keep - median)
        mad = float(np.median(deviations))
        robust_std = 1.4826 * mad
        if not math.isfinite(robust_std) or robust_std <= 1e-12:
            # A quantized detector region may have MAD==0 even with a few extreme
            # outliers.  Use the typical non-zero absolute deviation before falling
            # back to the ordinary standard deviation, otherwise a single cosmic ray
            # can inflate std enough to survive clipping.
            nonzero = deviations[deviations > 1e-12]
            if nonzero.size:
                robust_std = 1.4826 * float(np.median(nonzero))
            else:
                robust_std = float(np.std(keep))
        if not math.isfinite(robust_std) or robust_std <= 1e-12:
            break
        next_keep = keep[deviations <= sigma * robust_std]
        minimum_keep = max(3, int(math.ceil(data.size * 0.1)))
        if next_keep.size == keep.size or next_keep.size < minimum_keep:
            break
        keep = next_keep
    final_median = float(np.median(keep))
    return RobustStats(
        median=final_median,
        std=float(np.std(keep, ddof=1)) if keep.size > 1 else 0.0,
        mad=float(np.median(np.abs(keep - final_median))),
        mean=float(np.mean(keep)),
        count=int(keep.size),
    )



def _downsample_mean(image: np.ndarray, max_dim: int) -> tuple[np.ndarray, float, float]:
    height, width = image.shape
    scale = max(height / max_dim, width / max_dim, 1.0)
    if scale <= 1.0:
        return image.astype(np.float64, copy=False), 1.0, 1.0
    out_h = max(1, int(round(height / scale)))
    out_w = max(1, int(round(width / scale)))
    reduced = ndimage.zoom(image, (out_h / height, out_w / width), order=1, prefilter=False)
    return reduced, width / out_w, height / out_h



def _downsample_mask_any(mask: np.ndarray, max_dim: int) -> tuple[np.ndarray, float, float]:
    """Reduce a boolean mask with block-wise OR so isolated saturated pixels survive."""
    height, width = mask.shape
    factor = max(1, int(math.ceil(max(height / max_dim, width / max_dim))))
    if factor == 1:
        return np.asarray(mask, dtype=bool), 1.0, 1.0
    out_h = int(math.ceil(height / factor))
    out_w = int(math.ceil(width / factor))
    padded = np.zeros((out_h * factor, out_w * factor), dtype=bool)
    padded[:height, :width] = mask
    reduced = padded.reshape(out_h, factor, out_w, factor).any(axis=(1, 3))
    return np.asarray(reduced, dtype=bool), float(factor), float(factor)



def _local_moments(
    image: np.ndarray,
    x: int,
    y: int,
    radius: int = 7,
) -> tuple[float, float, float, bool]:
    y0, y1 = max(0, y - radius), min(image.shape[0], y + radius + 1)
    x0, x1 = max(0, x - radius), min(image.shape[1], x + radius + 1)
    patch = image[y0:y1, x0:x1].astype(np.float64, copy=False)
    if patch.size < 25:
        return float("nan"), float("nan"), float("nan"), True
    edge = np.concatenate([patch[0], patch[-1], patch[:, 0], patch[:, -1]])
    background = float(np.median(edge))
    signal = np.maximum(patch - background, 0.0)
    peak = float(np.max(signal))
    if peak <= 0:
        return float("nan"), float("nan"), background, True
    support = signal > max(peak * 0.08, float(np.std(edge)) * 3.0)
    hot_pixel_like = int(np.count_nonzero(support)) < 4
    weights = np.where(support, signal, 0.0)
    total = float(np.sum(weights))
    if total <= 0:
        return float("nan"), float("nan"), background, True
    yy, xx = np.indices(patch.shape, dtype=np.float64)
    cx = float(np.sum(xx * weights) / total)
    cy = float(np.sum(yy * weights) / total)
    dx, dy = xx - cx, yy - cy
    covariance = np.array(
        [
            [float(np.sum(weights * dx * dx) / total), float(np.sum(weights * dx * dy) / total)],
            [float(np.sum(weights * dx * dy) / total), float(np.sum(weights * dy * dy) / total)],
        ],
        dtype=np.float64,
    )
    eigenvalues = np.linalg.eigvalsh(covariance)
    if not np.all(np.isfinite(eigenvalues)) or eigenvalues[-1] <= 0:
        return float("nan"), float("nan"), background, True
    sigma_major = math.sqrt(max(float(eigenvalues[-1]), 0.0))
    sigma_minor = math.sqrt(max(float(eigenvalues[0]), 0.0))
    fwhm = 2.35482 * math.sqrt(max(sigma_major * sigma_minor, 0.0))
    eccentricity = math.sqrt(
        max(0.0, 1.0 - (sigma_minor * sigma_minor) / (sigma_major * sigma_major))
    )
    hot_pixel_like = hot_pixel_like or fwhm < 1.15
    return float(fwhm), float(eccentricity), background, hot_pixel_like



def detect_star_candidates(
    image: np.ndarray,
    max_candidates: int,
    max_dim: int = 2200,
) -> list[tuple[int, int]]:
    reduced, sx, sy = _downsample_mean(image, max_dim=max_dim)
    smooth = ndimage.gaussian_filter(reduced, sigma=1.0)
    background = ndimage.gaussian_filter(smooth, sigma=12.0, mode="reflect")
    highpass = smooth - background
    stats = sigma_clipped_stats(highpass)
    threshold = stats.median + max(5.0 * stats.std, 1e-9)
    local_max = highpass == ndimage.maximum_filter(highpass, size=5, mode="nearest")
    candidates = np.argwhere(local_max & (highpass > threshold))
    if candidates.size == 0:
        return []
    values = highpass[candidates[:, 0], candidates[:, 1]]
    brightness_order = np.argsort(values)[::-1]
    bright_count = min(80, max(10, max_candidates // 5), len(brightness_order))
    remaining = max(0, max_candidates - bright_count)
    if remaining and len(brightness_order) > bright_count:
        ranks = np.linspace(bright_count, len(brightness_order) - 1, remaining, dtype=int)
        order = np.concatenate([brightness_order[:bright_count], brightness_order[ranks]])
    else:
        order = brightness_order[:max_candidates]
    selected: list[tuple[int, int]] = []
    for index in order:
        ry, rx = candidates[index]
        x = int(round((float(rx) + 0.5) * sx - 0.5))
        y = int(round((float(ry) + 0.5) * sy - 0.5))
        if x < 10 or y < 10 or x >= image.shape[1] - 10 or y >= image.shape[0] - 10:
            continue
        if any((x - px) ** 2 + (y - py) ** 2 < 25 for px, py in selected[-100:]):
            continue
        selected.append((x, y))
        if len(selected) >= max_candidates:
            break
    return selected



def point_source_mask(image: np.ndarray, dilation: int = 2) -> np.ndarray:
    """Mask compact sources while preserving broad diffuse emission.

    The filter stays in float32 and is intended for a bounded ROI rather than the
    complete full-resolution sensor frame.  This avoids multi-gigabyte temporary
    arrays on modern astronomy cameras.
    """
    smooth = ndimage.gaussian_filter(image.astype(np.float32, copy=False), sigma=0.8)
    broad = ndimage.gaussian_filter(smooth, sigma=10.0, mode="reflect")
    residual = smooth - broad
    stats = sigma_clipped_stats(residual)
    threshold = stats.median + max(6.0 * stats.std, 1e-9)
    mask = residual > threshold
    labels, count = ndimage.label(mask)
    if count:
        sizes = np.bincount(labels.ravel())
        compact = sizes[labels] <= 500
        mask &= compact
    return ndimage.binary_dilation(mask, iterations=dilation)



def _aperture_measurement(
    image: np.ndarray,
    x: int,
    y: int,
    fwhm: float,
    domain: IntensityDomain,
    settings: AnalysisSettings,
    current_exposure_sec: float,
) -> StarMeasurement | None:
    aperture_radius = max(2.0, settings.aperture_radius_factor * fwhm)
    annulus_inner = max(aperture_radius + 1.0, settings.annulus_inner_factor * fwhm)
    annulus_outer = max(annulus_inner + 2.0, settings.annulus_outer_factor * fwhm)
    radius = int(math.ceil(annulus_outer))
    y0, y1 = max(0, y - radius), min(image.shape[0], y + radius + 1)
    x0, x1 = max(0, x - radius), min(image.shape[1], x + radius + 1)
    patch = image[y0:y1, x0:x1].astype(np.float64, copy=False)
    yy, xx = np.indices(patch.shape, dtype=np.float64)
    rr = np.hypot(xx + x0 - x, yy + y0 - y)
    aperture = rr <= aperture_radius
    annulus = (rr >= annulus_inner) & (rr <= annulus_outer)
    annulus_values = patch[annulus & np.isfinite(patch)]
    if annulus_values.size < 30:
        return None
    background_stats = sigma_clipped_stats(annulus_values)
    aperture_values = patch[aperture & np.isfinite(patch)]
    if aperture_values.size < 4 or not math.isfinite(background_stats.median):
        return None
    background = background_stats.median
    flux_adu = float(np.sum(aperture_values - background))
    peak_adu = float(np.max(aperture_values))
    peak_above = peak_adu - background
    if flux_adu <= 0 or peak_above <= 0:
        return None
    n_pix = int(aperture_values.size)
    n_bg = int(background_stats.count)
    gain = settings.gain_e_per_adu
    signal_e = flux_adu * gain
    background_e = max(background - settings.bias_offset_adu, 0.0) * gain * n_pix
    dark_e = settings.dark_current_e_per_pix_sec * current_exposure_sec * n_pix
    robust_bg_sigma = (
        1.4826 * background_stats.mad
        if math.isfinite(background_stats.mad) and background_stats.mad > 0
        else background_stats.std
    )
    # For a near-Gaussian distribution the standard error of the sample median is
    # about 1.2533*sigma/sqrt(N).  The report uses a median sky estimator, so propagate
    # the uncertainty of that estimator instead of treating it as an exact constant.
    background_median_se = 1.2533 * robust_bg_sigma / math.sqrt(max(n_bg, 1))
    background_estimator_var_e2 = (n_pix * background_median_se * gain) ** 2
    variance = (
        max(signal_e, 0.0)
        + background_e
        + dark_e
        + n_pix * settings.read_noise_e**2
        + background_estimator_var_e2
    )
    snr = signal_e / math.sqrt(variance) if variance > 0 else 0.0
    fwhm_check, eccentricity, _, hot = _local_moments(
        image, x, y, radius=min(9, max(5, int(round(fwhm * 2))))
    )
    if not math.isfinite(fwhm_check):
        return None
    saturated = bool(
        domain.quantitative_saturation_supported and peak_adu >= domain.saturation_threshold_adu
    )
    return StarMeasurement(
        x=float(x),
        y=float(y),
        flux_adu=flux_adu,
        peak_adu=peak_adu,
        peak_above_background_adu=peak_above,
        background_adu=background,
        background_std_adu=background_stats.std,
        background_pixels=n_bg,
        fwhm_px=fwhm_check,
        eccentricity=eccentricity,
        aperture_pixels=n_pix,
        saturated=saturated,
        hot_pixel_like=hot,
        snr=float(snr),
    )



def measure_stars(
    image: np.ndarray,
    domain: IntensityDomain,
    settings: AnalysisSettings,
    current_exposure_sec: float = 1.0,
) -> list[StarMeasurement]:
    candidates = detect_star_candidates(image, max_candidates=max(settings.max_stars * 3, 300))
    measurements: list[StarMeasurement] = []
    for x, y in candidates:
        fwhm, eccentricity, _, hot = _local_moments(image, x, y)
        if not math.isfinite(fwhm) or hot or fwhm < 1.15 or fwhm > 20.0 or eccentricity > 0.92:
            continue
        item = _aperture_measurement(
            image, x, y, fwhm, domain, settings, current_exposure_sec
        )
        if item is None or item.hot_pixel_like or item.fwhm_px < 1.15 or item.eccentricity > 0.92:
            continue
        measurements.append(item)
        if len(measurements) >= settings.max_stars:
            break
    return measurements



def analyze_saturation(
    raw_image: np.ndarray,
    domain: IntensityDomain,
    stars: list[StarMeasurement],
    policy: str,
) -> SaturationReport:
    finite = np.isfinite(raw_image)
    saturated_mask = finite & (raw_image >= domain.saturation_threshold_adu)
    saturated_count = int(np.count_nonzero(saturated_mask))
    total = int(np.count_nonzero(finite))
    fraction = saturated_count / total if total else 0.0
    reduced_mask, sx, sy = _downsample_mask_any(saturated_mask, max_dim=2400)
    labels, count = ndimage.label(reduced_mask, structure=np.ones((3, 3), dtype=np.uint8))
    star_like = isolated = streak = largest = 0
    for label_index, slc in enumerate(ndimage.find_objects(labels), start=1):
        if slc is None:
            continue
        region = labels[slc] == label_index
        area = max(1, int(round(np.count_nonzero(region) * sx * sy)))
        largest = max(largest, area)
        h, w = region.shape
        aspect = max(h, w) / max(1, min(h, w))
        if area <= max(2, int(round(sx * sy * 1.5))):
            isolated += 1
        elif aspect >= 4.0:
            streak += 1
        else:
            star_like += 1

    raw_usable_peaks: list[float] = []
    for star in stars:
        radius = max(3, int(math.ceil(max(star.fwhm_px, 1.2) * 2.0)))
        cx, cy = int(round(star.x)), int(round(star.y))
        y0, y1 = max(0, cy - radius), min(raw_image.shape[0], cy + radius + 1)
        x0, x1 = max(0, cx - radius), min(raw_image.shape[1], cx + radius + 1)
        patch = raw_image[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        raw_peak = float(np.nanmax(patch))
        star.saturated = bool(raw_peak >= domain.saturation_threshold_adu)
        if not star.saturated and not star.hot_pixel_like and math.isfinite(raw_peak):
            raw_usable_peaks.append(raw_peak)
    quantile_map = {"preserve_stars": 99.0, "balanced": 95.0, "target_priority": 85.0}
    quantile = quantile_map.get(policy, 95.0)
    reference = float(np.percentile(raw_usable_peaks, quantile)) if raw_usable_peaks else None
    minimum_reference_stars = 8 if policy == "preserve_stars" else 5
    exact_available = bool(
        domain.quantitative_saturation_supported
        and reference is not None
        and len(raw_usable_peaks) >= minimum_reference_stars
        and reference < domain.saturation_threshold_adu
        and fraction < 0.01
    )
    if not domain.quantitative_saturation_supported:
        reason = "센서 포화 ADU가 확정되지 않아 포화 상한을 정량 계산하지 않습니다."
    elif len(raw_usable_peaks) < minimum_reference_stars:
        reason = (
            f"포화 상한에 필요한 비포화 기준별이 {len(raw_usable_peaks)}개뿐입니다. "
            f"최소 {minimum_reference_stars}개가 필요합니다."
        )
    elif fraction >= 0.01:
        reason = "포화 픽셀 비율이 1% 이상이라 기준 영상이 너무 밝습니다. 더 짧은 시험 영상을 사용하세요."
    else:
        reason = f"비포화 별 peak {quantile:.0f}백분위수와 {len(raw_usable_peaks)}개 기준별을 사용합니다."
    return SaturationReport(
        threshold_adu=domain.saturation_threshold_adu,
        saturated_pixel_count=saturated_count,
        saturated_pixel_fraction=float(fraction),
        connected_components=int(count),
        star_like_components=star_like,
        isolated_components=isolated,
        streak_components=streak,
        largest_component_pixels=largest,
        usable_unsaturated_star_count=len(raw_usable_peaks),
        reference_peak_quantile=quantile if raw_usable_peaks else None,
        reference_peak_total_adu=reference,
        exact_limit_available=exact_available,
        reason=reason,
    )



def _parse_normalized_roi(text: str | None, shape: tuple[int, int]) -> dict[str, int] | None:
    if not text:
        return None
    try:
        raw = json.loads(text)
        x, y, w, h = (float(raw[key]) for key in ("x", "y", "w", "h"))
    except Exception as exc:
        raise ValueError("ROI JSON은 x, y, w, h를 가진 정규화 좌표여야 합니다.") from exc
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        raise ValueError("ROI 값이 유효하지 않습니다.")
    if x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        raise ValueError("ROI가 영상 범위를 벗어났습니다.")
    height, width = shape
    left, top = int(round(x * width)), int(round(y * height))
    roi_w, roi_h = int(round(w * width)), int(round(h * height))
    left, top = min(max(0, left), width - 2), min(max(0, top), height - 2)
    roi_w, roi_h = min(max(2, roi_w), width - left), min(max(2, roi_h), height - top)
    return {"x": left, "y": top, "w": roi_w, "h": roi_h}



def _roi_slice(roi: dict[str, int]) -> tuple[slice, slice]:
    return slice(roi["y"], roi["y"] + roi["h"]), slice(roi["x"], roi["x"] + roi["w"])



def propose_extended_rois(image: np.ndarray) -> tuple[dict[str, int], dict[str, int]]:
    reduced, sx, sy = _downsample_mean(image, max_dim=900)
    smooth = ndimage.gaussian_filter(reduced, sigma=max(2.0, min(reduced.shape) / 200.0))
    local_bg = ndimage.gaussian_filter(smooth, sigma=max(8.0, min(reduced.shape) / 35.0), mode="reflect")
    contrast = smooth - local_bg
    target_size = max(20, int(min(reduced.shape) * 0.16))
    mean_map = ndimage.uniform_filter(contrast, size=target_size, mode="reflect")
    margin = target_size // 2 + 2
    valid_map = mean_map.copy()
    valid_map[:margin] = valid_map[-margin:] = -np.inf
    valid_map[:, :margin] = valid_map[:, -margin:] = -np.inf
    ty, tx = np.unravel_index(np.argmax(valid_map), valid_map.shape)
    yy, xx = np.indices(reduced.shape)
    target_radius = target_size * 1.5
    sensor_cx, sensor_cy = (reduced.shape[1] - 1) / 2, (reduced.shape[0] - 1) / 2
    target_sensor_radius = math.hypot(tx - sensor_cx, ty - sensor_cy)
    distance_from_target = np.hypot(xx - tx, yy - ty)
    sensor_radius_delta = np.abs(np.hypot(xx - sensor_cx, yy - sensor_cy) - target_sensor_radius)
    # Prefer a local background at similar sensor radius, but do not choose the absolute
    # darkest outlier: use the 20th percentile candidate closest to the target.
    bg_mean = ndimage.uniform_filter(smooth, size=target_size, mode="reflect")
    allowed = (distance_from_target > target_radius) & (
        sensor_radius_delta < max(25.0, target_size * 0.8)
    )
    allowed[:margin] = allowed[-margin:] = False
    allowed[:, :margin] = allowed[:, -margin:] = False
    candidate_values = bg_mean[allowed]
    if candidate_values.size:
        threshold = float(np.percentile(candidate_values, 20))
        candidates = allowed & (bg_mean <= threshold)
        candidate_yx = np.argwhere(candidates)
        distances = np.hypot(candidate_yx[:, 1] - tx, candidate_yx[:, 0] - ty)
        by, bx = candidate_yx[int(np.argmin(distances))]
    else:
        by, bx = np.int64(margin), np.int64(margin)

    def make_roi(cx: float, cy: float) -> dict[str, int]:
        half = target_size / 2
        x0, y0 = int(round((cx - half) * sx)), int(round((cy - half) * sy))
        w, h = int(round(target_size * sx)), int(round(target_size * sy))
        x0, y0 = min(max(0, x0), image.shape[1] - 2), min(max(0, y0), image.shape[0] - 2)
        return {"x": x0, "y": y0, "w": min(max(2, w), image.shape[1] - x0), "h": min(max(2, h), image.shape[0] - y0)}

    return make_roi(float(tx), float(ty)), make_roi(float(bx), float(by))



def _background_aperture_scatter(
    values_2d: np.ndarray,
    mask_2d: np.ndarray,
    effective_pixels: int,
) -> float | None:
    side = max(2, int(round(math.sqrt(effective_pixels))))
    means: list[float] = []
    for y0 in range(0, values_2d.shape[0] - side + 1, side):
        for x0 in range(0, values_2d.shape[1] - side + 1, side):
            patch = values_2d[y0 : y0 + side, x0 : x0 + side]
            patch_mask = mask_2d[y0 : y0 + side, x0 : x0 + side]
            usable = patch[np.isfinite(patch) & ~patch_mask]
            if usable.size >= effective_pixels * 0.6:
                means.append(float(np.mean(usable)))
    return float(np.std(means, ddof=1)) if len(means) >= 5 else None



def measure_extended_source(
    image: np.ndarray,
    settings: AnalysisSettings,
    current_exposure_sec: float = 1.0,
) -> SignalMeasurement:
    target_roi = _parse_normalized_roi(settings.target_roi_json, image.shape)
    background_roi = _parse_normalized_roi(settings.background_roi_json, image.shape)
    notes: list[str] = []
    if target_roi is None or background_roi is None:
        if not settings.auto_roi:
            raise ValueError("확산천체 모드에서 수동 ROI가 없고 자동 ROI도 꺼져 있습니다.")
        auto_target, auto_background = propose_extended_rois(image)
        target_roi = target_roi or auto_target
        background_roi = background_roi or auto_background
        notes.append("자동 ROI는 후보 제안입니다. 오버레이를 확인하고 다시 분석하면 신뢰도가 올라갑니다.")
    target_slice, background_slice = _roi_slice(target_roi), _roi_slice(background_roi)
    target_image, background_image = image[target_slice], image[background_slice]
    # Build source masks only inside the two ROIs. A full-frame Gaussian source mask
    # can require several GiB for 25+ MP cameras and provides no additional information
    # for the selected target/background measurement.
    target_mask = point_source_mask(target_image)
    background_mask = point_source_mask(background_image)
    target_values = target_image[np.isfinite(target_image) & ~target_mask]
    background_values = background_image[np.isfinite(background_image) & ~background_mask]
    target_stats, background_stats = sigma_clipped_stats(target_values), sigma_clipped_stats(background_values)
    if target_stats.count < 100 or background_stats.count < 100:
        raise ValueError("별을 제외한 ROI 유효 픽셀이 부족합니다. ROI를 더 크게 잡으세요.")
    background_level = background_stats.median
    excess = target_stats.mean - background_level
    if not math.isfinite(excess) or excess <= 0:
        raise ValueError("대상 ROI의 평균 밝기가 배경 ROI 중앙값보다 높지 않습니다. ROI를 다시 지정하세요.")
    n_pix = max(1, min(int(settings.smoothing_pixels), target_stats.count))
    gain = settings.gain_e_per_adu
    signal_e = excess * gain * n_pix
    background_e = max(background_level - settings.bias_offset_adu, 0.0) * gain * n_pix
    dark_e = settings.dark_current_e_per_pix_sec * current_exposure_sec * n_pix
    robust_bg_sigma = (
        1.4826 * background_stats.mad
        if math.isfinite(background_stats.mad) and background_stats.mad > 0
        else background_stats.std
    )
    bg_estimator_std = 1.2533 * robust_bg_sigma / math.sqrt(max(background_stats.count, 1))
    bg_estimator_var_e2 = (n_pix * bg_estimator_std * gain) ** 2
    variance = (
        signal_e
        + background_e
        + dark_e
        + n_pix * settings.read_noise_e**2
        + bg_estimator_var_e2
    )
    model_snr = signal_e / math.sqrt(variance) if variance > 0 else 0.0
    aperture_scatter = _background_aperture_scatter(background_image, background_mask, n_pix)
    spatial_contrast_snr = (
        excess / aperture_scatter
        if aperture_scatter is not None and aperture_scatter > 0
        else None
    )
    # Spatial variation across the background is not temporal detector noise.  It may
    # be real nebulosity, gradients, flat-field residuals or stars.  Report it as a
    # separate contrast-quality diagnostic instead of silently replacing the physical
    # photon/read-noise SNR used by the exposure planner.
    current_snr = model_snr
    if spatial_contrast_snr is not None and spatial_contrast_snr < 10:
        notes.append(
            "배경의 공간적 변화가 커 대상 대비 신뢰도가 낮습니다. "
            "표시 SNR은 광자·읽기잡음 모델값이며 ROI와 배경 모델을 다시 확인하세요."
        )
    return SignalMeasurement(
        target_mode="extended",
        current_snr=float(current_snr),
        model_snr=float(model_snr),
        signal_adu_per_pixel=float(excess),
        background_adu_per_pixel=float(background_level),
        sensor_background_adu_per_pixel=None,
        background_std_adu=float(background_stats.std),
        target_std_adu=float(target_stats.std),
        background_estimator_std_adu=float(bg_estimator_std),
        effective_pixels=n_pix,
        target_pixels=target_stats.count,
        background_pixels=background_stats.count,
        spatial_contrast_snr=None if spatial_contrast_snr is None else float(spatial_contrast_snr),
        target_roi=target_roi,
        background_roi=background_roi,
        notes=notes,
    )



def measure_point_source(
    image: np.ndarray,
    stars: list[StarMeasurement],
    settings: AnalysisSettings,
) -> SignalMeasurement:
    valid = [
        star for star in stars
        if not star.hot_pixel_like and not star.saturated and math.isfinite(star.snr) and star.snr > 0
    ]
    if not valid:
        raise ValueError("점광원 측광에 사용할 비포화 유효 별이 없습니다. 더 짧은 시험 영상을 사용하세요.")
    target_roi = _parse_normalized_roi(settings.target_roi_json, image.shape)
    notes: list[str] = []
    if target_roi is not None:
        inside = [
            star for star in valid
            if target_roi["x"] <= star.x < target_roi["x"] + target_roi["w"]
            and target_roi["y"] <= star.y < target_roi["y"] + target_roi["h"]
        ]
        if not inside:
            raise ValueError("점광원 대상 ROI 안에서 비포화 별을 찾지 못했습니다.")
        center_x, center_y = target_roi["x"] + target_roi["w"] / 2, target_roi["y"] + target_roi["h"] / 2
        chosen = min(inside, key=lambda star: (star.x - center_x) ** 2 + (star.y - center_y) ** 2)
        notes.append("대상 ROI 중심에 가장 가까운 비포화 별을 측정했습니다.")
    else:
        if len(valid) < 3:
            raise ValueError("대상 ROI가 없고 통계에 사용할 비포화 별도 3개 미만입니다.")
        ordered = sorted(valid, key=lambda star: star.flux_adu)
        chosen = ordered[len(ordered) // 2]
        notes.append("특정 목표별 ROI가 없어 대표 별을 사용했습니다. 목표별 ROI를 지정하면 더 정확합니다.")
    bg_estimator_std = chosen.background_std_adu / math.sqrt(max(chosen.background_pixels, 1))
    return SignalMeasurement(
        target_mode="point",
        current_snr=float(chosen.snr),
        model_snr=float(chosen.snr),
        signal_adu_per_pixel=float(chosen.flux_adu),
        background_adu_per_pixel=float(chosen.background_adu),
        sensor_background_adu_per_pixel=None,
        background_std_adu=float(chosen.background_std_adu),
        target_std_adu=0.0,
        background_estimator_std_adu=float(bg_estimator_std),
        effective_pixels=int(chosen.aperture_pixels),
        target_pixels=int(chosen.aperture_pixels),
        background_pixels=int(chosen.background_pixels),
        point_flux_adu=float(chosen.flux_adu),
        fwhm_px=float(chosen.fwhm_px),
        star_count=1,
        target_roi=target_roi,
        notes=notes,
    )



def sensor_background_for_measurement(
    raw_image: np.ndarray,
    measurement: SignalMeasurement,
) -> float:
    if measurement.background_roi:
        values = raw_image[_roi_slice(measurement.background_roi)]
        stats = sigma_clipped_stats(values)
        if stats.count >= 30 and math.isfinite(stats.median):
            return float(stats.median)
    values = raw_image[np.isfinite(raw_image)]
    if values.size == 0:
        raise ValueError("원본 센서 배경 ADU를 측정할 수 없습니다.")
    # Exclude bright objects for a robust detector-domain sky estimate.
    upper = np.percentile(values, 80)
    stats = sigma_clipped_stats(values[values <= upper])
    if not math.isfinite(stats.median):
        raise ValueError("원본 센서 배경 ADU를 측정할 수 없습니다.")
    return float(stats.median)
