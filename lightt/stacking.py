from __future__ import annotations

"""Structure-aware stack-efficiency planning.

This module intentionally does *not* solve for a user-selected target SNR.
Instead it models how much useful target structure is recovered as integration
increases, then finds diminishing-return points on that information curve.
"""

import math
from typing import Any

import numpy as np

STACK_MODES = ("quick", "balanced", "deep", "very_deep")
_MODE_SLOPE_FRACTIONS = {
    "quick": 1.00,
    "balanced": 0.65,
    "deep": 0.35,
    "very_deep": 0.18,
}
_MODE_LABELS = {
    "quick": "빠름",
    "balanced": "균형",
    "deep": "고품질",
    "very_deep": "매우 깊게",
}

# A fixed soft information scale, not a user target.  For u=1-exp(-SNR/5),
# the log-time information-gain slope peaks near SNR~5 for a single zone.
_INFORMATION_SNR_SCALE = 5.0


def _snr_for_exposure(
    exposure_sec: float,
    signal_rate_e: float,
    background_rate_e_per_pix: float,
    dark_current_e_per_pix_sec: float,
    read_noise_e: float,
    effective_pixels: int,
) -> float:
    if exposure_sec <= 0 or signal_rate_e <= 0:
        return 0.0
    signal = signal_rate_e * exposure_sec
    variance = signal + effective_pixels * (
        (max(background_rate_e_per_pix, 0.0) + max(dark_current_e_per_pix_sec, 0.0)) * exposure_sec
        + max(read_noise_e, 0.0) ** 2
    )
    return signal / math.sqrt(max(variance, 1e-18))


