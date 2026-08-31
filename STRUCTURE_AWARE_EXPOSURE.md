# NØXIS v37.0 — 천체 내부 밝기 분포 기반 노출 계산

## 1. 문제

카탈로그의 통합등급과 각크기만 사용하면 확산천체를 하나의 평균 표면밝기로 취급하게 됩니다. 실제 천체는 밝은 핵, 중간 구조, 희미한 외곽의 밝기가 크게 다르기 때문에 평균 하나로는 다음 두 문제를 동시에 해결하기 어렵습니다.

1. 밝은 코어를 과노출하지 않는 단일노출 상한
2. 희미한 외곽을 목표 SNR까지 끌어올리는 총 적분시간

## 2. v37.0의 분리 원칙

NØXIS는 세 역할을 분리합니다.

- **절대 평균 광자율:** 기존 장비 기기영점 + 대상 등급/표면밝기 + 대기소광
- **상대 공간 구조:** 공개 DSS2 참조 영상의 밝기 비율
- **현재 하늘 노이즈:** 전천 영상 + Csys/상대배경 환산

따라서 서로 다른 survey/filter의 count를 사용자 카메라의 전자율이라고 가정하지 않습니다.

## 3. 참조 영상

고정 심우주 대상이 RA/Dec를 제공하면 CDS HiPS2FITS에서 작은 DSS2/red cutout을 요청합니다. FOV는 대상 각크기의 약 2.6배로 잡고 0.08°–8° 범위로 제한합니다. 320×320 픽셀만 받아 계산량과 네트워크 사용을 제한합니다.

조회 실패는 세션 실패가 아닙니다. 기존 평균 표면밝기 모델로 즉시 fallback합니다.

## 4. 구조 추출

1. 중앙의 catalog target radius를 계산합니다.
2. 바깥 annulus의 robust median/MAD로 survey background와 noise를 추정합니다.
3. Gaussian smoothing으로 diffuse morphology를 얻습니다.
4. Difference-of-Gaussians + local maximum으로 compact point-source seed를 찾고 PSF 주변을 mask합니다.
5. 배경보다 유의한 양의 diffuse target pixel만 남기고 작은 고립 island를 제거합니다.
6. 남은 픽셀의 평균을 1.0으로 정규화합니다.

compact mask가 대상의 15% 이상을 지우게 되면 실제 천체 구조를 별로 오인한 가능성이 있으므로 mask를 자동으로 포기합니다.

## 5. 밝기 구역

검출된 확산 구조를 다음 백분위 구역으로 기록합니다.

- P0–20: 매우 희미
- P20–40: 희미
- P40–60: 중간
- P60–80: 중간-밝음
- P80–95: 밝음
- P95–100: 코어

각 구역은 평균 대비 대표 밝기 비율과 차지하는 픽셀 비율을 JSON에 남깁니다.

## 6. 밝은 구조와 단일노출

`bright_structure_factor = q97.5 / mean`

평균 픽셀 신호율 `s_mean`에 이 계수를 곱해 robust bright-pixel rate를 만들고 센서 선형/포화 안전범위와 비교합니다.

`T_target,max = safe_fullwell / (s_mean × bright_factor × signal_uncertainty + B_high + D)`

따라서 핵이 강한 은하는 평균 표면밝기가 같아도 더 짧은 상한을 갖습니다.

## 7. 희미한 구조와 총 적분시간

`faint_structure_factor = q25 / mean`

가장 어두운 픽셀이 아니라 검출 가능한 diffuse structure의 25백분위를 사용합니다. catalog radius 가장자리의 background 오염이나 survey noise 때문에 비현실적으로 긴 시간을 요구하는 것을 막으면서도 평균보다 희미한 구조를 실제 계획에 포함합니다.

`S_faint = S_mean × faint_factor`

권장 단일노출 `t`에서 faint-zone SNR을 계산하고,

`N = ceil((SNR_target / (eta × SNR_faint,1))²)`

로 장수와 총 적분시간을 정합니다.

**중요:** faint zone은 단일노출을 길게 만드는 기준이 아닙니다. 총 장수를 늘리는 기준입니다.

## 8. 단일노출 자체의 결정

기존 v36.3 원칙을 유지합니다.

- sky/read-noise lower bound
- 90% read-noise + frame-overhead information-efficiency lower bound
- background/full-well upper bound
- target saturation upper bound
- tracking upper bound
- user maximum

이 범위 안에서 효율 목표를 만족하는 가장 짧은 실용 노출을 선택합니다.

## 9. 실제 촬영 데이터 prior

196개 Target Summary를 exact target / class level로 조회하지만 결과는 warning-only입니다. 공개 아카이브의 노출시간은 망원경, 센서, 필터, seeing, 연구 목적이 다르므로 물리 추천값을 수정하면 오히려 데이터 누출/과적합이 됩니다.

## 10. fallback과 신뢰도

다음 경우 구조 기반 총 적분을 강제하지 않습니다.

- 네트워크/HiPS2FITS 실패
- RA/Dec 없음
- point source
- 이동 태양계 대상
- 구조 검출 대비/유효면적 부족
- 구조 confidence=low

이 경우 평균 표면밝기 모델이 그대로 동작하며 결과 JSON에 이유가 남습니다.
