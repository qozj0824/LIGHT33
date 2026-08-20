from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lightt.models import AnalysisSettings, CalibrationSet  # noqa: E402
from lightt.pipeline import run_analysis  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a reproducible LIGHTT observation validation")
    parser.add_argument("--allsky", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--exposure", type=float, required=True)
    parser.add_argument(
        "--target-roi", required=True, help='Normalized JSON: {"x":...,"y":...,"w":...,"h":...}'
    )
    parser.add_argument("--background-roi", required=True)
    parser.add_argument("--sensor-clip", type=float, default=65535)
    parser.add_argument("--target-snr", type=float, default=150)
    args = parser.parse_args()
    settings = AnalysisSettings(
        current_exposure_sec=args.exposure,
        exposure_mode="manual",
        target_snr=args.target_snr,
        target_mode="extended",
        target_roi_json=args.target_roi,
        background_roi_json=args.background_roi,
        auto_roi=False,
        sensor_clip_adu=args.sensor_clip,
    )
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        result = run_analysis(
            allsky_path=args.allsky,
            scope_path=args.scope,
            settings=settings,
            allsky_calibration=CalibrationSet(),
            scope_calibration=CalibrationSet(),
            project_root=root,
            result_root=Path(temporary),
        )
        summary = {
            "current_snr": result.measurement.current_snr,
            "signal_adu_per_pixel": result.measurement.signal_adu_per_pixel,
            "background_adu_per_pixel": result.measurement.background_adu_per_pixel,
            "sensor_clip_adu": result.intensity_domain.sensor_clip_adu,
            "saturation_upper_sec": result.plan.saturation_upper_sec,
            "recommended_sub_exposure_sec": result.plan.recommended_sub_exposure_sec,
            "frames": result.plan.frames,
            "total_integration_sec": result.plan.total_integration_sec,
            "validity": result.validity,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