def _zone_model(structure_model: dict[str, Any] | None) -> tuple[list[dict[str, float | str]], bool]:
    model = structure_model or {}
    reliable = model.get("status") == "ok" and model.get("confidence") in {"high", "medium"}
    zones: list[dict[str, float | str]] = []
    if reliable:
        for raw in model.get("zones") or []:
            try:
                factor = float(raw.get("representative_relative_to_mean"))
                weight = float(raw.get("pixel_fraction"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not (math.isfinite(factor) and factor > 0 and math.isfinite(weight) and weight > 0):
                continue
            zones.append(
                {
                    "name": str(raw.get("name") or "구역"),
                    "factor": float(np.clip(factor, 0.02, 30.0)),
                    "weight": weight,
                    "percentile_low": float(raw.get("percentile_low") or 0.0),
                    "percentile_high": float(raw.get("percentile_high") or 100.0),
                }
            )
    if not zones:
        return [{"name": "평균 구조", "factor": 1.0, "weight": 1.0, "percentile_low": 0.0, "percentile_high": 100.0}], False
    total = sum(float(item["weight"]) for item in zones)
    if total <= 0:
        return [{"name": "평균 구조", "factor": 1.0, "weight": 1.0, "percentile_low": 0.0, "percentile_high": 100.0}], False
    for item in zones:
        item["weight"] = float(item["weight"]) / total
    return zones, True


def _first_descending_fraction(slope: np.ndarray, peak_idx: int, peak: float, fraction: float) -> tuple[int, bool]:
    if fraction >= 0.999:
        return peak_idx, peak_idx >= len(slope) - 1
    threshold = peak * fraction
    for idx in range(peak_idx + 1, len(slope)):
        if slope[idx] <= threshold:
            return idx, False
    return len(slope) - 1, True


def _sample_indices(length: int, max_points: int = 180) -> np.ndarray:
    if length <= max_points:
        return np.arange(length, dtype=int)
    raw = np.geomspace(1, length, max_points)
    indices = np.unique(np.clip(np.rint(raw).astype(int) - 1, 0, length - 1))
    if indices[-1] != length - 1:
        indices = np.append(indices, length - 1)
    return indices


def build_stack_efficiency_plan(
    *,
    sub_exposure_sec: float,
    mean_signal_rate_e: float,
    background_rate_e_per_pix: float,
    dark_current_e_per_pix_sec: float,
    read_noise_e: float,
    effective_pixels: int,
    stack_efficiency: float,
    frame_overhead_sec: float,
    max_frames: int,
    max_stack_hours: float,
    mode: str = "balanced",
    structure_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a finite-horizon, diminishing-return stack plan.

    The recommendation is a *knee on useful-structure information gain*, not an
    exposure required to hit a requested SNR.  Very faint targets therefore do
    not create absurd unbounded integration times: if the useful-information
    curve is still improving at the user's planning horizon, the result is marked
    horizon-limited instead of extrapolating indefinitely.
    """
    if mode not in STACK_MODES:
        mode = "balanced"
    if sub_exposure_sec <= 0 or mean_signal_rate_e <= 0:
        return {
            "status": "unavailable",
            "mode": mode,
            "mode_label": _MODE_LABELS[mode],
            "reason": "대상 신호 또는 단일노출 값이 없어 스택 효율을 계산할 수 없습니다.",
        }

    stack_efficiency = float(np.clip(stack_efficiency, 0.30, 1.0))
    max_frames = max(1, int(max_frames))
    max_stack_hours = float(np.clip(max_stack_hours, 0.05, 24.0 * 14.0))
    cycle_sec = max(sub_exposure_sec + max(frame_overhead_sec, 0.0), 1e-9)
    horizon_frames = max(1, int(math.floor(max_stack_hours * 3600.0 / cycle_sec)))
    frame_cap = max(1, min(max_frames, horizon_frames))
    frames = np.arange(1, frame_cap + 1, dtype=float)
    integration_sec = frames * sub_exposure_sec
    elapsed_sec = frames * cycle_sec

    zones, structure_aware = _zone_model(structure_model)
    zone_single_snr: list[float] = []
    zone_weights: list[float] = []
    for zone in zones:
        zone_single_snr.append(
            _snr_for_exposure(
                sub_exposure_sec,
                mean_signal_rate_e * float(zone["factor"]),
                background_rate_e_per_pix,
                dark_current_e_per_pix_sec,
                read_noise_e,
                effective_pixels,
            )
        )
        zone_weights.append(float(zone["weight"]))
    single = np.asarray(zone_single_snr, dtype=float)
    weights = np.asarray(zone_weights, dtype=float)
    weights /= max(float(np.sum(weights)), 1e-12)

    stacked = stack_efficiency * np.sqrt(frames)[:, None] * single[None, :]
    utility_zone = 1.0 - np.exp(-stacked / _INFORMATION_SNR_SCALE)
    utility = np.sum(utility_zone * weights[None, :], axis=1)
    detected_coverage = np.sum((stacked >= 3.0) * weights[None, :], axis=1)
    reliable_coverage = np.sum((stacked >= 5.0) * weights[None, :], axis=1)
    detailed_coverage = np.sum((stacked >= 10.0) * weights[None, :], axis=1)

    mean_single_snr = _snr_for_exposure(
        sub_exposure_sec,
        mean_signal_rate_e,
        background_rate_e_per_pix,
        dark_current_e_per_pix_sec,
        read_noise_e,
        effective_pixels,
    )
    mean_stack_snr = mean_single_snr * stack_efficiency * np.sqrt(frames)

    science_factor = 1.0
    if structure_aware:
        try:
            science_factor = float(structure_model.get("faint_structure_factor") or 1.0)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            science_factor = 1.0
        science_factor = float(np.clip(science_factor, 0.02, 1.0))
    science_single_snr = _snr_for_exposure(
        sub_exposure_sec,
        mean_signal_rate_e * science_factor,
        background_rate_e_per_pix,
        dark_current_e_per_pix_sec,
        read_noise_e,
        effective_pixels,
    )
    science_stack_snr = science_single_snr * stack_efficiency * np.sqrt(frames)

    if frame_cap >= 3:
        log_time = np.log(np.maximum(elapsed_sec, 1e-9))
        slope = np.gradient(utility, log_time)
        # A light smoothing prevents one quantized coverage transition from
        # dominating the selected knee while preserving broad structure.
        if frame_cap >= 7:
            kernel = np.ones(5, dtype=float) / 5.0
            padded = np.pad(slope, (2, 2), mode="edge")
            slope = np.convolve(padded, kernel, mode="valid")
    elif frame_cap == 2:
        slope = np.array([utility[1] - utility[0], utility[1] - utility[0]], dtype=float)
    else:
        slope = np.array([0.0], dtype=float)

    peak_idx = int(np.nanargmax(slope)) if slope.size else 0
    peak_slope = max(float(slope[peak_idx]), 0.0)
    tier_indices: dict[str, tuple[int, bool]] = {}
    if peak_slope <= 1e-12:
        tier_indices = {key: (frame_cap - 1, True) for key in STACK_MODES}
    else:
        for key, fraction in _MODE_SLOPE_FRACTIONS.items():
            tier_indices[key] = _first_descending_fraction(slope, peak_idx, peak_slope, fraction)

    def point(idx: int) -> dict[str, Any]:
        idx = int(np.clip(idx, 0, frame_cap - 1))
        n = idx + 1
        return {
            "frames": n,
            "integration_sec": float(integration_sec[idx]),
            "elapsed_sec": float(elapsed_sec[idx]),
            "mean_stack_snr": float(mean_stack_snr[idx]),
            "science_zone_stack_snr": float(science_stack_snr[idx]),
            "structure_utility": float(utility[idx]),
            "detected_structure_fraction": float(detected_coverage[idx]),
            "reliable_structure_fraction": float(reliable_coverage[idx]),
            "detailed_structure_fraction": float(detailed_coverage[idx]),
            "information_gain_slope": float(slope[idx]),
        }

    time_cap_active = horizon_frames <= max_frames
    frame_cap_active = max_frames <= horizon_frames

    def limit_reason(limited: bool) -> str | None:
        if not limited:
            return None
        if time_cap_active and frame_cap_active:
            return "time_horizon_and_max_frames"
        if time_cap_active:
            return "time_horizon"
        return "max_frames"

    tiers: dict[str, Any] = {}
    for key in STACK_MODES:
        idx, limited = tier_indices[key]
        payload = point(idx)
        reason = limit_reason(bool(limited))
        payload.update(
            {
                "mode": key,
                "label": _MODE_LABELS[key],
                "slope_fraction_of_peak": _MODE_SLOPE_FRACTIONS[key],
                "cap_limited": bool(limited),
                "cap_limit_reason": reason,
                "horizon_limited": bool(limited and time_cap_active),
                "max_frames_limited": bool(limited and frame_cap_active),
            }
        )
        tiers[key] = payload

    rec_idx, rec_limited = tier_indices[mode]
    rec_limit_reason = limit_reason(bool(rec_limited))
    recommended = point(rec_idx)
    one_hour_frames = max(1, int(round(3600.0 / cycle_sec)))
    future_idx = rec_idx + one_hour_frames
    if future_idx <= frame_cap - 1:
        additional_hour_utility_gain: float | None = max(0.0, float(utility[future_idx] - utility[rec_idx]))
        additional_hour_science_snr_gain: float | None = max(0.0, float(science_stack_snr[future_idx] - science_stack_snr[rec_idx]))
    else:
        additional_hour_utility_gain = None
        additional_hour_science_snr_gain = None
    previous_idx = max(0, rec_idx - one_hour_frames)
    recent_hour_utility_gain = max(0.0, float(utility[rec_idx] - utility[previous_idx]))
    recent_hour_science_snr_gain = max(0.0, float(science_stack_snr[rec_idx] - science_stack_snr[previous_idx]))

    sample = _sample_indices(frame_cap)
    curve = [
        {
            "frames": int(i + 1),
            "integration_sec": float(integration_sec[i]),
            "elapsed_sec": float(elapsed_sec[i]),
            "structure_utility": float(utility[i]),
            "detected_structure_fraction": float(detected_coverage[i]),
            "reliable_structure_fraction": float(reliable_coverage[i]),
            "detailed_structure_fraction": float(detailed_coverage[i]),
            "mean_stack_snr": float(mean_stack_snr[i]),
            "science_zone_stack_snr": float(science_stack_snr[i]),
            "information_gain_slope": float(slope[i]),
        }
        for i in sample
    ]

    zone_summary = []
    for index, zone in enumerate(zones):
        zone_summary.append(
            {
                **zone,
                "single_frame_snr": float(single[index]),
                "recommended_stack_snr": float(stacked[rec_idx, index]),
            }
        )

    still_rising = peak_idx >= frame_cap - 2
    return {
        "status": "ok",
        "mode": mode,
        "mode_label": _MODE_LABELS[mode],
        "selection_policy": "post-peak decline of useful-structure information gain",
        "information_utility": "weighted mean of 1-exp(-SNR/5) across brightness zones",
        "information_snr_scale": _INFORMATION_SNR_SCALE,
        "structure_aware": structure_aware,
        "science_zone_factor": science_factor,
        "recommended_frames": int(recommended["frames"]),
        "recommended_integration_sec": float(recommended["integration_sec"]),
        "recommended_elapsed_sec": float(recommended["elapsed_sec"]),
        "recommended_structure_utility": float(recommended["structure_utility"]),
        "recommended_detected_structure_fraction": float(recommended["detected_structure_fraction"]),
        "recommended_reliable_structure_fraction": float(recommended["reliable_structure_fraction"]),
        "recommended_detailed_structure_fraction": float(recommended["detailed_structure_fraction"]),
        "recommended_mean_stack_snr": float(recommended["mean_stack_snr"]),
        "recommended_science_zone_stack_snr": float(recommended["science_zone_stack_snr"]),
        "additional_hour_structure_utility_gain": additional_hour_utility_gain,
        "additional_hour_science_snr_gain": additional_hour_science_snr_gain,
        "recent_hour_structure_utility_gain": recent_hour_utility_gain,
        "recent_hour_science_snr_gain": recent_hour_science_snr_gain,
        "cap_limited": bool(rec_limited),
        "cap_limit_reason": rec_limit_reason,
        "horizon_limited": bool(rec_limited and time_cap_active),
        "max_frames_limited": bool(rec_limited and frame_cap_active),
        "curve_still_rising_at_horizon": bool(rec_limited and time_cap_active and still_rising),
        "max_stack_hours": max_stack_hours,
        "frame_cap": frame_cap,
        "max_frames_cap": max_frames,
        "time_horizon_frame_cap": horizon_frames,
        "peak_efficiency_frame": peak_idx + 1,
        "peak_efficiency_integration_sec": float(integration_sec[peak_idx]),
        "peak_information_gain_slope": peak_slope,
        "tiers": tiers,
        "zones": zone_summary,
        "curve": curve,
    }
