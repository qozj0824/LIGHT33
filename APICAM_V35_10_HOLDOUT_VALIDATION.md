# APICAM v35.10 archive hold-out validation

Reference frame: `APICAM.2018-06-14T04:00:23.000`

Independent archive frames:
- `APICAM.2018-06-14T03:57:55.000`
- `APICAM.2018-06-14T04:02:50.000`
- `APICAM.2018-06-14T04:05:18.000`
- `APICAM.2018-06-14T04:07:45.000`

## Same-frame pedestal robustness

The v35.9/v35.10 APICAM outer-field mask selected 4,494,653 detector pixels in every frame. The robust median pedestal was exactly 548 ADU in the reference plus all four hold-outs. The 5th-95th percentile widths were 30-31 ADU.

## Tracking behavior

Ten bright stellar centroids were followed in all four hold-out frames without re-fitting the APICAM optical model. Relative detector drift from the 04:00:23 reference was:

| UTC frame | Median drift | RMS drift | Maximum drift |
|---|---:|---:|---:|
| 03:57:55 | 1.54 px | 1.52 px | 1.82 px |
| 04:02:50 | 0.94 px | 0.91 px | 1.05 px |
| 04:05:18 | 1.66 px | 1.65 px | 1.91 px |
| 04:07:45 | 2.69 px | 2.65 px | 2.94 px |

This is consistent with ESO's description of APICAM-3 as mounted on a tracking station. It validates the temporal tracking behavior and the stability of the frame-local pedestal estimator.

## Important limitation

The original absolute APICAM camera solution was fitted on the 04:00:23 frame itself. These four frames therefore do not constitute an independent **absolute astrometric** hold-out. v35.10 fixes the time-dependent geometry architecture, but intentionally keeps `validation_star_count = 0` and Csys direction quality at `planning` until an independent absolute sky-coordinate validation is performed.
