from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import AnalysisSettings


def _parse_observation_time(value: str, timezone_name: str) -> datetime:
    if not value.strip():
        raise ValueError("RA/Dec 좌표를 사용할 때는 관측 시각이 필요합니다.")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("관측 시각은 ISO 형식이어야 합니다.") from exc
    if parsed.tzinfo is None:
        tz = timezone.utc if timezone_name.upper() == "UTC" else ZoneInfo("Asia/Seoul")
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


def resolve_target_altaz(
    settings: AnalysisSettings,
) -> tuple[float, float, dict[str, object]]:
    if settings.target_coordinate_mode == "altaz":
        return (
            settings.target_alt_deg,
            settings.target_az_deg,
            {
                "mode": "altaz",
                "alt_deg": settings.target_alt_deg,
                "az_deg": settings.target_az_deg,
            },
        )
    if settings.target_ra_deg is None or settings.target_dec_deg is None:
        raise ValueError("RA/Dec 모드에서는 적경과 적위가 모두 필요합니다.")
    try:
        import astropy.units as u
        from astropy.coordinates import AltAz, EarthLocation, SkyCoord
        from astropy.time import Time
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("RA/Dec 변환에는 astropy가 필요합니다.") from exc
    observed_at = _parse_observation_time(settings.observation_time, settings.timezone)
    location = EarthLocation(
        lat=settings.latitude * u.deg,
        lon=settings.longitude * u.deg,
        height=settings.height_m * u.m,
    )
    target = SkyCoord(
        ra=settings.target_ra_deg * u.deg,
        dec=settings.target_dec_deg * u.deg,
        frame="icrs",
    )
    transformed = target.transform_to(AltAz(obstime=Time(observed_at), location=location))
    alt = float(transformed.alt.deg)
    az = float(transformed.az.deg)
    if alt < 0:
        raise ValueError(f"선택한 시각과 위치에서 대상 고도가 {alt:.2f}°로 지평선 아래입니다.")
    return (
        alt,
        az,
        {
            "mode": "radec",
            "ra_deg": settings.target_ra_deg,
            "dec_deg": settings.target_dec_deg,
            "observation_time_utc": observed_at.isoformat(),
            "latitude": settings.latitude,
            "longitude": settings.longitude,
            "height_m": settings.height_m,
            "resolved_alt_deg": alt,
            "resolved_az_deg": az,
        },
    )
