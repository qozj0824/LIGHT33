from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, cast

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from scipy import ndimage

from .geometry import pixel_solid_angle_arcsec2, pixel_to_altaz, validate_fisheye_calibration
from .models import AnalysisSettings, FisheyeConfig, ImageFrame, SkyCell, SkyMapSummary
from .photometry import sigma_clipped_stats
from .plotting import plot_text



def _decimate(image: np.ndarray, max_dim: int = 1200) -> tuple[np.ndarray, float, float]:
    height, width = image.shape
    scale = max(height / max_dim, width / max_dim, 1.0)
    if scale <= 1:
        return image.astype(np.float64, copy=False), 1.0, 1.0
    out_h = max(1, int(round(height / scale)))
    out_w = max(1, int(round(width / scale)))
    reduced = ndimage.zoom(image, (out_h / height, out_w / width), order=1, prefilter=False)
    return reduced, width / out_w, height / out_h



def _star_mask(image: np.ndarray) -> tuple[np.ndarray, str, int]:
    """Detect stellar PSFs and mask their surrounding wings.

    DAOStarFinder is the report-aligned primary method. A morphology fallback is
    retained only so an unusual/undersampled frame can still be diagnosed; the
    chosen method is returned and exposed in the result diagnostics.
    """
    smooth = ndimage.gaussian_filter(image, sigma=0.7)
    background = ndimage.gaussian_filter(smooth, sigma=12.0, mode="reflect")
    residual = smooth - background
    stats = sigma_clipped_stats(residual)
    sigma = max(stats.std, 1e-9)
    mask = np.zeros(image.shape, dtype=bool)
    try:
        from photutils.detection import DAOStarFinder

        finder = DAOStarFinder(fwhm=3.0, threshold=5.5 * sigma, exclude_border=True)
        sources = finder(residual - stats.median)
        if sources is not None and len(sources):
            yy, xx = np.indices(image.shape)
            peaks = np.asarray(sources["peak"], dtype=float) if "peak" in sources.colnames else np.ones(len(sources))
            peak_ref = max(float(np.nanmedian(peaks[peaks > 0])) if np.any(peaks > 0) else 1.0, 1e-9)
            for row, peak in zip(sources, peaks, strict=False):
                x = float(row["xcentroid"])
                y = float(row["ycentroid"])
                brightness_factor = min(max(math.sqrt(max(float(peak), 0.0) / peak_ref), 1.0), 3.0)
                radius = 4.0 + 2.0 * brightness_factor
                x0, x1 = max(0, int(x - radius - 1)), min(image.shape[1], int(x + radius + 2))
                y0, y1 = max(0, int(y - radius - 1)), min(image.shape[0], int(y + radius + 2))
                local_x = xx[y0:y1, x0:x1]
                local_y = yy[y0:y1, x0:x1]
                mask[y0:y1, x0:x1] |= (local_x - x) ** 2 + (local_y - y) ** 2 <= radius**2
            if np.mean(mask) <= 0.40:
                return mask, "DAOStarFinder", int(len(sources))
    except Exception:
        pass

    threshold_sigma = 6.0
    core = residual > stats.median + threshold_sigma * sigma
    mask = ndimage.binary_dilation(core, iterations=2)
    while np.mean(mask) > 0.35 and threshold_sigma < 16:
        threshold_sigma += 1.5
        core = residual > stats.median + threshold_sigma * sigma
        mask = ndimage.binary_dilation(core, iterations=1)
    _, count = ndimage.label(core)
    return np.asarray(mask, dtype=bool), "morphology_fallback", int(count)



def _angular_distance(
    az1: np.ndarray,
    alt1: np.ndarray,
    az2: float,
    alt2: float,
) -> np.ndarray:
    az1r, alt1r = np.deg2rad(az1), np.deg2rad(alt1)
    az2r, alt2r = math.radians(az2), math.radians(alt2)
    cosine = (
        np.sin(alt1r) * math.sin(alt2r)
        + np.cos(alt1r) * math.cos(alt2r) * np.cos(az1r - az2r)
    )
    return np.rad2deg(np.arccos(np.clip(cosine, -1.0, 1.0)))



