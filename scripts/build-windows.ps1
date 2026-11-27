param(
    [switch]$SkipLockInstall
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $projectRoot ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$specFile = Join-Path $projectRoot "Pruner-Studio.spec"
$buildDir = Join-Path $projectRoot "build"
$versionFile = Join-Path $buildDir "windows-version-info.txt"
$buildLock = Join-Path $projectRoot "requirements-build.lock"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $candidates = @("py", "python", "python3.11",
        "$env:LocalAppData\Programs\Python\Python311\python.exe", "C:\Python311\python.exe")
    $created = $false
    foreach ($candidate in $candidates) {
        $exe = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $exe) { continue }
        $selector = @()
        if ($candidate -eq "py") { $selector = @("-3.11") }
        try {
            $version = & $exe.Source @selector -c "import struct,sys; print('%d.%d:%d' % (*sys.version_info[:2], struct.calcsize('P')*8))" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $version -ne "3.11:64") { continue }
        & $exe.Source @selector -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $created = $true
        break
    }
    if (-not $created) { throw "64-bit CPython 3.11 was not found." }
}

$pythonTarget = & $pythonExe -c "import struct, sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor) + ':' + str(struct.calcsize('P') * 8))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($pythonTarget.Trim() -ne "3.11:64") {
    throw "Windows packaging requires 64-bit CPython 3.11; found $pythonTarget"
}

if (-not $SkipLockInstall) {
    # Release builds install exact hash-locked dependencies from requirements-build.lock.
    # All 29 wheel packages enforce --only-binary=:all:; proxy_tools==0.1.0 is sdist-only
    # on PyPI and remains strictly hash-verified via --require-hashes.
    & $pythonExe -m pip install --require-hashes --only-binary=:all: --no-binary=proxy_tools -r $buildLock
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $pythonExe -m pip install --no-build-isolation --no-deps -e $projectRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Dependency check failed. Regenerate requirements-build.lock for the updated pyproject.toml dependencies before release packaging (or pass -SkipLockInstall when building from a prepared local .venv)."
}

& $pythonExe -c "import importlib.util, sys; req=['qrcode.image.pil','PIL','starlette','uvicorn','httpx','Crypto.Cipher.AES','webview','pystray']; missing=[m for m in req if importlib.util.find_spec(m) is None]; sys.exit('Missing runtime modules: ' + ', '.join(missing) + '. Regenerate requirements-build.lock before release packaging.') if missing else None"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path -LiteralPath $buildDir)) {
    New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
}

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

$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $stream = [System.IO.File]::OpenRead($artifact)
    try {
        $hashBytes = $sha256.ComputeHash($stream)
    }
    finally {
        $stream.Dispose()
    }
}
finally {
    $sha256.Dispose()
}
$hashHex = [System.BitConverter]::ToString($hashBytes).Replace("-", "")
Write-Host "Built: $artifact"
Write-Host "SHA256: $hashHex"
