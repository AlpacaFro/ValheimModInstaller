$ErrorActionPreference = "Stop"

# Build helper for ValheimModInstaller.
# Run with: powershell -ExecutionPolicy Bypass -File .\package_windows.ps1

$appName = "ValheimModInstaller"
$mainFile = "valheim_mod_downloader.py"
$iconPng = "assets\app_icon.png"
$iconIco = "assets\app_icon.ico"

function Stop-AppIfRunning {
    param([string]$Name)

    Get-Process -Name $Name -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Stopping running process: $($_.ProcessName) ($($_.Id))"
        Stop-Process -Id $_.Id -Force
        Wait-Process -Id $_.Id -Timeout 10 -ErrorAction SilentlyContinue
    }
}

function Wait-UntilFolderFilesAreUnlocked {
    param(
        [string]$Folder,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $lockedFile = $null

        foreach ($file in Get-ChildItem -Path $Folder -File -Recurse) {
            try {
                $stream = [System.IO.File]::Open($file.FullName, "Open", "Read", "None")
                $stream.Close()
            }
            catch {
                $lockedFile = $file.FullName
                break
            }
        }

        if (-not $lockedFile) {
            return
        }

        Write-Host "Waiting for file lock to release: $lockedFile"
        Start-Sleep -Seconds 2
    }

    throw "Timed out waiting for files in $Folder to become readable. Close the app, Explorer preview panes, and antivirus scan dialogs, then try again."
}

function Convert-PngToIco {
    param(
        [string]$PngPath,
        [string]$IcoPath
    )

    if (-not (Test-Path $PngPath)) {
        return
    }

    Add-Type -AssemblyName System.Drawing
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class NativeIconMethods {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool DestroyIcon(IntPtr hIcon);
}
"@

    $bitmap = [System.Drawing.Bitmap]::new((Resolve-Path $PngPath).Path)
    $resized = [System.Drawing.Bitmap]::new($bitmap, [System.Drawing.Size]::new(256, 256))
    $handle = $resized.GetHicon()

    try {
        $icon = [System.Drawing.Icon]::FromHandle($handle)
        $stream = [System.IO.File]::Create((Join-Path (Resolve-Path (Split-Path $IcoPath -Parent)).Path (Split-Path $IcoPath -Leaf)))
        try {
            $icon.Save($stream)
        }
        finally {
            $stream.Close()
            $icon.Dispose()
        }
    }
    finally {
        [NativeIconMethods]::DestroyIcon($handle) | Out-Null
        $resized.Dispose()
        $bitmap.Dispose()
    }
}

if (-not (Test-Path $mainFile)) {
    throw "Could not find $mainFile. Run this script from the project folder."
}

# Ask the same Python interpreter used for the build where CustomTkinter is installed.
# This prevents hardcoding Python312/Python313/venv-specific paths.
$customTkinterPath = python -c "import customtkinter, pathlib; print(pathlib.Path(customtkinter.__file__).parent)"
if (-not $customTkinterPath -or -not (Test-Path $customTkinterPath)) {
    throw "Could not find customtkinter at: $customTkinterPath"
}

Write-Host "Using CustomTkinter from: $customTkinterPath"

Stop-AppIfRunning -Name $appName
Convert-PngToIco -PngPath $iconPng -IcoPath $iconIco

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force "$appName.spec" -ErrorAction SilentlyContinue
Remove-Item -Force "$appName.zip" -ErrorAction SilentlyContinue

$pyInstallerArgs = @(
    "--onedir",
    "--windowed",
    "--name", $appName,
    "--add-data", "$customTkinterPath;customtkinter"
)

if (Test-Path $iconIco) {
    Write-Host "Using app icon: $iconIco"
    $pyInstallerArgs += @("--icon", $iconIco)
}

$pyInstallerArgs += $mainFile

python -m PyInstaller @pyInstallerArgs

Copy-Item README.txt "dist\$appName\" -Force
Wait-UntilFolderFilesAreUnlocked -Folder "dist\$appName"
Compress-Archive -Path "dist\$appName" -DestinationPath "$appName.zip" -Force

Write-Host ""
Write-Host "Build complete:"
Write-Host "dist\$appName\$appName.exe"
Write-Host "$appName.zip"
