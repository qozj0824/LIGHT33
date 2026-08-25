# NØXIS v35.8

## 기준 촬영시각 Alt/Az 재계산 수정
- FITS DATE-OBS를 UTC ISO 문자열로 정규화한 뒤 Astropy `Time`에 문자열 그대로 전달하던 경로를 수정했습니다.
- timezone offset이 포함된 문자열(`...+00:00`)을 `datetime`으로 파싱한 뒤 `Time(datetime)`으로 전달합니다.
- 이로 인해 FORS2와 같은 실제 FITS에서 발생하던 `기준 촬영시각의 고도·방위각 자동 재계산 ... (ValueError)` 경고를 제거합니다.
- 기존 APICAM 메모리 절약 및 전용 어안 좌표 보정은 그대로 유지합니다.
