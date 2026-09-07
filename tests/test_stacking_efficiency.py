from __future__ import annotations

from lightt.stacking import build_stack_efficiency_plan


def structure_model():
    return {
        "status": "ok",
        "confidence": "high",
        "faint_structure_factor": 0.35,
        "zones": [
            {"name": "매우 희미", "pixel_fraction": 0.20, "representative_relative_to_mean": 0.25, "percentile_low": 0, "percentile_high": 20},
            {"name": "희미", "pixel_fraction": 0.20, "representative_relative_to_mean": 0.45, "percentile_low": 20, "percentile_high": 40},
            {"name": "중간", "pixel_fraction": 0.20, "representative_relative_to_mean": 0.75, "percentile_low": 40, "percentile_high": 60},
            {"name": "중간-밝음", "pixel_fraction": 0.20, "representative_relative_to_mean": 1.10, "percentile_low": 60, "percentile_high": 80},
            {"name": "밝음", "pixel_fraction": 0.15, "representative_relative_to_mean": 1.80, "percentile_low": 80, "percentile_high": 95},
            {"name": "코어", "pixel_fraction": 0.05, "representative_relative_to_mean": 3.50, "percentile_low": 95, "percentile_high": 100},
        ],
    }


def plan(signal=5.0, mode="balanced", hours=12.0):
    return build_stack_efficiency_plan(
        sub_exposure_sec=80.0,
        mean_signal_rate_e=signal,
        background_rate_e_per_pix=1.5,
        dark_current_e_per_pix_sec=0.01,
        read_noise_e=3.0,
        effective_pixels=100,
        stack_efficiency=0.90,
        frame_overhead_sec=2.0,
        max_frames=2000,
        max_stack_hours=hours,
        mode=mode,
        structure_model=structure_model(),
    )


def test_modes_are_ordered_by_diminishing_return_depth():
    quick = plan(mode="quick")
    balanced = plan(mode="balanced")
    deep = plan(mode="deep")
    very_deep = plan(mode="very_deep")
    assert quick["recommended_frames"] <= balanced["recommended_frames"] <= deep["recommended_frames"] <= very_deep["recommended_frames"]
    assert balanced["recommended_structure_utility"] >= quick["recommended_structure_utility"]


def test_faint_target_is_capped_by_finite_horizon_not_unbounded_target_snr():
    result = plan(signal=0.2, mode="balanced", hours=6.0)
    assert result["status"] == "ok"
    assert result["horizon_limited"] is True
    assert result["recommended_elapsed_sec"] <= 6.0 * 3600.0 + 1e-6
    assert result["curve_still_rising_at_horizon"] is True


def test_structure_zones_contribute_separately():
    result = plan(signal=5.0)
    assert result["structure_aware"] is True
    assert len(result["zones"]) == 6
    assert result["zones"][0]["recommended_stack_snr"] < result["zones"][-1]["recommended_stack_snr"]
    assert 0.0 <= result["recommended_reliable_structure_fraction"] <= 1.0


def test_frame_cap_is_not_mislabeled_as_time_horizon():
    result = build_stack_efficiency_plan(
        sub_exposure_sec=1.0,
        mean_signal_rate_e=0.02,
        background_rate_e_per_pix=1.5,
        dark_current_e_per_pix_sec=0.01,
        read_noise_e=3.0,
        effective_pixels=100,
        stack_efficiency=0.90,
        frame_overhead_sec=0.0,
        max_frames=5,
        max_stack_hours=12.0,
        mode="balanced",
        structure_model=structure_model(),
    )
    assert result["cap_limited"] is True
    assert result["cap_limit_reason"] == "max_frames"
    assert result["max_frames_limited"] is True
    assert result["horizon_limited"] is False
