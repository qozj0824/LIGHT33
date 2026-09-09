# NØXIS v38.1 검증 보고서

## 결과

- `python -m pytest -q --ignore=tests/test_metadata_exposure.py`: **98 passed, 4 skipped, 0 failed**
- skip 4개: 현재 작업 환경에 `astropy`가 없어 자동 skip된 FITS/좌표 관련 테스트
- `tests/test_metadata_exposure.py`: 모듈 import 시 `astropy`를 직접 요구하므로 현재 작업 환경에서는 collection 대상에서 제외
- `python -m compileall -q lightt app.py`: PASS
- `node --check static/app.js`: PASS
- `import app`: PASS
- FastAPI version: `38.1.0`

## v38.1 회귀 검증

1. 임의 256×256 전천 PNG를 `/api/inspect`에 업로드
   - HTTP 200
   - upload token 생성 확인
   - metadata 256×256 확인
   - server preview URL 생성 확인
2. `/` 응답 `Cache-Control: no-store` 확인
3. HTML이 `/static/app.js?v=38.1.0`, `/static/style.css?v=38.1.0`을 참조하는지 확인
4. Render에 한글 Matplotlib 폰트가 없는 상황을 모사하여 스택 효율 그래프를 생성
   - DejaVu Sans missing-Hangul `Glyph` warning 없음
5. 기존 장비 프로필 복원, 스택 효율, 구조 분석 및 API 회귀 테스트 통과

## 수정 의도

`/api/inspect`가 성공한 뒤 UI 표시 코드에서 예외가 발생하더라도 서버의 성공 결과를 실패로 오판하지 않아야 한다. v38.1은 서버 응답 성공 상태를 DOM 렌더링보다 먼저 커밋하며, UI 표시 오류는 비치명적 진단으로 분리한다.
