Set-Location $PSScriptRoot
Write-Host "NØXIS: http://127.0.0.1:8010"
python -m uvicorn app:app --host 127.0.0.1 --port 8010
