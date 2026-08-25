from __future__ import annotations

import math
import os
import re
import tempfile
import warnings
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image, ImageOps

from .models import CalibrationSet, ImageFrame, ImageMetadata, IntensityDomain

FITS_EXTENSIONS = {".fits", ".fit", ".fts"}
RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2",
    ".pef", ".srw", ".raw", ".3fr", ".erf", ".mrw", ".kdc", ".x3f",
}
RENDERED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
SUPPORTED_EXTENSIONS = FITS_EXTENSIONS | RAW_EXTENSIONS | RENDERED_EXTENSIONS

# A disk-backed stack prevents calibration masters from consuming multiple GiB of RAM.
MAX_CALIBRATION_TEMP_BYTES = int(
    os.environ.get("LIGHTT_MAX_CALIBRATION_TEMP_BYTES", str(12 * 1024**3))
)
MASTER_TILE_TARGET_BYTES = int(
    os.environ.get("LIGHTT_MASTER_TILE_BYTES", str(192 * 1024**2))
)
MAX_IMAGE_PIXELS = int(os.environ.get("LIGHTT_MAX_IMAGE_PIXELS", str(120_000_000)))


def _check_pixel_count(width: int, height: int, label: str = "영상") -> None:
    pixels = int(width) * int(height)
    if width <= 0 or height <= 0:
        raise ValueError(f"{label} 크기가 올바르지 않습니다: {width}×{height}")
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"{label}이 {pixels/1_000_000:.1f}MP로 안전 제한 "
            f"{MAX_IMAGE_PIXELS/1_000_000:.1f}MP를 초과합니다."
        )


def _finite_array(data: np.ndarray) -> np.ndarray:
    arr = np.asarray(data)
    if arr.size == 0:
        raise ValueError("영상 데이터가 비어 있습니다.")
    if not np.issubdtype(arr.dtype, np.number):
        raise ValueError(f"지원하지 않는 영상 dtype입니다: {arr.dtype}")
    arr = arr.astype(np.float32, copy=False)
    finite = np.isfinite(arr)
    if not finite.any():
        raise ValueError("영상에 유효한 수치 픽셀이 없습니다.")
    if not finite.all():
        replacement = float(np.nanmedian(arr[finite]))
        arr = np.where(finite, arr, replacement)
    return arr


def _reduce_fits_axes(data: np.ndarray) -> np.ndarray:
    arr = np.squeeze(np.asarray(data))
    if arr.ndim == 2:
        return arr
    if arr.ndim == 3:
        if arr.shape[0] in (3, 4):
            channels = np.moveaxis(arr[:3], 0, -1)
        elif arr.shape[-1] in (3, 4):
            channels = arr[..., :3]
        else:
            raise ValueError(
                "3차원 FITS cube의 축 의미를 판단할 수 없습니다. "
                "2차원 단일 시험 영상 또는 RGB 3채널 FITS를 사용하세요."
            )
        return 0.2126 * channels[..., 0] + 0.7152 * channels[..., 1] + 0.0722 * channels[..., 2]
    raise ValueError(f"지원하지 않는 FITS 차원입니다: {arr.shape}")


