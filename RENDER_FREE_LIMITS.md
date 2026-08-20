# Render Free 제한과 LIGHTT 설계 대응

- Render Free Web Service: 512 MB RAM, 0.1 CPU.
- 15분 동안 요청이 없으면 서비스가 절전되고, 다음 요청 시 다시 시작됩니다.
- 무료 Web Service에는 persistent disk를 붙일 수 없습니다.
- 로컬 파일 변경 사항은 재배포·재시작·절전 시 유지되지 않습니다.

LIGHTT v35.0 Web 대응:

1. 장비 프로필 JSON → 브라우저 localStorage 저장.
2. 장비 프로필 원본 기준 영상 → 프로필 생성 후 서버에서 제거.
3. 전천/분석 결과 → 임시 작업 파일로만 유지.
4. 미리보기 토큰 소실 → 최종 분석 때 브라우저가 원본 전천 영상을 다시 전송하여 복구.
5. 동시 분석 → 1개.
6. 업로드 180 MiB/파일, 260 MiB/요청, 45 MP 제한.
7. Stellarium → 서버 프록시 대신 브라우저에서 사용자 PC의 Remote Control API로 직접 연결.
