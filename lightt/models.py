from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

TargetMode = Literal["extended", "point"]
CoordinateMode = Literal["altaz", "radec"]
ExposureMode = Literal["header", "manual", "auto"]
SaturationPolicy = Literal["preserve_stars", "balanced", "target_priority"]
ValidityLevel = Literal["quantitative_candidate", "planning_only", "diagnostic_only", "invalid"]
ReliabilityLevel = Literal["good", "caution", "low", "blocked", "missing"]


@dataclass(slots=True)
class ImageMetadata:
    filename: str
    source_type: str
    width: int
    height: int
    dtype: str
    bit_depth: int | None = None
    exposure_sec: float | None = None
    effective_exposure_sec: float | None = None
    stack_count: int | None = None
    stack_method: str | None = None
    date_obs: str | None = None
    camera: str | None = None
    filter_name: str | None = None
    gain_setting: float | None = None
    offset_setting: float | None = None
    sensor_temperature_c: float | None = None
    binning_x: int | None = None
    binning_y: int | None = None
    data_min: float | None = None
    data_max: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageFrame:
    intensity: np.ndarray
    metadata: ImageMetadata
    preview_rgb: np.ndarray | None = None
    green: np.ndarray | None = None
    # Linear, pre-flat detector-domain values used for saturation/background limits.
    raw_intensity: np.ndarray | None = None
    # A detector-domain plane that preserves saturation if any green photosite clips.
    saturation_intensity: np.ndarray | None = None
    coordinate_scale_x: float = 1.0
    coordinate_scale_y: float = 1.0
    photometric_area_multiplier: float = 1.0


@dataclass(slots=True)
class CalibrationSet:
    bias_paths: list[Path] = field(default_factory=list)
    dark_paths: list[Path] = field(default_factory=list)
    flat_paths: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class FisheyeConfig:
    mode: str = "auto_equidistant"
    center_x: float | None = None
    center_y: float | None = None
    horizon_radius: float | None = None
    sensor_width: int | None = None
    sensor_height: int | None = None
    north_offset_deg: float = 0.0
    E: float | None = None
    a0: float | None = None
    eps: float | None = None
    coefficients: list[float] = field(default_factory=list)
    fit_star_count: int = 0
    fit_rms_deg: float | None = None
    fit_edge_rms_deg: float | None = None
    # Directional validation recorded in the research report.  Pixel residuals are
    # sufficient for direction lookup but are intentionally NOT treated as an
    # angular-RMS validation for absolute solid-angle photometry.
    validation_star_count: int = 0
    validation_max_error_px: float | None = None
    validation_basis: str | None = None
    calibration_date: str | None = None
    camera_lens_id: str | None = None


@dataclass(slots=True)
class StandardPhotometryConfig:
    enabled: bool = False
    apply_background_scenario: bool = False
    allsky_zero_point: float | None = None
    allsky_extinction_k: float | None = None
    # Kept for backward compatibility. It is not applied to diffuse sky without a
    # validated sky spectral colour model.
    allsky_color_term: float = 0.0
    allsky_fit_star_count: int = 0
    allsky_fit_rms_mag: float | None = None
    allsky_fit_data_hash: str | None = None
    telescope_zero_point: float | None = None
    telescope_extinction_k: float | None = None
    telescope_fit_star_count: int = 0
    telescope_fit_rms_mag: float | None = None
    telescope_fit_data_hash: str | None = None
    telescope_pixel_scale_arcsec: float | None = None


@dataclass(slots=True)
class AnalysisSettings:
    current_exposure_sec: float
    exposure_mode: ExposureMode = "auto"
    target_snr: float = 100.0
    target_mode: TargetMode = "extended"
    gain_e_per_adu: float = 1.0
    read_noise_e: float = 3.0
    noise_parameters_confirmed: bool = False
    dark_current_e_per_pix_sec: float = 0.0
    bias_offset_adu: float = 0.0
    sensor_clip_adu: float | None = None
    saturation_safety_fraction: float = 0.80
    background_limit_fraction: float = 0.30
    max_sub_exposure_sec: float = 600.0
    min_sub_exposure_sec: float = 1.0
    tracking_limit_sec: float = 0.0
    frame_overhead_sec: float = 2.0
    stack_efficiency: float = 0.90
    max_recommended_frames: int = 2000
    saturation_policy: SaturationPolicy = "balanced"
    allow_unverified_saturation: bool = False
    aperture_radius_factor: float = 1.5
    annulus_inner_factor: float = 2.5
    annulus_outer_factor: float = 4.0
    max_stars: int = 400
    # Retained API name, but the UI exposes a beginner-friendly measurement scale.
    smoothing_pixels: int = 100
    target_roi_json: str | None = None
    background_roi_json: str | None = None
    auto_roi: bool = True
    auto_roi_confirmed: bool = False
    target_coordinate_mode: CoordinateMode = "altaz"
    target_alt_deg: float = 45.0
    target_az_deg: float = 180.0
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None
    target_name: str = ""
    allsky_exposure_sec: float | None = None
    latitude: float = 37.70
    longitude: float = 128.26
    height_m: float = 50.0
    observation_time: str = ""
    timezone: str = "KST"
    az_bins: int = 72
    alt_bins: int = 18
    minimum_sky_altitude_deg: float = 15.0
    beginner_mode: bool = True


