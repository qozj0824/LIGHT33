# NØXIS v38.2 검증 보고서

## 버전

- FastAPI / package version: `38.2.0`
- 목적: CR3/RAW 처리 중 순간 메모리 사용량 감소 및 Render 재시작 위험 완화

## 회귀 테스트

실행:

```text
python -m pytest -q --ignore=tests/test_metadata_exposure.py
```

결과:

- 102 passed
- 4 skipped
- 0 failed

4개 skip은 현재 검증 컨테이너에 Astropy가 설치되어 있지 않아 자동 skip된 기존 FITS 의존 테스트입니다. 배포 `requirements.txt`에는 Astropy가 포함되어 있습니다.

## RAW 전용 검증

가짜 rawpy 객체로 다음을 검증했습니다.

1. `.CR3`가 RAW 경로로 들어감
2. `raw.postprocess()`가 호출되지 않음
3. Green CFA 두 평면의 black-level 보정 및 평균값이 기존 수학과 일치함
4. 정밀 분석에서는 두 Green photosite의 최대값으로 saturation plane을 유지함
5. inspect 경량 모드에서는 saturation plane을 별도 할당하지 않고 Green 배열을 재사용함
6. `/api/inspect`가 `load_image(..., lightweight=True)`를 호출함
7. EOS Ra와 같은 6888×4546 RAW 크기 메타데이터가 안전 제한 120 MP 안에서 처리됨

## 실제 사용자 CR3 파일 확인

대화에 업로드된 `IMG_0246.CR3`, `stars.CR3`는 각각 약 27 MB의 ISO Media/CR3 컨테이너로 확인했습니다. 이 작업 컨테이너에는 rawpy가 설치되어 있지 않고 외부 패키지 다운로드도 차단되어 있어, 이 두 파일을 rawpy 0.27.0으로 직접 디코딩하는 검증은 수행하지 못했습니다. Render 배포본은 `rawpy==0.27.0`을 설치합니다.

## 정적 검증

- Python compileall
- JavaScript `node --check static/app.js`
- `import app`
- HTML asset cache version `38.2.0`

을 모두 통과했습니다.
