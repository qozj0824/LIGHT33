# NØXIS v38.1 변경사항

## 전천 영상 빠른 검사 오판 수정

- `/api/inspect`가 HTTP 200을 반환하면 upload token, metadata, assessment를 먼저 성공 상태로 확정합니다.
- 이후 미리보기/메타데이터 UI 렌더링이 실패해도 `allskyInspectFailed`를 다시 true로 바꾸지 않습니다.
- UI 표시 실패는 브라우저 console 진단으로 분리하고 분석은 이미 확보한 서버 검사 결과로 계속할 수 있습니다.
- assessment의 배열 필드가 예상과 다른 형태여도 표시 코드가 예외를 만들지 않도록 정규화합니다.

## Render 배포 후 브라우저 캐시 보강

- `/` HTML shell은 `Cache-Control: no-store`로 제공합니다.
- `app.js`와 `style.css`에 `?v=38.1.0` 버전 쿼리를 붙여 새 배포와 오래된 프론트엔드가 섞이는 위험을 낮췄습니다.

## Matplotlib 한글 glyph 경고 수정

- Render에 한글 폰트가 없는 경우 모든 동적 그래프 라벨도 `plot_text()`의 영문 fallback을 사용합니다.
- 스택 효율 그래프의 `빠름/균형/고품질/매우 깊게/선택`과 하늘 배경 지도 제목에 남아 있던 직접 한글 라벨을 제거했습니다.
- 한글 폰트가 설치된 환경에서는 기존처럼 한글 라벨을 사용합니다.

## 버전

- `38.1.0`
