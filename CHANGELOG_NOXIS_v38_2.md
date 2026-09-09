# NØXIS v38.2 변경사항

## CR3/RAW 메모리 안정화

- RAW 로더에서 전체 센서 CFA를 float32로 복사하던 경로를 제거했습니다.
- Green CFA 두 평면은 필요한 반해상도 분석 평면만 만들고, 두 번째 평면은 row chunk 단위로 합산합니다.
- `np.mean(list_of_planes)` / `np.max(list_of_planes)`가 만들던 대형 임시 배열을 제거했습니다.
- NØXIS 화면에서 사용하지 않던 `raw.postprocess()` 풀해상도 RGB 생성을 완전히 제거했습니다.
- `/api/inspect`는 RAW에서 별도의 saturation 보존 배열을 만들지 않는 경량 모드를 사용합니다.
- 실제 망원경 영상 분석은 두 Green photosite 중 하나라도 clipping되면 놓치지 않도록 saturation 보존 배열을 유지합니다.
- 전천 RAW와 RAW calibration frame은 saturation 보존 배열이 필요하지 않으므로 경량 로더를 사용합니다.
- `raw_decode_start`, `raw_decode_open_unpack_complete`, `raw_green_extract_complete`, `raw_decode_complete` 로그를 추가했습니다.

## 예상 메모리 효과

EOS Ra급 약 31 MP Bayer RAW의 Green 분석 평면은 약 31 MB입니다. v38.2의 지속 numpy 배열은 대략:

- inspect / 전천 분석: Green 약 31 MB
- 망원경 정밀 분석: Green + saturation 약 62 MB

여기에 rawpy/LibRaw 내부 디코딩 버퍼와 소규모 chunk 임시 배열이 추가됩니다. 이전 버전은 전체 CFA float32 복사, Green 평균/최댓값 임시 배열, 풀 RGB postprocess가 겹쳐 순간 메모리가 수백 MB까지 커질 수 있었습니다.

## 버전

- `38.2.0`
