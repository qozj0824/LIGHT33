from __future__ import annotations

import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .time_utils import datetime_to_julian_day, parse_observation_datetime

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_URL = "http://127.0.0.1:8090"
_MAX_RESPONSE_BYTES = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "Redirects are disabled", headers, fp)


def normalize_base_url(base_url: str | None) -> str:
    """Return a safe Stellarium Remote Control base URL.

    Only loopback and RFC1918/private addresses are accepted. Link-local and public
    destinations are rejected so this local helper cannot become a general SSRF proxy.
    """
    raw = (base_url or _DEFAULT_URL).strip()
    if not raw:
        raw = _DEFAULT_URL
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("Stellarium URL은 http 또는 https만 사용할 수 있습니다.")
    if parsed.username or parsed.password:
        raise ValueError("Stellarium URL에 사용자 정보는 넣을 수 없습니다.")
    if parsed.query or parsed.fragment:
        raise ValueError("Stellarium 기본 URL에는 query 또는 fragment를 넣지 마세요.")
    if parsed.path not in {"", "/"}:
        raise ValueError("Stellarium 기본 URL에는 경로를 넣지 마세요.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("Stellarium 호스트가 비어 있습니다.")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Stellarium 주소는 localhost 또는 사설 IP만 허용됩니다.") from exc
        if not (address.is_loopback or address.is_private) or address.is_link_local:
            raise ValueError("Stellarium 주소는 loopback 또는 사설망 IP여야 합니다.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Stellarium 포트가 올바르지 않습니다.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Stellarium 포트는 1~65535 범위여야 합니다.")
    netloc = parsed.netloc.lower()
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, "", "", "")).rstrip("/")


def fetch_json(base_url: str, endpoint: str, timeout: float = 3.0) -> Any:
    base = normalize_base_url(base_url)
    if not endpoint.startswith("/api/"):
        raise ValueError("허용되지 않은 Stellarium API 경로입니다.")
    url = base + endpoint
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain;q=0.8,*/*;q=0.2",
            "User-Agent": "LIGHTT-v34.2",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("Stellarium 응답이 허용 크기를 초과했습니다.")
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text[:20_000]}



def post_form(base_url: str, endpoint: str, fields: dict[str, object], timeout: float = 3.0) -> str:
    base = normalize_base_url(base_url)
    if not endpoint.startswith("/api/"):
        raise ValueError("허용되지 않은 Stellarium API 경로입니다.")
    body = urllib.parse.urlencode({key: str(value) for key, value in fields.items()}).encode("ascii")
    request = urllib.request.Request(
        base + endpoint,
        data=body,
        headers={
            "Accept": "text/plain,application/json;q=0.8,*/*;q=0.2",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "LIGHTT-v34.2",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(100_001)
        if len(raw) > 100_000:
            raise ValueError("Stellarium 응답이 허용 크기를 초과했습니다.")
    return raw.decode("utf-8", errors="replace").strip()


def set_simulation_time(base_url: str, observation_time_utc: str, *, pause: bool = True) -> dict[str, Any]:
    parsed = parse_observation_datetime(observation_time_utc, assume_utc_if_naive=False)
    if parsed is None:
        raise ValueError("기준 영상 촬영시각에 UTC 시간대 정보가 없어 Stellarium 시각을 자동 설정할 수 없습니다.")
    jday = datetime_to_julian_day(parsed)
    # Stellarium Remote Control /api/main/time expects Julian Day and time rate in JDay/sec.
    timerate = 0.0 if pause else (1.0 / 86400.0)
    response = post_form(base_url, "/api/main/time", {"time": f"{jday:.10f}", "timerate": f"{timerate:.12g}"})
    return {
        "ok": response.lower() in {"ok", ""} or "ok" in response.lower(),
        "base_url": normalize_base_url(base_url),
        "observation_time_utc": parsed.isoformat(),
        "julian_day": jday,
        "paused": pause,
        "response": response[:500],
    }

def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def first_recursive(value: Any, candidates: list[str]) -> Any:
    wanted = {_normalize_key(item) for item in candidates}
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_key(key) in wanted and item not in (None, ""):
                return item
        for item in value.values():
            found = first_recursive(item, candidates)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = first_recursive(item, candidates)
            if found not in (None, ""):
                return found
    return None


