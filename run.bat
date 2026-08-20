@echo off
setlocal
cd /d "%~dp0"
echo NØXIS: http://127.0.0.1:8010
python -m uvicorn app:app --host 127.0.0.1 --port 8010
pause
