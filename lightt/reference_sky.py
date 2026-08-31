from __future__ import annotations

"""Optional public-survey morphology retrieval for fixed deep-sky targets."""

import io
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from .target_structure import analyze_relative_structure, save_structure_diagnostic, unavailable_profile

HIPS_ENDPOINTS = (
    "https://alasky.cds.unistra.fr/hips-image-services/hips2fits",
    "https://alaskybis.cds.unistra.fr/hips-image-services/hips2fits",
)
DEFAULT_HIPS = "CDS/P/DSS2/red"

def survey_for_filter(filter_name: str | None) -> str:
    text = (filter_name or "").strip().casefold().replace("-", "_")
    blue_tokens = ("oiii", "o3", "johnson_b", "b_bess", "b_bessel", "blue", "g_sdss")
    if any(token in text for token in blue_tokens):
        return "CDS/P/DSS2/blue"
    return DEFAULT_HIPS


def _fov_for_target(size_deg: float | None) -> float:
    if size_deg is None or size_deg <= 0:
        return 0.70
    # Context around the target is required to estimate the survey background.
    return float(min(8.0, max(0.08, size_deg * 2.6)))


def fetch_target_structure(
    *,
    ra_deg: float | None,
    dec_deg: float | None,
    target_size_deg: float | None,
    target_mode: str,
    result_dir: Path,
    timeout_sec: float = 7.0,
    survey: str = DEFAULT_HIPS,
) -> tuple[dict[str, Any], list[str], str | None]:
    warnings: list[str] = []
    if target_mode != "extended":
        return unavailable_profile("점광원은 확산 구조 구역 분석을 적용하지 않습니다.").to_dict(), warnings, None
    if ra_deg is None or dec_deg is None:
        return unavailable_profile("RA/Dec가 없어 외부 참조 구조 영상을 조회하지 않았습니다.").to_dict(), warnings, None
    fov = _fov_for_target(target_size_deg)
    params = {
        "hips": survey,
        "width": 320,
        "height": 320,
        "projection": "TAN",
        "fov": f"{fov:.8f}",
        "ra": f"{float(ra_deg):.9f}",
        "dec": f"{float(dec_deg):.9f}",
        "coordsys": "icrs",
        "format": "fits",
    }
    payload: bytes | None = None
    used_endpoint: str | None = None
    last_error: str | None = None
    cache_dir = result_dir.parent / "_reference_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{survey}|{float(ra_deg):.6f}|{float(dec_deg):.6f}|{fov:.6f}|320".encode("utf-8")
    ).hexdigest()[:24]
    cache_path = cache_dir / f"{cache_key}.fits"
    if cache_path.exists() and 2880 <= cache_path.stat().st_size <= 16_000_000:
        payload = cache_path.read_bytes()
        used_endpoint = "local_cache"
    else:
        for endpoint in HIPS_ENDPOINTS:
            try:
                req = Request(endpoint + "?" + urlencode(params), headers={"User-Agent": "NOXIS/37 morphology planner"})
                with urlopen(req, timeout=timeout_sec) as response:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > 16_000_000:
                        raise ValueError("참조 FITS 응답이 16 MB 제한을 초과했습니다.")
                    payload = response.read(16_000_001)
                if len(payload) > 16_000_000:
                    raise ValueError("참조 FITS 응답이 16 MB 제한을 초과했습니다.")
                if len(payload) < 2880:
                    raise ValueError("참조 FITS 응답이 비정상적으로 작습니다.")
                if not payload[:80].lstrip().startswith(b"SIMPLE"):
                    raise ValueError("참조 서비스가 FITS가 아닌 응답을 반환했습니다.")
                used_endpoint = endpoint
                break
            except Exception as exc:  # network is optional; never fail the main analysis
                last_error = f"{type(exc).__name__}: {exc}"
                payload = None
    if payload is None:
        warnings.append(
            "CDS DSS2 참조 영상을 가져오지 못해 천체 내부 밝기 분포를 적용하지 않았습니다. "
            "기존 평균 표면밝기 모델로 자동 대체했습니다."
        )
        if last_error:
            warnings.append(f"참조 영상 조회 진단: {last_error}")
        return unavailable_profile("외부 참조 영상 조회 실패", source="CDS HiPS2FITS").to_dict(), warnings, None

    fits_path = result_dir / "target_reference_dss2_red.fits"
    try:
        # astropy is already an application dependency; lazy import keeps the pure
        # morphology module testable in lightweight environments.
        from astropy.io import fits
        with fits.open(io.BytesIO(payload), memmap=False) as hdul:
            data = np.asarray(hdul[0].data, dtype=float)
            while data.ndim > 2:
                data = data[0]
        fits_path.write_bytes(payload)
        if used_endpoint != "local_cache":
            try:
                cache_path.write_bytes(payload)
            except OSError:
                pass
        profile = analyze_relative_structure(
            data,
            target_diameter_deg=target_size_deg,
            fov_deg=fov,
            source="CDS HiPS2FITS morphology",
            survey=survey,
        )
        plot_path = result_dir / "target_structure_profile.png"
        if profile.status == "ok":
            save_structure_diagnostic(data, profile, plot_path, target_diameter_deg=target_size_deg)
            warnings.extend(profile.notes)
            result = profile.to_dict()
            result["retrieval_endpoint"] = used_endpoint
            return result, warnings, plot_path.name if plot_path.exists() else None
        warnings.extend(profile.notes)
        return profile.to_dict(), warnings, None
    except Exception as exc:
        if used_endpoint == "local_cache":
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass
        warnings.append(
            "참조 FITS는 내려받았지만 구조 분석에 실패해 평균 표면밝기 모델로 자동 대체했습니다. "
            f"({type(exc).__name__}: {exc})"
        )
        return unavailable_profile("참조 FITS 구조 분석 실패", source="CDS HiPS2FITS").to_dict(), warnings, None
