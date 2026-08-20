# 보정 프레임 안내

Bias, Dark, Flat은 UI에서 업로드합니다.

- light와 동일 카메라·해상도·binning 사용
- Dark는 메타데이터 노출시간이 있으면 light 노출에 비례해 scaling
- Dark 노출시간이 없으면 동일 노출로 가정하며 경고
- Flat은 bias를 뺀 뒤 중앙값으로 정규화
- Flat의 유효 양수 픽셀이 부족하거나 극단값 범위가 비정상이면 중단
- 렌더링 JPG/PNG는 보정 프레임으로 사용할 수 없음
