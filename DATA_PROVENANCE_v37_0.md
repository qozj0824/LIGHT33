# NØXIS v37.0 데이터 출처와 사용 정책

## Exposure evidence summary

프로그램에 포함된 `data/exposure_evidence_summary.json`은 기존 NØXIS 연구 데이터 파일
`NOXIS_Exposure_Evidence_Dataset_20260826.xlsx`의 **Target Summary** sheet를 압축한 것입니다.

포함 필드:

- target_key / target_name / target_class
- observation_count / standard_exposure_count / instrument_count
- subexposure min / p10 / median / p90 / max
- archive total exposure median
- seeing / airmass median
- first / last year

총 196개 target summary가 포함되며 class-level fallback은 target별 median exposure를 support count(대상당 최대 30 가중치)로 제한하여 weighted p10/p50/p90을 계산합니다.

이 데이터는 **warning-only prior**입니다. 서로 다른 전문 장비, 필터, 관측 목적의 archive 노출시간을 사용자의 장비에 직접 이식하지 않습니다.

## Public morphology survey

고정 심우주 대상의 상대 형태는 CDS HiPS2FITS에서 제공되는 DSS2 red/blue HiPS를 선택적으로 사용합니다. 요청 좌표는 공개 천체의 RA/Dec이며, survey count는 절대 photometry에 사용하지 않습니다.

기본 survey 선택:

- 대부분의 대상/필터: `CDS/P/DSS2/red`
- OIII/blue/B/g 계열 형태 참고: `CDS/P/DSS2/blue`

협대역은 실제 line morphology와 DSS plate passband가 다를 수 있으므로 구조 confidence를 낮춥니다.
