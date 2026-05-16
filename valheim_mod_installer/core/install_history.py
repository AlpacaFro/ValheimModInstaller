import json
from datetime import datetime
from pathlib import Path
from typing import List


APP_NAME = "ValheimModInstaller"
GAME_NAME = "valheim"


def history_path(bepinex_dir: Path) -> Path:
    return bepinex_dir / "_mod_installer_state" / "installed_files.json"


def save_installed_files(
    bepinex_dir: Path,
    mod_name: str,
    source_url: str,
    relative_paths: List[str],
    installed_at: str,
) -> Path:
    """Merge one mod's installed files into BepInEx/_mod_installer_state/installed_files.json.

    History format:
    {
      "app": "ValheimModInstaller",
      "game": "valheim",
      "last_updated": "...",
      "mods": {
        "Mod Name": {
          "mod_name": "Mod Name",
          "source_url": "...",
          "installed_at": "...",
          "files": ["plugins/Mod.dll", "config/Mod.cfg"]
        }
      }
    }
    """
    path = history_path(bepinex_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = load_history(path)
    now = datetime.now().isoformat(timespec="seconds")

    data["app"] = APP_NAME
    data["game"] = GAME_NAME
    data["last_updated"] = now
    data.setdefault("mods", {})
    data["mods"][mod_name] = {
        "mod_name": mod_name,
        "source_url": source_url,
        "installed_at": installed_at,
        "files": sorted(dict.fromkeys(relative_paths)),
    }

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    return path


def load_install_history(bepinex_dir: Path) -> dict:
    return load_history(history_path(bepinex_dir))


def write_install_history(bepinex_dir: Path, data: dict) -> Path:
    path = history_path(bepinex_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    data["app"] = APP_NAME
    data["game"] = GAME_NAME
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")
    if not isinstance(data.get("mods"), dict):
        data["mods"] = {}

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    return path


def load_history(path: Path) -> dict:
    if not path.exists():
        return {"app": APP_NAME, "game": GAME_NAME, "last_updated": "", "mods": {}}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"app": APP_NAME, "game": GAME_NAME, "last_updated": "", "mods": {}}

    if not isinstance(data, dict):
        return {"app": APP_NAME, "game": GAME_NAME, "last_updated": "", "mods": {}}
    if not isinstance(data.get("mods"), dict):
        data["mods"] = {}

    return data
