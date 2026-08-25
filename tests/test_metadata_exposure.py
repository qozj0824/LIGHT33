from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from lightt.io import load_image
from lightt.validation import assess_image_input


def _write_fits(path, **headers):
    hdu = fits.PrimaryHDU((1000 + np.arange(40 * 48).reshape(40, 48) % 37).astype(np.int16))
    for key, value in headers.items():
        hdu.header[key] = value
    hdu.writeto(path)


def test_standard_exptime_is_selected_and_cross_checked(tmp_path):
    path = tmp_path / "standard.fits"
    _write_fits(
        path,
        EXPTIME=20.0,
        DIT=5.0,
        NDIT=4,
    )
    frame = load_image(path)
    provenance = frame.metadata.extra["exposure_provenance"]
    assert frame.metadata.exposure_sec == 20.0
    assert provenance["selected_key"] == "EXPTIME"
    assert provenance["confidence"] == "high"
    assert provenance["conflicts"] == []


def test_eso_dit_times_ndit_is_derived_when_exptime_is_missing(tmp_path):
    path = tmp_path / "eso.fits"
    _write_fits(path, DIT=2.5, NDIT=8)
    frame = load_image(path)
    provenance = frame.metadata.extra["exposure_provenance"]
    assert frame.metadata.exposure_sec == 20.0
    assert provenance["selection_rule"] == "detector_integration_times_count"
    assert provenance["confidence"] == "medium"


def test_explicit_millisecond_header_is_converted_to_seconds(tmp_path):
    path = tmp_path / "milliseconds.fits"
    _write_fits(path, EXPTIME_MS=250)
    frame = load_image(path)
    assert frame.metadata.exposure_sec == 0.25
    assert frame.metadata.extra["exposure_provenance"]["selected_key"] == "EXPTIME_MS"


def test_conflicting_exposure_headers_are_visible_to_assessment(tmp_path):
    path = tmp_path / "conflict.fits"
    _write_fits(path, EXPTIME=10.0, DIT=5.0, NDIT=4)
    frame = load_image(path)
    provenance = frame.metadata.extra["exposure_provenance"]
    assert frame.metadata.exposure_sec == 10.0
    assert provenance["confidence"] == "low"
    assert provenance["conflicts"]
    assessment = assess_image_input(frame, role="scope")
    assert assessment["status"] == "usable_with_fallbacks"
    assert any("헤더 항목" in warning for warning in assessment["warnings"])


def test_mean_stack_keeps_per_frame_and_reports_total_integration(tmp_path):
    path = tmp_path / "mean-stack.fits"
    _write_fits(path, EXPTIME=30.0, STACKCNT=4, COMBINE="MEAN")
    frame = load_image(path)
    assert frame.metadata.exposure_sec == 30.0
    assert frame.metadata.effective_exposure_sec == 120.0
    assert frame.metadata.stack_count == 4
    assert frame.metadata.stack_method == "mean"


def test_exif_rational_exposure_is_read_as_seconds(tmp_path):
    path = tmp_path / "camera.jpg"
    image = Image.new("RGB", (48, 40), (20, 30, 40))
    exif = image.getexif()
    exif[33434] = IFDRational(1, 125)
    image.save(path, exif=exif)
    frame = load_image(path)
    assert frame.metadata.exposure_sec is not None
    assert abs(frame.metadata.exposure_sec - 0.008) < 1e-9
    assert frame.metadata.extra["exposure_provenance"]["selected_key"] == "EXIF ExposureTime"


def test_wcs_center_uses_only_celestial_cards_from_observatory_header(tmp_path):
    path = tmp_path / "observatory.fits"
    hdu = fits.PrimaryHDU(np.ones((80, 100), dtype=np.int16))
    hdu.header["EXPTIME"] = 120.0
    hdu.header["CTYPE1"] = "RA---TAN"
    hdu.header["CTYPE2"] = "DEC--TAN"
    hdu.header["CRPIX1"] = 50.5
    hdu.header["CRPIX2"] = 40.5
    hdu.header["CRVAL1"] = 251.7349
    hdu.header["CRVAL2"] = -2.0181
    hdu.header["CD1_1"] = -0.0001
    hdu.header["CD1_2"] = 0.0
    hdu.header["CD2_1"] = 0.0
    hdu.header["CD2_2"] = 0.0001
    for index in range(150):
        hdu.header[f"HIERARCH ESO INS TEST{index}"] = (float(index), "unrelated instrument card")
    hdu.writeto(path)

    frame = load_image(path)
    assert frame.metadata.exposure_sec == 120.0
    assert frame.metadata.extra["wcs_center_ra_deg"] == pytest.approx(251.7349, abs=1e-6)
    assert frame.metadata.extra["wcs_center_dec_deg"] == pytest.approx(-2.0181, abs=1e-6)
