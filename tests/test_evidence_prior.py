from lightt.evidence import exposure_evidence_prior


def test_exact_messier_match_is_warning_only():
    prior, warnings = exposure_evidence_prior(
        {"name": "M16 (Eagle Nebula)", "object_type": "nebula"}, 30.0
    )
    assert prior["status"] == "ok"
    assert prior["policy"].startswith("warning_only")
    assert prior["source_level"] == "exact_target"
    assert prior["matched_target"] == "M16 (Eagle Nebula)"
    assert prior["subexposure_median_sec"] is not None


def test_class_fallback_never_overrides():
    prior, warnings = exposure_evidence_prior(
        {"name": "Unknown test galaxy", "object_type": "galaxy"}, 30.0
    )
    assert prior["status"] == "ok"
    assert prior["source_level"] == "class_summary"
    assert prior["physics_recommendation_sec"] == 30.0