def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return float(values[order[np.searchsorted(cumulative, cumulative[-1] / 2)]])



def _save_rectangular_map(
    grid: np.ndarray,
    reliability_grid: np.ndarray,
    settings: AnalysisSettings,
    path: Path,
    *,
    title: str,
    colorbar_label: str,
    target: bool = True,
    vmin: float | None = None,
    vmax: float | None = None,
) -> None:
    masked = np.ma.masked_invalid(grid)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad("#d0d3d8")
    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    image = ax.imshow(
        masked,
        origin="lower",
        aspect="auto",
        extent=(0.0, 360.0, 0.0, 90.0),
        vmin=vmin,
        vmax=vmax,
        cmap=cmap,
    )
    blocked = reliability_grid == -1
    if np.any(blocked):
        blocked_mask = np.ma.masked_where(~blocked, np.ones_like(blocked, dtype=float))
        ax.imshow(
            blocked_mask,
            origin="lower",
            aspect="auto",
            extent=(0.0, 360.0, 0.0, 90.0),
            cmap=ListedColormap(["#3b3f48"]),
            alpha=0.75,
        )
    if target:
        ax.scatter([settings.target_az_deg], [settings.target_alt_deg], marker="x", s=85, label=plot_text("목표", "Target"))
    ax.axhline(settings.minimum_sky_altitude_deg, linestyle="--", linewidth=1.2, label=plot_text("분석 최저 고도", "Minimum analyzed altitude"))
    ax.legend(loc="upper right")
    ax.set_xlabel(plot_text("방위각 (°)", "Azimuth (deg)"))
    ax.set_ylabel(plot_text("고도 (°)", "Altitude (deg)"))
    ax.set_title(title)
    fig.colorbar(image, ax=ax, label=colorbar_label)
    fig.savefig(path, dpi=160)
    plt.close(fig)