def _header_float(header: object, keys: Iterable[str], *, positive: bool = True) -> float | None:
    for key in keys:
        try:
            value = header.get(key)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value is None:
            continue
        try:
            number = float(cast(Any, value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and (number > 0 if positive else True):
            return number
    return None


def _header_int(header: object, keys: Iterable[str]) -> int | None:
    value = _header_float(header, keys)
    return int(round(value)) if value is not None else None


def _header_text(header: object, keys: Iterable[str]) -> str | None:
    for key in keys:
        try:
            value = header.get(key)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value not in (None, ""):
            return str(value).strip()
    return None


def _header_value(header: object, key: str) -> object | None:
    try:
        return header.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None


def _seconds_value(value: object, *, key_scale: float = 1.0) -> float | None:
    """Parse an exposure value while preserving explicit units."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower().replace("µ", "u").replace("μ", "u")
        if not text:
            return None
        fraction = re.match(
            r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*/\s*"
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
            text,
        )
        if fraction:
            denominator = float(fraction.group(2))
            if denominator == 0:
                return None
            number = float(fraction.group(1)) / denominator
        else:
            match = re.search(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text)
            if not match:
                return None
            number = float(match.group(0))
        if re.search(r"(?:^|\s)(?:ms|msec|millisecond(?:s)?)(?:\s|$)", text):
            scale = 1e-3
        elif re.search(r"(?:^|\s)(?:us|usec|microsecond(?:s)?)(?:\s|$)", text):
            scale = 1e-6
        elif re.search(r"(?:^|\s)(?:ns|nsec|nanosecond(?:s)?)(?:\s|$)", text):
            scale = 1e-9
        else:
            scale = key_scale
    else:
        try:
            number = float(cast(Any, value))
        except (TypeError, ValueError, OverflowError):
            return None
        scale = key_scale
    seconds = number * scale
    return float(seconds) if math.isfinite(seconds) and seconds > 0 else None


def _fits_exposure_metadata(
    header: object,
) -> tuple[float | None, float | None, int | None, str | None, dict[str, Any]]:
    """Resolve FITS integration time and retain competing header values.

    ``exposure_sec`` is the integration represented by the image pixels. If a
    detector sequence only provides DIT/NDIT, it is derived as DIT×NDIT. A
    post-processing mean/median stack keeps per-frame EXPTIME and stores the
    summed integration separately in ``effective_exposure_sec``.
    """
    specs = [
        ("EXPTIME", "image", 1.0),
        ("EXPOSURE", "image", 1.0),
        ("EXP_TIME", "image", 1.0),
        ("EXPTIME_MS", "image", 1e-3),
        ("EXPOSURE_MS", "image", 1e-3),
        ("EXPMS", "image", 1e-3),
        ("EXPTIME_US", "image", 1e-6),
        ("EXPOSURE_US", "image", 1e-6),
        ("EXPUS", "image", 1e-6),
        ("DIT", "detector_integration", 1.0),
        ("ITIME", "detector_integration", 1.0),
        ("INTTIME", "detector_integration", 1.0),
        ("INT_TIME", "detector_integration", 1.0),
        ("ESO DET DIT", "detector_integration", 1.0),
        ("HIERARCH ESO DET DIT", "detector_integration", 1.0),
        ("ESO DET SEQ1 DIT", "detector_integration", 1.0),
        ("HIERARCH ESO DET SEQ1 DIT", "detector_integration", 1.0),
        ("ONTIME", "total", 1.0),
        ("LIVETIME", "total", 1.0),
        ("TOTEXP", "total", 1.0),
        ("TOTEXPT", "total", 1.0),
        ("EXPTOTAL", "total", 1.0),
    ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, role, scale in specs:
        canonical = key.removeprefix("HIERARCH ")
        if canonical in seen:
            continue
        raw = _header_value(header, key)
        seconds = _seconds_value(raw, key_scale=scale)
        if seconds is None:
            continue
        seen.add(canonical)
        candidates.append({"key": key, "role": role, "raw": str(raw), "seconds": seconds})

    detector_count = _header_int(
        header,
        [
            "NDIT", "ESO DET NDIT", "HIERARCH ESO DET NDIT",
            "ESO DET SEQ1 NDIT", "HIERARCH ESO DET SEQ1 NDIT",
        ],
    )
    if detector_count is not None and detector_count < 1:
        detector_count = None
    stack_count = _header_int(header, ["STACKCNT", "NCOMBINE"])
    stack_method = _normalise_stack_method(
        _header_text(header, ["COMBINE", "COMBMETH", "STACKMTH", "STACKING"])
    )
    by_role = {
        role: [candidate for candidate in candidates if candidate["role"] == role]
        for role in ("image", "detector_integration", "total")
    }
    selected: dict[str, Any] | None = None
    selection_rule = "missing"
    if by_role["image"]:
        selected = by_role["image"][0]
        selection_rule = "standard_image_exposure"
    elif by_role["total"]:
        selected = by_role["total"][0]
        selection_rule = "explicit_total_exposure"
    elif by_role["detector_integration"]:
        selected = dict(by_role["detector_integration"][0])
        if detector_count and detector_count > 1:
            selected["seconds"] = float(selected["seconds"]) * detector_count
            selected["key"] = f"{selected['key']} × NDIT"
            selected["role"] = "derived_total"
            selection_rule = "detector_integration_times_count"
        else:
            selection_rule = "single_detector_integration"

    exposure = float(selected["seconds"]) if selected is not None else None
    comparisons: list[dict[str, Any]] = []
    conflicts: list[str] = []

    def compare(label: str, value: float) -> None:
        if exposure is None or value <= 0:
            return
        relative_difference = abs(value - exposure) / max(value, exposure, 1e-12)
        comparisons.append({
            "label": label,
            "seconds": float(value),
            "relative_difference": float(relative_difference),
        })
        if relative_difference > 0.02:
            conflicts.append(
                f"{label}={value:g}초가 선택값 {exposure:g}초와 {relative_difference:.1%} 다릅니다."
            )

    if by_role["detector_integration"]:
        detector_total = float(by_role["detector_integration"][0]["seconds"])
        if detector_count and detector_count > 1:
            detector_total *= detector_count
        compare("DIT×NDIT" if detector_count and detector_count > 1 else "DIT", detector_total)
    if by_role["total"] and selection_rule != "explicit_total_exposure":
        compare(str(by_role["total"][0]["key"]), float(by_role["total"][0]["seconds"]))
    for duplicate in by_role["image"][1:]:
        compare(str(duplicate["key"]), float(duplicate["seconds"]))

    effective = None
    if exposure is not None and stack_count and stack_count > 1 and stack_method in {"mean", "median"}:
        effective = exposure * stack_count
    elif exposure is not None and selection_rule == "detector_integration_times_count":
        effective = exposure
    confidence = "none"
    if exposure is not None:
        confidence = "low" if conflicts else (
            "medium" if selection_rule.startswith("detector_") else "high"
        )
    provenance = {
        "selected_seconds": exposure,
        "selected_key": None if selected is None else selected["key"],
        "selected_role": None if selected is None else selected["role"],
        "selection_rule": selection_rule,
        "confidence": confidence,
        "candidates": candidates,
        "comparisons": comparisons,
        "conflicts": conflicts,
        "detector_integration_count": detector_count,
        "post_stack_count": stack_count,
        "post_stack_method": stack_method,
    }
    return exposure, effective, stack_count, stack_method, provenance


def _normalise_stack_method(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower().strip()
    if any(token in text for token in ("mean", "average", "avg")):
        return "mean"
    if "median" in text:
        return "median"
    if any(token in text for token in ("sum", "add")):
        return "sum"
    if any(token in text for token in ("single", "none")):
        return "single"
    return "unknown"


def _wcs_center_from_header(header: object, width: int, height: int) -> tuple[float | None, float | None]:
    """Evaluate the image center from a small, standards-only WCS header.

    Several observatory FITS files (notably older ESO products) contain hundreds
    of non-standard instrument cards. Passing that entire header to ``WCS`` makes
    Astropy repair and warn about every unrelated card, which can turn a preview
    request into a multi-minute operation. Only celestial WCS cards are needed to
    locate the image center, so copy those into a compact header first.
    """
    try:
        from astropy.io import fits
        from astropy.wcs import WCS
    except ImportError:  # pragma: no cover
        return None, None

    ctype1 = _header_text(header, ["CTYPE1"])
    ctype2 = _header_text(header, ["CTYPE2"])
    if not ctype1 or not ctype2:
        return None, None
    compact = fits.Header()
    compact["NAXIS"] = 2
    compact["NAXIS1"] = int(width)
    compact["NAXIS2"] = int(height)
    text_keys = ("CTYPE1", "CTYPE2", "CUNIT1", "CUNIT2", "RADESYS", "RADECSYS")
    numeric_keys = (
        "CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2", "CDELT1", "CDELT2",
        "CROTA1", "CROTA2", "EQUINOX", "LONPOLE", "LATPOLE",
        "CD1_1", "CD1_2", "CD2_1", "CD2_2",
        "PC1_1", "PC1_2", "PC2_1", "PC2_2",
    )
    for key in text_keys:
        text_value = _header_text(header, [key])
        if text_value:
            compact[key] = text_value
    for key in numeric_keys:
        numeric_value = _header_float(header, [key], positive=False)
        if numeric_value is not None:
            compact[key] = numeric_value
    try:
        # RADECSYS is an old alias. Keeping the original value is useful, but its
        # deprecation warning is not actionable for an uploaded science frame.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wcs = WCS(compact, relax=True)
            if not wcs.has_celestial:
                return None, None
            center = wcs.celestial.pixel_to_world((width - 1) / 2.0, (height - 1) / 2.0)
        return float(center.ra.deg) % 360.0, float(center.dec.deg)
    except Exception:
        return None, None


def _load_fits(path: Path) -> ImageFrame:
    try:
        from astropy.io import fits
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("FITS 입력에는 astropy가 필요합니다.") from exc

    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        hdu = None
        for item in hdul:
            header_candidate = getattr(item, "header", None)
            if header_candidate is None:
                continue
            try:
                naxis = int(header_candidate.get("NAXIS", 0) or 0)
                width = int(header_candidate.get("NAXIS1", 0) or 0)
                height = int(header_candidate.get("NAXIS2", 0) or 0)
            except (TypeError, ValueError):
                continue
            if naxis >= 2 and width > 0 and height > 0:
                _check_pixel_count(width, height, "FITS 영상")
                hdu = item
                break
        if hdu is None:
            raise ValueError("FITS 파일에서 영상 HDU를 찾지 못했습니다.")
        source_data = np.asarray(hdu.data)
        original_dtype = str(source_data.dtype)
        data = _reduce_fits_axes(source_data)
        header = hdu.header
        bitpix = header.get("BITPIX")
        # BITPIX describes the FITS container, not necessarily the camera ADC depth.
        container_bit_depth = (
            abs(int(bitpix))
            if isinstance(bitpix, (int, np.integer)) and int(bitpix) > 0
            else None
        )
        exposure, effective, stack_count, stack_method, exposure_provenance = (
            _fits_exposure_metadata(header)
        )
        arr = _finite_array(data)
        trusted_clip = _header_float(
            header,
            ["SATURATE", "SATURLEV", "ADCMAX", "WHITELEV"],
        )
        wcs_center_ra_deg, wcs_center_dec_deg = _wcs_center_from_header(
            header, int(arr.shape[1]), int(arr.shape[0])
        )
        site_latitude = _header_float(header, ["SITELAT", "LAT-OBS", "OBSGEO-B"], positive=False)
        site_longitude = _header_float(header, ["SITELONG", "SITELON", "LONG-OBS", "LON-OBS", "OBSGEO-L"], positive=False)
        site_height_m = _header_float(header, ["SITEELEV", "ALT-OBS", "ELEVATIO", "OBSGEO-H"], positive=False)
        metadata = ImageMetadata(
            filename=path.name,
            source_type="fits",
            width=int(arr.shape[1]),
            height=int(arr.shape[0]),
            dtype=original_dtype,
            bit_depth=container_bit_depth,
            exposure_sec=exposure,
            effective_exposure_sec=effective,
            stack_count=stack_count,
            stack_method=stack_method,
            date_obs=_header_text(header, ["DATE-OBS", "DATEOBS", "DATE"]),
            camera=_header_text(header, ["INSTRUME", "CAMERA", "DETECTOR"]),
            filter_name=_header_text(header, ["FILTER", "FILTERID"]),
            gain_setting=_header_float(header, ["GAIN", "EGAIN"]),
            offset_setting=_header_float(header, ["OFFSET", "BLKLEVEL", "BLACKLEV"], positive=False),
            sensor_temperature_c=_header_float(
                header, ["CCD-TEMP", "CCD_TEMP", "SENSORT", "TEMP"], positive=False
            ),
            binning_x=_header_int(header, ["XBINNING", "XBIN", "BINX"]),
            binning_y=_header_int(header, ["YBINNING", "YBIN", "BINY"]),
            data_min=float(np.min(arr)),
            data_max=float(np.max(arr)),
            extra={
                "bzero": header.get("BZERO"),
                "bscale": header.get("BSCALE"),
                "datamin": header.get("DATAMIN"),
                "datamax": header.get("DATAMAX"),
                "bitpix": bitpix,
                "trusted_sensor_clip_adu": trusted_clip,
                "sensor_clip_header_keys": [
                    key
                    for key in ("SATURATE", "SATURLEV", "ADCMAX", "WHITELEV")
                    if header.get(key) is not None
                ],
                "object_name": _header_text(header, ["OBJECT", "OBJNAME", "TARGET"]),
                "wcs_center_ra_deg": wcs_center_ra_deg,
                "wcs_center_dec_deg": wcs_center_dec_deg,
                "site_latitude_deg": site_latitude,
                "site_longitude_deg": site_longitude,
                "site_height_m": site_height_m,
                "local_sidereal_time_sec": _header_float(
                    header, ["LST", "SIDTIME", "ST"], positive=False
                ),
                "exposure_provenance": exposure_provenance,
            },
        )
        return ImageFrame(
            intensity=arr,
            raw_intensity=arr,
            saturation_intensity=arr,
            metadata=metadata,
        )


def _raw_cfa_codes(raw: Any) -> np.ndarray:
    pattern = np.asarray(raw.raw_pattern)
    if pattern.shape != (2, 2):
        raise ValueError("2×2 Bayer CFA 패턴만 지원합니다.")
    desc = raw.color_desc
    symbols = [chr(v) for v in desc] if isinstance(desc, bytes) else [str(v) for v in desc]
    code_to_letter = {i: symbols[i].upper() for i in range(len(symbols))}
    return np.vectorize(lambda code: code_to_letter.get(int(code), "?"))(pattern)


def _subtract_black_cfa(raw_values: np.ndarray, raw: Any) -> np.ndarray:
    values = raw_values.astype(np.float32, copy=True)
    pattern = np.asarray(raw.raw_pattern)
    black = list(getattr(raw, "black_level_per_channel", []) or [])
    if not black:
        return values
    for row in range(2):
        for col in range(2):
            code = int(pattern[row, col])
            level = float(black[code]) if code < len(black) else float(np.median(black))
            values[row::2, col::2] -= level
    return values


def _raw_metadata(raw: Any) -> tuple[float | None, str | None, str | None, float | None]:
    exposure = camera = date_obs = None
    iso = None
    metadata = getattr(raw, "metadata", None)
    if metadata is None:
        return exposure, camera, date_obs, iso
    try:
        shutter = float(getattr(metadata, "shutter", 0) or 0)
        exposure = shutter if math.isfinite(shutter) and shutter > 0 else None
    except Exception:
        exposure = None
    make = str(getattr(metadata, "make", "") or "").strip()
    model = str(getattr(metadata, "model", "") or "").strip()
    camera = " ".join(part for part in (make, model) if part) or None
    try:
        stamp = getattr(metadata, "timestamp", None)
        if isinstance(stamp, datetime):
            date_obs = stamp.astimezone(timezone.utc).isoformat()
        elif stamp:
            date_obs = str(stamp)
    except Exception:
        date_obs = None
    try:
        raw_iso = float(getattr(metadata, "iso_speed", 0) or 0)
        iso = raw_iso if math.isfinite(raw_iso) and raw_iso > 0 else None
    except Exception:
        iso = None
    return exposure, camera, date_obs, iso


def _load_raw(path: Path) -> ImageFrame:
    try:
        import rawpy
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("RAW 입력에는 rawpy가 필요합니다.") from exc

    with rawpy.imread(str(path)) as raw:
        try:
            raw_sizes = raw.sizes
            _check_pixel_count(int(raw_sizes.raw_width), int(raw_sizes.raw_height), "RAW 영상")
        except AttributeError:
            pass
        visible = np.asarray(raw.raw_image_visible)
        original_dtype = str(visible.dtype)
        corrected = np.maximum(_subtract_black_cfa(visible, raw), 0.0)
        letters = _raw_cfa_codes(raw)
        pattern = np.asarray(raw.raw_pattern)
        black_levels = [float(v) for v in (getattr(raw, "black_level_per_channel", []) or [])]
        raw_white_level = float(getattr(raw, "white_level", 0) or 0)
        green_planes: list[np.ndarray] = []
        green_headrooms: list[float] = []
        for row in range(2):
            for col in range(2):
                if str(letters[row, col]) != "G":
                    continue
                plane = corrected[row::2, col::2]
                green_planes.append(plane)
                code = int(pattern[row, col])
                black = black_levels[code] if code < len(black_levels) else 0.0
                if raw_white_level > black:
                    green_headrooms.append(raw_white_level - black)
        if not green_planes:
            raise ValueError("RAW CFA에서 Green 채널을 찾지 못했습니다.")
        min_h = min(p.shape[0] for p in green_planes)
        min_w = min(p.shape[1] for p in green_planes)
        cropped_green = [p[:min_h, :min_w] for p in green_planes]
        green = np.mean(cropped_green, axis=0).astype(np.float32)
        # Preserve clipping if either green photosite clips; averaging can hide it.
        saturation_plane = np.max(cropped_green, axis=0).astype(np.float32)
        preview = raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=True,
            output_bps=8,
            gamma=(2.222, 4.5),
        )
        exposure, camera, date_obs, iso = _raw_metadata(raw)
        try:
            sizes = raw.sizes
            full_width = int(sizes.raw_width)
            full_height = int(sizes.raw_height)
        except Exception:
            full_height, full_width = visible.shape
        linear_white_level = min(green_headrooms) if green_headrooms else max(raw_white_level, 0.0)
        metadata = ImageMetadata(
            filename=path.name,
            source_type="raw",
            width=int(green.shape[1]),
            height=int(green.shape[0]),
            dtype=original_dtype,
            bit_depth=int(np.iinfo(visible.dtype).bits)
            if np.issubdtype(visible.dtype, np.integer)
            else None,
            exposure_sec=exposure,
            date_obs=date_obs,
            camera=camera,
            gain_setting=iso,
            data_min=float(np.min(green)),
            data_max=float(np.max(green)),
            extra={
                "raw_visible_width": int(visible.shape[1]),
                "raw_visible_height": int(visible.shape[0]),
                "raw_full_width": full_width,
                "raw_full_height": full_height,
                "white_level": raw_white_level,
                "linear_white_level": linear_white_level,
                "green_headrooms": green_headrooms,
                "black_levels": black_levels,
                "cfa_pattern": letters.tolist(),
                "iso_speed": iso,
                "exposure_provenance": {
                    "selected_seconds": exposure,
                    "selected_key": "RAW metadata shutter" if exposure is not None else None,
                    "selected_role": "image",
                    "selection_rule": "raw_library_metadata" if exposure is not None else "missing",
                    "confidence": "high" if exposure is not None else "none",
                    "candidates": [],
                    "comparisons": [],
                    "conflicts": [],
                },
            },
        )
        return ImageFrame(
            intensity=_finite_array(green),
            green=_finite_array(green),
            raw_intensity=_finite_array(green),
            saturation_intensity=_finite_array(saturation_plane),
            preview_rgb=np.asarray(preview),
            metadata=metadata,
            coordinate_scale_x=2.0,
            coordinate_scale_y=2.0,
            photometric_area_multiplier=2.0,
        )


def _load_rendered(path: Path) -> ImageFrame:
    with Image.open(path) as opened:
        _check_pixel_count(int(opened.size[0]), int(opened.size[1]), "렌더링 영상")
        image = ImageOps.exif_transpose(opened)
        original_mode = image.mode
        original_array = np.asarray(image)
        if original_array.ndim == 2:
            intensity = original_array.astype(np.float32)
            preview = np.stack([original_array] * 3, axis=-1)
        else:
            rgb = np.asarray(image.convert("RGB"))
            preview = rgb
            # The research method uses the G channel for consistent visible-light
            # background comparison across colour inputs. Rendered images remain
            # diagnostic because in-camera tone curves may be nonlinear.
            intensity = rgb[..., 1].astype(np.float32)
        bit_depth = (
            int(np.iinfo(original_array.dtype).bits)
            if np.issubdtype(original_array.dtype, np.integer)
            else None
        )
        exposure = date_obs = camera = None
        try:
            exif = image.getexif()
            exposure_value = exif.get(33434)
            if exposure_value:
                exposure = _seconds_value(exposure_value)
            date_obs = str(exif.get(36867) or exif.get(306) or "") or None
            camera = " ".join(str(v) for v in [exif.get(271), exif.get(272)] if v).strip() or None
        except Exception:
            pass
        arr = _finite_array(intensity)
        metadata = ImageMetadata(
            filename=path.name,
            source_type="rendered",
            width=int(arr.shape[1]),
            height=int(arr.shape[0]),
            dtype=str(original_array.dtype),
            bit_depth=bit_depth,
            exposure_sec=exposure,
            date_obs=date_obs,
            camera=camera,
            data_min=float(np.min(arr)),
            data_max=float(np.max(arr)),
            extra={
                "mode": original_mode,
                "exposure_provenance": {
                    "selected_seconds": exposure,
                    "selected_key": "EXIF ExposureTime" if exposure is not None else None,
                    "selected_role": "image",
                    "selection_rule": "exif_exposure_time" if exposure is not None else "missing",
                    "confidence": "high" if exposure is not None else "none",
                    "candidates": [],
                    "comparisons": [],
                    "conflicts": [],
                },
            },
        )
        return ImageFrame(
            intensity=arr,
            raw_intensity=arr,
            saturation_intensity=arr,
            preview_rgb=np.asarray(preview),
            metadata=metadata,
        )


def load_image(path: Path) -> ImageFrame:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"지원하지 않는 파일 형식입니다: {suffix}")
    if suffix in FITS_EXTENSIONS:
        return _load_fits(path)
    if suffix in RAW_EXTENSIONS:
        return _load_raw(path)
    return _load_rendered(path)


def _resize_to_shape(arr: np.ndarray, target_shape: tuple[int, int]) -> None:
    if arr.shape != target_shape:
        raise ValueError(f"보정 프레임 크기가 light frame과 다릅니다: {arr.shape} != {target_shape}")


def _compare_optional(
    role: str,
    label: str,
    target_value: object,
    calibration_value: object,
    warnings: list[str],
    *,
    relative_tolerance: float = 0.02,
    hard_error: bool = False,
) -> None:
    if target_value is None and calibration_value is None:
        return
    if target_value is None or calibration_value is None:
        warnings.append(f"{role} 프레임의 {label} 일치 여부를 메타데이터에서 확인하지 못했습니다.")
        return
    if isinstance(target_value, str) or isinstance(calibration_value, str):
        equal = str(target_value).strip().lower() == str(calibration_value).strip().lower()
    else:
        try:
            target_number = float(str(target_value))
            calibration_number = float(str(calibration_value))
            equal = math.isclose(
                target_number,
                calibration_number,
                rel_tol=relative_tolerance,
                abs_tol=0.01,
            )
        except (TypeError, ValueError):
            return
    if not equal:
        message = f"{role} 프레임의 {label}가 light frame과 다릅니다."
        if hard_error:
            raise ValueError(message)
        warnings.append(message)


def _validate_calibration_frame(
    calibration: ImageFrame,
    target: ImageFrame,
    role: str,
    warnings: list[str],
) -> None:
    _resize_to_shape(calibration.intensity, target.intensity.shape)
    if calibration.metadata.source_type != target.metadata.source_type:
        raise ValueError(
            f"{role} 프레임 형식({calibration.metadata.source_type})이 "
            f"light frame 형식({target.metadata.source_type})과 다릅니다."
        )
    if calibration.metadata.source_type == "rendered":
        raise ValueError("JPG/PNG 등 렌더링 영상은 정량 보정 프레임으로 사용할 수 없습니다.")
    _compare_optional(role, "카메라", target.metadata.camera, calibration.metadata.camera, warnings, hard_error=True)
    _compare_optional(role, "GAIN/ISO", target.metadata.gain_setting, calibration.metadata.gain_setting, warnings)
    _compare_optional(role, "offset", target.metadata.offset_setting, calibration.metadata.offset_setting, warnings)
    _compare_optional(role, "X binning", target.metadata.binning_x, calibration.metadata.binning_x, warnings, hard_error=True)
    _compare_optional(role, "Y binning", target.metadata.binning_y, calibration.metadata.binning_y, warnings, hard_error=True)
    if role == "Dark":
        target_temp = target.metadata.sensor_temperature_c
        cal_temp = calibration.metadata.sensor_temperature_c
        if target_temp is not None and cal_temp is not None and abs(target_temp - cal_temp) > 3.0:
            warnings.append(
                f"Dark 온도가 light와 {abs(target_temp-cal_temp):.1f}°C 다릅니다. "
                "dark current 비례가 정확하지 않을 수 있습니다."
            )


def _disk_backed_master(
    paths: list[Path],
    target: ImageFrame,
    role: str,
    limit: int,
    preprocess: Callable[[ImageFrame], np.ndarray],
) -> tuple[np.ndarray | None, int, list[str]]:
    if not paths:
        return None, 0, []
    selected = paths[:limit]
    warnings: list[str] = []
    if len(paths) > limit:
        warnings.append(f"{role} 프레임 {len(paths)}개 중 앞의 {limit}개만 사용했습니다.")
    height, width = target.intensity.shape
    estimated = len(selected) * height * width * np.dtype(np.float32).itemsize
    if estimated > MAX_CALIBRATION_TEMP_BYTES:
        raise ValueError(
            f"{role} master 임시 저장 예상량이 {estimated/1024**3:.1f} GiB로 제한을 초과합니다. "
            "프레임 수를 줄이거나 미리 master frame을 만들어 사용하세요."
        )
    temporary = tempfile.NamedTemporaryFile(prefix="lightt_master_", suffix=".dat", delete=False)
    temporary_path = Path(temporary.name)
    temporary.close()
    mmap: np.memmap | None = None
    try:
        mmap = np.memmap(
            temporary_path,
            mode="w+",
            dtype=np.float32,
            shape=(len(selected), height, width),
        )
        for index, path in enumerate(selected):
            frame = load_image(path)
            _validate_calibration_frame(frame, target, role, warnings)
            array = np.asarray(preprocess(frame), dtype=np.float32)
            _resize_to_shape(array, target.intensity.shape)
            mmap[index] = array
            mmap.flush()
            del frame, array
        result = np.empty((height, width), dtype=np.float32)
        bytes_per_row = max(1, len(selected) * width * np.dtype(np.float32).itemsize)
        rows_per_tile = max(1, min(height, MASTER_TILE_TARGET_BYTES // bytes_per_row))
        for y0 in range(0, height, rows_per_tile):
            y1 = min(height, y0 + rows_per_tile)
            result[y0:y1] = np.median(np.asarray(mmap[:, y0:y1, :]), axis=0)
        return result, len(selected), warnings
    finally:
        if mmap is not None:
            del mmap
        temporary_path.unlink(missing_ok=True)


def apply_calibration(
    frame: ImageFrame,
    calibration: CalibrationSet,
    *,
    light_exposure_sec: float | None = None,
    max_frames: int = 30,
) -> tuple[ImageFrame, dict[str, object]]:
    if not (calibration.bias_paths or calibration.dark_paths or calibration.flat_paths):
        return frame, {
            "bias_frames": 0,
            "dark_frames": 0,
            "flat_frames": 0,
            "applied": False,
            "offset_removed": False,
            "warnings": [],
        }

    bias, bias_count, bias_warnings = _disk_backed_master(
        calibration.bias_paths,
        frame,
        "Bias",
        max_frames,
        lambda item: item.intensity,
    )

    dark_scales: list[float] = []

    def preprocess_dark(item: ImageFrame) -> np.ndarray:
        data = item.intensity.astype(np.float32, copy=True)
        if bias is not None:
            data -= bias
        scale = 1.0
        dark_exposure = item.metadata.exposure_sec
        if light_exposure_sec is not None and light_exposure_sec > 0:
            if dark_exposure is not None and dark_exposure > 0:
                scale = light_exposure_sec / dark_exposure
            else:
                dark_warnings_local.append(
                    "Dark 노출시간 메타데이터가 없어 light와 동일 노출이라고 가정했습니다."
                )
        elif dark_exposure is not None:
            dark_warnings_local.append(
                "Light 노출시간을 알 수 없어 Dark 노출시간 비례 보정을 적용하지 않았습니다."
            )
        dark_scales.append(float(scale))
        return data * scale

    dark_warnings_local: list[str] = []
    dark, dark_count, dark_warnings = _disk_backed_master(
        calibration.dark_paths,
        frame,
        "Dark",
        max_frames,
        preprocess_dark,
    )
    dark_warnings.extend(dark_warnings_local)

    def preprocess_flat(item: ImageFrame) -> np.ndarray:
        data = item.intensity.astype(np.float32, copy=True)
        if bias is not None:
            data -= bias
        return data

    flat, flat_count, flat_warnings = _disk_backed_master(
        calibration.flat_paths,
        frame,
        "Flat",
        max_frames,
        preprocess_flat,
    )
    warnings = bias_warnings + dark_warnings + flat_warnings
    if flat_count and dark_count == 0:
        warnings.append(
            "Flat-dark가 제공되지 않았습니다. 짧은 flat에서는 영향이 작을 수 있지만 "
            "장노출 flat/CMOS glow 환경에서는 별도 flat-dark를 권장합니다."
        )

    data = frame.intensity.astype(np.float32, copy=True)
    report: dict[str, object] = {
        "bias_frames": bias_count,
        "dark_frames": dark_count,
        "flat_frames": flat_count,
        "dark_scale_factors": dark_scales,
        "applied": False,
        "offset_removed": False,
        "master_method": "disk_backed_tiled_median",
        "warnings": warnings,
    }
    if bias is not None:
        data -= bias
        report["applied"] = True
        report["offset_removed"] = True
    if dark is not None:
        data -= dark
        report["applied"] = True
        report["offset_removed"] = True
    if flat is not None:
        finite_positive = flat[np.isfinite(flat) & (flat > 0)]
        if finite_positive.size < flat.size * 0.5:
            raise ValueError("Flat frame의 유효 양수 픽셀이 50% 미만입니다.")
        norm = float(np.median(finite_positive))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Flat frame 중앙값이 유효하지 않습니다.")
        normalized = flat / norm
        valid_flat = np.isfinite(normalized) & (normalized > 0)
        p_low, p_high = np.percentile(normalized[valid_flat], [0.1, 99.9])
        if p_low < 0.05 or p_high > 20.0:
            raise ValueError(
                "Flat 정규화 범위가 비정상적입니다. bias/flat-dark 처리와 노출을 확인하세요."
            )
        safe_flat = np.where(valid_flat & (normalized >= 0.05), normalized, np.nan)
        data = np.asarray(data / safe_flat, dtype=np.float32)
        report["applied"] = True
        report["flat_normalization_median"] = norm
        report["flat_normalized_p001"] = float(p_low)
        report["flat_normalized_p999"] = float(p_high)

    finite = np.isfinite(data)
    if not finite.any():
        raise ValueError("보정 후 영상에 유효한 픽셀이 없습니다.")
    invalid_fraction = 1.0 - float(np.mean(finite))
    if invalid_fraction > 0.01:
        raise ValueError(f"보정 후 무효 픽셀 비율이 {invalid_fraction:.2%}로 너무 높습니다.")
    if not finite.all():
        # Keep the fact visible in diagnostics; replacement only avoids downstream crashes.
        data = np.where(finite, data, float(np.nanmedian(data[finite])))
        warnings.append(f"보정 후 무효 픽셀 {invalid_fraction:.4%}를 중앙값으로 대체했습니다.")

    calibrated = ImageFrame(
        intensity=data,
        raw_intensity=frame.raw_intensity,
        saturation_intensity=frame.saturation_intensity,
        preview_rgb=frame.preview_rgb,
        green=data if frame.green is not None else None,
        metadata=frame.metadata,
        coordinate_scale_x=frame.coordinate_scale_x,
        coordinate_scale_y=frame.coordinate_scale_y,
        photometric_area_multiplier=frame.photometric_area_multiplier,
    )
    return calibrated, report


def resolve_exposure(frame: ImageFrame, manual_sec: float, mode: str) -> tuple[float, str]:
    if frame.metadata.stack_count and frame.metadata.stack_count > 1:
        method = frame.metadata.stack_method
        if method not in {"mean", "median", "sum"}:
            raise ValueError(
                "스택 FITS의 결합 방식(mean/median/sum)을 확인할 수 없습니다. "
                "정량 계획에는 단일 시험 프레임을 권장합니다."
            )
        if method in {"mean", "median"}:
            # Pixel scale is per-frame; EXPTIME must be per-frame exposure.
            if not frame.metadata.exposure_sec:
                raise ValueError("평균/중앙값 스택에는 한 장당 EXPTIME이 필요합니다.")
        elif method == "sum":
            raise ValueError(
                "합(sum) 스택은 ADU/s 해석이 프로그램마다 달라 자동 계획에 사용하지 않습니다. "
                "단일 시험 프레임을 사용하세요."
            )
    if mode == "manual":
        chosen = manual_sec
        source = "manual"
    elif mode == "header":
        if not frame.metadata.exposure_sec:
            raise ValueError("헤더 노출시간 모드이지만 영상 메타데이터에 노출시간이 없습니다.")
        chosen = frame.metadata.exposure_sec
        source = "header"
    else:
        if frame.metadata.exposure_sec:
            chosen = frame.metadata.exposure_sec
            source = "header(auto)"
        else:
            chosen = manual_sec
            source = "manual(auto fallback)"
    if not math.isfinite(chosen) or chosen <= 0:
        raise ValueError("현재 노출시간은 0보다 큰 유한한 값이어야 합니다.")
    return float(chosen), source


def infer_intensity_domain(
    frame: ImageFrame,
    requested_clip_adu: float | None,
    safety_fraction: float,
    sample_max_pixels: int | None = None,
) -> IntensityDomain:
    raw = (
        frame.saturation_intensity
        if frame.saturation_intensity is not None
        else frame.raw_intensity
        if frame.raw_intensity is not None
        else frame.intensity
    )
    sampled = raw
    if sample_max_pixels is not None and sample_max_pixels > 0 and raw.size > sample_max_pixels:
        step = max(1, int(math.ceil(math.sqrt(raw.size / sample_max_pixels))))
        sampled = raw[::step, ::step]
    values = sampled[np.isfinite(sampled)]
    if values.size == 0:
        raise ValueError("ADU 범위를 판정할 유효 픽셀이 없습니다.")
    # Min/max are cheap reductions and remain exact; median/p99.9 may use the
    # deterministic inspection sample to avoid a full-size temporary array.
    observed_min = float(np.nanmin(raw))
    observed_median = float(np.median(values))
    observed_p999 = float(np.percentile(values, 99.9))
    observed_max = float(np.nanmax(raw))
    rendered = frame.metadata.source_type == "rendered"
    warnings: list[str] = []
    source = "unknown"
    confidence = "low"
    requires_confirmation = False

    if requested_clip_adu is not None and math.isfinite(requested_clip_adu) and requested_clip_adu > 0:
        clip = float(requested_clip_adu)
        source = "user_confirmed"
        confidence = "high"
    else:
        linear_white_level = frame.metadata.extra.get("linear_white_level")
        trusted_header_clip = frame.metadata.extra.get("trusted_sensor_clip_adu")
        if isinstance(linear_white_level, (int, float)) and float(linear_white_level) > 0:
            clip = float(linear_white_level)
            source = "raw_white_level"
            confidence = "high"
        elif isinstance(trusted_header_clip, (int, float)) and float(trusted_header_clip) > 0:
            clip = float(trusted_header_clip)
            source = "trusted_fits_header"
            confidence = "medium"
            warnings.append(
                "FITS 포화값을 SATURATE/SATURLEV/ADCMAX/WHITELEV 계열의 신뢰 헤더에서 읽었습니다. 장비 매뉴얼과 한 번 확인하세요."
            )
        elif rendered:
            if frame.metadata.bit_depth and frame.metadata.bit_depth <= 16:
                clip = float((1 << frame.metadata.bit_depth) - 1)
            else:
                clip = max(observed_max, 1.0)
            source = "rendered_container"
            confidence = "low"
            requires_confirmation = True
            warnings.append(
                "JPG/PNG/TIFF의 bit depth는 원본 센서 ADC 범위를 뜻하지 않습니다. 포화 계산은 진단용입니다."
            )
        else:
            # Never infer a detector clip from a dark observation maximum or FITS BITPIX.
            clip = max(observed_max, 1.0)
            source = "unverified_placeholder"
            confidence = "unknown"
            requires_confirmation = True
            warnings.append(
                "센서 포화 ADU를 신뢰할 근거가 없습니다. FITS 저장 비트수나 관측 최댓값으로 추측하지 않았습니다. "
                "카메라의 실제 clipping ADU를 입력해야 안전한 노출 상한을 계산합니다."
            )

    if not 0.05 <= safety_fraction <= 0.99:
        raise ValueError("포화 안전비율은 0.05~0.99 범위여야 합니다.")
    if source not in {"unverified_placeholder", "rendered_container"}:
        if observed_median >= clip:
            raise ValueError(
                f"영상 중앙값({observed_median:.3f} ADU)이 센서 포화값({clip:.3f} ADU) 이상입니다. "
                "ADU 단위와 카메라 설정을 확인하세요."
            )
        if observed_p999 > clip * 1.10:
            raise ValueError(
                f"영상 p99.9({observed_p999:.3f} ADU)가 센서 포화값({clip:.3f} ADU)을 크게 초과합니다. "
                "영상과 포화값이 같은 단위가 아닙니다."
            )
        if observed_max > clip * 1.01:
            raise ValueError(
                f"영상 최댓값({observed_max:.3f} ADU)이 센서 포화값({clip:.3f} ADU)을 초과합니다."
            )
    quantitative_supported = (
        not rendered
        and not requires_confirmation
        and source in {"user_confirmed", "raw_white_level", "trusted_fits_header"}
    )
    return IntensityDomain(
        source_kind=frame.metadata.source_type,
        dtype=frame.metadata.dtype,
        observed_min=observed_min,
        observed_median=observed_median,
        observed_p999=observed_p999,
        observed_max=observed_max,
        sensor_clip_adu=clip,
        saturation_threshold_adu=clip * 0.995,
        is_rendered=rendered,
        quantitative_saturation_supported=quantitative_supported,
        clip_source=source,
        clip_confidence=confidence,
        requires_user_confirmation=requires_confirmation,
        warnings=warnings,
    )
