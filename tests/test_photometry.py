from __future__ import annotations

import numpy as np

from lightt.models import AnalysisSettings, ImageFrame, ImageMetadata
from lightt.io import infer_intensity_domain
from lightt.photometry import analyze_saturation, measure_stars


def gaussian_star(image: np.ndarray, x: int, y: int, peak: float, sigma: float = 2.0) -> None:
    yy, xx = np.indices(image.shape)
    image += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))


def test_hot_pixel_does_not_set_star_reference() -> None:
    rng = np.random.default_rng(2)
    image = rng.normal(1000, 3, (320, 420)).astype(np.float32)
    for index, peak in enumerate([12000, 16000, 22000, 30000, 42000, 50000, 56000]):
        gaussian_star(image, 40 + index * 50, 100 + (index % 2) * 80, peak)
    image[250, 350] = 65535
    meta = ImageMetadata("test.fits", "fits", 420, 320, "float32", bit_depth=16)
    frame = ImageFrame(image, meta, raw_intensity=image)
    domain = infer_intensity_domain(frame, 65535, 0.8)
    settings = AnalysisSettings(current_exposure_sec=30, target_mode="point", max_stars=100)
    stars = measure_stars(image, domain, settings)
    report = analyze_saturation(image, domain, stars, "balanced")
    assert report.reference_peak_total_adu is not None
    assert report.reference_peak_total_adu < 65000
    assert report.usable_unsaturated_star_count >= 5


def test_saturated_stars_are_excluded_from_reference() -> None:
    rng = np.random.default_rng(4)
    image = rng.normal(900, 2, (300, 400)).astype(np.float32)
    for index, peak in enumerate([10000, 18000, 26000, 36000, 48000, 60000]):
        gaussian_star(image, 50 + index * 55, 100 + (index % 2) * 100, peak)
    gaussian_star(image, 200, 150, 90000)
    np.clip(image, 0, 65535, out=image)
    meta = ImageMetadata("test.fits", "fits", 400, 300, "float32", bit_depth=16)
    frame = ImageFrame(image, meta, raw_intensity=image)
    domain = infer_intensity_domain(frame, 65535, 0.8)
    settings = AnalysisSettings(current_exposure_sec=30, target_mode="point", max_stars=100)
    stars = measure_stars(image, domain, settings)
    report = analyze_saturation(image, domain, stars, "balanced")
    assert report.saturated_pixel_count > 0
    assert report.reference_peak_total_adu is not None
    assert report.reference_peak_total_adu < domain.saturation_threshold_adu


def test_extended_snr_keeps_physical_noise_model_separate_from_spatial_variation() -> None:
    from lightt.photometry import measure_extended_source

    rng = np.random.default_rng(32)
    image = rng.normal(1000, 6, (300, 420)).astype(np.float32)
    # Add a smooth background gradient and a real diffuse target. The gradient is a
    # spatial systematics diagnostic, not temporal detector shot noise.
    image += np.linspace(0, 80, image.shape[1], dtype=np.float32)[None, :]
    image[80:220, 150:300] += 120
    settings = AnalysisSettings(
        current_exposure_sec=60,
        target_mode="extended",
        target_roi_json='{"x":0.36,"y":0.27,"w":0.35,"h":0.46}',
        background_roi_json='{"x":0.02,"y":0.27,"w":0.25,"h":0.46}',
        auto_roi=False,
        smoothing_pixels=100,
    )
    result = measure_extended_source(image, settings, current_exposure_sec=60)
    assert result.current_snr == result.model_snr
    assert result.spatial_contrast_snr is not None
    assert result.spatial_contrast_snr > 0


def test_sigma_clipped_stats_reports_retained_mad():
    from lightt.photometry import sigma_clipped_stats

    values = np.array([10, 10, 11, 9, 10, 10, 5000], dtype=float)
    stats = sigma_clipped_stats(values)
    assert stats.count < len(values)
    assert stats.median == 10
    assert stats.mad <= 1