def build_sky_map(
    frame: ImageFrame,
    settings: AnalysisSettings,
    fisheye: FisheyeConfig,
    result_dir: Path,
    *,
    flat_applied: bool = False,
) -> SkyMapSummary:
    image, sx, sy = _decimate(frame.intensity)
    original_width = frame.intensity.shape[1] * frame.coordinate_scale_x
    original_height = frame.intensity.shape[0] * frame.coordinate_scale_y
    calibration_scale_x = 1.0
    calibration_scale_y = 1.0
    if fisheye.mode == "calibrated_kannala_brandt":
        if fisheye.sensor_width is not None and fisheye.sensor_height is not None:
            width_ratio = original_width / fisheye.sensor_width
            height_ratio = original_height / fisheye.sensor_height
            exact_width = abs(width_ratio - 1.0) <= 0.02
            exact_height = abs(height_ratio - 1.0) <= 0.02
            if not (exact_width and exact_height):
                # A uniformly resized frame preserves the calibrated optical geometry.
                # Map its pixels back to detector coordinates instead of rejecting it.
                scale_x = fisheye.sensor_width / max(original_width, 1e-9)
                scale_y = fisheye.sensor_height / max(original_height, 1e-9)
                proportional = abs(scale_x / max(scale_y, 1e-9) - 1.0) <= 0.02
                if not proportional:
                    raise ValueError(
                        "전천 영상 종횡비가 어안 보정 영상과 다릅니다. "
                        f"입력 {original_width:.0f}×{original_height:.0f}, "
                        f"보정 {fisheye.sensor_width}×{fisheye.sensor_height}."
                    )
                calibration_scale_x = scale_x
                calibration_scale_y = scale_y
        elif fisheye.sensor_width is not None:
            relative = abs(original_width - fisheye.sensor_width) / fisheye.sensor_width
            if relative > 0.02:
                raise ValueError(
                    f"전천 영상 원본 폭({original_width:.0f})이 어안 보정 폭({fisheye.sensor_width})과 다릅니다."
                )
        elif fisheye.sensor_height is not None:
            relative = abs(original_height - fisheye.sensor_height) / fisheye.sensor_height
            if relative > 0.02:
                raise ValueError(
                    f"전천 영상 원본 높이({original_height:.0f})가 어안 보정 높이({fisheye.sensor_height})과 다릅니다."
                )
    yy, xx = np.indices(image.shape, dtype=np.float64)
    total_scale_x = frame.coordinate_scale_x * sx * calibration_scale_x
    total_scale_y = frame.coordinate_scale_y * sy * calibration_scale_y
    az, alt, valid = pixel_to_altaz(
        xx,
        yy,
        image.shape,
        fisheye,
        coordinate_scale_x=total_scale_x,
        coordinate_scale_y=total_scale_y,
    )
    solid_angle_map: np.ndarray | None = None
    # Direction lookup may be report-validated in detector pixels while absolute
    # solid-angle photometry requires the stricter angular-RMS validation.
    if fisheye.mode == "calibrated_kannala_brandt" and not validate_fisheye_calibration(fisheye):
        solid_angle_map = pixel_solid_angle_arcsec2(
            xx,
            yy,
            fisheye,
            coordinate_scale_x=total_scale_x,
            coordinate_scale_y=total_scale_y,
            area_multiplier=(
                frame.photometric_area_multiplier * calibration_scale_x * calibration_scale_y
            ),
        )
        solid_angle_map = np.where(
            np.isfinite(solid_angle_map) & (solid_angle_map > 0), solid_angle_map, np.nan
        )
    star_mask, star_detection_method, detected_star_count = _star_mask(image)
    finite = np.isfinite(image)
    min_alt = settings.minimum_sky_altitude_deg
    sky_geometry = valid & finite & (alt >= min_alt) & (alt <= 90)
    usable = sky_geometry & ~star_mask
    if np.count_nonzero(usable) < 1000:
        raise ValueError(
            "최저 고도와 별 마스크를 적용한 뒤 사용할 하늘 픽셀이 부족합니다. "
            "어안 보정, 전천 영상, 최저 고도를 확인하세요."
        )

    az_edges = np.linspace(0.0, 360.0, settings.az_bins + 1)
    alt_edges = np.linspace(0.0, 90.0, settings.alt_bins + 1)
    cells: list[SkyCell] = []
    for alt_index in range(settings.alt_bins):
        alt_lo, alt_hi = alt_edges[alt_index], alt_edges[alt_index + 1]
        alt_center = float((alt_lo + alt_hi) / 2)
        alt_sel = (alt >= alt_lo) & (
            alt < alt_hi if alt_index < settings.alt_bins - 1 else alt <= alt_hi
        )
        for az_index in range(settings.az_bins):
            az_lo, az_hi = az_edges[az_index], az_edges[az_index + 1]
            cell_geom = valid & finite & alt_sel & (az >= az_lo) & (az < az_hi)
            n_geom = int(np.count_nonzero(cell_geom))
            values = image[cell_geom & usable]
            n = int(values.size)
            masked_fraction = 1.0 - n / n_geom if n_geom else 1.0
            background = std = mad = standard_error = cv = None
            blocked_reason = None
            if alt_center < min_alt:
                reliability = "blocked"
                blocked_reason = f"최저 분석 고도 {min_alt:.1f}° 아래"
            elif n_geom == 0:
                reliability = "missing"
            elif n >= 20:
                stats = sigma_clipped_stats(values)
                background = stats.median
                std = stats.std
                mad = stats.mad if math.isfinite(stats.mad) else None
                robust_sigma = 1.4826 * mad if mad is not None else std
                standard_error = robust_sigma / math.sqrt(max(stats.count, 1))
                cv = robust_sigma / max(abs(background), 1e-9)
                coverage = 1.0 - masked_fraction
                relative_se = standard_error / max(abs(background), 1e-9)
                if (masked_fraction > 0.80 or cv > 0.55) and alt_center < max(35.0, min_alt + 10.0):
                    reliability = "blocked"
                    blocked_reason = "저고도 장애물·렌즈 가장자리 또는 매우 큰 내부 변동"
                    background = std = mad = standard_error = cv = None
                elif n >= 80 and coverage >= 0.70 and relative_se <= 0.03 and cv <= 0.35:
                    reliability = "good"
                elif n >= 40 and coverage >= 0.45 and relative_se <= 0.08 and cv <= 0.60:
                    reliability = "caution"
                else:
                    reliability = "low"
            else:
                reliability = "missing"
            cell_solid_angle = None
            if solid_angle_map is not None:
                omega_values = solid_angle_map[cell_geom & np.isfinite(solid_angle_map)]
                if omega_values.size:
                    cell_solid_angle = float(np.median(omega_values))
            cells.append(
                SkyCell(
                    az_center_deg=float((az_lo + az_hi) / 2),
                    alt_center_deg=alt_center,
                    background_adu=None if background is None else float(background),
                    background_std_adu=None if std is None else float(std),
                    background_mad_adu=None if mad is None else float(mad),
                    standard_error_adu=None if standard_error is None else float(standard_error),
                    coefficient_of_variation=None if cv is None else float(cv),
                    n_pixels=n,
                    masked_fraction=float(masked_fraction),
                    reliability=reliability,  # type: ignore[arg-type]
                    blocked_reason=blocked_reason,
                    solid_angle_arcsec2=cell_solid_angle,
                )
            )

    # Conservative low-altitude obstruction rejection.  A building/roof segment can
    # be nearly uniform and therefore escape a CV-only test.  Compare each low-altitude
    # cell with the robust median of its altitude ring and reject only extreme dark
    # discontinuities.  The threshold is intentionally strict so genuine directional
    # sky-brightness gradients are retained as sky signal.
    obstruction_rejections = 0
    for alt_index in range(settings.alt_bins):
        row = cells[alt_index * settings.az_bins:(alt_index + 1) * settings.az_bins]
        if not row:
            continue
        altitude = row[0].alt_center_deg
        if altitude >= max(35.0, min_alt + 15.0):
            continue
        ring_values = [
            float(cell.background_adu)
            for cell in row
            if cell.background_adu is not None
            and cell.reliability in {"good", "caution", "low"}
            and math.isfinite(cell.background_adu)
        ]
        if len(ring_values) < max(8, settings.az_bins // 8):
            continue
        ring_median = float(np.median(np.asarray(ring_values, dtype=float)))
        if ring_median <= 0:
            continue
        for cell in row:
            if (
                cell.background_adu is not None
                and cell.reliability in {"good", "caution", "low"}
                and cell.background_adu < 0.25 * ring_median
            ):
                cell.reliability = "blocked"
                cell.blocked_reason = "저고도 고체 장애물 의심: 같은 고도대 하늘보다 비정상적으로 어두움"
                cell.background_adu = None
                cell.background_std_adu = None
                cell.background_mad_adu = None
                cell.standard_error_adu = None
                cell.coefficient_of_variation = None
                obstruction_rejections += 1

    usable_cells = [
        cell
        for cell in cells
        if cell.reliability in {"good", "caution", "low"}
        and cell.background_adu is not None
        and math.isfinite(cell.background_adu)
    ]
    if len(usable_cells) < max(20, len(cells) * 0.08):
        raise ValueError("신뢰 가능한 전천지도 셀이 너무 적습니다.")
    backgrounds = np.array(
        [float(cell.background_adu) for cell in usable_cells if cell.background_adu is not None],
        dtype=np.float64,
    )
    if all(cell.solid_angle_arcsec2 is not None and cell.solid_angle_arcsec2 > 0 for cell in usable_cells):
        weights = np.array(
            [
                float(cell.solid_angle_arcsec2) * max(cell.n_pixels, 1)
                for cell in usable_cells
                if cell.solid_angle_arcsec2 is not None
            ],
            dtype=np.float64,
        )
    else:
        weights = np.array([max(cell.n_pixels, 1) for cell in usable_cells], dtype=float)
    sky_median = _weighted_median(backgrounds, weights)

    interpolation_cells = [cell for cell in usable_cells if cell.reliability in {"good", "caution"}]
    target_background = target_factor = target_uncertainty = target_solid_angle = None
    notes = [
        "이 지도는 광해만 분리한 지도가 아니라 관측 시점의 방향별 하늘 배경 ADU 지도입니다.",
        f"별 제거 방법: {star_detection_method} · 검출 후보 {detected_star_count:,}개 · 마스크 비율 {float(np.mean(star_mask))*100:.2f}%.",
        "전천지도는 망원경 대상 신호나 현재 SNR을 변경하지 않습니다.",
        f"고도 {min_alt:.1f}° 아래는 건물·나무·지상광 영향을 줄이기 위해 차폐 처리했습니다.",
    ]
    if obstruction_rejections:
        notes.append(f"저고도에서 같은 고도대 대비 극단적으로 어두운 {obstruction_rejections}개 셀을 고체 장애물 의심 영역으로 추가 차폐했습니다.")
    if not flat_applied:
        notes.append("전천 flat이 없어 렌즈 비네팅이 방향 차이에 일부 포함될 수 있습니다.")
    if interpolation_cells:
        cell_az = np.array([cell.az_center_deg for cell in interpolation_cells])
        cell_alt = np.array([cell.alt_center_deg for cell in interpolation_cells])
        distances = _angular_distance(cell_az, cell_alt, settings.target_az_deg, settings.target_alt_deg)
        nearest_order = np.argsort(distances)
        chosen_indices = [int(i) for i in nearest_order if distances[int(i)] <= 7.5][:6]
        if len(chosen_indices) >= 2:
            chosen = [interpolation_cells[i] for i in chosen_indices]
            chosen_distances = distances[chosen_indices]
            chosen_values = np.array(
                [float(cell.background_adu) for cell in chosen if cell.background_adu is not None],
                dtype=np.float64,
            )
            reliability_weight = np.array(
                [1.0 if cell.reliability == "good" else 0.55 for cell in chosen], dtype=float
            )
            inverse = 1.0 / np.maximum(chosen_distances, 0.7) ** 2
            combined = inverse * reliability_weight
            target_background = float(np.sum(combined * chosen_values) / np.sum(combined))
            cell_se = np.array(
                [float(cell.standard_error_adu or 0.0) for cell in chosen], dtype=float
            )
            interpolation_var = float(
                np.sum(combined * (chosen_values - target_background) ** 2) / np.sum(combined)
            )
            measurement_var = float(np.sum((combined / np.sum(combined)) ** 2 * cell_se**2))
            target_uncertainty = math.sqrt(max(interpolation_var + measurement_var, 0.0))
            target_factor = target_background / sky_median if sky_median > 0 else None
            omega_pairs = [
                (float(cell.solid_angle_arcsec2), float(combined[index]))
                for index, cell in enumerate(chosen)
                if cell.solid_angle_arcsec2 is not None and cell.solid_angle_arcsec2 > 0
            ]
            if omega_pairs:
                omega_values = np.array([pair[0] for pair in omega_pairs])
                omega_weights = np.array([pair[1] for pair in omega_pairs])
                target_solid_angle = float(np.sum(omega_values * omega_weights) / np.sum(omega_weights))
        else:
            notes.append("목표 방향 7.5° 이내에 good/caution 셀이 2개 미만이라 방향값을 계산하지 않았습니다.")

    table_path = result_dir / "sky_background.tsv"
    with table_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow([
            "az_deg", "alt_deg", "background_adu", "std_adu", "mad_adu", "standard_error_adu",
            "coefficient_of_variation", "n_pixels", "masked_fraction", "reliability",
            "blocked_reason", "solid_angle_arcsec2",
        ])
        for cell in cells:
            writer.writerow([
                f"{cell.az_center_deg:.6f}", f"{cell.alt_center_deg:.6f}",
                "nan" if cell.background_adu is None else f"{cell.background_adu:.8f}",
                "nan" if cell.background_std_adu is None else f"{cell.background_std_adu:.8f}",
                "nan" if cell.background_mad_adu is None else f"{cell.background_mad_adu:.8f}",
                "nan" if cell.standard_error_adu is None else f"{cell.standard_error_adu:.8f}",
                "nan" if cell.coefficient_of_variation is None else f"{cell.coefficient_of_variation:.8f}",
                cell.n_pixels, f"{cell.masked_fraction:.6f}", cell.reliability,
                cell.blocked_reason or "",
                "nan" if cell.solid_angle_arcsec2 is None else f"{cell.solid_angle_arcsec2:.8f}",
            ])

    preview_path = result_dir / "allsky_preview.png"
    p1, p99 = np.percentile(image[np.isfinite(image)], [1, 99.5])
    normalized = np.clip((image - p1) / max(p99 - p1, 1e-9), 0, 1)
    plt.imsave(preview_path, normalized, cmap="gray", origin="upper")

    coordinate_overlay_path = result_dir / "allsky_coordinate_overlay.png"
    target_distance = _angular_distance(az, alt, settings.target_az_deg, settings.target_alt_deg)
    candidates = np.where(valid & np.isfinite(target_distance), target_distance, np.inf)
    target_y, target_x = np.unravel_index(int(np.argmin(candidates)), candidates.shape)
    fig, ax = plt.subplots(figsize=(9, 7), constrained_layout=True)
    ax.imshow(normalized, cmap="gray", origin="upper")
    if np.isfinite(candidates[target_y, target_x]):
        ax.scatter([target_x], [target_y], marker="x", s=110, linewidths=2.5, label=plot_text("목표", "Target"))
    try:
        ax.contour((alt >= min_alt).astype(float), levels=[0.5], linewidths=1.2)
        ax.contour(valid.astype(float), levels=[0.5], linewidths=0.8)
    except ValueError:
        pass
    ax.set_title(plot_text(f"전천 좌표 오버레이 · 목표 방위각 {settings.target_az_deg:.1f}° / 고도 {settings.target_alt_deg:.1f}°", f"All-sky coordinate overlay · target Az {settings.target_az_deg:.1f} deg / Alt {settings.target_alt_deg:.1f} deg"))
    ax.set_axis_off()
    ax.legend(loc="upper right")
    fig.savefig(coordinate_overlay_path, dpi=160)
    plt.close(fig)

    grid = np.full((settings.alt_bins, settings.az_bins), np.nan)
    reliability_grid = np.full((settings.alt_bins, settings.az_bins), np.nan)
    rel_value = {"blocked": -1, "missing": 0, "low": 1, "caution": 2, "good": 3}
    for index, cell in enumerate(cells):
        ai, zi = divmod(index, settings.az_bins)
        grid[ai, zi] = np.nan if cell.background_adu is None else cell.background_adu
        reliability_grid[ai, zi] = rel_value[cell.reliability]

    # Azimuth is numerically unstable very close to the zenith, where a tiny sensor
    # area is split across many azimuth bins.  Keep the raw TSV unchanged, but display
    # the top 10 degrees as one altitude representative so the polar map does not show
    # artificial white radial spikes.
    display_grid = grid.copy()
    display_reliability = reliability_grid.copy()
    alt_centers_for_display = (alt_edges[:-1] + alt_edges[1:]) / 2
    zenith_smoothed = False
    for row_index, alt_center_value in enumerate(alt_centers_for_display):
        if alt_center_value < 80.0:
            continue
        row_values = grid[row_index]
        row_reliability = reliability_grid[row_index]
        usable_row = np.isfinite(row_values) & (row_reliability >= 1)
        if np.count_nonzero(usable_row) >= 3:
            representative = float(np.median(row_values[usable_row]))
            display_grid[row_index, :] = representative
            good_fraction_row = float(np.mean(row_reliability[usable_row] == 3))
            display_reliability[row_index, :] = 3 if good_fraction_row >= 0.5 else 2
            zenith_smoothed = True
    if zenith_smoothed:
        notes.append(
            "천정 80° 이상은 방위각이 불안정하므로 그림에서 고도별 대표값으로 표시했습니다. "
            "TSV에는 원래 셀 값이 남아 있습니다."
        )

    finite_grid = display_grid[np.isfinite(display_grid)]
    if finite_grid.size:
        percentile_values = np.percentile(finite_grid, [3, 97])
        vmin, vmax = float(percentile_values[0]), float(percentile_values[1])
    else:
        vmin, vmax = 0.0, 1.0
    map_label = (
        "Flat 보정된 관측 시점 방향별 하늘 배경 ADU 지도"
        if flat_applied
        else "비네팅 미보정 관측 시점 방향별 하늘 배경 ADU 지도"
    )
    map_path = result_dir / "sky_background_map.png"
    _save_rectangular_map(
        display_grid, display_reliability, settings, map_path,
        title=map_label, colorbar_label=plot_text("배경 ADU", "Background ADU"), vmin=vmin, vmax=vmax,
    )

    relative_grid = display_grid / sky_median if sky_median > 0 else display_grid * np.nan
    relative_map_path = result_dir / "sky_relative_map.png"
    _save_rectangular_map(
        relative_grid, display_reliability, settings, relative_map_path,
        title=plot_text("중앙 하늘 대비 상대 배경 밝기", "Relative sky background vs all-sky median"), colorbar_label=plot_text("배경비 (1 = 중앙값)", "Background ratio (1 = median)"),
        vmin=0.6, vmax=1.8,
    )

    polar_map_path = result_dir / "sky_polar_relative.png"
    az_centers = np.deg2rad((az_edges[:-1] + az_edges[1:]) / 2)
    alt_centers = (alt_edges[:-1] + alt_edges[1:]) / 2
    theta_edges = np.deg2rad(az_edges)
    radius_edges = 90.0 - alt_edges[::-1]
    polar_data = relative_grid[::-1, :]
    fig = plt.figure(figsize=(8, 8), constrained_layout=True)
    ax = cast(Any, fig.add_subplot(111, projection="polar"))
    mesh = ax.pcolormesh(
        theta_edges, radius_edges, np.ma.masked_invalid(polar_data),
        cmap="viridis", vmin=0.6, vmax=1.8, shading="auto"
    )
    polar_reliability = display_reliability[::-1, :]
    blocked_overlay = np.ma.masked_where(polar_reliability != -1, np.ones_like(polar_reliability))
    missing_overlay = np.ma.masked_where(polar_reliability != 0, np.ones_like(polar_reliability))
    if np.any(polar_reliability == -1):
        ax.pcolormesh(
            theta_edges, radius_edges, blocked_overlay,
            cmap=ListedColormap(["#3b3f48"]), shading="auto", vmin=0, vmax=1
        )
    if np.any(polar_reliability == 0):
        ax.pcolormesh(
            theta_edges, radius_edges, missing_overlay,
            cmap=ListedColormap(["#d0d3d8"]), shading="auto", vmin=0, vmax=1
        )
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    max_radius = 90.0 - max(min_alt, 0.0)
    ax.set_ylim(0.0, max_radius)
    radial_ticks = [value for value in (0, 15, 30, 45, 60, 75) if value <= max_radius]
    ax.set_yticks(radial_ticks)
    ax.set_yticklabels([f"{90 - value:.0f}°" for value in radial_ticks])
    ax.scatter([math.radians(settings.target_az_deg)], [90 - settings.target_alt_deg], marker="x", s=90, label=plot_text("목표", "Target"))
    ax.set_title(plot_text("하늘을 올려다본 원형 상대 배경지도\n가운데=천정, 바깥=지평선", "Polar relative sky-background map\ncenter=zenith, edge=horizon"))
    ax.legend(loc="upper right", bbox_to_anchor=(1.15, 1.12))
    fig.colorbar(mesh, ax=ax, pad=0.10, label=plot_text("중앙값 대비 배경비", "Background ratio to median"))
    fig.savefig(polar_map_path, dpi=170)
    plt.close(fig)

    reliability_path = result_dir / "sky_reliability.png"
    cmap = ListedColormap(["#3b3f48", "#d0d3d8", "#d95f5f", "#f0c75e", "#4caf70"])
    norm = BoundaryNorm([-1.5, -0.5, 0.5, 1.5, 2.5, 3.5], cmap.N)
    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    image_rel = ax.imshow(display_reliability, origin="lower", aspect="auto", extent=(0, 360, 0, 90), cmap=cmap, norm=norm)
    ax.axhline(min_alt, linestyle="--", linewidth=1.2)
    ax.set_xlabel(plot_text("방위각 (°)", "Azimuth (deg)"))
    ax.set_ylabel(plot_text("고도 (°)", "Altitude (deg)"))
    ax.set_title(plot_text("전천 셀 신뢰도 · 차폐/결측/낮음/주의/좋음", "All-sky cell reliability · blocked/missing/low/caution/good"))
    colorbar = fig.colorbar(image_rel, ax=ax, ticks=[-1, 0, 1, 2, 3])
    colorbar.ax.set_yticklabels([
        plot_text("차폐", "blocked"),
        plot_text("결측", "missing"),
        plot_text("낮음", "low"),
        plot_text("주의", "caution"),
        plot_text("좋음", "good"),
    ])
    fig.savefig(reliability_path, dpi=160)
    plt.close(fig)

    horizon_profile_path = result_dir / "sky_altitude_profiles.png"
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    for requested_alt in (20.0, 40.0, 60.0):
        if requested_alt < min_alt:
            continue
        index = int(np.argmin(np.abs(alt_centers - requested_alt)))
        values = relative_grid[index]
        ax.plot(np.rad2deg(az_centers), values, label=plot_text(f"고도 {alt_centers[index]:.0f}°", f"Altitude {alt_centers[index]:.0f} deg"))
    ax.axhline(1.0, linestyle="--", linewidth=1.0, label=plot_text("전천 중앙값", "All-sky median"))
    ax.set_xlim(0, 360)
    ax.set_xlabel(plot_text("방위각 (°)", "Azimuth (deg)"))
    ax.set_ylabel(plot_text("중앙값 대비 배경비", "Background ratio to median"))
    ax.set_title(plot_text("방향별 하늘 배경 비교", "Sky background by direction"))
    ax.legend(loc="best")
    fig.savefig(horizon_profile_path, dpi=160)
    plt.close(fig)

    distribution_path = result_dir / "sky_background_distribution.png"
    fig, ax = plt.subplots(figsize=(9, 5), constrained_layout=True)
    ax.hist(backgrounds, bins=min(60, max(15, int(np.sqrt(backgrounds.size)))))
    ax.axvline(sky_median, linestyle="--", label=plot_text(f"전천 중앙값 {sky_median:.2f}", f"All-sky median {sky_median:.2f}"))
    if target_background is not None:
        ax.axvline(target_background, linestyle=":", label=plot_text(f"목표 방향 {target_background:.2f}", f"Target direction {target_background:.2f}"))
    # ADU values should be shown literally.  Matplotlib's default +2.2e3 offset
    # made a 2200 ADU test image look like it was centered at 0.
    try:
        formatter = ax.xaxis.get_major_formatter()
        formatter.set_useOffset(False)
        formatter.set_scientific(False)
    except Exception:
        pass
    ax.set_xlabel(plot_text("셀별 하늘 배경 ADU", "Sky background ADU per cell"))
    ax.set_ylabel(plot_text("셀 수", "Cell count"))
    ax.set_title(plot_text("사용 가능한 하늘 배경 셀 분포", "Distribution of usable sky cells"))
    ax.legend(loc="best")
    fig.savefig(distribution_path, dpi=160)
    plt.close(fig)

    good = sum(cell.reliability == "good" for cell in cells)
    blocked = sum(cell.reliability == "blocked" for cell in cells)
    usable_count = sum(cell.reliability in {"good", "caution", "low"} for cell in cells)
    return SkyMapSummary(
        cells=cells,
        sky_median_adu=sky_median,
        target_background_adu=target_background,
        target_relative_factor=target_factor,
        target_uncertainty_adu=target_uncertainty,
        target_solid_angle_arcsec2=target_solid_angle,
        usable_fraction=usable_count / len(cells),
        good_fraction=good / len(cells),
        blocked_fraction=blocked / len(cells),
        map_label=map_label,
        star_detection_method=star_detection_method,
        detected_star_count=detected_star_count,
        star_mask_fraction=float(np.mean(star_mask)),
        preview_path=preview_path.name,
        coordinate_overlay_path=coordinate_overlay_path.name,
        map_path=map_path.name,
        relative_map_path=relative_map_path.name,
        polar_map_path=polar_map_path.name,
        reliability_path=reliability_path.name,
        horizon_profile_path=horizon_profile_path.name,
        distribution_path=distribution_path.name,
        table_path=table_path.name,
        notes=notes,
    )
