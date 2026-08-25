# NØXIS v36.1 변경 내역

## 600초 추천 오류 수정

- `최대 단일노출`을 목표값이 아니라 넘지 말아야 할 상한으로 해석합니다.
- v36.0처럼 안전 상한까지 무조건 늘리지 않습니다.
- 읽기잡음과 프레임 오버헤드를 함께 고려한 정보 획득 효율이 90%에 처음 도달하는 노출을 기본 후보로 선택합니다.
- 효율 후보는 촬영하기 쉬운 간격으로 올림하되 실제 배경·추적·대상 포화 상한은 넘지 않습니다.
- 기준 영상의 대표 별 포화시간은 다른 시야의 절대 강제 상한으로 단정하지 않지만, 효율 후보보다 짧으면 보수적인 권고에 반영합니다.

## result (7) 재현

- 사용자 최대: 600초
- 하늘배경 read-noise 하한: 42.62초
- 90% 효율 도달시간: 96.30초
- 기준 시야 대표 별 80% 포화 진단: 125.42초
- 수정 전 추천: 600초
- 수정 후 추천: 100초

## 진단 개선

- 결과 JSON에 `hard_upper_constraint`, `exposure_efficiency_target`, `exposure_efficiency_lower_sec`, `exposure_efficiency_at_recommendation`, `reference_star_advisory_applied`를 기록합니다.
- Render 완료 로그에 단일노출 선택 근거를 함께 기록합니다.

## 검증

- 자동 테스트 91개 통과.
- Mypy, Ruff, Python compileall, JavaScript 문법 검사, FastAPI 서버 스모크 테스트 통과.
