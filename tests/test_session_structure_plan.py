from lightt.equipment import EquipmentProfile
from lightt.session import _build_plan


def profile():
    return EquipmentProfile(
        profile_id="test",
        name="test",
        created_at="test",
        gain_e_per_adu=1.0,
        read_noise_e=3.0,
        dark_current_e_per_pix_sec=0.01,
        sensor_clip_adu=60000.0,
        bias_offset_adu=500.0,
        reference_peak_e_per_sec=None,
        confidence="high",
        c_sys=1.0,
        c_sys_quality="good",
    )


def kwargs():
    return dict(
        profile=profile(),
        target={"target_mode": "extended", "name": "M42", "object_type": "nebula"},
        background_rate_adu_per_pix=1.5,
        target_signal_rate_e=30.0,
        effective_pixels=100,
        target_snr=20.0,
        min_sub_exposure_sec=1.0,
        max_sub_exposure_sec=600.0,
        tracking_limit_sec=300.0,
        background_limit_fraction=0.30,
        saturation_safety_fraction=0.80,
        stack_efficiency=0.90,
        max_frames=2000,
        frame_overhead_sec=2.0,
        background_uncertainty_fraction=0.10,
        signal_uncertainty_fraction=0.40,
        target_signal_rate_e_per_pixel=0.30,
    )


def test_structure_uses_faint_zone_for_total_integration_and_bright_zone_for_saturation():
    base = _build_plan(**kwargs(), target_structure_model=None)
    structured = _build_plan(
        **kwargs(),
        target_structure_model={
            "status": "ok",
            "confidence": "high",
            "faint_structure_factor": 0.35,
            "bright_structure_factor": 4.0,
            "science_percentile": 25.0,
            "zones": [
                {"name": "매우 희미", "pixel_fraction": 0.2, "representative_relative_to_mean": 0.25, "percentile_low": 0, "percentile_high": 20},
                {"name": "희미", "pixel_fraction": 0.2, "representative_relative_to_mean": 0.45, "percentile_low": 20, "percentile_high": 40},
                {"name": "중간", "pixel_fraction": 0.2, "representative_relative_to_mean": 0.75, "percentile_low": 40, "percentile_high": 60},
                {"name": "중간-밝음", "pixel_fraction": 0.2, "representative_relative_to_mean": 1.1, "percentile_low": 60, "percentile_high": 80},
                {"name": "밝음", "pixel_fraction": 0.15, "representative_relative_to_mean": 1.8, "percentile_low": 80, "percentile_high": 95},
                {"name": "코어", "pixel_fraction": 0.05, "representative_relative_to_mean": 3.5, "percentile_low": 95, "percentile_high": 100},
            ],
        },
    )
    assert structured["snr_basis"] == "faint_structure_zone"
    assert structured["stack_efficiency_plan"]["structure_aware"] is True
    assert structured["stack_efficiency_plan"]["recommended_science_zone_stack_snr"] < structured["stack_efficiency_plan"]["recommended_mean_stack_snr"]
    assert structured["target_saturation_upper_sec"] < base["target_saturation_upper_sec"]
    # Faint structure must not lengthen the chosen sub-exposure.
    assert structured["recommended_sub_exposure_sec"] <= base["recommended_sub_exposure_sec"]
