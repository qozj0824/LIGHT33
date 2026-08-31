# NØXIS v37.0 변경 내역

## 핵심: Structure-aware exposure planner

- 고정 심우주 대상의 RA/Dec와 각크기를 이용해 CDS HiPS2FITS `CDS/P/DSS2/red` 320×320 FITS cutout을 선택적으로 조회합니다.
- survey 영상은 **상대 형태만** 사용합니다. DSS count를 사용자 카메라 e-/s로 변환하지 않습니다.
- 배경 annulus, diffuse smoothing, compact-source masking을 거쳐 대상 내부를 밝기 백분위 구역으로 분할합니다.
- `bright_structure_factor`: 검출 구조 q97.5 / 평균. 밝은 코어의 대상 픽셀 포화 상한에 사용합니다.
- `faint_structure_factor`: 검출 구조 q25 / 평균. 목표 SNR을 만족하는 총 적분시간/필요 장수에 사용합니다.
- 희미한 외곽 때문에 단일노출을 늘리지 않습니다. 단일노출은 read-noise/overhead 효율과 포화·추적 상한으로 결정됩니다.
- 구조 영상이 없거나 신뢰도가 낮으면 v36.3 평균 표면밝기 방식으로 자동 fallback합니다.
- CDS 조회 결과는 좌표/크기 기반 캐시를 사용해 같은 대상의 반복 분석 지연을 줄입니다.

## Evidence prior

- 기존 `NOXIS_Exposure_Evidence_Dataset_20260826.xlsx`의 Target Summary에서 196개 대상의 요약을 `data/exposure_evidence_summary.json`으로 포함했습니다.
- M/NGC/IC exact match를 우선하고 없으면 galaxy/nebula/cluster 등 class-level p10/p50/p90을 사용합니다.
- prior는 **warning-only**입니다. archive 노출 분포가 물리 기반 추천값을 수정하거나 clamp하지 않습니다.

## 결과/UI

- `target_structure_model`, `exposure_evidence_prior`를 결과 JSON에 추가했습니다.
- `predicted_snr_per_sub_mean`과 `predicted_snr_per_sub_science_zone`을 분리했습니다.
- `required_frames_mean_target`과 structure-aware `required_frames_unbounded`를 함께 기록합니다.
- 결과 개요에 `target_structure_profile.png`를 추가하고 참조 FITS를 다운로드할 수 있습니다.

## 검증

- 새 합성 성운 구조 분석, flat-background rejection, exact/class evidence match, structure-aware planner 단위 테스트를 추가했습니다.
- 이 작업 환경에서 `89 passed, 4 skipped`를 확인했습니다. skip 4개는 `astropy`가 없는 작업 환경에서 FITS 관련 테스트가 자동 skip된 항목입니다.
- `tests/test_metadata_exposure.py`는 파일 자체가 `astropy.io.fits`를 직접 import하므로 현재 작업 환경에서는 collection할 수 없습니다. 실제 배포 requirements에는 `astropy==8.0.1`이 유지되어 있습니다.
