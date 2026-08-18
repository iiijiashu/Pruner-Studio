$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$webDir = Join-Path $projectRoot "web"
$entryPoint = Join-Path $PSScriptRoot "pruner_studio_entry.py"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv $venvDir
}

& $pythonExe -m pip install -e "${projectRoot}[build]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $projectRoot
try {
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name "Pruner-Studio" `
        --paths (Join-Path $projectRoot "src") `
        --add-data "$webDir;web" `
        --collect-all qrcode `
        --collect-all PIL `
        $entryPoint
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

$artifact = Join-Path $projectRoot "dist\Pruner-Studio.exe"
if (-not (Test-Path -LiteralPath $artifact)) {
    throw "Build finished without producing $artifact"
}

$hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
Write-Host "Built: $artifact"
Write-Host "SHA256: $($hash.Hash)"
