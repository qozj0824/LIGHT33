# NØXIS v35.10 update-only

Overwrite the matching files in an existing v35.9 project, then commit/push and let Render redeploy.

Main change: APICAM-3 is treated as a sidereally tracked camera. The FITS `LST` is used to rotate reference-epoch detector geometry into local azimuth/altitude at the observation epoch. The v35.9 APICAM same-frame pedestal correction is retained.

Csys remains planning-grade because the four-frame hold-out validates temporal tracking stability, not an independent absolute astrometric solution.