def _parse_sexagesimal(text: str, *, is_ra: bool) -> float | None:
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if not numbers:
        return None
    if len(numbers) == 1:
        value = float(numbers[0])
        lower = text.lower()
        if is_ra and any(token in lower for token in ("h", "hour")):
            value *= 15.0
        elif any(token in lower for token in ("rad", "radian")):
            value = math.degrees(value)
        return value
    sign = -1.0 if text.lstrip().startswith("-") else 1.0
    values = [abs(float(item)) for item in numbers[:3]]
    angle = values[0] + values[1] / 60.0 + (values[2] if len(values) > 2 else 0.0) / 3600.0
    if is_ra and any(token in text.lower() for token in ("h", "hour")):
        return angle * 15.0
    return angle if is_ra else sign * angle


def parse_angle(value: Any, *, is_ra: bool = False, unit_hint: str | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and value:
        return parse_angle(value[0], is_ra=is_ra, unit_hint=unit_hint)
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            return None
        if unit_hint == "radian":
            result = math.degrees(result)
        elif unit_hint == "hour":
            result *= 15.0
        return result
    return _parse_sexagesimal(str(value).strip(), is_ra=is_ra)


def _pair(value: Any) -> tuple[Any, Any]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[0], value[1]
    text = str(value or "")
    parts = re.split(r"[,;/|]", text, maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


def _target_mode(name: str, object_type: str) -> str:
    text = f"{name} {object_type}".lower()
    # Asteroids/minor planets are effectively point sources at ordinary amateur
    # image scales even though the word ``planet`` appears in the type string.
    if any(token in text for token in ("minor planet", "asteroid")):
        return "point"
    extended = (
        "nebula",
        "galaxy",
        "supernova remnant",
        "h ii",
        "hii",
        "planetary",
        "dark nebula",
        "emission",
        "reflection",
        "cluster",
        "globular",
        "open cluster",
        "planet",
        "moon",
        "sun",
        "comet",
    )
    return "extended" if any(token in text for token in extended) else "point"


def normalize_selected_object(info: Any, status: Any | None = None) -> dict[str, Any]:
    status = status if isinstance(status, dict) else {}
    info = info if isinstance(info, dict) else {}
    name = first_recursive(
        info,
        ["englishName", "localizedName", "name", "objectName", "designation"],
    )
    object_type = first_recursive(info, ["type", "objectType", "object-type", "otype", "category"])

    ra_value = first_recursive(info, ["ra", "rightAscension", "raJ2000", "raj2000", "j2000ra"])
    dec_value = first_recursive(info, ["dec", "declination", "decJ2000", "dej2000", "j2000dec"])
    alt_value = first_recursive(info, ["alt", "altitude", "altDeg", "altitudeDeg"])
    az_value = first_recursive(info, ["az", "azimuth", "azDeg", "azimuthDeg"])
    if ra_value is None or dec_value is None:
        ra_pair, dec_pair = _pair(first_recursive(info, ["radec", "j2000", "equatorial"] ))
        ra_value = ra_value if ra_value is not None else ra_pair
        dec_value = dec_value if dec_value is not None else dec_pair
    if alt_value is None or az_value is None:
        az_pair, alt_pair = _pair(first_recursive(info, ["azalt", "altaz", "horizontal"] ))
        az_value = az_value if az_value is not None else az_pair
        alt_value = alt_value if alt_value is not None else alt_pair

    ra_deg = parse_angle(ra_value, is_ra=True)
    dec_deg = parse_angle(dec_value)
    alt_deg = parse_angle(alt_value)
    az_deg = parse_angle(az_value)
    vmag_value = first_recursive(info, ["vmag", "visualMagnitude", "magnitude", "mag"] )
    vmage_value = first_recursive(info, ["vmage", "extinctedMagnitude", "magnitudeAfterExtinction"] )
    size_deg_value = first_recursive(info, ["size-dd", "sizeDeg", "angularSizeDeg"] )
    size_raw_value = first_recursive(info, ["size", "angularSize"] )
    try:
        vmag = float(vmag_value) if vmag_value is not None else None
        if vmag is not None and not math.isfinite(vmag):
            vmag = None
    except (TypeError, ValueError):
        vmag = None
    try:
        vmage = float(vmage_value) if vmage_value is not None else None
        if vmage is not None and not math.isfinite(vmage):
            vmage = None
    except (TypeError, ValueError):
        vmage = None
    size_deg = parse_angle(size_deg_value)
    # Stellarium exposes `size-dd` in degrees and the raw `size` field in radians.
    # Use the explicit degree field whenever it exists; only fall back to radians.
    if size_deg is None:
        try:
            raw_number = float(size_raw_value) if size_raw_value is not None else None
        except (TypeError, ValueError):
            raw_number = None
        if raw_number is not None and math.isfinite(raw_number) and 0 <= raw_number <= math.pi:
            size_deg = math.degrees(raw_number)
    if ra_deg is not None:
        ra_deg %= 360.0
    if dec_deg is not None and not -90.0 <= dec_deg <= 90.0:
        dec_deg = None
    if alt_deg is not None and not -90.0 <= alt_deg <= 90.0:
        alt_deg = None
    if az_deg is not None:
        az_deg %= 360.0

    location = status.get("location") if isinstance(status.get("location"), dict) else {}
    time_info = status.get("time") if isinstance(status.get("time"), dict) else {}
    latitude = parse_angle(first_recursive(location, ["latitude", "lat"]))
    longitude = parse_angle(first_recursive(location, ["longitude", "lon", "lng"]))
    altitude_m = first_recursive(location, ["altitude", "height", "elevation"])
    try:
        height_m = float(altitude_m) if altitude_m is not None else None
    except (TypeError, ValueError):
        height_m = None
    local_time = first_recursive(time_info, ["local", "localTime"])
    utc_time = first_recursive(time_info, ["utc", "utcTime"])
    return {
        "name": str(name or "Stellarium selected object"),
        "object_type": str(object_type or "unknown"),
        "target_mode": _target_mode(str(name or ""), str(object_type or "")),
        "vmag": vmag,
        "vmage": vmage,
        "size_deg": size_deg,
        "ra_deg": ra_deg,
        "dec_deg": dec_deg,
        "alt_deg": alt_deg,
        "az_deg": az_deg,
        "location": {
            "name": first_recursive(location, ["name", "city", "location"]),
            "latitude": latitude,
            "longitude": longitude,
            "height_m": height_m,
        },
        "time": {
            "local": None if local_time is None else str(local_time),
            "utc": None if utc_time is None else str(utc_time),
            "time_zone": first_recursive(time_info, ["timeZone", "timezone"]),
            "gmt_shift": first_recursive(time_info, ["gmtShift", "utcOffset"]),
        },
        "raw_keys": sorted(str(key) for key in info.keys())[:80],
    }


def ping(base_url: str) -> dict[str, Any]:
    normalized = normalize_base_url(base_url)
    status = fetch_json(normalized, "/api/main/status", timeout=2.5)
    return {
        "ok": True,
        "base_url": normalized,
        "status_keys": sorted(status.keys())[:40] if isinstance(status, dict) else [],
    }


def import_selected(base_url: str) -> dict[str, Any]:
    normalized = normalize_base_url(base_url)
    status = fetch_json(normalized, "/api/main/status", timeout=3.0)
    warnings: list[str] = []
    try:
        info = fetch_json(normalized, "/api/objects/info?format=json", timeout=3.0)
    except Exception as exc:
        info = {}
        warnings.append(f"선택 천체 정보 endpoint를 읽지 못했습니다: {type(exc).__name__}")
    if not isinstance(info, dict) or not info or "raw_text" in info:
        fallback = first_recursive(
            status,
            ["selectioninfo", "selectionInfo", "selectedObject", "objectInfo"],
        )
        if isinstance(fallback, dict):
            info = fallback
        else:
            raise ValueError(
                "선택된 천체 정보를 읽지 못했습니다. Stellarium에서 Remote Control을 켜고 천체를 선택하세요."
            )
    result = normalize_selected_object(info, status)
    result.update({"ok": True, "base_url": normalized, "warnings": warnings})
    if result["ra_deg"] is None and result["alt_deg"] is None:
        result["warnings"].append("좌표를 해석하지 못했습니다. Stellarium 선택 정보를 확인하세요.")
    return result
