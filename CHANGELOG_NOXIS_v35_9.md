# NØXIS v35.9

## APICAM same-frame pedestal calibration

- Adds an APICAM-only fallback that estimates the combined bias + dark pedestal from the optically dark detector area outside the calibrated 180° fisheye horizon circle.
- The fallback is used only when no explicit all-sky bias/offset calibration is available and no flat has already been applied.
- The estimator records sample count, horizon/cutoff radius and robust percentiles in profile/result JSON.
- Equipment-profile Csys and normal analysis use the same frame-local pedestal logic.
- Existing Canon EOS R + Sigma 8 mm processing is unchanged.

This does **not** promote APICAM directional calibration to independently validated status; Csys remains planning until hold-out directional validation is completed.
