from __future__ import annotations

import gc
import json
import math
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .geometry import select_fisheye_config, validate_fisheye_directional_calibration
from .io import apply_calibration, infer_intensity_domain, load_image, resolve_exposure
from .models import AnalysisSettings, CalibrationSet
from .photometry import analyze_saturation, measure_extended_source, measure_point_source, measure_stars
from .sky import build_sky_map, prepare_sky_analysis_frame
from .time_utils import image_observation_time_utc, observation_time_difference_minutes, parse_observation_datetime


PROFILE_SCHEMA_VERSION = 5


@dataclass(slots=True)
class EquipmentProfile:
    profile_id: str
    name: str
    created_at: str
    telescope_name: str = ""
    camera_name: str = ""
    filter_name: str = ""
    capture_gain_setting: str = ""
    binning: str = ""
    gain_e_per_adu: float = 1.0
    read_noise_e: float = 3.0
    dark_current_e_per_pix_sec: float = 0.0
    noise_parameters_confirmed: bool = False
    bias_offset_adu: float = 0.0
    sensor_clip_adu: float | None = None
    pixel_scale_arcsec: float | None = None
    extinction_k_mag_per_airmass: float = 0.20
    reference_exposure_sec: float = 0.0
    reference_target_name: str = ""
    reference_target_type: str = "unknown"
    reference_target_mode: str = "extended"
    reference_target_mag: float | None = None
    reference_target_size_deg: float | None = None
    reference_target_alt_deg: float | None = None
    reference_target_az_deg: float | None = None
    reference_airmass: float | None = None
    reference_net_flux_adu: float | None = None
    reference_net_flux_e_per_sec: float | None = None
    reference_background_adu_per_pix_sec: float | None = None
    reference_peak_e_per_sec: float | None = None
    reference_psf_peak_fraction: float | None = None
    reference_fwhm_px: float | None = None
    reference_aperture_pixels: int | None = None
    photometric_zero_point_mag: float | None = None
    zero_point_quality: str = "unavailable"
    c_sys: float | None = None
    c_sys_quality: str = "unavailable"
    reference_allsky_background_adu_per_sec: float | None = None
    reference_scope_background_adu_per_pix_sec: float | None = None
    reference_allsky_exposure_sec: float | None = None
    reference_allsky_camera: str | None = None
    reference_allsky_gain_setting: float | None = None
    reference_allsky_width: int | None = None
    reference_allsky_height: int | None = None
    reference_allsky_flat_applied: bool = False
    reference_scope_flat_applied: bool = False
    source_scope_filename: str = ""
    source_allsky_filename: str | None = None
    schema_version: int = PROFILE_SCHEMA_VERSION
    confidence: str = "low"
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EquipmentProfile":
        allowed = {field_.name for field_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        values = {key: value for key, value in raw.items() if key in allowed}
        return cls(**values)


def _finite_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _effective_detector_offset(frame: Any, calibration_report: dict[str, Any], fallback: float | None = None) -> tuple[float, bool]:
    """Return offset in the current intensity domain and whether it is grounded.

    RAW input is black-level corrected by the loader.  Bias-calibrated frames also
    have their offset removed.  Otherwise a FITS header offset or explicit fallback
    may be used.  A guessed zero is returned only as an unverified fallback.
    """
    if frame.metadata.source_type == "raw" or bool(calibration_report.get("offset_removed")):
        return 0.0, True
    if frame.metadata.offset_setting is not None and math.isfinite(float(frame.metadata.offset_setting)):
        return float(frame.metadata.offset_setting), True
    if fallback is not None and math.isfinite(float(fallback)):
        return float(fallback), True
    return 0.0, False


def airmass_from_altitude(alt_deg: float | None) -> float | None:
    if alt_deg is None or not math.isfinite(alt_deg) or alt_deg <= 0:
        return None
    # Kasten & Young style approximation.  This is much better behaved near the horizon
    # than sec(z) while staying simple enough for an observation-planning model.
    if alt_deg >= 90:
        return 1.0
    return 1.0 / (
        math.sin(math.radians(alt_deg))
        + 0.50572 * (alt_deg + 6.07995) ** -1.6364
    )


def _central_roi_json(fraction: float = 0.30) -> str:
    size = min(max(float(fraction), 0.05), 0.80)
    start = (1.0 - size) / 2.0
    return json.dumps({"x": start, "y": start, "w": size, "h": size})


def _profile_dir(root: Path, profile_id: str) -> Path:
    return root / profile_id


def list_profiles(root: Path) -> list[EquipmentProfile]:
    root.mkdir(parents=True, exist_ok=True)
    profiles: list[EquipmentProfile] = []
    for path in sorted(root.glob("*/profile.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profiles.append(EquipmentProfile.from_dict(raw))
        except Exception:
            continue
    profiles.sort(key=lambda item: item.created_at, reverse=True)
    return profiles


def load_profile(root: Path, profile_id: str) -> EquipmentProfile:
    if not profile_id or any(ch not in "0123456789abcdef" for ch in profile_id.lower()) or len(profile_id) > 64:
        raise ValueError("장비 프로필 ID가 올바르지 않습니다.")
    path = _profile_dir(root, profile_id) / "profile.json"
    if not path.exists():
        raise ValueError("장비 프로필을 찾을 수 없습니다.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return EquipmentProfile.from_dict(raw)


def save_profile(root: Path, profile: EquipmentProfile) -> Path:
    directory = _profile_dir(root, profile.profile_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profile.json"
    tmp = directory / "profile.json.tmp"
    tmp.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)
    return path


def delete_profile(root: Path, profile_id: str) -> None:
    profile = load_profile(root, profile_id)
    del profile
    shutil.rmtree(_profile_dir(root, profile_id), ignore_errors=False)


def _target_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(payload.get("name") or "기준 천체"),
        "object_type": str(payload.get("object_type") or payload.get("type") or "unknown"),
        "target_mode": str(payload.get("target_mode") or "extended"),
        "vmag": _finite_or_none(payload.get("vmag")),
        "size_deg": _finite_or_none(payload.get("size_deg")),
        "alt_deg": _finite_or_none(payload.get("alt_deg")),
        "az_deg": _finite_or_none(payload.get("az_deg")),
        "ra_deg": _finite_or_none(payload.get("ra_deg")),
        "dec_deg": _finite_or_none(payload.get("dec_deg")),
        "observation_time_utc": str(payload.get("observation_time_utc") or "") or None,
        "observation_time_local": str(payload.get("observation_time_local") or "") or None,
        "latitude": _finite_or_none(payload.get("latitude")),
        "longitude": _finite_or_none(payload.get("longitude")),
    }



def _prepare_reference_target_for_capture(
    target: dict[str, Any], scope_frame: Any, warnings: list[str]
) -> tuple[dict[str, Any], float | None, str, str | None]:
    """Resolve the reference direction at the actual scope-frame capture epoch.

    An old equipment image can be registered while Stellarium is left at the current
    date.  When RA/Dec, capture time and observing location are known, recompute
    Alt/Az at the image epoch instead of rejecting the profile.  If that is not
    possible, stale Alt/Az values are discarded and only time-independent equipment
    properties are saved.
    """
    prepared = dict(target)
    extra = scope_frame.metadata.extra if isinstance(scope_frame.metadata.extra, dict) else {}
    capture_time_utc = image_observation_time_utc(
        scope_frame.metadata.date_obs, scope_frame.metadata.source_type
    )

    wcs_ra = _finite_or_none(extra.get("wcs_center_ra_deg"))
    wcs_dec = _finite_or_none(extra.get("wcs_center_dec_deg"))
    if prepared.get("ra_deg") is None and wcs_ra is not None:
        prepared["ra_deg"] = wcs_ra
    if prepared.get("dec_deg") is None and wcs_dec is not None:
        prepared["dec_deg"] = wcs_dec
    if (not prepared.get("name") or prepared.get("name") == "기준 천체") and extra.get("object_name"):
        prepared["name"] = str(extra.get("object_name"))[:200]

    site_lat = _finite_or_none(extra.get("site_latitude_deg"))
    site_lon = _finite_or_none(extra.get("site_longitude_deg"))
    site_height = _finite_or_none(extra.get("site_height_m"))
    if site_lat is not None:
        prepared["latitude"] = site_lat
    if site_lon is not None:
        prepared["longitude"] = site_lon

    epoch_delta = observation_time_difference_minutes(
        prepared.get("observation_time_utc"),
        capture_time_utc or scope_frame.metadata.date_obs,
        first_assume_utc_if_naive=True,
        second_assume_utc_if_naive=False if capture_time_utc else (scope_frame.metadata.source_type == "fits"),
    )
    position_source = "stellarium_current_epoch"

    ra = _finite_or_none(prepared.get("ra_deg"))
    dec = _finite_or_none(prepared.get("dec_deg"))
    lat = _finite_or_none(prepared.get("latitude"))
    lon = _finite_or_none(prepared.get("longitude"))
    if capture_time_utc and None not in (ra, dec, lat, lon):
        try:
            import astropy.units as u
            from astropy.coordinates import AltAz, EarthLocation, SkyCoord
            from astropy.time import Time

            location = EarthLocation(
                lat=float(lat) * u.deg,
                lon=float(lon) * u.deg,
                height=float(site_height or 0.0) * u.m,
            )
            skycoord = SkyCoord(ra=float(ra) * u.deg, dec=float(dec) * u.deg, frame="icrs")
            capture_datetime = parse_observation_datetime(capture_time_utc, assume_utc_if_naive=True)
            if capture_datetime is None:
                raise ValueError("기준 영상 촬영시각을 UTC datetime으로 해석하지 못했습니다.")
            transformed = skycoord.transform_to(AltAz(obstime=Time(capture_datetime), location=location))
            alt = float(transformed.alt.deg)
            az = float(transformed.az.deg) % 360.0
            if alt >= 0.0:
                prepared["alt_deg"] = alt
                prepared["az_deg"] = az
                prepared["observation_time_utc"] = capture_time_utc
                position_source = "radec_recomputed_at_capture"
                if epoch_delta is not None and epoch_delta > 10.0:
                    warnings.append(
                        f"Stellarium 시각과 기준 영상 촬영시각이 {epoch_delta:.1f}분 달랐지만, "
                        "RA/Dec와 관측 위치로 기준 촬영시각의 고도·방위각을 자동 재계산했습니다."
                    )
            else:
                prepared["alt_deg"] = None
                prepared["az_deg"] = None
                position_source = "radec_recomputed_below_horizon"
                warnings.append(
                    f"기준 촬영시각으로 좌표를 재계산한 결과 대상 고도가 {alt:.2f}°였습니다. "
                    "촬영시각·관측 위치 또는 기준 천체를 확인하세요. 방향 의존 보정은 사용하지 않습니다."
                )
        except Exception as exc:
            warnings.append(
                f"기준 촬영시각의 고도·방위각 자동 재계산을 완료하지 못했습니다({type(exc).__name__}). "
                "프로필은 생성하지만 시간 의존 보정은 제한합니다."
            )

    if position_source == "stellarium_current_epoch":
        if epoch_delta is not None and epoch_delta > 30.0:
            prepared["alt_deg"] = None
            prepared["az_deg"] = None
            position_source = "stale_altaz_discarded"
            warnings.append(
                f"Stellarium 시각과 기준 영상 촬영시각이 {epoch_delta:.1f}분 차이이며 당시 Alt/Az를 "
                "재계산할 정보가 부족합니다. 장비 프로필은 저장하되 Csys·기준 대기질량 등 "
                "시간 의존 보정은 비활성화합니다."
            )
        elif epoch_delta is None:
            if capture_time_utc is None:
                warnings.append(
                    "기준 영상 촬영시각의 시간대를 확정하지 못했습니다. 장비 프로필은 저장하지만 "
                    "시간 의존 보정은 계획용으로 제한합니다."
                )
            else:
                warnings.append(
                    "기준 천체의 Stellarium 시각을 비교하지 못했습니다. 장비 프로필은 생성하며 "
                    "시간 의존 보정은 확인 가능한 정보만 사용합니다."
                )
        elif epoch_delta > 10.0:
            warnings.append(
                f"기준 Stellarium 시각과 망원경 기준 영상 촬영시각이 {epoch_delta:.1f}분 차이납니다."
            )

    return prepared, epoch_delta, position_source, capture_time_utc

def create_equipment_profile(
    *,
    profile_root: Path,
    profile_name: str,
    scope_path: Path,
    reference_target: dict[str, Any],
    project_root: Path,
    telescope_name: str = "",
    camera_name: str = "",
    filter_name: str = "",
    capture_gain_setting: str = "",
    binning: str = "",
    gain_e_per_adu: float = 1.0,
    read_noise_e: float = 3.0,
    dark_current_e_per_pix_sec: float = 0.0,
    noise_parameters_confirmed: bool = False,
    bias_offset_adu: float | None = None,
    sensor_clip_adu: float | None = None,
    pixel_scale_arcsec: float | None = None,
    extinction_k_mag_per_airmass: float = 0.20,
    scope_exposure_sec: float | None = None,
    scope_calibration: CalibrationSet | None = None,
    reference_allsky_path: Path | None = None,
    reference_allsky_exposure_sec: float | None = None,
    allsky_calibration: CalibrationSet | None = None,
    result_root: Path | None = None,
) -> EquipmentProfile:
    target = _target_from_payload(reference_target)
    target_mode = target["target_mode"] if target["target_mode"] in {"point", "extended"} else "extended"
    warnings: list[str] = []
    profile_id = uuid.uuid4().hex[:16]
    directory = _profile_dir(profile_root, profile_id)
    directory.mkdir(parents=True, exist_ok=False)

    try:
        scope_original = load_image(scope_path)
        target, reference_epoch_delta_min, reference_position_source, reference_capture_time_utc = (
            _prepare_reference_target_for_capture(target, scope_original, warnings)
        )
        manual_exposure = scope_exposure_sec
        if scope_original.metadata.exposure_sec is None and manual_exposure is None:
            raise ValueError("망원경 기준 영상의 노출시간을 읽지 못했습니다. 기준 노출시간을 직접 입력하세요.")
        resolved_exposure, exposure_source = resolve_exposure(
            scope_original,
            float(manual_exposure or scope_original.metadata.exposure_sec or 0.0),
            "auto",
        )
        scope_calibration = scope_calibration or CalibrationSet()
        scope_frame, scope_cal_report = apply_calibration(
            scope_original,
            scope_calibration,
            light_exposure_sec=resolved_exposure,
        )
        if isinstance(scope_cal_report.get("warnings"), list):
            warnings.extend(str(item) for item in scope_cal_report["warnings"])

        effective_scope_bias, scope_offset_known = _effective_detector_offset(
            scope_original, scope_cal_report, bias_offset_adu
        )
        if not scope_offset_known:
            warnings.append("망원경 기준 영상의 Bias/offset을 확인하지 못했습니다. 배경률 보정 신뢰도를 낮춥니다.")

        settings = AnalysisSettings(
            current_exposure_sec=resolved_exposure,
            exposure_mode="auto",
            target_snr=100.0,
            target_mode=target_mode,  # type: ignore[arg-type]
            gain_e_per_adu=gain_e_per_adu,
            read_noise_e=read_noise_e,
            noise_parameters_confirmed=bool(noise_parameters_confirmed),
            dark_current_e_per_pix_sec=dark_current_e_per_pix_sec,
            bias_offset_adu=effective_scope_bias,
            sensor_clip_adu=sensor_clip_adu,
            auto_roi=True,
            auto_roi_confirmed=True,
            target_roi_json=_central_roi_json(0.30) if target_mode == "point" else None,
            smoothing_pixels=100,
        )
        domain = infer_intensity_domain(scope_original, sensor_clip_adu, 0.80)
        warnings.extend(domain.warnings)
        stars = measure_stars(scope_frame.intensity, domain, settings, current_exposure_sec=resolved_exposure)
        saturation_source = (
            scope_original.saturation_intensity
            if scope_original.saturation_intensity is not None
            else scope_original.raw_intensity
            if scope_original.raw_intensity is not None
            else scope_original.intensity
        )
        saturation = analyze_saturation(saturation_source, domain, stars, "balanced")
        if target_mode == "point":
            measurement = measure_point_source(scope_frame.intensity, stars, settings)
        else:
            measurement = measure_extended_source(scope_frame.intensity, settings, resolved_exposure)

        effective_bias = settings.bias_offset_adu
        if measurement.point_flux_adu is not None:
            net_flux_adu = float(measurement.point_flux_adu)
        else:
            # The report defines target net signal as target-area flux after subtracting
            # a same-area background estimate.  target_pixels is the retained ROI size.
            net_flux_adu = float(measurement.signal_adu_per_pixel * max(measurement.target_pixels, 1))
        net_flux_e_per_sec = net_flux_adu * gain_e_per_adu / resolved_exposure
        bg_adu_per_pix_sec = max(measurement.background_adu_per_pixel - effective_bias, 0.0) / resolved_exposure

        # Store a *source-only* stellar peak rate for future saturation prediction.
        # ``SaturationReport.reference_peak_total_adu`` contains the detector-domain
        # sky/background as well; adding the current sky to that value would double
        # count background.  Re-measure each usable stellar peak in the original
        # detector-domain image and subtract a local annular background first.
        detector_peak_rates: list[float] = []
        for star in stars:
            if star.saturated or star.hot_pixel_like or not math.isfinite(star.fwhm_px):
                continue
            radius = max(3.0, float(star.fwhm_px) * 2.0)
            outer = max(radius + 2.0, float(star.fwhm_px) * 4.0)
            cx, cy = float(star.x), float(star.y)
            x0 = max(0, int(math.floor(cx - outer - 1)))
            x1 = min(saturation_source.shape[1], int(math.ceil(cx + outer + 2)))
            y0 = max(0, int(math.floor(cy - outer - 1)))
            y1 = min(saturation_source.shape[0], int(math.ceil(cy + outer + 2)))
            patch = np.asarray(saturation_source[y0:y1, x0:x1], dtype=float)
            if patch.size < 25:
                continue
            py, px = np.indices(patch.shape, dtype=float)
            rr = np.hypot(px + x0 - cx, py + y0 - cy)
            core_values = patch[np.isfinite(patch) & (rr <= radius)]
            annulus_values = patch[np.isfinite(patch) & (rr >= radius * 1.35) & (rr <= outer)]
            if core_values.size < 3 or annulus_values.size < 15:
                continue
            raw_peak = float(np.percentile(core_values, 99.5))
            local_bg = float(np.median(annulus_values))
            source_peak_adu = raw_peak - local_bg
            if math.isfinite(source_peak_adu) and source_peak_adu > 0:
                detector_peak_rates.append(source_peak_adu * gain_e_per_adu / resolved_exposure)
        peak_e_per_sec: float | None = None
        if len(detector_peak_rates) >= 5:
            peak_e_per_sec = float(np.percentile(np.asarray(detector_peak_rates, dtype=float), 95))
        elif detector_peak_rates:
            warnings.append(
                f"대표 별 포화율에 사용할 비포화 별이 {len(detector_peak_rates)}개뿐이라 "
                "장비 프로필의 일반 별 포화 상한을 확정하지 않았습니다."
            )

        usable_star_apertures = [
            int(star.aperture_pixels)
            for star in stars
            if not star.saturated and not star.hot_pixel_like and star.aperture_pixels > 0
        ]
        usable_star_fwhm = [
            float(star.fwhm_px)
            for star in stars
            if not star.saturated and not star.hot_pixel_like and math.isfinite(star.fwhm_px) and star.fwhm_px > 0
        ]
        representative_star_aperture = (
            int(round(float(np.median(np.asarray(usable_star_apertures, dtype=float)))))
            if usable_star_apertures
            else int(measurement.effective_pixels)
        )
        representative_star_fwhm = (
            float(np.median(np.asarray(usable_star_fwhm, dtype=float)))
            if usable_star_fwhm
            else measurement.fwhm_px
        )

        peak_fractions = [
            star.peak_above_background_adu / star.flux_adu
            for star in stars
            if (not star.saturated and not star.hot_pixel_like and star.flux_adu > 0
                and star.peak_above_background_adu > 0)
        ]
        psf_peak_fraction = (
            float(np.percentile(np.asarray(peak_fractions, dtype=float), 85))
            if peak_fractions else None
        )
        if psf_peak_fraction is not None:
            psf_peak_fraction = min(max(psf_peak_fraction, 1e-6), 1.0)

        reference_airmass = airmass_from_altitude(target["alt_deg"])

        zero_point: float | None = None
        zero_point_quality = "unavailable"
        magnitude = target["vmag"]
        if magnitude is not None and net_flux_e_per_sec > 0:
            zero_point = float(magnitude + 2.5 * math.log10(net_flux_e_per_sec))
            if scope_original.metadata.source_type == "rendered":
                zero_point_quality = "diagnostic"
                warnings.append("JPG/PNG/TIFF 기준 영상은 비선형 처리 가능성이 있어 기기영점을 정량값으로 사용하지 않습니다.")
            else:
                zero_point_quality = "good" if target_mode == "point" else "approximate"
            if reference_airmass is None and zero_point_quality == "good":
                zero_point_quality = "planning"
                warnings.append(
                    "기준 촬영시각의 대기질량을 확정하지 못해 기기영점은 계획용으로 저장합니다. "
                    "장비 응답 자체는 저장되지만 다른 고도에 대한 소광 보정 정확도는 낮아집니다."
                )
            if target_mode != "point":
                warnings.append(
                    "기준 천체가 확산천체라 자동 ROI의 통합 신호로 기기영점을 추정했습니다. "
                    "점광원 기준별로 보정한 프로필보다 대상 신호 예측 오차가 클 수 있습니다."
                )
        else:
            warnings.append(
                "기준 천체의 카탈로그 V등급이 없어 기기영점을 계산하지 못했습니다. "
                "단일노출 상한은 계산할 수 있지만 목표 SNR 기반 촬영 장수는 제한될 수 있습니다."
            )

        c_sys: float | None = None
        c_sys_quality = "unavailable"
        allsky_rate: float | None = None
        allsky_filename: str | None = None
        c_sys_diagnostics: dict[str, Any] = {}
        ref_allsky_exposure_value: float | None = None
        ref_allsky_camera: str | None = None
        ref_allsky_gain: float | None = None
        ref_allsky_width: int | None = None
        ref_allsky_height: int | None = None
        ref_allsky_flat_applied = False
        if reference_allsky_path is not None:
            allsky_filename = reference_allsky_path.name
            allsky_original = load_image(reference_allsky_path)
            paired_epoch_delta_min = observation_time_difference_minutes(
                scope_original.metadata.date_obs,
                allsky_original.metadata.date_obs,
                first_assume_utc_if_naive=(scope_original.metadata.source_type == "fits"),
                second_assume_utc_if_naive=(allsky_original.metadata.source_type == "fits"),
            )
            paired_time_compatible = paired_epoch_delta_min is None or paired_epoch_delta_min <= 30.0
            if paired_epoch_delta_min is not None and paired_epoch_delta_min > 30.0:
                warnings.append(
                    f"기준 망원경 영상과 기준 전천 영상의 촬영시각이 {paired_epoch_delta_min:.1f}분 차이므로 Csys를 계산하지 않았습니다."
                )
            elif paired_epoch_delta_min is None:
                warnings.append(
                    "기준 망원경/전천 영상의 촬영시각 또는 시간대를 비교하지 못했습니다. Csys가 계산되더라도 계획용으로 취급합니다."
                )
            ref_allsky_camera = allsky_original.metadata.camera
            ref_allsky_gain = allsky_original.metadata.gain_setting
            ref_allsky_width = allsky_original.metadata.width
            ref_allsky_height = allsky_original.metadata.height
            allsky_exposure = reference_allsky_exposure_sec or allsky_original.metadata.exposure_sec
            ref_allsky_exposure_value = allsky_exposure
            if allsky_exposure is None or allsky_exposure <= 0:
                warnings.append("기준 전천 영상의 노출시간을 알 수 없어 Csys를 계산하지 못했습니다.")
            elif target["alt_deg"] is None or target["az_deg"] is None or target["alt_deg"] < 0:
                warnings.append("기준 천체의 당시 고도·방위각이 없어 Csys를 계산하지 못했습니다.")
            elif not paired_time_compatible:
                pass
            else:
                allsky_calibration = allsky_calibration or CalibrationSet()
                allsky_frame, allsky_cal_report = apply_calibration(
                    allsky_original,
                    allsky_calibration,
                    light_exposure_sec=allsky_exposure,
                )
                ref_allsky_flat_applied = bool(allsky_cal_report.get("flat_frames"))
                allsky_offset, allsky_offset_known = _effective_detector_offset(allsky_original, allsky_cal_report)
                if not allsky_offset_known:
                    warnings.append(
                        "기준 전천 영상의 Bias/black offset을 확인하지 못했습니다. Csys는 계획용으로만 취급합니다."
                    )

                allsky_settings = AnalysisSettings(
                    current_exposure_sec=resolved_exposure,
                    target_mode=target_mode,  # type: ignore[arg-type]
                    target_alt_deg=float(target["alt_deg"]),
                    target_az_deg=float(target["az_deg"]),
                    allsky_exposure_sec=float(allsky_exposure),
                    minimum_sky_altitude_deg=15.0,
                )
                fisheye = select_fisheye_config(
                    project_root,
                    camera_name=allsky_original.metadata.camera,
                    filename=allsky_original.metadata.filename,
                    width=allsky_original.metadata.width,
                    height=allsky_original.metadata.height,
                )
                fisheye_errors = validate_fisheye_directional_calibration(fisheye)
                map_dir = directory / "reference_allsky_analysis"
                map_dir.mkdir(exist_ok=True)

                # The 4k APICAM frame is ~64 MiB as float32.  Geometry needs several
                # temporary coordinate arrays, so keeping the full detector frame alive
                # here can push small Render workers over their memory limit.  Build the
                # exact same low-resolution working frame up front, preserve detector
                # coordinate scaling, then release the 4k arrays before map generation.
                allsky_source_type = allsky_original.metadata.source_type
                compact_allsky_frame = prepare_sky_analysis_frame(allsky_frame)
                del allsky_frame
                del allsky_original
                gc.collect()
                sky = build_sky_map(
                    compact_allsky_frame,
                    allsky_settings,
                    fisheye,
                    map_dir,
                    flat_applied=bool(allsky_cal_report.get("flat_frames")),
                )
                if sky.target_background_adu is not None:
                    allsky_rate = max(sky.target_background_adu - allsky_offset, 0.0) / allsky_exposure
                    if allsky_rate > 0 and bg_adu_per_pix_sec > 0:
                        c_sys = float(bg_adu_per_pix_sec / allsky_rate)
                        quantitative_sources = (
                            scope_original.metadata.source_type != "rendered"
                            and allsky_source_type != "rendered"
                            and scope_offset_known
                            and allsky_offset_known
                        )
                        c_sys_quality = "good" if (
                            not fisheye_errors and quantitative_sources and paired_epoch_delta_min is not None
                        ) else "planning"
                        if fisheye_errors:
                            warnings.append("어안 방향 보정의 보고서 검증 조건을 충족하지 못해 Csys 좌표 신뢰도를 낮췄습니다.")
                        if not quantitative_sources:
                            warnings.append("기준 영상의 선형 ADU 또는 offset 근거가 불충분해 Csys를 계획용으로 표시합니다.")
                    else:
                        warnings.append("기준 전천/망원경 배경률이 0 이하라 Csys를 계산하지 못했습니다.")
                c_sys_diagnostics = {
                    "fisheye_validation_errors": fisheye_errors,
                    "sky_target_background_adu": sky.target_background_adu,
                    "sky_target_relative_factor": sky.target_relative_factor,
                    "allsky_exposure_sec": allsky_exposure,
                    "allsky_offset_adu": allsky_offset if 'allsky_offset' in locals() else None,
                    "allsky_offset_known": allsky_offset_known if 'allsky_offset_known' in locals() else False,
                    "paired_frame_time_difference_min": paired_epoch_delta_min,
                }

        confidence = "high"
        if zero_point is None or c_sys is None:
            confidence = "medium"
        if not noise_parameters_confirmed:
            confidence = "low"
            warnings.append("변환 Gain·읽기잡음 등 센서 물성값이 장비 사양에서 확인되지 않아 결과 신뢰도를 낮춥니다.")
        if zero_point is None and c_sys is None:
            confidence = "low"
        if not domain.quantitative_saturation_supported:
            confidence = "low"
        if target_mode == "extended" and confidence == "high":
            confidence = "medium"

        profile = EquipmentProfile(
            profile_id=profile_id,
            name=(profile_name.strip() or f"장비 {profile_id[:6]}")[:100],
            created_at=datetime.now(timezone.utc).isoformat(),
            telescope_name=telescope_name[:120],
            camera_name=(camera_name or scope_original.metadata.camera or "")[:120],
            filter_name=(filter_name or scope_original.metadata.filter_name or "")[:80],
            capture_gain_setting=capture_gain_setting[:80],
            binning=binning[:40],
            gain_e_per_adu=float(gain_e_per_adu),
            read_noise_e=float(read_noise_e),
            dark_current_e_per_pix_sec=float(dark_current_e_per_pix_sec),
            noise_parameters_confirmed=bool(noise_parameters_confirmed),
            bias_offset_adu=float(effective_scope_bias),
            sensor_clip_adu=float(domain.sensor_clip_adu) if domain.quantitative_saturation_supported else sensor_clip_adu,
            pixel_scale_arcsec=pixel_scale_arcsec,
            extinction_k_mag_per_airmass=float(extinction_k_mag_per_airmass),
            reference_exposure_sec=float(resolved_exposure),
            reference_target_name=target["name"],
            reference_target_type=target["object_type"],
            reference_target_mode=target_mode,
            reference_target_mag=magnitude,
            reference_target_size_deg=target["size_deg"],
            reference_target_alt_deg=target["alt_deg"],
            reference_target_az_deg=target["az_deg"],
            reference_airmass=reference_airmass,
            reference_net_flux_adu=float(net_flux_adu),
            reference_net_flux_e_per_sec=float(net_flux_e_per_sec),
            reference_background_adu_per_pix_sec=float(bg_adu_per_pix_sec),
            reference_peak_e_per_sec=peak_e_per_sec,
            reference_psf_peak_fraction=psf_peak_fraction,
            reference_fwhm_px=representative_star_fwhm,
            reference_aperture_pixels=representative_star_aperture,
            photometric_zero_point_mag=zero_point,
            zero_point_quality=zero_point_quality,
            c_sys=c_sys,
            c_sys_quality=c_sys_quality,
            reference_allsky_background_adu_per_sec=allsky_rate,
            reference_scope_background_adu_per_pix_sec=float(bg_adu_per_pix_sec),
            reference_allsky_exposure_sec=ref_allsky_exposure_value,
            reference_allsky_camera=ref_allsky_camera,
            reference_allsky_gain_setting=ref_allsky_gain,
            reference_allsky_width=ref_allsky_width,
            reference_allsky_height=ref_allsky_height,
            reference_allsky_flat_applied=ref_allsky_flat_applied,
            reference_scope_flat_applied=bool(scope_cal_report.get("flat_frames")),
            source_scope_filename=scope_path.name,
            source_allsky_filename=allsky_filename,
            confidence=confidence,
            warnings=list(dict.fromkeys(warnings)),
            diagnostics={
                "scope_metadata": asdict(scope_original.metadata),
                "scope_calibration": scope_cal_report,
                "scope_exposure_source": exposure_source,
                "intensity_domain": asdict(domain),
                "saturation": asdict(saturation),
                "measurement": asdict(measurement),
                "star_count": len(stars),
                "reference_target": target,
                "reference_capture_time_utc": reference_capture_time_utc,
                "reference_epoch_difference_min": reference_epoch_delta_min,
                "reference_position_source": reference_position_source,
                "c_sys": c_sys_diagnostics,
            },
        )

        stored_scope = directory / ("reference_scope" + scope_path.suffix.lower())
        shutil.copy2(scope_path, stored_scope)
        if reference_allsky_path is not None:
            stored_allsky = directory / ("reference_allsky" + reference_allsky_path.suffix.lower())
            shutil.copy2(reference_allsky_path, stored_allsky)
        save_profile(profile_root, profile)
        return profile
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
