from __future__ import annotations

"""Warning-only empirical exposure prior derived from the NØXIS evidence workbook."""

import json
import math
from functools import lru_cache
from pathlib import Path
import re
from typing import Any


def _norm(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (text or "").upper())


def _aliases(name: str | None) -> set[str]:
    text = (name or "").upper()
    out = {_norm(text)}
    # Common Messier/NGC/IC identifiers are the safest cross-catalog keys.
    for prefix, num in re.findall(r"\b(M|NGC|IC)\s*0*(\d{1,4})\b", text):
        out.add(f"{prefix}{int(num)}")
    return {x for x in out if x}


def _class_from_object_type(object_type: str | None, name: str | None = None) -> str | None:
    text = f"{object_type or ''} {name or ''}".casefold()
    if any(x in text for x in ("galaxy", "galax", "은하")): return "galaxy"
    if any(x in text for x in ("cluster", "성단")) and any(x in text for x in ("nebula", "성운")): return "cluster_nebula"
    if any(x in text for x in ("nebula", "성운", "snr", "hii")): return "nebula"
    if any(x in text for x in ("cluster", "성단")): return "cluster"
    if any(x in text for x in ("planet", "moon", "comet", "asteroid", "solar", "행성", "달", "혜성")): return "solar_system"
    if any(x in text for x in ("star", "별", "variable", "double")): return "star"
    return None


@lru_cache(maxsize=1)
def _dataset() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "data" / "exposure_evidence_summary.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"targets": [], "class_summary": {}}


def exposure_evidence_prior(target: dict[str, Any], recommended_sec: float | None) -> tuple[dict[str, Any], list[str]]:
    data = _dataset()
    warnings: list[str] = []
    if recommended_sec is None or recommended_sec <= 0:
        return {"status": "unavailable", "policy": "warning_only"}, warnings
    target_aliases = _aliases(str(target.get("name") or ""))
    exact = None
    for row in data.get("targets", []):
        row_aliases = _aliases(str(row.get("target_name") or "")) | _aliases(str(row.get("target_key") or ""))
        if target_aliases & row_aliases:
            exact = row
            break
    if exact:
        p10 = exact.get("subexposure_p10_sec")
        p50 = exact.get("subexposure_median_sec")
        p90 = exact.get("subexposure_p90_sec")
        source_level = "exact_target"
        support = int(exact.get("standard_exposure_count") or 0)
        target_class = exact.get("target_class")
    else:
        target_class = _class_from_object_type(str(target.get("object_type") or ""), str(target.get("name") or ""))
        row = data.get("class_summary", {}).get(target_class or "", {})
        p10, p50, p90 = row.get("p10_target_median_sec"), row.get("p50_target_median_sec"), row.get("p90_target_median_sec")
        source_level = "class_summary" if p50 is not None else "unavailable"
        support = int(row.get("target_count") or 0)
    comparable = all(isinstance(x, (int, float)) and math.isfinite(float(x)) and float(x) > 0 for x in (p10, p50, p90))
    comparison = "not_comparable"
    if comparable:
        if recommended_sec < float(p10) / 2.0: comparison = "far_below_archive_range"
        elif recommended_sec > float(p90) * 2.0: comparison = "far_above_archive_range"
        elif recommended_sec < float(p10): comparison = "below_archive_range"
        elif recommended_sec > float(p90): comparison = "above_archive_range"
        else: comparison = "within_archive_range"
        if comparison.startswith("far_"):
            warnings.append(
                "물리 기반 추천 단일노출이 공개 관측 아카이브의 경험적 분포와 크게 다릅니다. "
                "아카이브는 장비·필터·연구 목적이 서로 달라 추천값을 덮어쓰지 않으며, 입력 장비값과 포화/배경 조건만 재확인하세요."
            )
    result = {
        "status": "ok" if comparable else "unavailable",
        "policy": "warning_only_prior_never_overrides_physics",
        "source_level": source_level,
        "matched_target": exact.get("target_name") if exact else None,
        "target_class": target_class,
        "support_count": support,
        "subexposure_p10_sec": p10,
        "subexposure_median_sec": p50,
        "subexposure_p90_sec": p90,
        "physics_recommendation_sec": float(recommended_sec),
        "comparison": comparison,
        "comparability_note": "Archive exposures mix instruments, filters and science goals; use only as a sanity-check prior.",
    }
    return result, warnings
