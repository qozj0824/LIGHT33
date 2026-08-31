import numpy as np
from scipy import ndimage

from lightt.target_structure import analyze_relative_structure


def synthetic_nebula(size=320):
    y, x = np.indices((size, size), dtype=float)
    c = (size - 1) / 2
    r2 = ((x - c) / 55.0) ** 2 + ((y - c) / 38.0) ** 2
    core = 120.0 * np.exp(-0.5 * r2)
    halo = 24.0 * np.exp(-0.5 * (((x - c) / 100.0) ** 2 + ((y - c) / 80.0) ** 2))
    image = 1000.0 + core + halo
    # Compact field-star spikes should be masked rather than define the target core.
    for sx, sy, amp in [(180, 150, 1400), (118, 190, 1000), (230, 205, 800)]:
        image[sy, sx] += amp
    image = ndimage.gaussian_filter(image, 0.45)
    rng = np.random.default_rng(42)
    image += rng.normal(0, 1.8, image.shape)
    return image


def test_structure_profile_finds_faint_and_bright_zones():
    profile = analyze_relative_structure(
        synthetic_nebula(), target_diameter_deg=0.8, fov_deg=2.08, survey="synthetic"
    )
    assert profile.status == "ok"
    assert profile.confidence in {"high", "medium"}
    assert 0.03 <= profile.faint_structure_factor < 1.0
    assert profile.bright_structure_factor > 1.0
    assert profile.dynamic_range_factor > 1.0
    assert len(profile.zones) >= 5
    reps = [z.representative_relative_to_mean for z in profile.zones]
    assert reps == sorted(reps)
    assert profile.star_mask_fraction < 0.15


def test_structure_profile_rejects_flat_background():
    rng = np.random.default_rng(2)
    image = 1000 + rng.normal(0, 1, (160, 160))
    profile = analyze_relative_structure(image, target_diameter_deg=0.4, fov_deg=1.0)
    assert profile.status == "unavailable"
