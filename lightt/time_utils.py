from __future__ import annotations

from datetime import datetime, timezone


def parse_observation_datetime(value: str | None, *, assume_utc_if_naive: bool = False) -> datetime | None:
    """Parse common FITS/RAW/Stellarium timestamps without guessing local EXIF zones."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    candidates = [text]
    if len(text) >= 19 and text[4:5] == ":" and text[7:8] == ":":
        candidates.append(text[:4] + "-" + text[5:7] + "-" + text[8:10] + "T" + text[11:])
    parsed: datetime | None = None
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        if not assume_utc_if_naive:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def observation_time_difference_minutes(
    first: str | None,
    second: str | None,
    *,
    first_assume_utc_if_naive: bool = False,
    second_assume_utc_if_naive: bool = False,
) -> float | None:
    a = parse_observation_datetime(first, assume_utc_if_naive=first_assume_utc_if_naive)
    b = parse_observation_datetime(second, assume_utc_if_naive=second_assume_utc_if_naive)
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 60.0


def image_observation_time_utc(value: str | None, source_type: str | None) -> str | None:
    """Return an ISO UTC capture time when the image timestamp has a defensible timezone.

    FITS DATE-OBS is conventionally UTC when no offset is present. RAW timestamps loaded
    by LIGHTT are normally normalized to UTC by rawpy. Rendered-image EXIF timestamps
    without an offset are deliberately left unresolved instead of guessing a timezone.
    """
    kind = str(source_type or "").strip().lower()
    parsed = parse_observation_datetime(value, assume_utc_if_naive=(kind == "fits"))
    return parsed.isoformat() if parsed is not None else None


def datetime_to_julian_day(value: datetime) -> float:
    """Convert an aware datetime to astronomical Julian Day."""
    if value.tzinfo is None:
        raise ValueError("Julian Day 변환에는 시간대가 포함된 시각이 필요합니다.")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.timestamp() / 86400.0 + 2440587.5
