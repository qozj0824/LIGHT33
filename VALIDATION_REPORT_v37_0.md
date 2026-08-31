# NØXIS v37.0 검증 보고서

## 정적/단위 검증

실행한 명령:

```text
python -m pytest -q --ignore=tests/test_metadata_exposure.py
python -m compileall -q lightt app.py
node --check static/app.js
```

결과:

- 89 passed
- 4 skipped: 현재 작업 환경에 `astropy`가 없어서 FITS 관련 테스트가 pytest의 `importorskip`로 자동 skip
- Python compileall 통과
- JavaScript syntax check 통과
- `import app` 통과
- NØXIS version: 37.0.0

`tests/test_metadata_exposure.py`는 테스트 모듈 최상단에서 `astropy.io.fits`를 직접 import하므로 이 작업 환경에서는 collection 단계에서 실행할 수 없었습니다. 배포본 `requirements.txt`에는 기존과 동일하게 `astropy==8.0.1`이 포함되어 있습니다.

## v37.0 신규 테스트

### 1. 합성 확산천체 구조

Gaussian core + diffuse halo + compact field-star spike를 합성한 영상에서:

- structure status = ok
- faint factor < 1
- bright factor > 1
- zone 대표 밝기가 백분위 순서대로 증가
- compact-source mask가 과도하게 target morphology를 지우지 않음

을 검증했습니다.

### 2. Flat background rejection

구조 없는 잡음 배경은 target structure로 오인하지 않고 `unavailable`로 반환하여 기존 평균 표면밝기 방식으로 fallback하는 것을 검증했습니다.

### 3. Exposure evidence prior

- `M16 (Eagle Nebula)` exact match
- unknown galaxy의 class-level fallback
- prior 정책이 `warning_only`이며 physics recommendation을 보존

을 검증했습니다.

### 4. Structure-aware planner

동일한 평균 신호/하늘/센서 조건에서 structure factor를 추가했을 때:

- q25 faint zone을 사용한 필요 장수가 평균 신호 기준보다 증가
- q97.5 bright factor가 클수록 target saturation upper가 감소
- faint zone 때문에 recommended sub-exposure가 더 길어지지 않음

을 검증했습니다.

## 외부 참조 서비스 설계 검증

CDS HiPS2FITS의 공개 API와 `CDS/P/DSS2/red`, `CDS/P/DSS2/blue` HiPS 식별자를 기준으로 구현했습니다. 네트워크는 계산의 필수 조건이 아니며, timeout/서비스 오류/비-FITS 응답/파싱 실패 시 기존 계산으로 자동 fallback합니다.

## 남은 실제 관측 검증

소프트웨어 회귀 테스트와 실제 천문 촬영의 절대 정확도 검증은 다릅니다. v37.0을 최종 연구 결과로 정량 검증하려면 최소한 다음이 필요합니다.

1. 구조가 강한 은하/성운과 비교적 균일한 대상으로 각각 재촬영
2. 추천 sub-exposure에서 bright core의 실제 peak ADU와 예측 headroom 비교
3. q25로 계획한 총 적분 후 동일 ROI/스무딩 조건에서 faint-zone SNR 측정
4. 구조 모델 미적용(v36.3 방식)과 v37.0 방식의 faint-zone SNR/포화율 비교
5. 광대역과 협대역을 분리해 morphology passband mismatch 평가

따라서 v37.0은 기존보다 구조적으로 더 일반화된 관측 계획 모델이지만, 모든 장비/필터/천체에 대해 절대적 최적 노출을 이미 실험적으로 보증한다고 주장하지 않습니다.
