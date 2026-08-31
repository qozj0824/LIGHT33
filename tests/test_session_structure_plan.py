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
        },
    )
    assert structured["snr_basis"] == "faint_structure_zone"
    assert structured["required_frames_unbounded"] > structured["required_frames_mean_target"]
    assert structured["target_saturation_upper_sec"] < base["target_saturation_upper_sec"]
    # Faint structure must not lengthen the chosen sub-exposure.
    assert structured["recommended_sub_exposure_sec"] <= base["recommended_sub_exposure_sec"]
