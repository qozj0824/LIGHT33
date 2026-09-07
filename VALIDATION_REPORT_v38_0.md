# NØXIS v38.0 검증 보고서

## 정적/구문 검사
- `python -m compileall -q lightt app.py`: PASS
- `node --check static/app.js`: PASS
- `import app`: PASS, FastAPI version `38.0.0`

## 자동 테스트
로컬 검증 명령:

`pytest -q --ignore=tests/test_metadata_exposure.py`

결과: **95 passed, 4 skipped**.

4개 skip은 로컬 작업 환경에 `astropy`가 설치되지 않아 FITS/Astropy 의존 테스트가 자동 skip된 것입니다. `requirements.txt`에는 Astropy가 포함되어 있으며 배포 환경에서는 설치 대상입니다. `tests/test_metadata_exposure.py`는 import 시점에 Astropy가 필요하므로 이 로컬 검증에서는 collection 대상에서 제외했습니다.

## v38 핵심 신규 테스트
- 목표 SNR 값이 달라도 현재 메인 세션의 단일노출/스택 권고가 바뀌지 않음(legacy 입력은 기록만 하고 무시)
- 빠름 ≤ 균형 ≤ 고품질 ≤ 매우 깊게 순서 유지
- 매우 희미한 대상은 무한 장수 대신 유한 계획범위 끝에서 `horizon_limited` 처리
- 6개 구조 구역에서 faint zone의 SNR이 core보다 낮게 계산됨
- 신호/배경 불확실성이 스택 추천 장수와 적분시간 범위로 전파됨
- 브라우저 compact snapshot만으로 EquipmentProfile 복구 가능
- 복구 API가 profile id 경로 조작 입력을 거부
- 기존 단일노출 read-noise/포화/추적/사용자 cap 회귀 테스트 유지

## 정책 검증
v38의 스택 추천은 특정 SNR을 '정답'으로 간주하지 않습니다. 단일노출 물리 모델과 총 적분 의사결정을 분리하고, 총 적분은 유한 범위의 구조 정보 한계효용으로 선택합니다. 따라서 희미한 구조는 프레임 수를 늘릴 가치에는 영향을 주지만 단일노출을 강제로 길게 만들지 않습니다.
