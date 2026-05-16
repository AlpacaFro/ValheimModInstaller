ValheimModInstaller
===================

How to run
----------
Portable zip:
1. Extract the zip file to a normal folder, such as your Desktop.
2. Open the extracted folder.
3. Run ValheimModInstaller.exe.

Installer:
1. Run ValheimModInstallerSetup.exe.
2. Open Valheim Mod Installer from the Start Menu or Desktop shortcut.

Installing mods
---------------
When the app asks for your install target, select your BepInEx folder, not the plugins folder.

Example:
D:\SteamLibrary\steamapps\common\Valheim\BepInEx

The app can also accept the Valheim game folder if it contains a BepInEx folder.

Backups
-------
If an installed file already exists, the app creates a backup before replacing it:
BepInEx\_mod_installer_backups\YYYY-MM-DD_HH-MM-SS\

Building for friends
--------------------
Portable zip:
powershell -ExecutionPolicy Bypass -File .\package_windows.ps1

Windows installer:
1. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
2. Run:
   powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

Outputs:
- ValheimModInstaller.zip
- release\ValheimModInstallerSetup.exe
