from __future__ import annotations

"""Relative surface-brightness structure model for extended targets.

The module deliberately treats survey cutouts as *morphology only*.  Absolute target
flux continues to come from NØXIS' calibrated photometric model.  This avoids mixing
DSS/HiPS plate counts, filter passbands and the user's detector response.
"""

from dataclasses import dataclass, asdict
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage


@dataclass(frozen=True)
class StructureZone:
    name: str
    percentile_low: float
    percentile_high: float
    pixel_fraction: float
    median_relative_to_mean: float
    representative_relative_to_mean: float


@dataclass(frozen=True)
class TargetStructureProfile:
    status: str
    confidence: str
    source: str
    survey: str | None
    fov_deg: float | None
    target_radius_px: float | None
    usable_pixels: int
    star_mask_fraction: float
    detection_floor: float | None
    diffuse_contrast_sigma: float | None
    mean_signal_reference_units: float | None
    faint_structure_factor: float | None
    bright_structure_factor: float | None
    dynamic_range_factor: float | None
    science_percentile: float
    zones: tuple[StructureZone, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["zones"] = [asdict(z) for z in self.zones]
        return data


def unavailable_profile(*notes: str, source: str = "unavailable") -> TargetStructureProfile:
    return TargetStructureProfile(
        status="unavailable",
        confidence="none",
        source=source,
        survey=None,
        fov_deg=None,
        target_radius_px=None,
        usable_pixels=0,
        star_mask_fraction=0.0,
        detection_floor=None,
        diffuse_contrast_sigma=None,
        mean_signal_reference_units=None,
        faint_structure_factor=None,
        bright_structure_factor=None,
        dynamic_range_factor=None,
        science_percentile=25.0,
        zones=(),
        notes=tuple(notes),
    )


def _robust_median_sigma(values: np.ndarray) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0, 0.0
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    sigma = 1.4826 * mad
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = float(np.std(values)) if values.size > 1 else 0.0
    return med, max(sigma, 1e-12)


def _central_geometry(shape: tuple[int, int], target_diameter_deg: float | None, fov_deg: float | None) -> tuple[np.ndarray, float]:
    h, w = shape
    yy, xx = np.indices(shape, dtype=float)
    cy = (h - 1) / 2.0
    cx = (w - 1) / 2.0
    rr = np.hypot(xx - cx, yy - cy)
    base = min(h, w)
    if target_diameter_deg and fov_deg and target_diameter_deg > 0 and fov_deg > 0:
        radius = 0.5 * base * target_diameter_deg / fov_deg * 1.30
    else:
        radius = base * 0.22
    radius = float(np.clip(radius, base * 0.10, base * 0.38))
    return rr, radius


def _compact_source_mask(image: np.ndarray, valid: np.ndarray, target_mask: np.ndarray) -> np.ndarray:
    """Mask compact positive peaks without treating broad target structure as stars."""
    small = ndimage.gaussian_filter(image, 0.8, mode="nearest")
    broad = ndimage.gaussian_filter(image, 3.5, mode="nearest")
    highpass = small - broad
    sample = highpass[valid & target_mask]
    med, sigma = _robust_median_sigma(sample)
    if sample.size < 100 or sigma <= 0:
        return np.zeros_like(valid, dtype=bool)
    threshold = med + 7.0 * sigma
    candidates = valid & target_mask & (highpass > threshold)
    # Require local maxima-like compact islands and expand through PSF wings.
    local_max = small >= ndimage.maximum_filter(small, size=5, mode="nearest")
    seeds = candidates & local_max
    mask = ndimage.binary_dilation(seeds, iterations=3)
    # If an unusually structured nebula triggers the compact detector, disable rather
    # than erasing real morphology.
    fraction = float(np.mean(mask[target_mask])) if np.any(target_mask) else 0.0
    if fraction > 0.15:
        return np.zeros_like(valid, dtype=bool)
    return mask


def analyze_relative_structure(
    image: np.ndarray,
    *,
    target_diameter_deg: float | None = None,
    fov_deg: float | None = None,
    source: str = "survey_cutout",
    survey: str | None = None,
    science_percentile: float = 25.0,
) -> TargetStructureProfile:
    """Measure a robust relative brightness distribution from a centered cutout.

    ``science_percentile`` is intentionally not the faintest pixel.  The 25th
    percentile of detected diffuse target pixels is used for integration-time
    planning, which protects faint structure while remaining resistant to catalogue
    size errors and residual background pixels.
    """
    arr = np.asarray(image, dtype=float)
    notes: list[str] = []
    if arr.ndim != 2 or min(arr.shape) < 48:
        return unavailable_profile("참조 영상이 2차원 48px 이상이 아니어서 구조 분석을 생략했습니다.", source=source)
    valid = np.isfinite(arr)
    if np.mean(valid) < 0.75:
        return unavailable_profile("참조 영상의 유효 픽셀이 부족해 구조 분석을 생략했습니다.", source=source)
    fill = float(np.nanmedian(arr[valid]))
    work = np.where(valid, arr, fill)
    rr, target_radius = _central_geometry(arr.shape, target_diameter_deg, fov_deg)
    target_mask = valid & (rr <= target_radius)
    base = min(arr.shape)
    outer_inner = max(target_radius * 1.45, base * 0.39)
    background_mask = valid & (rr >= outer_inner) & (rr <= base * 0.49)
    if np.sum(background_mask) < 200:
        background_mask = valid & (rr >= min(base * 0.34, target_radius * 1.25))
    bg_med, bg_sigma = _robust_median_sigma(work[background_mask])

    star_mask = _compact_source_mask(work, valid, target_mask)
    diffuse = ndimage.gaussian_filter(work, 1.5, mode="nearest") - bg_med
    target_values_all = diffuse[target_mask & ~star_mask]
    if target_values_all.size < 200:
        return unavailable_profile("대상 영역의 유효 픽셀이 부족해 구조 분석을 생략했습니다.", source=source)

    positive_q95 = float(np.percentile(target_values_all, 95))
    detection_floor = max(1.25 * bg_sigma, positive_q95 * 0.025, 1e-12)
    detected = target_mask & ~star_mask & (diffuse > detection_floor)

    # Remove tiny disconnected islands (typically residual field stars/noise) but
    # allow multiple large components for fragmented nebulae.
    labels, count = ndimage.label(detected)
    if count:
        sizes = np.bincount(labels.ravel())
        min_island = max(12, int(0.0008 * np.sum(target_mask)))
        keep = sizes >= min_island
        keep[0] = False
        detected = keep[labels]

    values = diffuse[detected]
    if values.size < max(150, int(np.sum(target_mask) * 0.03)):
        return unavailable_profile(
            "DSS 참조 영상에서 배경보다 유의하게 밝은 확산 구조를 충분히 분리하지 못했습니다. 기존 평균 표면밝기 모델로 되돌립니다.",
            source=source,
        )

    mean_signal = float(np.mean(values))
    if not math.isfinite(mean_signal) or mean_signal <= 0:
        return unavailable_profile("참조 영상의 확산 신호 평균이 유효하지 않습니다.", source=source)

    science_percentile = float(np.clip(science_percentile, 10.0, 45.0))
    faint_value = float(np.percentile(values, science_percentile))
    # q97.5 is deliberately robust to a handful of residual stellar cores.
    bright_value = float(np.percentile(values, 97.5))
    faint_factor = float(np.clip(faint_value / mean_signal, 0.03, 1.0))
    bright_factor = float(np.clip(bright_value / mean_signal, 1.0, 30.0))
    dynamic = bright_factor / max(faint_factor, 1e-12)

    edges = [0.0, 20.0, 40.0, 60.0, 80.0, 95.0, 100.0]
    names = ["매우 희미", "희미", "중간", "중간-밝음", "밝음", "코어"]
    zones: list[StructureZone] = []
    total = values.size
    for i, name in enumerate(names):
        lo, hi = edges[i], edges[i + 1]
        vlo = float(np.percentile(values, lo))
        vhi = float(np.percentile(values, hi))
        if i == len(names) - 1:
            member = (values >= vlo) & (values <= vhi)
        else:
            member = (values >= vlo) & (values < vhi)
        zv = values[member]
        if zv.size == 0:
            continue
        rel_med = float(np.median(zv) / mean_signal)
        rel_rep = float(np.percentile(zv, 50) / mean_signal)
        zones.append(StructureZone(name, lo, hi, float(zv.size / total), rel_med, rel_rep))

    contrast_sigma = float((np.percentile(values, 90) - detection_floor) / max(bg_sigma, 1e-12))
    star_fraction = float(np.mean(star_mask[target_mask])) if np.any(target_mask) else 0.0
    coverage = float(values.size / max(np.sum(target_mask & ~star_mask), 1))
    if values.size >= 2000 and contrast_sigma >= 8 and star_fraction <= 0.08 and coverage >= 0.08:
        confidence = "high"
    elif values.size >= 600 and contrast_sigma >= 4 and star_fraction <= 0.12:
        confidence = "medium"
    else:
        confidence = "low"
    if confidence == "low":
        notes.append("참조 영상의 대비/유효 면적이 낮아 구조 계수의 신뢰도를 낮게 표시합니다.")
    notes.append(
        f"구조 영상은 절대 광량이 아니라 상대 형태만 사용했습니다. {science_percentile:.0f}백분위 밝기를 희미한 과학 구역 기준으로 사용합니다."
    )
    return TargetStructureProfile(
        status="ok",
        confidence=confidence,
        source=source,
        survey=survey,
        fov_deg=None if fov_deg is None else float(fov_deg),
        target_radius_px=target_radius,
        usable_pixels=int(values.size),
        star_mask_fraction=star_fraction,
        detection_floor=detection_floor,
        diffuse_contrast_sigma=contrast_sigma,
        mean_signal_reference_units=mean_signal,
        faint_structure_factor=faint_factor,
        bright_structure_factor=bright_factor,
        dynamic_range_factor=float(dynamic),
        science_percentile=science_percentile,
        zones=tuple(zones),
        notes=tuple(notes),
    )


def save_structure_diagnostic(
    image: np.ndarray,
    profile: TargetStructureProfile,
    output_path: Path,
    *,
    target_diameter_deg: float | None = None,
) -> None:
    """Save a compact morphology diagnostic. Failure is non-fatal to planning."""
    if profile.status != "ok":
        return
    import matplotlib.pyplot as plt

    arr = np.asarray(image, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return
    lo, hi = np.percentile(finite, [2, 99.5])
    fig = plt.figure(figsize=(10, 4.7), constrained_layout=True)
    ax1 = fig.add_subplot(1, 2, 1)
    ax1.imshow(arr, origin="lower", cmap="gray", vmin=lo, vmax=hi)
    if profile.target_radius_px:
        from matplotlib.patches import Circle
        cy, cx = (np.array(arr.shape) - 1) / 2
        ax1.add_patch(Circle((cx, cy), profile.target_radius_px, fill=False, linewidth=1.2))
    ax1.set_title("Reference morphology")
    ax1.set_xticks([]); ax1.set_yticks([])

    ax2 = fig.add_subplot(1, 2, 2)
    labels = [f"P{z.percentile_low:.0f}-{z.percentile_high:.0f}" for z in profile.zones]
    vals = [z.representative_relative_to_mean for z in profile.zones]
    ax2.bar(range(len(vals)), vals)
    ax2.axhline(1.0, linewidth=1.0, linestyle="--")
    ax2.axhline(profile.faint_structure_factor or 0, linewidth=1.0, linestyle=":")
    ax2.axhline(profile.bright_structure_factor or 0, linewidth=1.0, linestyle=":")
    ax2.set_xticks(range(len(vals)), labels, rotation=35, ha="right")
    ax2.set_ylabel("Relative to detected target mean")
    ax2.set_title("Brightness zones")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
