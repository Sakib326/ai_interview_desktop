# Builds dist\overlay\overlay.exe with PyInstaller.
#
#   .\packaging\build.ps1            # windowed (ship build)
#   .\packaging\build.ps1 -Console   # console build for debugging
#
# The binary name is intentional: initialize-plan.md §10.4 treats it as a
# stealth input, so it must not describe the product.

param([switch]$Console)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    $pyinstallerArgs = @(
        "--noconfirm",
        "--clean",
        "--name", "overlay",
        "--paths", ".",
        "--collect-data", "sounddevice",
        "--collect-all", "soundcard",
        "--exclude-module", "pytest",
        "--exclude-module", "tests",
        "--distpath", "dist",
        "--workpath", "build",
        "--specpath", "packaging",
        "packaging/entry.py"
    )

    if ($Console) {
        $pyinstallerArgs += "--console"
    } else {
        $pyinstallerArgs += "--windowed"
    }

    # PyInstaller writes progress to stderr; don't let that trip the strict
    # error preference, and judge success by the exit code instead.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & .\.venv\Scripts\python.exe -m PyInstaller @pyinstallerArgs 2>&1 |
            Select-Object -Last 8 |
            ForEach-Object { Write-Output $_ }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    if ($exitCode -ne 0) {
        throw "PyInstaller failed with exit code $exitCode"
    }

    Write-Output "Built: $root\dist\overlay\overlay.exe"
} finally {
    Pop-Location
}
