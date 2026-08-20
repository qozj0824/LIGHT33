from __future__ import annotations

import pytest

from lightt.io import _check_pixel_count


def test_image_pixel_safety_limit_rejects_absurd_dimensions():
    _check_pixel_count(10_000, 10_000, "test")
    with pytest.raises(ValueError):
        _check_pixel_count(20_000, 20_000, "test")
