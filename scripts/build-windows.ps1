$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$specFile = Join-Path $projectRoot "Pruner-Studio.spec"
$versionFile = Join-Path $projectRoot "build\windows-version-info.txt"
$buildLock = Join-Path $projectRoot "requirements-build.lock"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & py -3.11 -m venv $venvDir
    }
    else {
        python -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$pythonTarget = & $pythonExe -c "import struct, sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor) + ':' + str(struct.calcsize('P') * 8))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($pythonTarget.Trim() -ne "3.11:64") {
    throw "Windows packaging requires 64-bit CPython 3.11; found $pythonTarget"
}

& $pythonExe -m pip install --require-hashes --only-binary=:all: -r $buildLock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $pythonExe -m pip install --no-build-isolation --no-deps -e $projectRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $pythonExe -m pruner_studio.windows_version_info $versionFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $projectRoot
try {
    & $pythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        $specFile
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
