# NØXIS v35.10 — APICAM sidereal tracking geometry

## Why this patch exists
ESO documents APICAM-3 as mounted on a tracking station so stars are not trailed. The v35.6-v35.9 APICAM camera model treated its fitted rotation as permanently fixed to local ENU. That is correct only at the reference epoch and becomes wrong when the same detector model is reused at a different sidereal time.

## Changes
- The APICAM rotation is now anchored to the reference frame `APICAM.2018-06-14T04:00:23.000`.
- FITS `LST` is preserved as `metadata.extra.local_sidereal_time_sec`.
- For `tracking_mode = sidereal`, detector rays are converted from reference-epoch ENU to declination/hour-angle and advanced by the observation LST before azimuth/altitude binning.
- Non-APICAM fisheye models are unchanged.
- The v35.9 in-frame APICAM pedestal estimator is unchanged.

## Independent archive temporal hold-out
Four frames not used in the original camera-model fit were checked: 03:57:55, 04:02:50, 04:05:18 and 04:07:45 UTC on 2018-06-14. Ten bright stellar centroids were tracked in every frame. Relative to the 04:00:23 reference detector positions:

| Frame | RMS drift | Max drift |
|---|---:|---:|
| 03:57:55 | 1.52 px | 1.82 px |
| 04:02:50 | 0.91 px | 1.05 px |
| 04:05:18 | 1.65 px | 1.91 px |
| 04:07:45 | 2.65 px | 2.94 px |

The outer-field pedestal median was 548 ADU in the reference frame and all four hold-outs. This strongly validates the frame-local pedestal method and confirms the expected tracking behavior. It does **not** independently validate absolute sky coordinates, because the absolute optical solution is still based on the original 04:00:23 fit. Therefore `validation_star_count` remains 0 and Csys directional quality remains `planning`.

## Regression tests
The existing suite remains green in the build environment. FITS/Astropy-dependent tests are skipped when Astropy is unavailable.
