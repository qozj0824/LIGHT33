# NØXIS v38.3 변경사항

## Canon CR3 노출시간 EXIF fallback

일부 Canon CR3는 rawpy/LibRaw에서 픽셀 디코딩은 정상인데 `metadata.shutter`가 0 또는 비어 있는 경우가 있습니다. v38.3은 이 경우에만 CR3 파일 시작 부분의 Canon CMT/TIFF EXIF 블록을 직접 읽어 표준 메타데이터를 복구합니다.

복구 대상:

- `ExposureTime (0x829A)` → 노출시간
- `PhotographicSensitivity / ISO (0x8827)` → ISO
- `FNumber (0x829D)` → 조리개값(진단용)
- `Make (0x010F)` / `Model (0x0110)` → 카메라명
- `DateTimeOriginal (0x9003)` / `DateTime (0x0132)` → 촬영시각
- 시간대 EXIF가 있으면 함께 보존

## 메모리 안전성 유지

CR3 메타데이터를 얻기 위해 전체 RAW 파일을 `read_bytes()`로 복사하지 않습니다. 기본적으로 파일 앞부분 최대 4 MiB만 읽고 TIFF/EXIF 블록을 탐색하므로 v38.2에서 줄인 RAW 메모리 사용량을 유지합니다.

## 우선순위

1. rawpy/LibRaw가 정상 노출시간을 제공하면 기존 값을 그대로 사용
2. 해당 값이 없을 때만 CR3 embedded EXIF fallback 사용
3. 둘 다 없으면 기존처럼 사용자 수동 노출시간 입력 요청

## 실제 EOS Ra 검증

대화에서 제공된 실제 Canon EOS Ra CR3 두 파일에 직접 적용한 결과:

- `stars.CR3` → 29.9 s, ISO 1600, f/4, Canon EOS Ra
- `IMG_0246.CR3` → 28.6 s, ISO 1600, f/4, Canon EOS Ra

버전: `38.3.0`
