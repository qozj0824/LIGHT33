# NØXIS v35.6 — APICAM external-validation support

## Purpose
v35.5 used one global all-sky calibration (`Canon EOS R + Sigma 8 mm`, 6720×4480). A real ESO APICAM 4096×4096 FITS frame therefore could not be used scientifically: its aspect ratio and optical projection differ from the Canon/Sigma calibration.

v35.6 adds a camera-specific directional calibration path for ESO APICAM while keeping the existing Canon/Sigma path unchanged.

## Changes
- Added `config/fisheye_apicam.json` for ESO APICAM-3 (KAF-16803 + Canon 12 mm 180° fisheye).
- Added automatic fisheye-config selection from FITS instrument/file identity. `INSTRUME=APICAM` selects the APICAM model; a generic square frame does not.
- Added `calibrated_camera_model`, using:
  - calibrated detector center,
  - focal scale in px/rad,
  - radial theta polynomial,
  - 3D camera rotation,
  - explicit horizontal mirror handling.
- Added inverse pixel → local ENU → azimuth/altitude transformation.
- Generalized calibrated-image aspect-ratio checking so APICAM may be used at native 4096×4096 or a uniform resize, while non-uniform stretching is rejected.
- Equipment-profile Csys creation, normal session analysis, and legacy pipeline now select the all-sky calibration belonging to the actual camera instead of always loading the Canon/Sigma file.
- Absolute pixel solid-angle photometry remains limited to the existing strictly validated Kannala–Brandt path. APICAM is used for directional background lookup only.

## APICAM calibration status
The bundled APICAM model was fit on the real ESO frame `APICAM.2018-06-14T04:00:23.000` using bright-star detector positions. The 36 same-frame fit matches have median nearest-star residual ≈0.94 px and RMS ≈1.01 px after robust fitting. This is **not an independent hold-out validation**, so NØXIS intentionally reports APICAM directional/Csys output as planning-grade until the external FORS2/APICAM validation is completed.

## Validation performed for this build
- APICAM config auto-selection: pass.
- Non-APICAM 4096×4096 image does not inherit APICAM calibration: pass.
- Six bright-star direction cross-checks: <0.15° in azimuth/altitude components: pass.
- Uniform 1/2 resize preserves mapped direction numerically: pass.
- Wrong APICAM aspect ratio is rejected: pass.
- Real 4096×4096 APICAM frame smoke test: sky map, target-direction background, coordinate overlay and diagnostics generated without the v35.5 aspect-ratio failure.
- Full local suite: 64 passed, 4 skipped because this container does not have Astropy installed. Render dependencies include Astropy/Photutils via `requirements.txt`.

## External-validation pair now supported
- Telescope: `FORS2.2018-06-14T04:00:19.101`, NGC 6218, ~110 s.
- All-sky: `APICAM.2018-06-14T04:00:23.000`, 120 s.
- Exposure start difference: 3.9 s.

Use this pair to create the ESO FORS2 equipment profile. Because the APICAM directional solution is still marked planning-grade and no all-sky flat is supplied, report Csys/exposure results as external-validation measurements, not as a finished absolute calibration claim.
