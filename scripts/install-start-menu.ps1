param(
    [string]$ExePath,
    [switch]$AlsoDesktop
)

$ErrorActionPreference = "Stop"

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "Pruner-Studio.exe"
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        $ExePath = Join-Path $PSScriptRoot "..\dist\Pruner-Studio.exe"
    }
}
$ExePath = (Resolve-Path -LiteralPath $ExePath -ErrorAction Stop).Path

$startMenuPrograms = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
if (-not (Test-Path -LiteralPath $startMenuPrograms -PathType Container)) {
    New-Item -ItemType Directory -Path $startMenuPrograms -Force | Out-Null
}

function New-AppShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $ExePath
    $shortcut.WorkingDirectory = Split-Path -Parent $ExePath
    $shortcut.IconLocation = "$ExePath,0"
    $shortcut.Description = "Pruner Studio"
    $shortcut.Save()
    Write-Host "Registered shortcut: $ShortcutPath"
}

$startMenuShortcut = Join-Path $startMenuPrograms "Pruner Studio.lnk"
New-AppShortcut -ShortcutPath $startMenuShortcut

if ($AlsoDesktop) {
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    if (-not (Test-Path -LiteralPath $desktopDir -PathType Container)) {
        throw "Desktop folder not found: $desktopDir"
    }
    $desktopShortcut = Join-Path $desktopDir "Pruner Studio.lnk"
    New-AppShortcut -ShortcutPath $desktopShortcut
}
