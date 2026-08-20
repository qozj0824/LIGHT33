# LIGHTT Render 배포

이 배포본은 저장소 **루트**에 `app.py`, `lightt/`, `static/`, `index.html`, `requirements.txt`, `render.yaml`, `.python-version`이 모두 있어야 합니다.

## 권장 설정
- Runtime: Python
- Build command: `python -m pip install --upgrade pip && python -m pip install -r requirements.txt`
- Start command: `python -m uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`
- Python: 3.12.11

`render.yaml`을 사용하는 Blueprint 배포라면 위 설정이 자동 적용됩니다.

## 무료 인스턴스 주의
Render 무료 웹 서비스의 로컬 파일은 영구 저장소가 아닙니다. 서버가 재시작/재배포/유휴 종료된 뒤에는 런타임에 생성한 장비 프로필과 업로드 파일이 사라질 수 있습니다. 현재 공개 웹 배포본은 관측 세션용 테스트/공유에 적합하며, 장비 프로필의 장기 보존은 추후 브라우저 저장 또는 외부 저장소로 분리하는 것이 권장됩니다.

Render 배포본의 Stellarium 연동은 Render 서버가 아니라 **사용자 브라우저에서 `127.0.0.1:8090`으로 직접 연결**합니다. 따라서 Stellarium Remote Control을 켜고 CORS 허용 Origin에 배포 주소(예: `https://light33.onrender.com`)를 등록해야 합니다. 로컬 실행에서도 같은 브라우저 직접 연결 방식을 사용합니다.
