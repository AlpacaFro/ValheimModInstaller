$ErrorActionPreference = "Stop"

# Builds a friend-friendly Windows installer.
# Requires Inno Setup 6: https://jrsoftware.org/isinfo.php
# Run with: powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

$appName = "ValheimModInstaller"
$setupName = "ValheimModInstallerSetup.exe"

function Find-InnoCompiler {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $defaultPaths = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )

    foreach ($path in $defaultPaths) {
        if ($path -and (Test-Path $path)) {
            return $path
        }
    }

    return $null
}

Write-Host "Building portable app folder first..."
powershell -ExecutionPolicy Bypass -File .\package_windows.ps1

$iscc = Find-InnoCompiler
if (-not $iscc) {
    Write-Host ""
    Write-Host "Portable zip is ready: $appName.zip"
    Write-Host "Inno Setup compiler was not found, so setup.exe was not built."
    Write-Host "Install Inno Setup 6, then rerun this script to create release\$setupName."
    exit 0
}

Remove-Item -Recurse -Force release -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path release | Out-Null

Write-Host "Compiling installer with Inno Setup..."
& $iscc .\installer.iss

Write-Host ""
Write-Host "Installer complete:"
Write-Host "release\$setupName"
Write-Host ""
Write-Host "Send this file to your friend. If Windows SmartScreen warns them, they can choose More info > Run anyway."
