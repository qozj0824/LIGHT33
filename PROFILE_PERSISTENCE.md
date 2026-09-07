# NØXIS v38.0 — Render Free 장비 프로필 보존 구조

Render Free Web Service의 로컬 파일은 영구 저장소로 간주하지 않습니다. 따라서 `profiles/`는 실행 중 편의를 위한 서버 사본이고, 장기 보존의 유일한 근거가 아닙니다.

## 브라우저 핵심 백업
프로필의 계산 핵심값만 `noxis.profileCoreSnapshots.v2`에 저장합니다. 미리보기 data URL은 별도 저장소로 분리하므로 큰 이미지가 핵심 JSON의 저장/복구를 방해하지 않습니다. 분석 요청의 `profile_snapshot_json`에도 미리보기를 넣지 않습니다.

## 자동 재생성
페이지가 서버 프로필 목록을 읽을 때 브라우저에는 있지만 Render 서버에는 없는 프로필을 발견하면 `/api/equipment/profiles/restore`로 검증 후 현재 인스턴스의 `profiles/`에 다시 생성합니다. 분석 중에도 서버 파일이 사라졌다면 compact snapshot을 검증해 복구합니다.

## 수동 백업
프로필 관리 화면에서 `.noxis-profile.json`으로 내보내고 다시 가져올 수 있습니다. 브라우저 데이터 삭제, 다른 PC/브라우저, origin 변경에 대비한 수동 백업 수단입니다.

## 보안/제한
복원 입력은 EquipmentProfile 스키마로 다시 파싱/검증하며 profile id도 안전한 16진수 형태만 허용합니다. RAW/FITS 원본과 과거 분석 결과 파일은 이 백업의 대상이 아닙니다.