@dataclass(slots=True)
class IntensityDomain:
    source_kind: str
    dtype: str
    observed_min: float
    observed_median: float
    observed_p999: float
    observed_max: float
    sensor_clip_adu: float
    saturation_threshold_adu: float
    is_rendered: bool
    quantitative_saturation_supported: bool
    clip_source: str
    clip_confidence: str
    requires_user_confirmation: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StarMeasurement:
    x: float
    y: float
    flux_adu: float
    peak_adu: float
    peak_above_background_adu: float
    background_adu: float
    background_std_adu: float
    background_pixels: int
    fwhm_px: float
    eccentricity: float
    aperture_pixels: int
    saturated: bool
    hot_pixel_like: bool
    snr: float


@dataclass(slots=True)
class SaturationReport:
    threshold_adu: float
    saturated_pixel_count: int
    saturated_pixel_fraction: float
    connected_components: int
    star_like_components: int
    isolated_components: int
    streak_components: int
    largest_component_pixels: int
    usable_unsaturated_star_count: int
    reference_peak_quantile: float | None
    reference_peak_total_adu: float | None
    exact_limit_available: bool
    reason: str


@dataclass(slots=True)
class SignalMeasurement:
    target_mode: str
    current_snr: float
    model_snr: float
    signal_adu_per_pixel: float
    background_adu_per_pixel: float
    sensor_background_adu_per_pixel: float | None
    background_std_adu: float
    target_std_adu: float
    background_estimator_std_adu: float
    effective_pixels: int
    target_pixels: int
    background_pixels: int
    spatial_contrast_snr: float | None = None
    point_flux_adu: float | None = None
    fwhm_px: float | None = None
    star_count: int = 0
    target_roi: dict[str, int] | None = None
    background_roi: dict[str, int] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExposurePlan:
    recommended_sub_exposure_sec: float | None
    predicted_snr_per_sub: float | None
    frames: int | None
    total_integration_sec: float | None
    total_elapsed_sec: float | None
    exposure_for_single_frame_target_snr_sec: float | None
    sky_limited_lower_sec: float | None
    background_upper_sec: float | None
    saturation_upper_sec: float | None
    practical_upper_sec: float | None
    limiting_constraint: str
    status: str
    confidence: str = "low"
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SkyCell:
    az_center_deg: float
    alt_center_deg: float
    background_adu: float | None
    background_std_adu: float | None
    background_mad_adu: float | None
    standard_error_adu: float | None
    coefficient_of_variation: float | None
    n_pixels: int
    masked_fraction: float
    reliability: ReliabilityLevel
    blocked_reason: str | None = None
    solid_angle_arcsec2: float | None = None


@dataclass(slots=True)
class SkyMapSummary:
    cells: list[SkyCell]
    sky_median_adu: float
    target_background_adu: float | None
    target_relative_factor: float | None
    target_uncertainty_adu: float | None
    target_solid_angle_arcsec2: float | None
    usable_fraction: float
    good_fraction: float
    blocked_fraction: float
    map_label: str
    star_detection_method: str = "unknown"
    detected_star_count: int = 0
    star_mask_fraction: float = 0.0
    preview_path: str | None = None
    coordinate_overlay_path: str | None = None
    map_path: str | None = None
    relative_map_path: str | None = None
    polar_map_path: str | None = None
    reliability_path: str | None = None
    horizon_profile_path: str | None = None
    distribution_path: str | None = None
    table_path: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisResult:
    job_id: str
    validity: ValidityLevel
    validity_reasons: list[str]
    beginner_summary: dict[str, Any]
    scope_metadata: ImageMetadata
    allsky_metadata: ImageMetadata
    intensity_domain: IntensityDomain
    measurement: SignalMeasurement
    saturation: SaturationReport
    plan: ExposurePlan
    sky: SkyMapSummary
    artifacts: dict[str, str]
    diagnostics: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
