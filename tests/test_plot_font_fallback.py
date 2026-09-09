from __future__ import annotations

import warnings

from lightt import visualization


def test_stack_curve_has_no_hangul_glyph_warning_when_plot_text_falls_back(tmp_path, monkeypatch) -> None:
    # Simulate Render without a Korean Matplotlib font.  Even if tier labels stored
    # in the plan are Korean, all text reaching Matplotlib must use the English
    # fallback so DejaVu Sans does not emit missing-Hangul glyph warnings.
    monkeypatch.setattr(visualization, "plot_text", lambda korean, english: english)
    curve = [
        {"integration_sec": 60.0, "structure_utility": 0.12, "reliable_structure_fraction": 0.08},
        {"integration_sec": 600.0, "structure_utility": 0.35, "reliable_structure_fraction": 0.25},
        {"integration_sec": 3600.0, "structure_utility": 0.62, "reliable_structure_fraction": 0.52},
    ]
    tiers = {
        "quick": {"integration_sec": 60.0, "label": "빠름"},
        "balanced": {"integration_sec": 600.0, "label": "균형"},
        "deep": {"integration_sec": 3600.0, "label": "고품질"},
    }
    output = tmp_path / "stack.png"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        visualization.save_stack_efficiency_curve(curve, tiers, output, selected_mode="balanced")
    assert output.exists()
    assert not [item for item in caught if "Glyph" in str(item.message)]
