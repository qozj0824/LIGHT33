# LIGHTT v35.0 Web — 배포 검증 기록

## 검증 결과

- Python compile: 통과
- JavaScript syntax (`node --check`): 통과
- Pytest: **53 passed, 4 skipped**
- Hosted-mode Uvicorn smoke test: 통과
- `/health`: hosted=true, profile_storage=browser 확인
- `/api/stellarium/normalize`: raw Stellarium JSON → RA/Dec/Alt/Az 정규화 확인

## skip 4건

현재 패키징 환경에 Astropy가 설치되어 있지 않아 Astropy/FITS 통합 테스트 4건이 skip되었습니다. 배포 `requirements.txt`에는 `astropy==8.0.1`이 포함되어 있습니다.

## 웹 배포에서 별도로 바뀐 부분

1. Render Free의 ephemeral filesystem을 장비 프로필 영구 저장에 사용하지 않음.
2. 장비 프로필 전체 JSON은 브라우저 localStorage에 저장.
3. 프로필 생성용 기준 원본은 임시 작업 폴더에만 저장 후 제거.
4. public server가 사용자의 localhost에 접근하지 않도록 Stellarium 연결을 브라우저 직접 CORS 방식으로 변경.
5. 서버는 브라우저가 가져온 Stellarium JSON을 정규화만 함.
6. 무료 인스턴스 절전 후 미리보기 토큰이 사라질 수 있어 최종 분석 요청에 원본 전천 파일을 다시 포함하고 서버가 자동 fallback.
7. 512 MB RAM을 고려해 동시 분석 1개, 파일 180 MiB, 요청 260 MiB, 45 MP 기본 제한.

## 남는 제한

- 무료 Render는 0.1 CPU / 512 MB RAM이므로 로컬 PC보다 분석이 느리고 대형 FITS/다수 calibration frame에서 메모리 한계가 발생할 수 있습니다.
- 15분 유휴 후 절전되므로 첫 요청이 늦을 수 있습니다.
- 브라우저 localStorage는 사용자가 사이트 데이터를 삭제하면 사라집니다. 프로필 JSON 내보내기를 권장합니다.
- Stellarium Remote Control은 사용자가 CORS를 허용해야 합니다.
