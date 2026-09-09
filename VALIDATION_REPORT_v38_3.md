# NØXIS v38.3 검증 보고서

## 버전

- FastAPI / package version: `38.3.0`
- 목적: Canon CR3의 rawpy 메타데이터 누락 시 embedded TIFF/EXIF fallback

## 자동 회귀테스트

실행:

`python -m pytest -q --ignore=tests/test_metadata_exposure.py`

결과:

- 104 passed
- 4 skipped (로컬 검증 환경에 astropy 미설치)
- 0 failed

추가로 `python -m compileall -q lightt app.py`, `node --check static/app.js`, `import app`를 통과했습니다.

## v38.3 전용 테스트

1. 합성 CR3/TIFF 블록에서 `ExposureTime`, `FNumber`, `ISO`, `Make`, `Model`, `DateTimeOriginal` 복구
2. rawpy가 `shutter=0`, `iso_speed=0`, 빈 카메라 메타데이터를 반환하는 상황에서 embedded EXIF fallback이 실제 `ImageMetadata`에 적용되는지 검증
3. provenance가 `cr3_embedded_exif_fallback`으로 기록되는지 검증

## 실제 Canon EOS Ra CR3 검증

대화에서 제공된 실제 파일에 `_cr3_embedded_exif_metadata()`를 직접 적용했습니다.

- `stars.CR3`: ExposureTime 29.9 s, ISO 1600, f/4, Canon EOS Ra
- `IMG_0246.CR3`: ExposureTime 28.6 s, ISO 1600, f/4, Canon EOS Ra

두 파일 모두 CR3 전체를 Python bytes로 복사하지 않고 파일 앞부분 4 MiB만 스캔하여 값을 얻었습니다.

## 메모리 관련 확인

v38.2의 RAW 메모리 최적화는 유지됩니다.

- full-resolution RGB `raw.postprocess()` 미사용
- 전천/inspect CR3는 lightweight Green CFA 평면 사용
- CR3 EXIF fallback은 최대 4 MiB prefix만 읽음
- rawpy가 정상 shutter를 제공하면 fallback 값은 노출시간을 덮어쓰지 않음

## 배포 캐시

- `/static/app.js?v=38.3.0`
- `/static/style.css?v=38.3.0`
