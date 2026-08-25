# NØXIS v35.7 — Render memory-safe 4k all-sky analysis

## Fix
- 4096×4096 APICAM/ALPACA frames now use an 600 px working grid before expensive coordinate transforms.
- Smaller all-sky inputs retain the previous 1200 px working grid.
- DAOStarFinder masking no longer allocates full-frame X/Y index arrays; each star uses a tiny local ogrid.
- Equipment-profile and session analysis compact the all-sky frame and release the original 4k array before coordinate transforms.
- This targets Render worker restarts during equipment-profile creation with a 4k all-sky FITS.

## Scientific behavior
- The persisted sky product is 72 azimuth bins × 18 altitude bins. An 600 px working grid still oversamples the output cells by a wide margin.
- Detector-coordinate scaling is preserved, so the APICAM camera calibration still evaluates coordinates in the original 4096×4096 detector system.
- No change was made to the FORS2 photometry, Csys formula, exposure-planning equations, or APICAM calibration parameters.
