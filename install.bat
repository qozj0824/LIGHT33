@echo off
setlocal
cd /d "%~dp0"
echo [NØXIS] Python package installation
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Installation complete.
pause
exit /b 0
:error
echo.
echo Installation failed. Check the messages above.
pause
exit /b 1
