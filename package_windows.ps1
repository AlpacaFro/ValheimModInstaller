$ErrorActionPreference = "Stop"

# Build helper for ValheimModInstaller.
# Run with: powershell -ExecutionPolicy Bypass -File .\package_windows.ps1

$appName = "ValheimModInstaller"
$mainFile = "valheim_mod_downloader.py"

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

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -Force "$appName.spec" -ErrorAction SilentlyContinue
Remove-Item -Force "$appName.zip" -ErrorAction SilentlyContinue

python -m PyInstaller `
    --onedir `
    --windowed `
    --name $appName `
    --add-data "$customTkinterPath;customtkinter" `
    $mainFile

Copy-Item README.txt "dist\$appName\" -Force
Wait-UntilFolderFilesAreUnlocked -Folder "dist\$appName"
Compress-Archive -Path "dist\$appName" -DestinationPath "$appName.zip" -Force

Write-Host ""
Write-Host "Build complete:"
Write-Host "dist\$appName\$appName.exe"
Write-Host "$appName.zip"
