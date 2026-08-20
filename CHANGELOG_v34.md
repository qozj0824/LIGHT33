# CHANGELOG — LIGHTT v34 (이전 구조)

최신 변경사항은 `CHANGELOG_v34_1.md`를 참고하십시오.


## 핵심 구조 변경
- 매 관측마다 망원경 영상을 업로드하는 구조 제거
- 망원경 기준 영상을 1회 분석해 장비 프로필로 지속 저장
- 실제 관측은 `현재 전천 영상 + Stellarium 대상 + 장비 프로필` 구조
- 프로필 삭제 기능 추가

## 장비 모델
- 망원경/카메라/필터/Gain·ISO/binning 조합 정보 저장
- 기준 background rate, PSF/FWHM, representative peak, photometric zero point 저장
- 선택적 동일조건 전천 영상으로 Csys 계산
- 현재 전천 장비 조건이 Csys 기준과 다르면 Csys 사용 중단

## 전천 분석
- G 채널 정합
- DAOStarFinder 우선 별 검출 + fallback
- 원형 별 마스크, sigma clipping
- 극좌표·직사각형·상대배경·신뢰도 시각화
- 저고도/장애물/결측 처리

## 관측 대상
- Stellarium 선택 천체 자동 가져오기
- 음수 고도는 정상 좌표로 파싱하고 지평선 아래 상태로 판정
- 대상 유형 자동 분류
- V등급/소광등급/각크기 사용

## 계획 계산
- 목표 SNR과 단일노출 결정 분리
- background/read/dark/source noise 분리
- 센서 포화, 대표별, 점광원 target saturation, 추적 상한
- 필요한 stack frame과 총 적분시간 계산
- 근거가 부족한 조건은 planning-only
