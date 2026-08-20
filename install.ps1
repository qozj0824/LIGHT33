Set-Location $PSScriptRoot
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pip install -r requirements.txt
exit $LASTEXITCODE
