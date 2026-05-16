import json
import re
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import unquote

from .thunderstore import parse_thunderstore_package_url


KNOWN_DEPENDENCIES = {
    "com.jotunn.jotunn": {
        "name": "Jotunn",
        "author": "ValheimModding",
        "package": "Jotunn",
        "url": "https://thunderstore.io/c/valheim/p/ValheimModding/Jotunn/",
    }
}


def detect_missing_dependencies(
    extract_dir: Path,
    mod: dict,
    enabled_mods: List[dict],
    bepinex_dir: Optional[Path],
    log: Callable[[str], None],
) -> List[dict]:
    detected_dependencies = dependencies_from_manifest(extract_dir, mod, log)
    detected_dependencies.extend(dependencies_from_dll_strings(extract_dir, log))

    missing = []
    seen = set()
    for dependency in detected_dependencies:
        key = dependency["key"].lower()
        if key in seen:
            continue
        seen.add(key)

        if dependency.get("package", "").lower() == "bepinexpack_valheim" and bepinex_dir is not None:
            log(f"Detected dependency {dependency['display_name']} required by {mod['name']} (ignored: BepInEx selected)")
            continue

        if dependency_present(dependency, enabled_mods):
            log(f"Detected dependency {dependency['display_name']} required by {mod['name']} (present)")
            continue

        warning = {
            "key": f"{dependency['key']} required by {mod['name']}",
            "display_name": dependency["display_name"],
            "required_by": mod["name"],
            "url": dependency.get("url", ""),
        }
        missing.append(warning)
        add_text = f" Add: {warning['url']}" if warning["url"] else ""
        log(f"Missing dependency: {warning['display_name']} required by {mod['name']}.{add_text}")

    return missing


def dependencies_from_manifest(extract_dir: Path, mod: dict, log: Callable[[str], None]) -> List[dict]:
    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.exists():
        return []

    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Could not read dependencies for {mod['name']}: {exc}")
        return []

    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list):
        log(f"Ignoring invalid dependency list in {mod['name']} manifest")
        return []

    detected = []
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue

        dependency_info = dependency_info_from_thunderstore_id(dependency)
        if dependency_info:
            detected.append(dependency_info)

    return detected


def dependencies_from_dll_strings(extract_dir: Path, log: Callable[[str], None]) -> List[dict]:
    detected = []
    for dll_path in extract_dir.rglob("*.dll"):
        try:
            if dll_path.stat().st_size > 100 * 1024 * 1024:
                log(f"Skipping dependency scan for very large DLL: {dll_path.name}")
                continue
            dll_bytes = dll_path.read_bytes()
        except OSError as exc:
            log(f"Could not scan {dll_path.name} for dependencies: {exc}")
            continue

        lower_bytes = dll_bytes.lower()
        for guid, metadata in KNOWN_DEPENDENCIES.items():
            if guid.encode("utf-8") in lower_bytes:
                detected.append(dependency_info_from_known_guid(guid, metadata))

    return detected


def dependency_info_from_thunderstore_id(dependency: str) -> Optional[dict]:
    normalized = normalize_dependency_id(dependency)
    if not normalized:
        return None

    author, package = normalized.split("-", 1) if "-" in normalized else ("", normalized)
    guid = known_guid_for_package(author, package)
    known = KNOWN_DEPENDENCIES.get(guid or "")
    return {
        "key": guid or normalized,
        "display_name": known["name"] if known else package,
        "author": known["author"] if known else author,
        "package": known["package"] if known else package,
        "url": known["url"] if known else f"https://thunderstore.io/c/valheim/p/{author}/{package}/",
        "guid": guid or "",
        "thunderstore_id": normalized,
    }


def dependency_info_from_known_guid(guid: str, metadata: dict) -> dict:
    return {
        "key": guid,
        "display_name": metadata["name"],
        "author": metadata["author"],
        "package": metadata["package"],
        "url": metadata["url"],
        "guid": guid,
        "thunderstore_id": f"{metadata['author']}-{metadata['package']}",
    }


def known_guid_for_package(author: str, package: str) -> Optional[str]:
    for guid, metadata in KNOWN_DEPENDENCIES.items():
        if metadata["author"].lower() == author.lower() and metadata["package"].lower() == package.lower():
            return guid
    return None


def normalize_dependency_id(dependency: str) -> str:
    """Convert Author-Package-Version into Author-Package for matching."""
    parts = dependency.strip().split("-")
    if len(parts) < 2:
        return dependency.strip()
    return "-".join(parts[:2])


def dependency_present(dependency: dict, enabled_mods: List[dict]) -> bool:
    author = dependency.get("author", "")
    package = dependency.get("package", "")
    normalized_dependency = dependency.get("thunderstore_id", f"{author}-{package}")
    guid = dependency.get("guid", "")
    loose_package = loose_match_key(package)
    dependency_key = loose_match_key(normalized_dependency)

    for mod in enabled_mods:
        url = str(mod.get("url", ""))
        name = str(mod.get("name", ""))
        package_id = str(mod.get("package_id", ""))
        known_guid = str(mod.get("dependency_guid", ""))
        parsed_package = parse_thunderstore_package_url(url)

        if parsed_package:
            parsed_author, parsed_name = parsed_package
            if parsed_author.lower() == author.lower() and parsed_name.lower() == package.lower():
                return True
        if package_id.lower() == normalized_dependency.lower():
            return True
        if guid and known_guid.lower() == guid.lower():
            return True

        url_lower = unquote(url).lower()
        if f"/{author}/{package}/".lower() in url_lower:
            return True
        if normalized_dependency.lower() in url_lower or normalized_dependency.replace("-", "_").lower() in url_lower:
            return True
        if guid and guid.lower() in url_lower:
            return True

        if loose_match_key(name) == loose_package:
            return True
        if dependency_key and dependency_key in loose_match_key(name):
            return True

    return False


def loose_match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
