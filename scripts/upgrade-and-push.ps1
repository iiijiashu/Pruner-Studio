param(
    [string]$CommitMessage = "",
    [switch]$Push,
    [switch]$SkipTests,
    [switch]$Relaunch
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distExe = Join-Path $projectRoot "dist\Pruner-Studio.exe"

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        Write-Host "[1/4] Running frontend UI scan and test suites..."
        & node tests/frontend/scan_ui.js
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & node tests/frontend/run_all.js
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

        if (Test-Path -LiteralPath $venvPython) {
            Write-Host "[1/4] Running Python syntax check..."
            & $venvPython -m compileall -q src tests/backend scripts
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
    }

    Write-Host "[2/4] Stopping running Pruner-Studio.exe and rebuilding in-place..."
    & cmd /c "taskkill /F /IM Pruner-Studio.exe >nul 2>&1 & exit /b 0"
    Start-Sleep -Milliseconds 600

    & (Join-Path $PSScriptRoot "build-windows.ps1") -SkipLockInstall
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/4] Verifying refreshed dist\\Pruner-Studio.exe and Start Menu shortcut..."
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython scripts/verify-windows.py
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "install-start-menu.ps1")) {
        & (Join-Path $PSScriptRoot "install-start-menu.ps1") -ExePath $distExe
    }

    if ($Push) {
        if (-not $CommitMessage) {
            throw "CommitMessage is required when -Push is specified."
        }
        Write-Host "[4/4] Staging, committing, and pushing to GitHub..."
        & git add -A
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & git diff --cached --quiet
        if ($LASTEXITCODE -ne 0) {
            & git commit -m $CommitMessage
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        } else {
            Write-Host "No staged changes to commit."
        }
        & git push
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    if ($Relaunch -and (Test-Path -LiteralPath $distExe)) {
        Write-Host "Relaunching updated $distExe ..."
        Start-Process -FilePath $distExe -WorkingDirectory (Split-Path -Parent $distExe)
    }
}
finally {
    Pop-Location
}
