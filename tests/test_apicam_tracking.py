import math
import numpy as np

from lightt.geometry import pixel_to_altaz
from lightt.models import FisheyeConfig


def _local_to_dec_h(az_deg: float, alt_deg: float, lat_deg: float) -> tuple[float, float]:
    az = math.radians(az_deg)
    alt = math.radians(alt_deg)
    phi = math.radians(lat_deg)
    east = math.cos(alt) * math.sin(az)
    north = math.cos(alt) * math.cos(az)
    up = math.sin(alt)
    dec = math.asin(max(-1.0, min(1.0, north * math.cos(phi) + up * math.sin(phi))))
    hour_angle = math.atan2(-east, up * math.cos(phi) - north * math.sin(phi))
    return dec, hour_angle


def test_sidereal_tracking_advances_hour_angle_but_preserves_declination():
    cfg = FisheyeConfig(
        mode="calibrated_camera_model",
        center_x=1000.0,
        center_y=1000.0,
        sensor_width=2000,
        sensor_height=2000,
        focal_length_px=650.0,
        rotation_vector=[0.08, -0.03, 0.15],
        radial_theta_coefficients=[-0.02, 0.001],
        mirror_x=True,
        tracking_mode="sidereal",
        tracking_reference_lst_sec=20000.0,
        tracking_site_latitude_deg=-24.6276,
    )
    x = np.array([500.0, 1100.0, 1500.0])
    y = np.array([700.0, 900.0, 1300.0])
    az0, alt0, valid0 = pixel_to_altaz(
        x, y, (2000, 2000), cfg,
        observation_lst_sec=20000.0,
        site_latitude_deg=-24.6276,
    )
    az1, alt1, valid1 = pixel_to_altaz(
        x, y, (2000, 2000), cfg,
        observation_lst_sec=20360.0,
        site_latitude_deg=-24.6276,
    )
    assert np.all(valid0 & valid1)
    delta_expected = 360.0 * 2.0 * math.pi / 86400.0
    for a0, h0, a1, h1 in zip(az0, alt0, az1, alt1, strict=True):
        dec0, H0 = _local_to_dec_h(float(a0), float(h0), -24.6276)
        dec1, H1 = _local_to_dec_h(float(a1), float(h1), -24.6276)
        assert abs(dec1 - dec0) < 1e-10
        delta = math.atan2(math.sin(H1 - H0), math.cos(H1 - H0))
        assert abs(delta - delta_expected) < 1e-10


def test_reference_lst_matches_reference_epoch_geometry():
    cfg = FisheyeConfig(
        mode="calibrated_camera_model",
        center_x=100.0,
        center_y=100.0,
        focal_length_px=80.0,
        rotation_vector=[0.02, 0.04, -0.1],
        radial_theta_coefficients=[-0.01],
        tracking_mode="sidereal",
        tracking_reference_lst_sec=12345.0,
        tracking_site_latitude_deg=-24.6,
    )
    x = np.array([90.0, 120.0])
    y = np.array([80.0, 105.0])
    az_plain, alt_plain, _ = pixel_to_altaz(x, y, (200, 200), cfg)
    az_ref, alt_ref, _ = pixel_to_altaz(
        x, y, (200, 200), cfg,
        observation_lst_sec=12345.0,
        site_latitude_deg=-24.6,
    )
    assert np.allclose(az_plain, az_ref, atol=1e-10)
    assert np.allclose(alt_plain, alt_ref, atol=1e-10)
