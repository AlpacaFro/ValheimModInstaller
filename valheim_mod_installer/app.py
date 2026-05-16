import json
import os
import queue
import re
import shutil
import tkinter as tk
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import customtkinter as ctk
import requests
from tkinter import filedialog, messagebox

from .core.dependencies import detect_missing_dependencies, loose_match_key
from .core.downloader import CHUNK_SIZE, REQUEST_TIMEOUT
from .core.install_history import load_install_history, save_installed_files, write_install_history
from .core.thunderstore import (
    UnsupportedURL,
    get_latest_thunderstore_package_info,
    parse_thunderstore_package_url,
    resolve_download_url,
)
from .models.mod import INITIAL_MODS
from .utils.files import guess_download_filename, sanitize_filename


COLORS = {
    "bg": "#0B1020",
    "surface": "#111827",
    "surface_2": "#172033",
    "card": "#1B2540",
    "card_hover": "#24314F",
    "primary": "#38BDF8",
    "primary_hover": "#0EA5E9",
    "secondary": "#8B5CF6",
    "secondary_hover": "#7C3AED",
    "success": "#22C55E",
    "success_hover": "#16A34A",
    "warning": "#F59E0B",
    "warning_hover": "#D97706",
    "danger": "#EF4444",
    "danger_hover": "#DC2626",
    "pink": "#EC4899",
    "text": "#F8FAFC",
    "muted": "#94A3B8",
    "muted_2": "#64748B",
    "border": "#334155",
    "border_hot": "#38BDF8",
}

LIGHT_COLORS = {
    "bg": "#EAF3FF",
    "surface": "#F8FAFC",
    "surface_2": "#E2E8F0",
    "card": "#FFFFFF",
    "card_hover": "#EEF6FF",
    "primary": "#0284C7",
    "primary_hover": "#0369A1",
    "secondary": "#7C3AED",
    "secondary_hover": "#6D28D9",
    "success": "#16A34A",
    "success_hover": "#15803D",
    "warning": "#D97706",
    "warning_hover": "#B45309",
    "danger": "#DC2626",
    "danger_hover": "#B91C1C",
    "pink": "#DB2777",
    "text": "#0F172A",
    "muted": "#475569",
    "muted_2": "#64748B",
    "border": "#CBD5E1",
    "border_hot": "#0284C7",
}


def style_primary_button(button: Any) -> None:
    """Centralized theme helper: bright cyan is reserved for the main happy path."""
    button.configure(
        corner_radius=18,
        fg_color=COLORS["primary"],
        hover_color=COLORS["primary_hover"],
        text_color="#03111F",
        font=ctk.CTkFont(size=14, weight="bold"),
        border_width=1,
        border_color="#7DD3FC",
    )


def style_secondary_button(button: Any) -> None:
    """Centralized theme helper: purple buttons are supportive, not primary."""
    button.configure(
        corner_radius=16,
        fg_color=COLORS["secondary"],
        hover_color=COLORS["secondary_hover"],
        text_color=COLORS["text"],
        font=ctk.CTkFont(size=13, weight="bold"),
        border_width=1,
        border_color="#A78BFA",
    )


def style_muted_button(button: Any) -> None:
    button.configure(
        corner_radius=16,
        fg_color=COLORS["surface_2"],
        hover_color=COLORS["card_hover"],
        text_color=COLORS["text"],
        font=ctk.CTkFont(size=13, weight="bold"),
        border_width=1,
        border_color=COLORS["border"],
    )


def style_danger_button(button: Any) -> None:
    button.configure(
        corner_radius=16,
        fg_color=COLORS["danger"],
        hover_color=COLORS["danger_hover"],
        text_color=COLORS["text"],
        font=ctk.CTkFont(size=13, weight="bold"),
        border_width=1,
        border_color=COLORS["pink"],
    )


def create_status_badge(parent: Any, text: str, width: int = 118) -> ctk.CTkLabel:
    """Centralized badge helper keeps all status pills visually consistent."""
    style = get_status_style(text)
    return ctk.CTkLabel(
        parent,
        text=text,
        width=width,
        height=26,
        corner_radius=14,
        fg_color=style["fg_color"],
        text_color=style["text_color"],
        font=ctk.CTkFont(size=12, weight="bold"),
    )


@dataclass
class InstalledPluginRecord:
    dll_file_name: str
    absolute_path: str
    relative_path: str
    source: str


@dataclass
class InstalledPluginIndex:
    records: List[InstalledPluginRecord] = field(default_factory=list)
    by_mod_name: Dict[str, List[InstalledPluginRecord]] = field(default_factory=dict)
    by_dll_name: Dict[str, InstalledPluginRecord] = field(default_factory=dict)
    by_loose_name: Dict[str, InstalledPluginRecord] = field(default_factory=dict)
    by_source_url: Dict[str, List[InstalledPluginRecord]] = field(default_factory=dict)


def scan_installed_plugins(bepinex_path: Path) -> InstalledPluginIndex:
    index = InstalledPluginIndex()
    bepinex_root = bepinex_path.resolve()
    history = load_install_history(bepinex_root)
    history_mods = history.get("mods", {})

    def add_record(record: InstalledPluginRecord, mod_name: str = "", source_url: str = "") -> None:
        index.records.append(record)
        if mod_name:
            index.by_mod_name.setdefault(loose_match_key(mod_name), []).append(record)
        if source_url:
            index.by_source_url.setdefault(source_url.lower(), []).append(record)
        index.by_dll_name.setdefault(record.dll_file_name.lower(), record)
        index.by_loose_name.setdefault(loose_match_key(Path(record.dll_file_name).stem), record)

    if isinstance(history_mods, dict):
        for mod_name, entry in history_mods.items():
            if not isinstance(entry, dict):
                continue
            source_url = str(entry.get("source_url", ""))
            for relative_file in entry.get("files", []):
                if not isinstance(relative_file, str) or not relative_file.lower().endswith(".dll"):
                    continue
                absolute_path = bepinex_root / relative_file
                add_record(
                    InstalledPluginRecord(
                        dll_file_name=Path(relative_file).name,
                        absolute_path=str(absolute_path),
                        relative_path=relative_file,
                        source="history",
                    ),
                    str(mod_name),
                    source_url,
                )

    for folder_name in ("plugins", "patchers"):
        scan_root = bepinex_root / folder_name
        if not scan_root.exists():
            continue
        for dll_path in scan_root.rglob("*.dll"):
            try:
                relative_path = dll_path.resolve().relative_to(bepinex_root).as_posix()
            except ValueError:
                continue
            if dll_path.name.lower() in index.by_dll_name:
                continue
            add_record(
                InstalledPluginRecord(
                    dll_file_name=dll_path.name,
                    absolute_path=str(dll_path.resolve()),
                    relative_path=relative_path,
                    source="filesystem",
                )
            )

    return index


def normalize_bepinex_selection(selected_path: Path) -> Optional[Path]:
    """Accept the BepInEx folder itself or a Valheim folder containing BepInEx."""
    try:
        selected_path = selected_path.resolve()
    except OSError:
        return None

    if selected_path.name.lower() == "bepinex":
        return selected_path

    nested_bepinex = selected_path / "BepInEx"
    if nested_bepinex.exists():
        return nested_bepinex

    if (selected_path / "Valheim.exe").exists() and nested_bepinex.exists():
        return nested_bepinex

    return None


def validate_bepinex_path(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    if path.name.lower() == "bepinex":
        return True
    return any((path / child).exists() for child in ("plugins", "config", "patchers", "core"))


def get_status_style(status: str) -> dict:
    normalized = str(status or "Ready").strip().lower()
    styles = {
        "ready": {"fg_color": COLORS["surface_2"], "text_color": COLORS["text"]},
        "missing": {"fg_color": "#075985", "text_color": "#E0F2FE"},
        "downloading": {"fg_color": COLORS["primary_hover"], "text_color": "#E0F2FE"},
        "downloaded": {"fg_color": "#2563EB", "text_color": "#EFF6FF"},
        "extracting": {"fg_color": COLORS["warning"], "text_color": "#111827"},
        "installing": {"fg_color": "#F97316", "text_color": "#111827"},
        "installed": {"fg_color": COLORS["success"], "text_color": "#052E16"},
        "failed": {"fg_color": COLORS["danger"], "text_color": "#FFF1F2"},
        "disabled": {"fg_color": COLORS["border"], "text_color": COLORS["muted"]},
        "update available": {"fg_color": COLORS["warning"], "text_color": "#111827"},
        "missing dependencies": {"fg_color": COLORS["pink"], "text_color": "#FFF1F2"},
        "uninstalling": {"fg_color": "#F97316", "text_color": "#111827"},
        "uninstalled": {"fg_color": COLORS["success"], "text_color": "#052E16"},
        "up to date": {"fg_color": COLORS["success"], "text_color": "#052E16"},
        "unknown version": {"fg_color": COLORS["secondary"], "text_color": COLORS["text"]},
        "unknown source": {"fg_color": COLORS["danger"], "text_color": "#FFF1F2"},
    }
    return styles.get(normalized, {"fg_color": COLORS["surface_2"], "text_color": COLORS["text"]})


def validate_mod_data(data: object) -> List[dict]:
    if not isinstance(data, list):
        raise ValueError("The JSON file must contain a list of mods.")

    clean_mods = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Item {index} is not a JSON object.")

        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        parsed = urlparse(url)

        if not name:
            raise ValueError(f"Item {index} is missing a mod name.")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Item {index} has an invalid URL.")

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = str(enabled).strip().lower() not in {"false", "0", "no", "off"}

        status = str(item.get("status", "Ready") or "Ready")
        package_id = str(item.get("package_id", "")).strip()

        clean_mod = {"name": name, "url": url, "enabled": enabled, "status": status}
        if package_id:
            clean_mod["package_id"] = package_id
        for field in (
            "author",
            "package",
            "installed_version",
            "latest_version",
            "latest_download_url",
            "update_status",
            "source_url",
        ):
            value = str(item.get(field, "")).strip()
            if value:
                clean_mod[field] = value

        clean_mods.append(clean_mod)

    return clean_mods


class ValheimModDownloader(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Valheim Mod Downloader")
        self.geometry("920x680")
        self.minsize(760, 560)
        self.configure(fg_color=COLORS["bg"])

        self.mods = [mod.copy() for mod in INITIAL_MODS]
        self.ui_queue: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.is_busy = False
        self.selected_bepinex_path: Optional[Path] = None
        self.missing_dependencies: List[dict] = []
        self.installed_plugin_index = InstalledPluginIndex()
        self.pending_skipped_installed = 0
        self.recent_logs: List[str] = []
        self.mod_details_modal: Optional[ctk.CTkToplevel] = None
        self.mod_details_icon_image: Optional[tk.PhotoImage] = None

        self.mod_name_var = ctk.StringVar()
        self.mod_url_var = ctk.StringVar()
        self.status_var = ctk.StringVar(value="Ready")
        self.bepinex_path_var = ctk.StringVar(value="No folder selected")
        self.bepinex_validation_var = ctk.StringVar(value="Invalid")
        self.install_mode_var = ctk.StringVar(value="gather_dlls")
        self.mod_search_var = ctk.StringVar()
        self.mod_filter_var = ctk.StringVar(value="All")
        self.overwrite_installed_var = ctk.BooleanVar(value=False)
        self.show_advanced_var = ctk.BooleanVar(value=False)
        self.show_disabled_var = ctk.BooleanVar(value=False)
        self.show_modal_technical_var = ctk.BooleanVar(value=False)
        # UI-only state: card expansion and list filters are intentionally not exported.
        self.expanded_mods: set[int] = set()
        self.mod_search_var.trace_add("write", lambda *_: self._render_mods())

        self._build_ui()
        self._refresh_action_availability()
        self._render_mods()
        self.after(100, self._process_ui_queue)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        setup_frame = ctk.CTkFrame(self, corner_radius=22, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"])
        setup_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        setup_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            setup_frame,
            text="Valheim Mod Installer",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text"],
        ).grid(
            row=0,
            column=0,
            columnspan=4,
            sticky="w",
            padx=20,
            pady=(18, 4),
        )
        self.bepinex_path_label = ctk.CTkLabel(
            setup_frame,
            textvariable=self.bepinex_path_var,
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=13),
        )
        self.bepinex_path_label.grid(row=1, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 16))

        self.select_bepinex_button = ctk.CTkButton(
            setup_frame,
            text="Select BepInEx Folder",
            command=self.select_bepinex_folder,
        )
        self.select_bepinex_button.grid(row=1, column=2, padx=(0, 10), pady=(0, 12))
        style_secondary_button(self.select_bepinex_button)

        self.open_bepinex_button = ctk.CTkButton(
            setup_frame,
            text="Open BepInEx Folder",
            command=self.open_bepinex_folder,
            state="disabled",
        )
        self.open_bepinex_button.grid(row=1, column=3, padx=(0, 16), pady=(0, 12))
        style_muted_button(self.open_bepinex_button)

        self.bepinex_validation_label = ctk.CTkLabel(
            setup_frame,
            textvariable=self.bepinex_validation_var,
            width=86,
            height=28,
            corner_radius=14,
            fg_color=COLORS["surface_2"],
            text_color=COLORS["warning"],
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.bepinex_validation_label.grid(row=0, column=3, sticky="e", padx=18, pady=(18, 4))

        action_frame = ctk.CTkFrame(self, corner_radius=22, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"])
        action_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(16, 8))
        action_frame.grid_columnconfigure(3, weight=1)

        self.import_button = ctk.CTkButton(action_frame, text="Load Pack", command=self.import_list)
        self.import_button.grid(row=0, column=0, padx=(0, 10), pady=(12, 6))
        style_secondary_button(self.import_button)

        self.export_button = ctk.CTkButton(action_frame, text="Save Pack", command=self.export_list)
        self.export_button.grid(row=0, column=1, padx=(0, 10), pady=(12, 6))
        style_secondary_button(self.export_button)

        self.check_installed_button = ctk.CTkButton(action_frame, text="Scan", command=self.check_installed_mods)
        self.check_installed_button.grid(row=0, column=2, padx=(0, 10), pady=(12, 6))
        style_secondary_button(self.check_installed_button)

        self.count_label = ctk.CTkLabel(action_frame, text="", anchor="e", text_color=COLORS["muted"], font=ctk.CTkFont(size=13, weight="bold"))
        self.count_label.grid(row=0, column=3, sticky="ew", padx=12, pady=(12, 6))

        self.main_action_button = ctk.CTkButton(
            action_frame,
            text="Install Missing",
            height=42,
            command=self.install_missing_mods,
        )
        self.main_action_button.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(0, 10), pady=(6, 12))
        style_primary_button(self.main_action_button)

        self.overwrite_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="Repair existing mods",
            variable=self.overwrite_installed_var,
        )
        self.overwrite_checkbox.grid(row=1, column=3, sticky="w", padx=12, pady=(6, 12))
        self.overwrite_checkbox.configure(
            text_color=COLORS["muted"],
            fg_color=COLORS["primary"],
            hover_color=COLORS["primary_hover"],
            border_color=COLORS["border"],
            checkbox_width=20,
            checkbox_height=20,
        )

        self.advanced_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="Advanced",
            variable=self.show_advanced_var,
            command=self._toggle_advanced_actions,
        )
        self.advanced_checkbox.grid(row=1, column=4, sticky="e", padx=(0, 12), pady=(6, 12))
        self.advanced_checkbox.configure(
            text_color=COLORS["muted"],
            fg_color=COLORS["secondary"],
            hover_color=COLORS["secondary_hover"],
            border_color=COLORS["border"],
            checkbox_width=20,
            checkbox_height=20,
        )

        self.show_disabled_checkbox = ctk.CTkCheckBox(
            action_frame,
            text="Show disabled",
            variable=self.show_disabled_var,
            command=self._render_mods,
        )
        self.show_disabled_checkbox.grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(0, 12))
        self.show_disabled_checkbox.configure(
            text_color=COLORS["muted"],
            fg_color=COLORS["surface_2"],
            hover_color=COLORS["card_hover"],
            border_color=COLORS["border"],
            checkbox_width=18,
            checkbox_height=18,
        )

        self.next_step_label = ctk.CTkLabel(
            action_frame,
            text="Select your BepInEx folder to scan and install mods.",
            anchor="w",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.next_step_label.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(0, 12), pady=(0, 12))

        self.advanced_frame = ctk.CTkFrame(self, corner_radius=18, fg_color=COLORS["surface_2"], border_width=1, border_color=COLORS["border"])
        self.advanced_frame.grid_columnconfigure(1, weight=1)
        self.advanced_frame.grid_columnconfigure(3, weight=2)

        ctk.CTkLabel(self.advanced_frame, text="Mod Name", text_color=COLORS["muted"], font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, padx=(12, 8), pady=10)
        self.name_entry = ctk.CTkEntry(
            self.advanced_frame,
            textvariable=self.mod_name_var,
            placeholder_text="Better Portals",
            corner_radius=14,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            placeholder_text_color=COLORS["muted_2"],
        )
        self.name_entry.grid(row=0, column=1, padx=(0, 12), pady=10, sticky="ew")

        ctk.CTkLabel(self.advanced_frame, text="Thunderstore / Direct URL", text_color=COLORS["muted"], font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=2, padx=(0, 8), pady=10)
        self.url_entry = ctk.CTkEntry(
            self.advanced_frame,
            textvariable=self.mod_url_var,
            placeholder_text="https://thunderstore.io/c/valheim/p/Tekla/AutoRepair/",
            corner_radius=14,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            placeholder_text_color=COLORS["muted_2"],
        )
        self.url_entry.grid(row=0, column=3, padx=(0, 12), pady=10, sticky="ew")

        self.add_button = ctk.CTkButton(self.advanced_frame, text="Add Mod", command=self.add_mod)
        self.add_button.grid(row=0, column=4, padx=(0, 12), pady=10)
        style_primary_button(self.add_button)

        self.install_mode_selector = ctk.CTkSegmentedButton(
            self.advanced_frame,
            values=["Download only", "Download + Gather DLLs", "Full BepInEx Install"],
            command=self.on_install_mode_changed,
        )
        self.install_mode_selector.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 10))
        self.install_mode_selector.set("Full BepInEx Install")
        self.install_mode_selector.configure(
            corner_radius=16,
            fg_color=COLORS["surface"],
            selected_color=COLORS["primary"],
            selected_hover_color=COLORS["primary_hover"],
            unselected_color=COLORS["surface_2"],
            unselected_hover_color=COLORS["card_hover"],
            text_color=COLORS["text"],
        )
        self.install_mode_var.set("full_install")

        self.run_mode_button = ctk.CTkButton(
            self.advanced_frame,
            text="Run Selected Mode",
            command=self.run_selected_mode,
        )
        self.run_mode_button.grid(row=1, column=2, sticky="ew", padx=(0, 10), pady=(0, 10))
        style_muted_button(self.run_mode_button)

        self.uninstall_button = ctk.CTkButton(
            self.advanced_frame,
            text="Uninstall Enabled Mods",
            fg_color="#7f1d1d",
            hover_color="#991b1b",
            command=self.uninstall_selected_mods,
        )
        self.uninstall_button.grid(row=1, column=3, sticky="ew", padx=(0, 10), pady=(0, 10))
        style_danger_button(self.uninstall_button)

        self.add_dependencies_button = ctk.CTkButton(
            self.advanced_frame,
            text="Add Missing Dependencies",
            state="disabled",
            command=self.add_missing_dependencies,
        )
        self.add_dependencies_button.grid(row=1, column=4, sticky="ew", padx=(0, 10), pady=(0, 10))
        style_secondary_button(self.add_dependencies_button)

        self.check_updates_button = ctk.CTkButton(
            self.advanced_frame,
            text="Check Updates",
            command=self.check_updates,
        )
        self.check_updates_button.grid(row=2, column=4, sticky="ew", padx=(0, 12), pady=(0, 10))
        style_secondary_button(self.check_updates_button)

        self.log_box = ctk.CTkTextbox(
            self.advanced_frame,
            height=120,
            wrap="word",
            corner_radius=16,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["muted"],
        )
        self.log_box.grid(row=3, column=0, columnspan=5, sticky="ew", padx=12, pady=(0, 12))
        self.log_box.configure(state="disabled")

        self.mod_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=COLORS["bg"])
        self.mod_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=8)
        self.mod_frame.grid_columnconfigure(0, weight=1)
        self.mod_frame.grid_rowconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, corner_radius=0, fg_color=COLORS["bg"])
        footer.grid(row=4, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(footer)
        self.progress.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 8))
        self.progress.configure(progress_color=COLORS["primary"], fg_color=COLORS["surface_2"], border_color=COLORS["border"])
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(footer, textvariable=self.status_var, anchor="w", text_color=COLORS["muted"])
        self.status_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

    def _render_mods(self) -> None:
        for child in self.mod_frame.winfo_children():
            child.destroy()

        columns = {
            "To Install": [],
            "Installed": [],
            "Needs Attention": [],
        }
        for index, mod in enumerate(self.mods):
            column_name = self._kanban_column_for_mod(index, mod)
            if column_name == "Disabled" and not self.show_disabled_var.get():
                continue
            if column_name == "Disabled":
                column_name = "Needs Attention"
            columns[column_name].append((index, mod))

        for column_index, (column_name, column_mods) in enumerate(columns.items()):
            self.mod_frame.grid_columnconfigure(column_index, weight=1, uniform="kanban")
            column = ctk.CTkScrollableFrame(
                self.mod_frame,
                label_text=f"{column_name} ({len(column_mods)})",
                corner_radius=20,
                fg_color=COLORS["surface"],
                border_width=1,
                border_color=COLORS["border"],
                label_fg_color=COLORS["surface_2"],
                label_text_color=COLORS["text"],
            )
            column.grid(row=0, column=column_index, sticky="nsew", padx=10, pady=6)
            column.grid_columnconfigure(0, weight=1)

            if not column_mods:
                ctk.CTkLabel(column, text="Nothing here", text_color=COLORS["muted_2"], font=ctk.CTkFont(size=13)).grid(
                    row=0,
                    column=0,
                    sticky="ew",
                    padx=8,
                    pady=28,
                )
                continue

            for row_index, (mod_index, mod) in enumerate(column_mods):
                self._render_kanban_card(column, row_index, mod_index, mod, column_name)

        hidden_disabled = sum(1 for mod in self.mods if not mod.get("enabled", True) and not self.show_disabled_var.get())
        hidden_text = f" | {hidden_disabled} disabled hidden" if hidden_disabled else ""
        self.count_label.configure(text=f"{len(self.mods)} mod{'s' if len(self.mods) != 1 else ''}{hidden_text}")

    def _render_kanban_card(self, parent: Any, row_index: int, mod_index: int, mod: dict, column_name: str) -> None:
        accent = self._column_accent_color(column_name)
        card = ctk.CTkFrame(parent, corner_radius=18, fg_color=COLORS["card"], border_width=1, border_color=accent)
        card.grid(row=row_index, column=0, sticky="ew", padx=7, pady=8)
        card.grid_columnconfigure(1, weight=1)
        self._bind_open_mod_modal(card, mod_index)

        accent_strip = ctk.CTkFrame(card, width=5, corner_radius=8, fg_color=accent)
        accent_strip.grid(row=0, column=0, rowspan=5, sticky="nsw", padx=(8, 0), pady=10)

        name_label = ctk.CTkLabel(
            card,
            text=mod["name"],
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=15, weight="bold"),
            wraplength=320,
        )
        name_label.grid(row=0, column=1, sticky="ew", padx=12, pady=(12, 6))
        self._bind_open_mod_modal(name_label, mod_index)

        badge_text = self._simple_status_label(mod_index, mod)
        badge = create_status_badge(card, badge_text, width=124)
        badge.grid(row=1, column=1, sticky="w", padx=12, pady=(0, 8))
        self._bind_open_mod_modal(badge, mod_index)

        action_row = ctk.CTkFrame(card, fg_color="transparent")
        action_row.grid(row=2, column=1, sticky="ew", padx=12, pady=(0, 12))
        action_row.grid_columnconfigure(1, weight=1)

        actions = self._card_actions_for_column(column_name, mod_index)
        for action_index, (label, command) in enumerate(actions):
            button = ctk.CTkButton(action_row, text=label, width=78, height=30, command=command)
            button.grid(
                row=0,
                column=action_index,
                sticky="ew",
                padx=(0, 6),
            )
            if label in {"Install", "Update", "Repair"}:
                style_primary_button(button)
                if self.selected_bepinex_path is None:
                    button.configure(state="disabled")
            elif label == "Uninstall":
                style_danger_button(button)
            else:
                style_muted_button(button)

    def _column_accent_color(self, column_name: str) -> str:
        colors = {
            "To Install": COLORS["primary"],
            "Installed": COLORS["success"],
            "Needs Attention": COLORS["warning"],
            "Disabled": COLORS["muted_2"],
        }
        return colors.get(column_name, COLORS["border"])

    def _bind_open_mod_modal(self, widget: Any, mod_index: int) -> None:
        widget.bind("<Button-1>", lambda _event, idx=mod_index: self.open_mod_details_modal(idx))
        try:
            widget.configure(cursor="hand2")
        except (tk.TclError, TypeError, ValueError):
            pass

    def open_mod_details_modal(self, mod_index: int) -> None:
        if not (0 <= mod_index < len(self.mods)):
            return

        self.close_mod_details_modal()
        mod = self.mods[mod_index]
        self.show_modal_technical_var.set(False)

        # Modal behavior stays UI-only: one borderless CTkToplevel, centered over
        # the app, grabs focus, and never touches worker-thread state directly.
        modal = ctk.CTkToplevel(self)
        self.mod_details_modal = modal
        self.mod_details_icon_image = None
        modal.withdraw()
        modal.transient(self)
        modal.resizable(False, False)
        modal.overrideredirect(True)
        modal.attributes("-topmost", True)
        modal.bind("<Escape>", lambda _event: self.close_mod_details_modal())

        shell = ctk.CTkFrame(modal, corner_radius=24, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border_hot"])
        shell.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        shell.grid_columnconfigure(1, weight=1)

        close_button = ctk.CTkButton(shell, text="X", width=32, height=28, command=self.close_mod_details_modal)
        close_button.grid(row=0, column=2, sticky="ne", padx=14, pady=12)
        style_muted_button(close_button)

        self._render_mod_icon(shell, mod)

        title_block = ctk.CTkFrame(shell, fg_color="transparent")
        title_block.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(18, 8))
        title_block.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            title_block,
            text=mod["name"],
            anchor="w",
            text_color=COLORS["text"],
            font=ctk.CTkFont(size=24, weight="bold"),
            wraplength=330,
        ).grid(row=0, column=0, sticky="ew")

        badge_text = self._simple_status_label(mod_index, mod)
        create_status_badge(title_block, badge_text, width=156).grid(row=1, column=0, sticky="w", pady=(8, 0))

        summary = ctk.CTkFrame(shell, corner_radius=18, fg_color=COLORS["card"], border_width=1, border_color=COLORS["border"])
        summary.grid(row=1, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 12))
        summary.grid_columnconfigure(1, weight=1)

        summary_rows = self._modal_summary_rows(mod_index, mod)
        for row, (label, value, color) in enumerate(summary_rows):
            ctk.CTkLabel(summary, text=label, anchor="w", text_color=COLORS["muted"], font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=row,
                column=0,
                sticky="w",
                padx=(12, 10),
                pady=(8 if row == 0 else 3, 8 if row == len(summary_rows) - 1 else 3),
            )
            ctk.CTkLabel(summary, text=value, anchor="w", text_color=color, font=ctk.CTkFont(size=13), wraplength=380).grid(
                row=row,
                column=1,
                sticky="ew",
                padx=(0, 12),
                pady=(8 if row == 0 else 3, 8 if row == len(summary_rows) - 1 else 3),
            )

        technical_toggle = ctk.CTkCheckBox(shell, text="Technical details", variable=self.show_modal_technical_var)
        technical_toggle.grid(row=2, column=0, columnspan=3, sticky="w", padx=18, pady=(0, 8))
        technical_toggle.configure(
            text_color=COLORS["muted"],
            fg_color=COLORS["secondary"],
            hover_color=COLORS["secondary_hover"],
            border_color=COLORS["border"],
            checkbox_width=18,
            checkbox_height=18,
        )

        details = ctk.CTkFrame(shell, corner_radius=18, fg_color=COLORS["surface_2"], border_width=1, border_color=COLORS["border"])
        details.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            details,
            text=self._modal_details_text(mod_index, mod),
            justify="left",
            anchor="w",
            text_color=COLORS["muted"],
            wraplength=500,
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=10)

        def toggle_technical_details() -> None:
            if self.show_modal_technical_var.get():
                details.grid(row=3, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 12))
            else:
                details.grid_remove()
            modal.update_idletasks()
            self._center_modal(modal)

        technical_toggle.configure(command=toggle_technical_details)
        details.grid_remove()

        actions = ctk.CTkFrame(shell, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", padx=18, pady=(0, 18))
        actions.grid_columnconfigure(3, weight=1)
        self._render_modal_actions(actions, mod_index)

        modal.update_idletasks()
        self._center_modal(modal)
        modal.deiconify()
        modal.lift(self)
        modal.focus_force()
        modal.grab_set()

    def _render_mod_icon(self, parent: Any, mod: dict) -> None:
        icon_path = str(mod.get("icon_path") or mod.get("cached_icon") or mod.get("cached_icon_path") or mod.get("icon") or "").strip()
        icon_frame = ctk.CTkFrame(
            parent,
            width=118,
            height=118,
            corner_radius=24,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["primary"],
        )
        icon_frame.grid(row=0, column=0, sticky="nw", padx=18, pady=18)
        icon_frame.grid_propagate(False)

        if icon_path and Path(icon_path).exists():
            try:
                image = tk.PhotoImage(file=icon_path)
                shrink_by = max(image.width() // 96, image.height() // 96, 1)
                self.mod_details_icon_image = image.subsample(shrink_by, shrink_by) if shrink_by > 1 else image
                ctk.CTkLabel(icon_frame, text="", image=self.mod_details_icon_image).place(relx=0.5, rely=0.5, anchor="center")
                return
            except tk.TclError:
                self.mod_details_icon_image = None

        initial = str(mod.get("name", "?")).strip()[:1].upper() or "?"
        ctk.CTkLabel(icon_frame, text=initial, font=ctk.CTkFont(size=46, weight="bold"), text_color=COLORS["primary"]).place(
            relx=0.5,
            rely=0.5,
            anchor="center",
        )

    def _modal_summary_rows(self, mod_index: int, mod: dict) -> List[Tuple[str, str, str]]:
        missing = self._missing_dependency_details_for_mod(mod_index, mod)
        dependency_text = f"{len(missing)} missing" if missing else "Looks okay"
        dependency_color = COLORS["warning"] if missing else COLORS["muted"]
        installed_files = self._installed_files_for_mod(mod)
        file_summary = f"{len(installed_files)} tracked" if installed_files else "None tracked"
        return [
            ("State", "Enabled" if mod.get("enabled", True) else "Disabled", COLORS["text"]),
            ("Author", str(mod.get("author") or "Unknown"), COLORS["text"]),
            ("Package", str(mod.get("package") or mod.get("package_id") or "Unknown"), COLORS["text"]),
            ("Version", str(mod.get("installed_version") or mod.get("latest_version") or "Unknown"), COLORS["text"]),
            ("Update", str(mod.get("update_status") or "Not checked"), COLORS["text"]),
            ("Dependencies", dependency_text, dependency_color),
            ("Installed files", file_summary, COLORS["text"]),
        ]

    def _modal_details_text(self, mod_index: int, mod: dict) -> str:
        installed_files = self._installed_files_for_mod(mod)
        missing = self._missing_dependency_details_for_mod(mod_index, mod)
        latest_error = self._latest_failure_for_mod(mod)
        source_url = str(mod.get("source_url") or mod.get("url") or "Not known")
        resolved_url = str(mod.get("url") or "Not known")
        latest_download_url = str(mod.get("latest_download_url") or "")
        if latest_download_url:
            resolved_url = latest_download_url

        lines = [
            "Full installed file paths: " + (", ".join(installed_files) if installed_files else "None tracked"),
            "Missing dependencies: " + ("; ".join(missing) if missing else "None known"),
            "Raw dependency IDs: " + (", ".join(self._known_dependency_refs_for_mod(mod)) or "None known"),
            "Latest failure: " + (latest_error or "None"),
            "Source URL: " + source_url,
            "Resolved URL: " + resolved_url,
            "Internal status: " + str(mod.get("status", "Ready")),
        ]
        return "\n".join(lines)

    def _render_modal_actions(self, parent: Any, mod_index: int) -> None:
        mod = self.mods[mod_index]
        installed = self._is_mod_installed(mod_index, mod)
        disabled = not mod.get("enabled", True)
        column = 0

        if disabled:
            button = ctk.CTkButton(parent, text="Enable", command=lambda idx=mod_index: self._modal_enable_mod(idx))
            button.grid(
                row=0,
                column=column,
                padx=(0, 8),
            )
            style_primary_button(button)
            if self.selected_bepinex_path is None:
                button.configure(state="disabled")
            column += 1
        elif installed:
            button = ctk.CTkButton(parent, text="Repair", command=lambda idx=mod_index: self._modal_repair_mod(idx))
            button.grid(
                row=0,
                column=column,
                padx=(0, 8),
            )
            style_primary_button(button)
            if self.selected_bepinex_path is None:
                button.configure(state="disabled")
            column += 1
            button = ctk.CTkButton(
                parent,
                text="Uninstall",
                command=lambda idx=mod_index: self._modal_uninstall_mod(idx),
            )
            button.grid(row=0, column=column, padx=(0, 8))
            style_danger_button(button)
            if self.selected_bepinex_path is None:
                button.configure(state="disabled")
            column += 1
        else:
            button = ctk.CTkButton(parent, text="Install", command=lambda idx=mod_index: self._modal_install_mod(idx))
            button.grid(
                row=0,
                column=column,
                padx=(0, 8),
            )
            style_primary_button(button)
            if self.selected_bepinex_path is None:
                button.configure(state="disabled")
            column += 1

        close_button = ctk.CTkButton(parent, text="Close", command=self.close_mod_details_modal)
        close_button.grid(row=0, column=4, sticky="e")
        style_muted_button(close_button)

    def _center_modal(self, modal: ctk.CTkToplevel) -> None:
        self.update_idletasks()
        main_x = self.winfo_rootx()
        main_y = self.winfo_rooty()
        main_width = self.winfo_width()
        main_height = self.winfo_height()
        modal_width = modal.winfo_reqwidth()
        modal_height = modal.winfo_reqheight()
        x = main_x + max((main_width - modal_width) // 2, 0)
        y = main_y + max((main_height - modal_height) // 2, 0)
        modal.geometry(f"+{x}+{y}")

    def close_mod_details_modal(self) -> None:
        if self.mod_details_modal is None:
            return
        try:
            self.mod_details_modal.grab_release()
        except tk.TclError:
            pass
        self.mod_details_modal.destroy()
        self.mod_details_modal = None
        self.mod_details_icon_image = None

    def _modal_install_mod(self, mod_index: int) -> None:
        self.close_mod_details_modal()
        self.install_missing_mods([mod_index])

    def _modal_repair_mod(self, mod_index: int) -> None:
        self.close_mod_details_modal()
        self._queue_log(f"Repair requested for {self.mods[mod_index]['name']}")
        self.install_missing_mods([mod_index], force_overwrite=True)

    def _modal_uninstall_mod(self, mod_index: int) -> None:
        self.close_mod_details_modal()
        self.uninstall_mods([mod_index])

    def _modal_enable_mod(self, mod_index: int) -> None:
        self.close_mod_details_modal()
        self.set_mod_enabled(mod_index, True)

    def _card_actions_for_column(self, column_name: str, mod_index: int) -> List[Tuple[str, Any]]:
        mod = self.mods[mod_index]
        if not mod.get("enabled", True):
            return [("Enable", lambda idx=mod_index: self.set_mod_enabled(idx, True))]
        if column_name == "To Install":
            return [("Install", lambda idx=mod_index: self.install_missing_mods([idx]))]
        if column_name == "Installed":
            return [("Repair", lambda idx=mod_index: self.install_missing_mods([idx], force_overwrite=True))]
        if str(mod.get("update_status", "")) == "Update available":
            return [("Update", lambda idx=mod_index: self.install_missing_mods([idx], force_overwrite=True))]
        return [("Open", lambda idx=mod_index: self.open_mod_details_modal(idx))]

    def _kanban_column_for_mod(self, index: int, mod: dict) -> str:
        if not mod.get("enabled", True):
            return "Disabled"
        if str(mod.get("status", "")) == "Failed":
            return "Needs Attention"
        if self._missing_dependency_details_for_mod(index, mod):
            return "Needs Attention"
        if str(mod.get("update_status", "")) == "Update available":
            return "Needs Attention"
        if self._is_mod_installed(index, mod):
            return "Installed"
        return "To Install"

    def _simple_status_label(self, index: int, mod: dict) -> str:
        if not mod.get("enabled", True):
            return "Disabled"
        if str(mod.get("status", "")) == "Failed":
            return "Failed"
        if self._missing_dependency_details_for_mod(index, mod):
            return "Missing dependencies"
        if str(mod.get("update_status", "")) == "Update available":
            return "Update available"
        if self._is_mod_installed(index, mod):
            return "Installed"
        return "Missing"

    def _set_mod_filter(self, selected_filter: str) -> None:
        self.mod_filter_var.set(selected_filter)
        self._render_mods()

    def toggle_mod_details(self, index: int) -> None:
        if index in self.expanded_mods:
            self.expanded_mods.remove(index)
        else:
            self.expanded_mods.add(index)
        self._render_mods()

    def _mod_matches_current_view(self, index: int, mod: dict) -> bool:
        search_text = self.mod_search_var.get().strip().lower()
        if search_text:
            haystack = " ".join(
                str(mod.get(field, ""))
                for field in (
                    "name",
                    "url",
                    "source_url",
                    "author",
                    "package",
                    "package_id",
                    "status",
                    "update_status",
                    "installed_version",
                    "latest_version",
                )
            ).lower()
            if search_text not in haystack:
                return False

        current_filter = self.mod_filter_var.get()
        status = str(mod.get("status", "Ready"))
        if current_filter == "Enabled":
            return bool(mod.get("enabled", True))
        if current_filter == "Disabled":
            return not bool(mod.get("enabled", True))
        if current_filter == "Installed":
            return status == "Installed"
        if current_filter == "Failed":
            return status == "Failed"
        if current_filter == "Updates":
            return str(mod.get("update_status", "")) == "Update available"
        if current_filter == "Missing deps":
            return bool(self._missing_dependency_details_for_mod(index, mod))
        return True

    def _mod_subtitle(self, mod: dict) -> str:
        source = str(mod.get("source_url") or mod.get("url") or "")
        update_status = str(mod.get("update_status", "")).strip()
        latest_version = str(mod.get("latest_version", "")).strip()
        if update_status == "Unknown version" and latest_version:
            return f"{source}  |  Latest: {latest_version}"
        if update_status:
            return f"{source}  |  {update_status}"
        return source

    def _card_badge_text(self, index: int, mod: dict) -> str:
        if self._missing_dependency_details_for_mod(index, mod):
            return "Missing dependencies"
        if str(mod.get("update_status", "")) == "Update available":
            return "Update available"
        return str(mod.get("status", "Ready") or "Ready")

    def _mod_detail_lines(self, index: int, mod: dict) -> List[str]:
        source_url = str(mod.get("source_url") or mod.get("url") or "Not known")
        resolved_url = str(mod.get("url") or "Not known")
        latest_download_url = str(mod.get("latest_download_url") or "")
        if latest_download_url and latest_download_url != resolved_url:
            resolved_url = latest_download_url

        lines = [
            f"Source URL: {source_url}",
            f"Resolved download URL: {resolved_url}",
            f"Author: {mod.get('author') or 'Unknown'}",
            f"Package: {mod.get('package') or mod.get('package_id') or 'Unknown'}",
            f"Installed version: {mod.get('installed_version') or 'Unknown'}",
            f"Latest version: {mod.get('latest_version') or 'Unknown'}",
            f"Update status: {mod.get('update_status') or 'Not checked'}",
        ]

        installed_files = self._installed_files_for_mod(mod)
        lines.append("Installed files: " + (", ".join(installed_files[:8]) if installed_files else "None tracked"))
        if len(installed_files) > 8:
            lines.append(f"Installed files continued: {len(installed_files) - 8} more tracked")

        missing = self._missing_dependency_details_for_mod(index, mod)
        if missing:
            lines.append("Missing dependencies: " + "; ".join(missing))
        else:
            lines.append("Missing dependencies: None known")

        dependency_refs = self._known_dependency_refs_for_mod(mod)
        lines.append("Dependencies: " + (", ".join(dependency_refs) if dependency_refs else "None known"))
        related_logs = self._related_logs_for_mod(mod)
        lines.append("Logs: " + (" | ".join(related_logs) if related_logs else "None yet"))
        return lines

    def _installed_files_for_mod(self, mod: dict) -> List[str]:
        if self.selected_bepinex_path is None:
            return [record.relative_path for record in self._installed_records_for_mod(mod)]

        history = load_install_history(self.selected_bepinex_path)
        installed_mods = history.get("mods", {})
        if not isinstance(installed_mods, dict):
            return [record.relative_path for record in self._installed_records_for_mod(mod)]

        history_entry = installed_mods.get(mod.get("name", ""))
        if not isinstance(history_entry, dict):
            source_url = str(mod.get("source_url") or mod.get("url") or "")
            for candidate in installed_mods.values():
                if isinstance(candidate, dict) and candidate.get("source_url") == source_url:
                    history_entry = candidate
                    break

        files = history_entry.get("files", []) if isinstance(history_entry, dict) else []
        tracked_files = [str(path) for path in files if path]
        if tracked_files:
            return tracked_files
        return [record.relative_path for record in self._installed_records_for_mod(mod)]

    def _missing_dependency_details_for_mod(self, index: int, mod: dict) -> List[str]:
        names = {str(mod.get("name", "")).lower()}
        package = str(mod.get("package") or mod.get("package_id") or "").lower()
        if package:
            names.add(package)

        details = []
        for dependency in self.missing_dependencies:
            required_by = str(dependency.get("required_by", "")).lower()
            if required_by not in names:
                continue
            display_name = dependency.get("display_name", "Unknown dependency")
            url = dependency.get("url", "")
            details.append(f"{display_name} ({url})" if url else str(display_name))
        return details

    def _known_dependency_refs_for_mod(self, mod: dict) -> List[str]:
        refs = []
        dependency_guid = str(mod.get("dependency_guid", "")).strip()
        if dependency_guid:
            refs.append(dependency_guid)
        package_id = str(mod.get("package_id", "")).strip()
        if package_id:
            refs.append(package_id)
        return refs

    def _is_mod_installed(self, index: int, mod: dict) -> bool:
        return bool(self._installed_records_for_mod(mod))

    def _installed_records_for_mod(self, mod: dict) -> List[InstalledPluginRecord]:
        name_key = loose_match_key(str(mod.get("name", "")))
        source_url = str(mod.get("source_url") or mod.get("url") or "").lower()
        package = str(mod.get("package") or mod.get("package_id") or mod.get("name") or "")
        package_key = loose_match_key(package)
        records: List[InstalledPluginRecord] = []

        records.extend(self.installed_plugin_index.by_mod_name.get(name_key, []))
        if source_url:
            records.extend(self.installed_plugin_index.by_source_url.get(source_url, []))
        if package_key:
            candidate = self.installed_plugin_index.by_loose_name.get(package_key)
            if candidate:
                records.append(candidate)

        for token in {name_key, package_key}:
            if not token:
                continue
            for dll_key, record in self.installed_plugin_index.by_loose_name.items():
                if token == dll_key or token in dll_key or dll_key in token:
                    records.append(record)

        deduped = {}
        for record in records:
            deduped[record.relative_path] = record
        return list(deduped.values())

    def _refresh_installed_state_from_scan(self) -> None:
        for index, mod in enumerate(self.mods):
            if not mod.get("enabled", True):
                mod["status"] = "Disabled"
            elif self._is_mod_installed(index, mod) and str(mod.get("status", "")) != "Failed":
                mod["status"] = "Installed"
            elif str(mod.get("status", "")) in {"Installed", "Uninstalled"}:
                mod["status"] = "Ready"

    def _related_logs_for_mod(self, mod: dict) -> List[str]:
        name = str(mod.get("name", "")).lower()
        if not name:
            return []
        return [line for line in self.recent_logs[-80:] if name in line.lower()][-6:]

    def _latest_failure_for_mod(self, mod: dict) -> str:
        name = str(mod.get("name", "")).lower()
        if not name:
            return ""
        for line in reversed(self.recent_logs):
            lower_line = line.lower()
            if name in lower_line and ("fail" in lower_line or "error" in lower_line):
                return line
        return ""

    def _status_color(self, status: str) -> str:
        colors = {
            "Ready": COLORS["text"],
            "Downloading": COLORS["primary"],
            "Downloaded": COLORS["primary"],
            "Extracting": COLORS["warning"],
            "Installing": COLORS["warning"],
            "Installed": COLORS["success"],
            "Uninstalling": COLORS["warning"],
            "Uninstalled": COLORS["success"],
            "Failed": COLORS["danger"],
            "Disabled": COLORS["muted_2"],
            "Update available": COLORS["warning"],
            "Missing dependencies": COLORS["pink"],
        }
        return colors.get(status, COLORS["text"])

    def _update_status_color(self, status: str) -> str:
        colors = {
            "Up to date": COLORS["success"],
            "Update available": COLORS["warning"],
            "Unknown version": COLORS["primary"],
            "Unknown source": COLORS["danger"],
            "Missing dependencies": COLORS["pink"],
        }
        return colors.get(status, COLORS["muted"])

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.add_button,
            self.export_button,
            self.import_button,
            self.main_action_button,
            self.uninstall_button,
            self.run_mode_button,
            self.add_dependencies_button,
            self.check_updates_button,
            self.select_bepinex_button,
            self.open_bepinex_button,
            self.check_installed_button,
            self.install_mode_selector,
            self.overwrite_checkbox,
            self.advanced_checkbox,
        ):
            if button is self.add_dependencies_button and not busy:
                button.configure(state="normal" if self.missing_dependencies else "disabled")
            elif button is self.open_bepinex_button and not busy:
                button.configure(state="normal" if self.selected_bepinex_path is not None else "disabled")
            else:
                button.configure(state=state)
        if not busy:
            self._refresh_action_availability()

    def _refresh_action_availability(self) -> None:
        has_bepinex = self.selected_bepinex_path is not None
        install_state = "normal" if has_bepinex and self.mods and not self.is_busy else "disabled"
        scan_state = "normal" if has_bepinex and not self.is_busy else "disabled"
        self.main_action_button.configure(state=install_state)
        self.check_installed_button.configure(state=scan_state)
        self.open_bepinex_button.configure(state=scan_state)
        if has_bepinex:
            if self.mods:
                self.next_step_label.configure(text="Load a pack, scan installed mods, then install what is missing.", text_color=COLORS["muted"])
            else:
                self.next_step_label.configure(text="BepInEx is ready. Load a pack to see what to install.", text_color=COLORS["muted"])
            style_secondary_button(self.select_bepinex_button)
        else:
            self.next_step_label.configure(text="Select your BepInEx folder to scan and install mods.", text_color=COLORS["warning"])
            style_primary_button(self.select_bepinex_button)

    def on_install_mode_changed(self, selected_label: str) -> None:
        label_to_mode = {
            "Download only": "download_only",
            "Download + Gather DLLs": "gather_dlls",
            "Full BepInEx Install": "full_install",
        }

        mode = label_to_mode[selected_label]
        self.install_mode_var.set(mode)

    def set_mod_enabled(self, index: int, enabled: bool) -> None:
        if self.is_busy or not (0 <= index < len(self.mods)):
            return
        self.mods[index]["enabled"] = enabled
        self.mods[index]["status"] = "Ready" if enabled else "Disabled"
        self._render_mods()
        self._queue_log(f"{'Enabled' if enabled else 'Disabled'} {self.mods[index]['name']}")

    def install_missing_mods(self, selected_indices: Optional[List[int]] = None, force_overwrite: bool = False) -> None:
        if self.is_busy:
            return
        if self.selected_bepinex_path is None:
            messagebox.showerror("BepInEx Folder Required", "Select a valid BepInEx folder before installing mods.")
            return
        if not self.mods:
            messagebox.showinfo("No Mods", "Import or add a modpack first.")
            return

        self.check_installed_mods(show_message=False)
        overwrite = force_overwrite or self.overwrite_installed_var.get()
        target_indices = set(selected_indices if selected_indices is not None else range(len(self.mods)))
        install_indices = []
        skipped_installed = 0

        # Default safe path: install only missing enabled mods. Installed mods are
        # skipped unless the user explicitly opts into overwrite/update.
        for index, mod in enumerate(self.mods):
            if index not in target_indices or not mod.get("enabled", True):
                continue
            installed = self._is_mod_installed(index, mod)
            update_available = str(mod.get("update_status", "")) == "Update available"
            if installed and not overwrite and not (force_overwrite and update_available):
                skipped_installed += 1
                mod["status"] = "Installed"
                self._queue_log(f"Skipping {mod['name']} because it is already installed.")
                continue
            if selected_indices is None and installed and not overwrite:
                continue
            install_indices.append(index)

        if not install_indices:
            self._render_mods()
            messagebox.showinfo("Nothing To Install", f"All selected mods are already installed. Skipped: {skipped_installed}.")
            return

        target_dir = self.selected_bepinex_path / "_mod_installer_downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        mods_snapshot = []
        for index, mod in enumerate(self.mods):
            mod_copy = mod.copy()
            mod_copy["enabled"] = index in install_indices
            mods_snapshot.append(mod_copy)
            if index in install_indices:
                self.mods[index]["status"] = "Ready"

        self.pending_skipped_installed = skipped_installed
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("Installing missing mods...")
        self._render_mods()
        self._queue_log(f"Installing {len(install_indices)} mod(s); skipped already installed {skipped_installed}")

        worker = threading.Thread(
            target=self._download_worker,
            args=(mods_snapshot, target_dir, "full_install", self.selected_bepinex_path),
            daemon=True,
        )
        worker.start()

    def uninstall_mods(self, selected_indices: List[int]) -> None:
        if self.is_busy:
            return
        bepinex_dir = self.selected_bepinex_path
        if bepinex_dir is None:
            messagebox.showerror("BepInEx Folder Required", "Select a valid BepInEx folder before uninstalling mods.")
            return
        selected_mods = [(index, self.mods[index].copy()) for index in selected_indices if 0 <= index < len(self.mods)]
        if not selected_mods:
            return
        self._start_uninstall_worker(selected_mods, bepinex_dir)

    def select_bepinex_folder(self) -> None:
        if self.is_busy:
            return

        selected_target = filedialog.askdirectory(title="Choose BepInEx Folder or Valheim Folder")
        if not selected_target:
            return

        selected_path = Path(selected_target)
        self._queue_log(f"Selected folder: {selected_path}")
        bepinex_dir = normalize_bepinex_selection(selected_path)
        if bepinex_dir is None or not validate_bepinex_path(bepinex_dir):
            self.selected_bepinex_path = None
            self.installed_plugin_index = InstalledPluginIndex()
            self.bepinex_path_label.configure(text_color=COLORS["danger"])
            self.bepinex_validation_var.set("Invalid")
            self.bepinex_validation_label.configure(fg_color="#3B1424", text_color="#FDA4AF", border_width=1, border_color=COLORS["danger"])
            self.open_bepinex_button.configure(state="disabled")
            self.bepinex_path_var.set("No folder selected")
            self._refresh_action_availability()
            self._queue_log("BepInEx validation failed")
            messagebox.showerror(
                "BepInEx Not Found",
                "Could not find BepInEx. Select either your Valheim folder or your Valheim/BepInEx folder.",
            )
            return

        self.selected_bepinex_path = bepinex_dir
        self.bepinex_path_label.configure(text_color=COLORS["success"])
        self.bepinex_validation_var.set("Valid")
        self.bepinex_validation_label.configure(fg_color="#052E16", text_color=COLORS["success"], border_width=1, border_color=COLORS["success"])
        self.open_bepinex_button.configure(state="normal")
        self.bepinex_path_var.set(f"BepInEx detected: {bepinex_dir}")
        self._queue_log(f"Detected BepInEx folder: {bepinex_dir}")
        self._queue_log("BepInEx validation passed")
        self._refresh_action_availability()
        # Simplified flow: choosing BepInEx immediately scans installed plugins so
        # the board can show what is missing without asking the user to think about it.
        self.check_installed_mods(show_message=False)

    def open_bepinex_folder(self) -> None:
        if self.selected_bepinex_path is None:
            messagebox.showinfo("No Folder Selected", "Select a BepInEx folder first.")
            return
        os.startfile(str(self.selected_bepinex_path))

    def check_installed_mods(self, show_message: bool = True) -> None:
        if self.selected_bepinex_path is None:
            if show_message:
                messagebox.showinfo("No Folder Selected", "Select a BepInEx folder first.")
            return
        self.installed_plugin_index = scan_installed_plugins(self.selected_bepinex_path)
        self._refresh_installed_state_from_scan()
        self._render_mods()
        self.status_var.set(f"Scanned {len(self.installed_plugin_index.records)} installed plugin DLLs")
        self._refresh_action_availability()
        self._queue_log(f"Scanned installed plugins: {len(self.installed_plugin_index.records)} DLLs indexed")
        if show_message:
            messagebox.showinfo("Installed Mods Checked", f"Found {len(self.installed_plugin_index.records)} plugin DLLs.")

    def _toggle_advanced_actions(self) -> None:
        if self.show_advanced_var.get():
            self.advanced_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        else:
            self.advanced_frame.grid_remove()

    def _queue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def _set_missing_dependencies(self, dependency_warnings: List[dict]) -> None:
        self.missing_dependencies = []
        seen = set()

        for warning in dependency_warnings:
            url = warning.get("url", "")
            display_name = warning.get("display_name", "")
            if not url or not display_name:
                continue

            package_parts = parse_thunderstore_package_url(url)
            author = package_parts[0] if package_parts else warning.get("author", "")
            package = package_parts[1] if package_parts else warning.get("package", display_name)
            key = (author.lower(), package.lower(), url.lower())
            if key in seen:
                continue

            seen.add(key)
            self.missing_dependencies.append(
                {
                    "display_name": display_name,
                    "author": author,
                    "package": package,
                    "url": url,
                    "required_by": warning.get("required_by", ""),
                }
            )

        self.add_dependencies_button.configure(state="normal" if self.missing_dependencies and not self.is_busy else "disabled")

    def add_missing_dependencies(self) -> None:
        if self.is_busy:
            return
        if not self.missing_dependencies:
            messagebox.showinfo("No Missing Dependencies", "There are no missing dependencies to add.")
            return

        added = 0
        for dependency in self.missing_dependencies:
            if self._dependency_already_in_mod_list(dependency):
                continue

            package_id = ""
            package_parts = parse_thunderstore_package_url(dependency["url"])
            if package_parts:
                package_id = f"{package_parts[0]}-{package_parts[1]}"

            self.mods.append(
                {
                    "name": dependency["display_name"],
                    "url": dependency["url"],
                    "enabled": True,
                    "status": "Ready",
                    **({"package_id": package_id} if package_id else {}),
                }
            )
            added += 1
            self._append_log(f"Added missing dependency: {dependency['display_name']} required by {dependency['required_by']}")

        # Dependencies are added to the list only. The user must run install again
        # so they can review the new mods before anything is downloaded or installed.
        self.missing_dependencies = []
        self.add_dependencies_button.configure(state="disabled")
        self._render_mods()
        messagebox.showinfo("Dependencies Added", f"Added {added} missing dependencies. Run install again.")

    def _dependency_already_in_mod_list(self, dependency: dict) -> bool:
        target_name = loose_match_key(dependency.get("display_name", ""))
        target_url = dependency.get("url", "").lower()
        target_author = dependency.get("author", "").lower()
        target_package = dependency.get("package", "").lower()

        for mod in self.mods:
            if loose_match_key(str(mod.get("name", ""))) == target_name:
                return True
            if str(mod.get("url", "")).lower() == target_url:
                return True

            package_parts = parse_thunderstore_package_url(str(mod.get("url", "")))
            if package_parts and package_parts[0].lower() == target_author and package_parts[1].lower() == target_package:
                return True

        return False

    def delete_mod(self, index: int) -> None:
        if self.is_busy:
            return
        del self.mods[index]
        self.expanded_mods = {
            expanded_index if expanded_index < index else expanded_index - 1
            for expanded_index in self.expanded_mods
            if expanded_index != index
        }
        self._render_mods()

    def toggle_mod_enabled(self, index: int) -> None:
        if self.is_busy or not (0 <= index < len(self.mods)):
            return

        enabled = not self.mods[index].get("enabled", True)
        self.set_mod_enabled(index, enabled)

    def add_mod(self) -> None:
        if self.is_busy:
            return

        name = self.mod_name_var.get().strip()
        url = self.mod_url_var.get().strip()
        parsed = urlparse(url)

        if not name:
            messagebox.showerror("Missing Name", "Enter a mod name before adding it.")
            return
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            messagebox.showerror("Invalid URL", "Enter a valid http or https URL.")
            return

        self._set_busy(True)
        self.status_var.set("Validating URL...")
        worker = threading.Thread(target=self._validate_url_worker, args=(name, url), daemon=True)
        worker.start()

    def _validate_url_worker(self, name: str, url: str) -> None:
        try:
            # Resolve page URLs in the worker thread so API calls never freeze the GUI.
            package_id = parse_thunderstore_package_url(url)
            download_url = resolve_download_url(url)
            mod_data = {"name": name, "url": download_url, "enabled": True, "status": "Ready"}
            if package_id:
                mod_data["package_id"] = f"{package_id[0]}-{package_id[1]}"
                mod_data["author"] = package_id[0]
                mod_data["package"] = package_id[1]
                mod_data["source_url"] = url
            self.ui_queue.put(("validation_success", mod_data))
        except requests.RequestException as exc:
            self.ui_queue.put(("validation_error", f"Could not reach the URL: {exc}"))
        except UnsupportedURL as exc:
            self.ui_queue.put(("validation_error", str(exc)))
        except ValueError as exc:
            self.ui_queue.put(("validation_error", str(exc)))

    def export_list(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export Mod List",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile="valheim_modpack.json",
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as handle:
                export_data = [
                    {
                        "name": mod["name"],
                        "url": mod["url"],
                        "enabled": mod.get("enabled", True),
                        "status": mod.get("status", "Ready"),
                        **({"package_id": mod["package_id"]} if mod.get("package_id") else {}),
                        **({"author": mod["author"]} if mod.get("author") else {}),
                        **({"package": mod["package"]} if mod.get("package") else {}),
                        **({"installed_version": mod["installed_version"]} if mod.get("installed_version") else {}),
                        **({"latest_version": mod["latest_version"]} if mod.get("latest_version") else {}),
                        **({"latest_download_url": mod["latest_download_url"]} if mod.get("latest_download_url") else {}),
                        **({"update_status": mod["update_status"]} if mod.get("update_status") else {}),
                        **({"source_url": mod["source_url"]} if mod.get("source_url") else {}),
                    }
                    for mod in self.mods
                ]
                json.dump(export_data, handle, indent=2, ensure_ascii=False)
            self.status_var.set(f"Exported {len(self.mods)} mods to {Path(path).name}")
            self._queue_log(f"Exported {len(self.mods)} mods to {path}")
        except OSError as exc:
            messagebox.showerror("Export Failed", f"Could not save the file:\n{exc}")

    def import_list(self) -> None:
        if self.is_busy:
            return

        path = filedialog.askopenfilename(
            title="Import Mod List",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as handle:
                imported = validate_mod_data(json.load(handle))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            messagebox.showerror("Import Failed", f"Could not import the list:\n{exc}")
            return

        self.mods = imported
        self.expanded_mods.clear()
        self.mod_search_var.set("")
        self.mod_filter_var.set("All")
        if hasattr(self, "mod_filter_selector"):
            self.mod_filter_selector.set("All")
        if self.selected_bepinex_path is not None:
            self.installed_plugin_index = scan_installed_plugins(self.selected_bepinex_path)
            self._refresh_installed_state_from_scan()
        self._render_mods()
        self._refresh_action_availability()
        self.progress.set(0)
        self.status_var.set(f"Imported {len(self.mods)} mods from {Path(path).name}")
        self._queue_log(f"Imported {len(self.mods)} mods from {path}")

    def run_selected_mode(self) -> None:
        if self.is_busy:
            return
        if not self.mods:
            messagebox.showinfo("No Mods", "There are no mods left to download.")
            return
        if not any(mod.get("enabled", True) for mod in self.mods):
            messagebox.showinfo("No Enabled Mods", "No enabled mods selected.")
            self._queue_log("Run blocked because no enabled mods are selected")
            return

        directory = filedialog.askdirectory(title="Choose Download Folder")
        if not directory:
            return

        mode = self.install_mode_var.get()
        bepinex_dir = None
        if mode == "full_install":
            bepinex_dir = self.selected_bepinex_path
            if bepinex_dir is None:
                messagebox.showerror(
                    "BepInEx Folder Required",
                    "Select a valid BepInEx folder before using Full BepInEx Install.",
                )
                self._queue_log("Full install blocked because no valid BepInEx folder is selected")
                return

        self._set_busy(True)
        self.progress.set(0)
        mode_labels = {
            "download_only": "download-only run",
            "gather_dlls": "download and DLL gather run",
            "full_install": "full BepInEx install run",
        }
        self.status_var.set(f"Starting {mode_labels.get(mode, mode)}...")
        for mod in self.mods:
            mod["status"] = "Ready" if mod.get("enabled", True) else "Disabled"
        self._render_mods()
        self._queue_log(f"Starting {mode_labels.get(mode, mode)}")
        skipped_installed = 0
        if mode == "full_install" and bepinex_dir is not None and not self.overwrite_installed_var.get():
            self.check_installed_mods(show_message=False)
        mods_snapshot = []
        for index, mod in enumerate(self.mods):
            mod_copy = mod.copy()
            if (
                mode == "full_install"
                and bepinex_dir is not None
                and not self.overwrite_installed_var.get()
                and self._is_mod_installed(index, mod)
            ):
                mod_copy["enabled"] = False
                skipped_installed += 1
                self._queue_log(f"Skipping {mod['name']} because it is already installed.")
            mods_snapshot.append(mod_copy)
        if mode == "full_install" and not any(mod.get("enabled", True) for mod in mods_snapshot):
            self.status_var.set("Nothing to install; selected mods are already installed.")
            self._set_busy(False)
            self._render_mods()
            return
        self.pending_skipped_installed = skipped_installed

        worker = threading.Thread(
            target=self._download_worker,
            args=(mods_snapshot, Path(directory), mode, bepinex_dir),
            daemon=True,
        )
        worker.start()

    def uninstall_selected_mods(self) -> None:
        if self.is_busy:
            return

        bepinex_dir = self.selected_bepinex_path
        if bepinex_dir is None:
            messagebox.showerror("BepInEx Folder Required", "Select a valid BepInEx folder before uninstalling mods.")
            self._queue_log("Uninstall blocked because no valid BepInEx folder is selected")
            return

        selected_mods = [(index, mod.copy()) for index, mod in enumerate(self.mods) if mod.get("enabled", True)]
        if not selected_mods:
            messagebox.showinfo("No Enabled Mods", "No enabled mods selected.")
            self._queue_log("Uninstall blocked because no enabled mods are selected")
            return

        self._start_uninstall_worker(selected_mods, bepinex_dir)

    def _start_uninstall_worker(self, selected_mods: List[Tuple[int, dict]], bepinex_dir: Path) -> None:
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("Starting uninstall...")
        for mod in self.mods:
            mod["status"] = "Ready" if mod.get("enabled", True) else "Disabled"
        self._render_mods()
        self._queue_log(f"Starting uninstall for {len(selected_mods)} selected mods")

        worker = threading.Thread(
            target=self._uninstall_worker,
            args=(selected_mods, bepinex_dir),
            daemon=True,
        )
        worker.start()

    def check_updates(self) -> None:
        if self.is_busy:
            return

        enabled_mods = [(index, mod.copy()) for index, mod in enumerate(self.mods) if mod.get("enabled", True)]
        if not enabled_mods:
            messagebox.showinfo("No Enabled Mods", "No enabled mods selected.")
            self._queue_log("Update check blocked because no enabled mods are selected")
            return

        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("Checking Thunderstore updates...")
        self._queue_log(f"Checking updates for {len(enabled_mods)} enabled mods")

        worker = threading.Thread(target=self._check_updates_worker, args=(enabled_mods,), daemon=True)
        worker.start()

    def _check_updates_worker(self, enabled_mods: List[Tuple[int, dict]]) -> None:
        total_mods = len(enabled_mods)
        checked = 0
        update_count = 0
        unknown_count = 0
        failures: List[str] = []

        for enabled_index, (original_index, mod) in enumerate(enabled_mods):
            name = mod["name"]
            self.ui_queue.put(("status", f"Checking updates for {name}..."))

            package_ref = self._resolve_mod_package_reference(mod)
            if package_ref is None:
                unknown_count += 1
                metadata = {"update_status": "Unknown source"}
                self.ui_queue.put(("mod_update_checked", original_index, metadata))
                self.ui_queue.put(("log", f"{name}: Unknown source"))
                self.ui_queue.put(("progress", (enabled_index + 1) / total_mods))
                continue

            author, package = package_ref
            try:
                latest_info = get_latest_thunderstore_package_info(author, package)
                latest_version = latest_info.get("version_number", "")
                latest_download_url = latest_info.get("download_url", "")
                installed_version = str(mod.get("installed_version", "")).strip()

                if not installed_version:
                    # Unknown-version handling: we can show the latest version, but
                    # we do not claim an update is available without a known baseline.
                    update_status = "Unknown version"
                    unknown_count += 1
                    log_message = f"{name}: Latest: {latest_version or 'unknown'}"
                elif installed_version != latest_version:
                    # Version comparison is intentionally conservative: Thunderstore
                    # versions are treated as strings so unusual semver forms stay intact.
                    update_status = "Update available"
                    update_count += 1
                    log_message = f"{name}: Update available {installed_version} -> {latest_version}"
                else:
                    update_status = "Up to date"
                    log_message = f"{name}: Up to date"

                checked += 1
                metadata = {
                    "author": author,
                    "package": package,
                    "package_id": f"{author}-{package}",
                    "latest_version": latest_version,
                    "latest_download_url": latest_download_url,
                    "update_status": update_status,
                }
                self.ui_queue.put(("mod_update_checked", original_index, metadata))
                self.ui_queue.put(("log", log_message))
            except (requests.RequestException, ValueError) as exc:
                unknown_count += 1
                failures.append(f"{name}: {exc}")
                self.ui_queue.put(("mod_update_checked", original_index, {"update_status": "Unknown source"}))
                self.ui_queue.put(("log", f"{name}: Unknown source ({exc})"))

            self.ui_queue.put(("progress", (enabled_index + 1) / total_mods))

        self.ui_queue.put(("updates_complete", checked, update_count, unknown_count, failures))

    def _resolve_mod_package_reference(self, mod: dict) -> Optional[Tuple[str, str]]:
        url = str(mod.get("source_url") or mod.get("url") or "")
        package_ref = parse_thunderstore_package_url(url)
        if package_ref:
            return package_ref

        author = str(mod.get("author", "")).strip()
        package = str(mod.get("package", "")).strip()
        if author and package:
            return author, package

        package_id = str(mod.get("package_id", "")).strip()
        if "-" in package_id:
            author, package = package_id.split("-", 1)
            if author and package:
                return author, package

        # Resolved Thunderstore download URLs often include Author-Package-Version.
        # Recover Author/Package when that pattern is visible in the direct URL.
        match = re.search(r"([A-Za-z0-9_]+)-([A-Za-z0-9_]+)-\d+(?:\.\d+)*", url)
        if match:
            return match.group(1), match.group(2)

        return None

    def _uninstall_worker(self, selected_mods: List[Tuple[int, dict]], bepinex_dir: Path) -> None:
        history = load_install_history(bepinex_dir)
        history_mods = history.get("mods", {})
        if not isinstance(history_mods, dict):
            history_mods = {}
            history["mods"] = history_mods

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_uninstall")
        backup_dir = bepinex_dir / "_mod_installer_backups" / timestamp
        failures: List[str] = []
        backed_up_files = 0
        deleted_files = 0
        processed_mods = 0
        history_changed = False
        total_mods = len(selected_mods)

        for selected_index, (original_index, mod) in enumerate(selected_mods):
            mod_name = mod["name"]
            processed_mods += 1
            self.ui_queue.put(("mod_status", original_index, "Uninstalling"))
            self.ui_queue.put(("status", f"Uninstalling {mod_name}..."))
            self.ui_queue.put(("log", f"Uninstalling {mod_name}"))

            entry = history_mods.get(mod_name)
            if not isinstance(entry, dict):
                self.ui_queue.put(("log", f"No install history found for {mod_name}. Skipping."))
                self.ui_queue.put(("mod_status", original_index, "Uninstalled"))
                self.ui_queue.put(("progress", (selected_index + 1) / total_mods))
                continue

            tracked_files = entry.get("files", [])
            if not isinstance(tracked_files, list):
                tracked_files = []

            remaining_files = []
            mod_failed = False
            for relative_file in tracked_files:
                if not isinstance(relative_file, str):
                    continue

                try:
                    backup_count, delete_count = self._backup_and_delete_tracked_file(
                        bepinex_dir,
                        backup_dir,
                        relative_file,
                    )
                    backed_up_files += backup_count
                    deleted_files += delete_count
                    if delete_count:
                        history_changed = True
                    else:
                        remaining_files.append(relative_file)
                except (OSError, ValueError) as exc:
                    mod_failed = True
                    failures.append(f"{mod_name}: {relative_file}: {exc}")
                    remaining_files.append(relative_file)
                    self.ui_queue.put(("log", f"Failed to uninstall {relative_file} for {mod_name}: {exc}"))

            if remaining_files:
                entry["files"] = remaining_files
            else:
                history_mods.pop(mod_name, None)
                history_changed = True

            self.ui_queue.put(("mod_status", original_index, "Failed" if mod_failed else "Uninstalled"))
            self.ui_queue.put(("progress", (selected_index + 1) / total_mods))

        history_path = ""
        if history_changed:
            saved_path = write_install_history(bepinex_dir, history)
            history_path = str(saved_path)
            self.ui_queue.put(("log", f"Updated install history: {saved_path}"))

        self.ui_queue.put(("uninstall_complete", processed_mods, backed_up_files, deleted_files, failures, history_path))

    def _backup_and_delete_tracked_file(self, bepinex_dir: Path, backup_dir: Path, relative_file: str) -> Tuple[int, int]:
        relative_path = Path(relative_file)
        if relative_path.is_absolute():
            raise ValueError("history path is absolute; refusing to delete")

        bepinex_root = bepinex_dir.resolve()
        target_path = (bepinex_root / relative_path).resolve()

        # Safety check: only delete paths that resolve inside the selected BepInEx folder.
        try:
            target_path.relative_to(bepinex_root)
        except ValueError:
            raise ValueError("resolved path is outside the selected BepInEx folder")

        if not target_path.exists():
            self.ui_queue.put(("log", f"Tracked file no longer exists, skipping: {relative_file}"))
            return 0, 0
        if not target_path.is_file():
            raise ValueError("tracked path is not a file; refusing to delete")

        backup_path = (backup_dir / relative_path).resolve()

        # Safety check: preserve BepInEx-relative structure in the uninstall backup folder.
        backup_root = backup_dir.resolve()
        try:
            backup_path.relative_to(backup_root)
        except ValueError:
            raise ValueError("backup path escaped uninstall backup folder")

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self._unique_destination(backup_path)
        shutil.copy2(target_path, backup_path)
        self.ui_queue.put(("log", f"Backed up {relative_file} to {backup_path}"))

        target_path.unlink()
        self.ui_queue.put(("log", f"Deleted tracked file: {relative_file}"))
        return 1, 1

    def _resolve_bepinex_folder(self, selected_path: Path) -> Path:
        """Accept either a Valheim folder containing BepInEx or the BepInEx folder itself."""
        bepinex_dir = normalize_bepinex_selection(selected_path)
        if bepinex_dir is None or not validate_bepinex_path(bepinex_dir):
            raise ValueError(
                "Could not find BepInEx. Select either your Valheim folder or your Valheim/BepInEx folder."
            )

        # The installer can create these standard BepInEx folders if they are missing,
        # but the parent BepInEx folder must already exist to avoid installing elsewhere.
        for folder_name in ("plugins", "config", "patchers"):
            target = bepinex_dir / folder_name
            if target.exists() and not target.is_dir():
                raise ValueError(f"BepInEx/{folder_name} exists but is not a folder.")

        return bepinex_dir

    def _download_worker(
        self,
        mods: List[dict],
        target_dir: Path,
        install_mode: str = "gather_dlls",
        bepinex_dir: Optional[Path] = None,
    ) -> None:
        successes = 0
        gathered_dlls = 0
        installed_files = 0
        backed_up_files = 0
        history_path = ""
        failures: List[str] = []
        dependency_warnings: List[dict] = []
        ready_plugins_dir = target_dir / "ready_plugins"
        backup_dir = None
        if bepinex_dir is not None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_dir = bepinex_dir / "_mod_installer_backups" / timestamp

        # Keep the original UI row index with each enabled mod so status updates
        # target the correct row after disabled mods are filtered out.
        enabled_mods = [(index, mod) for index, mod in enumerate(mods) if mod.get("enabled", True)]
        total_mods = len(enabled_mods)
        if total_mods == 0:
            self.ui_queue.put(("downloads_complete", 0, 0, 0, 0, "", ["No enabled mods selected."], []))
            return
        enabled_mod_refs = [mod for _, mod in enabled_mods]

        for enabled_index, (original_index, mod) in enumerate(enabled_mods):
            self.ui_queue.put(("mod_status", original_index, "Downloading"))
            self.ui_queue.put(("log", f"Downloading {mod['name']}"))
            self.ui_queue.put(("status", f"Downloading {mod['name']}..."))
            base_progress = enabled_index / total_mods
            destination = None

            try:
                # Imported/initial lists may still contain Thunderstore page URLs. Resolve them
                # here in the worker before downloading, then update the GUI list afterward.
                download_url = resolve_download_url(mod["url"])
                if download_url != mod["url"]:
                    package_id = parse_thunderstore_package_url(mod["url"])
                    if package_id:
                        mod["package_id"] = f"{package_id[0]}-{package_id[1]}"
                        mod["author"] = package_id[0]
                        mod["package"] = package_id[1]
                        mod["source_url"] = mod["url"]
                        self.ui_queue.put(("mod_package_id_resolved", original_index, mod["package_id"]))
                        self.ui_queue.put(
                            (
                                "mod_metadata_resolved",
                                original_index,
                                {"author": package_id[0], "package": package_id[1], "source_url": mod["url"]},
                            )
                        )
                    self.ui_queue.put(("mod_url_resolved", original_index, download_url))
                    mod["url"] = download_url

                with requests.get(download_url, stream=True, allow_redirects=True, timeout=REQUEST_TIMEOUT) as response:
                    response.raise_for_status()

                    filename = guess_download_filename(mod, response)
                    destination = self._unique_destination(target_dir / filename)
                    total_bytes = int(response.headers.get("content-length") or 0)
                    downloaded = 0

                    with open(destination, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                            if not chunk:
                                continue

                            handle.write(chunk)
                            downloaded += len(chunk)

                            if total_bytes:
                                current_progress = base_progress + (downloaded / total_bytes / total_mods)
                                self.ui_queue.put(("progress", min(current_progress, 1.0)))

                successes += 1
                self.ui_queue.put(("mod_status", original_index, "Downloaded"))
                self.ui_queue.put(("log", f"Downloaded {mod['name']}"))

                # Mode controls the post-download work. Download-only stops here;
                # the safer default extracts/gathers DLLs; full install also copies into BepInEx.
                if install_mode in {"gather_dlls", "full_install"}:
                    try:
                        self.ui_queue.put(("mod_status", original_index, "Extracting"))
                        copied_count, extract_dir = self._gather_dlls_from_download(
                            destination, mod, target_dir, ready_plugins_dir
                        )
                        gathered_dlls += copied_count
                        if copied_count:
                            self.ui_queue.put(("status", f"Copied {copied_count} DLL files from {mod['name']}"))
                            self.ui_queue.put(("log", f"Copied {copied_count} DLL files from {mod['name']} into ready_plugins"))

                        if extract_dir is not None:
                            dependency_warnings.extend(
                                detect_missing_dependencies(
                                    extract_dir,
                                    mod,
                                    enabled_mod_refs,
                                    bepinex_dir,
                                    lambda message: self.ui_queue.put(("log", message)),
                                )
                            )

                        if install_mode == "full_install" and bepinex_dir is not None and extract_dir is not None and backup_dir is not None:
                            self.ui_queue.put(("mod_status", original_index, "Installing"))
                            self.ui_queue.put(("status", f"Installing {mod['name']} to BepInEx..."))
                            installed_count, backup_count, installed_paths = self._install_extracted_mod(
                                extract_dir, mod, bepinex_dir, backup_dir
                            )
                            installed_files += installed_count
                            backed_up_files += backup_count
                            if installed_paths:
                                saved_path = save_installed_files(
                                    bepinex_dir,
                                    mod["name"],
                                    mod.get("url", ""),
                                    installed_paths,
                                    datetime.now().isoformat(timespec="seconds"),
                                )
                                history_path = str(saved_path)
                                self.ui_queue.put(("log", f"Install history saved: {saved_path}"))
                            self.ui_queue.put(("status", f"Installed {installed_count} files from {mod['name']}"))
                            self.ui_queue.put(("log", f"Installed {installed_count} files from {mod['name']}"))
                            self.ui_queue.put(("mod_status", original_index, "Installed"))
                        elif install_mode == "full_install" and destination.suffix.lower() == ".dll" and bepinex_dir is not None and backup_dir is not None:
                            self.ui_queue.put(("mod_status", original_index, "Installing"))
                            self.ui_queue.put(("status", f"Installing {mod['name']} to BepInEx..."))
                            installed_count, backup_count, installed_path = self._copy_with_backup(
                                destination, bepinex_dir / "plugins" / destination.name, backup_dir
                            )
                            installed_files += installed_count
                            backed_up_files += backup_count
                            if installed_path:
                                saved_path = save_installed_files(
                                    bepinex_dir,
                                    mod["name"],
                                    mod.get("url", ""),
                                    [installed_path],
                                    datetime.now().isoformat(timespec="seconds"),
                                )
                                history_path = str(saved_path)
                                self.ui_queue.put(("log", f"Install history saved: {saved_path}"))
                            self.ui_queue.put(("status", f"Installed {installed_count} files from {mod['name']}"))
                            self.ui_queue.put(("log", f"Installed {installed_count} files from {mod['name']}"))
                            self.ui_queue.put(("mod_status", original_index, "Installed"))
                        else:
                            self.ui_queue.put(("mod_status", original_index, "Downloaded"))
                    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                        failures.append(f"{mod['name']} extraction/install: {exc}")
                        self.ui_queue.put(("mod_status", original_index, "Failed"))
                        self.ui_queue.put(("log", f"Failed {mod['name']} extraction/install: {exc}"))
                else:
                    self.ui_queue.put(("mod_status", original_index, "Downloaded"))
            except requests.RequestException as exc:
                failures.append(f"{mod['name']}: {exc}")
                self.ui_queue.put(("mod_status", original_index, "Failed"))
                self.ui_queue.put(("log", f"Failed {mod['name']}: {exc}"))
            except ValueError as exc:
                failures.append(f"{mod['name']}: {exc}")
                self.ui_queue.put(("mod_status", original_index, "Failed"))
                self.ui_queue.put(("log", f"Failed {mod['name']}: {exc}"))
            except OSError as exc:
                failures.append(f"{mod['name']}: {exc}")
                self.ui_queue.put(("mod_status", original_index, "Failed"))
                self.ui_queue.put(("log", f"Failed {mod['name']}: {exc}"))

            self.ui_queue.put(("progress", (enabled_index + 1) / total_mods))

        unique_dependency_warnings = list({warning["key"]: warning for warning in dependency_warnings}.values())
        self.ui_queue.put(
            (
                "downloads_complete",
                successes,
                gathered_dlls,
                installed_files,
                backed_up_files,
                history_path,
                failures,
                unique_dependency_warnings,
            )
        )

    def _gather_dlls_from_download(
        self,
        downloaded_file: Path,
        mod: dict,
        target_dir: Path,
        ready_plugins_dir: Path,
    ) -> Tuple[int, Optional[Path]]:
        """Extract/copy downloaded mod DLLs into one ready-to-install plugins folder."""
        suffix = downloaded_file.suffix.lower()
        ready_plugins_dir.mkdir(parents=True, exist_ok=True)

        if suffix == ".dll":
            destination = self._unique_destination(ready_plugins_dir / downloaded_file.name)
            shutil.copy2(downloaded_file, destination)
            return 1, None

        if suffix != ".zip":
            return 0, None

        self.ui_queue.put(("status", f"Extracting {mod['name']}..."))
        extract_dir = self._unique_destination(target_dir / "_extracted" / sanitize_filename(mod["name"]))
        extract_dir.mkdir(parents=True, exist_ok=True)

        # Thunderstore packages are zip files. We unpack each mod into an isolated
        # folder, then gather every nested plugin DLL into ready_plugins.
        with zipfile.ZipFile(downloaded_file) as archive:
            self._safe_extract_zip(archive, extract_dir)

        copied_count = 0
        for dll_path in extract_dir.rglob("*.dll"):
            destination = self._unique_destination(ready_plugins_dir / dll_path.name)
            shutil.copy2(dll_path, destination)
            copied_count += 1

        return copied_count, extract_dir

    def _install_extracted_mod(
        self,
        extract_dir: Path,
        mod: dict,
        bepinex_dir: Path,
        backup_dir: Path,
    ) -> Tuple[int, int, List[str]]:
        """Install known Thunderstore folders while preserving BepInEx structure."""
        installed = 0
        backed_up = 0
        installed_paths: List[str] = []

        # Thunderstore packages usually include plugins/config/patchers folders.
        # Copy each folder's *contents* into the matching BepInEx destination.
        install_rules = {
            "plugins": bepinex_dir / "plugins",
            "config": bepinex_dir / "config",
            "patchers": bepinex_dir / "patchers",
        }

        found_plugins_folder = False
        for folder_name, destination_root in install_rules.items():
            for source_root in self._find_named_folders(extract_dir, folder_name):
                if folder_name == "plugins":
                    found_plugins_folder = True
                copied_count, backup_count, copied_paths = self._copy_tree_contents_with_backup(source_root, destination_root, backup_dir)
                installed += copied_count
                backed_up += backup_count
                installed_paths.extend(copied_paths)

        # Some packages do not have a plugins folder and place DLLs elsewhere.
        # In that case, copy every DLL into BepInEx/plugins as a safe fallback.
        if not found_plugins_folder:
            for dll_path in extract_dir.rglob("*.dll"):
                copied_count, backup_count, copied_path = self._copy_with_backup(
                    dll_path, bepinex_dir / "plugins" / dll_path.name, backup_dir
                )
                installed += copied_count
                backed_up += backup_count
                if copied_path:
                    installed_paths.append(copied_path)

        return installed, backed_up, installed_paths

    def _find_named_folders(self, root: Path, folder_name: str) -> List[Path]:
        return [path for path in root.rglob("*") if path.is_dir() and path.name.lower() == folder_name]

    def _safe_extract_zip(self, archive: zipfile.ZipFile, extract_dir: Path) -> None:
        """Extract a zip while blocking paths that would escape the chosen extraction folder."""
        extract_root = extract_dir.resolve()
        for member in archive.infolist():
            destination = (extract_root / member.filename).resolve()
            try:
                destination.relative_to(extract_root)
            except ValueError:
                raise ValueError(f"Unsafe zip path blocked: {member.filename}")
        archive.extractall(extract_root)

    def _copy_tree_contents_with_backup(self, source_root: Path, destination_root: Path, backup_dir: Path) -> Tuple[int, int, List[str]]:
        copied = 0
        backed_up = 0
        copied_paths: List[str] = []
        for source_path in source_root.rglob("*"):
            if not source_path.is_file():
                continue
            relative_path = source_path.relative_to(source_root)
            copied_count, backup_count, copied_path = self._copy_with_backup(source_path, destination_root / relative_path, backup_dir)
            copied += copied_count
            backed_up += backup_count
            if copied_path:
                copied_paths.append(copied_path)
        return copied, backed_up, copied_paths

    def _copy_with_backup(self, source_path: Path, destination_path: Path, backup_dir: Path) -> Tuple[int, int, str]:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        backed_up = 0

        if destination_path.exists():
            # Keep the same BepInEx-relative folder layout inside the backup folder.
            backup_path = backup_dir / destination_path.name
            try:
                relative_backup_path = destination_path.relative_to(backup_dir.parents[1])
                backup_path = backup_dir / relative_backup_path
            except ValueError:
                pass

            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = self._unique_destination(backup_path)
            shutil.copy2(destination_path, backup_path)
            backed_up = 1

        shutil.copy2(source_path, destination_path)
        bepinex_dir = backup_dir.parents[1]
        relative_path = destination_path.relative_to(bepinex_dir).as_posix()
        return 1, backed_up, relative_path

    def _unique_destination(self, destination: Path) -> Path:
        if not destination.exists():
            return destination

        stem = destination.stem
        suffix = destination.suffix
        parent = destination.parent

        counter = 2
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _process_ui_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                event_name = event[0]

                if event_name == "validation_success":
                    self.mods.append(event[1])
                    self.mod_name_var.set("")
                    self.mod_url_var.set("")
                    self._render_mods()
                    self._set_busy(False)
                    self.status_var.set(f"Added {event[1]['name']}")

                elif event_name == "validation_error":
                    self._set_busy(False)
                    self.status_var.set("URL validation failed")
                    messagebox.showerror("Validation Failed", event[1])

                elif event_name == "status":
                    self.status_var.set(event[1])

                elif event_name == "log":
                    self._append_log(event[1])

                elif event_name == "mod_status":
                    _, index, status = event
                    if 0 <= index < len(self.mods):
                        self.mods[index]["status"] = status
                        self._render_mods()

                elif event_name == "progress":
                    self.progress.set(float(event[1]))

                elif event_name == "mod_url_resolved":
                    _, index, download_url = event
                    if 0 <= index < len(self.mods):
                        self.mods[index]["url"] = download_url
                    self._render_mods()

                elif event_name == "mod_package_id_resolved":
                    _, index, package_id = event
                    if 0 <= index < len(self.mods):
                        self.mods[index]["package_id"] = package_id

                elif event_name == "mod_metadata_resolved":
                    _, index, metadata = event
                    if 0 <= index < len(self.mods):
                        self.mods[index].update(metadata)

                elif event_name == "mod_update_checked":
                    _, index, metadata = event
                    if 0 <= index < len(self.mods):
                        for key, value in metadata.items():
                            if value:
                                self.mods[index][key] = value
                        self._render_mods()

                elif event_name == "downloads_complete":
                    _, successes, gathered_dlls, installed_files, backed_up_files, history_path, failures, dependency_warnings = event
                    self._set_busy(False)
                    self.progress.set(1 if successes else 0)
                    skipped_installed = self.pending_skipped_installed
                    self.pending_skipped_installed = 0
                    if self.selected_bepinex_path is not None:
                        self.installed_plugin_index = scan_installed_plugins(self.selected_bepinex_path)
                        self._refresh_installed_state_from_scan()
                    simple_summary = (
                        f"Installed: {successes}\n"
                        f"Skipped already installed: {skipped_installed}\n"
                        f"Failed: {len(failures)}\n"
                        f"Missing dependencies: {len(dependency_warnings)}"
                    )
                    self.status_var.set(
                        f"Installed {successes}, skipped {skipped_installed}, "
                        f"failed {len(failures)}, missing deps {len(dependency_warnings)}"
                    )
                    self._append_log(
                        f"Finished. Downloaded {successes}, installed {installed_files}, skipped already installed {skipped_installed}, "
                        f"gathered {gathered_dlls} DLLs, backed up {backed_up_files}, "
                        f"failures {len(failures)}, missing dependencies {len(dependency_warnings)}"
                    )
                    history_text = f"\n\nInstall history saved:\n{history_path}" if history_path else ""
                    if history_path:
                        self._append_log(f"Install history saved at {history_path}")

                    dependency_text = ""
                    if dependency_warnings:
                        dependency_text = "\n\nMissing dependencies detected:\n" + "\n".join(
                            self._format_dependency_warning(warning) for warning in dependency_warnings[:20]
                        )
                        self._set_missing_dependencies(dependency_warnings)
                        self._append_log("Missing dependencies detected:")
                        for warning in dependency_warnings:
                            self._append_log(self._format_dependency_warning(warning))
                    else:
                        self._set_missing_dependencies([])

                    if failures:
                        messagebox.showwarning(
                            "Downloads Complete With Errors",
                            simple_summary + "\n\n"
                            "Some operations failed:\n\n"
                            + "\n".join(failures[:10])
                            + history_text
                            + dependency_text,
                        )
                    elif dependency_warnings:
                        messagebox.showwarning(
                            "Missing Dependencies Detected",
                            simple_summary
                            + history_text
                            + dependency_text,
                        )
                    else:
                        messagebox.showinfo(
                            "Downloads Complete",
                            simple_summary
                            + history_text,
                        )
                    self._render_mods()

                elif event_name == "uninstall_complete":
                    _, processed_mods, backed_up_files, deleted_files, failures, history_path = event
                    self._set_busy(False)
                    self.progress.set(1)
                    if self.selected_bepinex_path is not None:
                        self.installed_plugin_index = scan_installed_plugins(self.selected_bepinex_path)
                        self._refresh_installed_state_from_scan()
                        self._render_mods()
                    self.status_var.set(
                        f"Uninstall finished: {processed_mods} mods processed, {deleted_files} deleted, "
                        f"{backed_up_files} backed up, {len(failures)} failures"
                    )
                    self._append_log(
                        f"Uninstall finished. Mods processed {processed_mods}, backed up {backed_up_files}, "
                        f"deleted {deleted_files}, failures {len(failures)}"
                    )

                    history_text = f"\n\nInstall history updated:\n{history_path}" if history_path else ""
                    summary = (
                        f"{processed_mods} mods processed\n"
                        f"{backed_up_files} files backed up\n"
                        f"{deleted_files} files deleted\n"
                        f"{len(failures)} failures"
                        + history_text
                    )

                    if failures:
                        messagebox.showwarning(
                            "Uninstall Complete With Errors",
                            summary + "\n\nFailures:\n" + "\n".join(failures[:10]),
                        )
                    else:
                        messagebox.showinfo("Uninstall Complete", summary)

                elif event_name == "updates_complete":
                    _, checked, update_count, unknown_count, failures = event
                    self._set_busy(False)
                    self.progress.set(1)
                    self.status_var.set(
                        f"Update check finished: {checked} checked, {update_count} updates, "
                        f"{unknown_count} unknown, {len(failures)} failures"
                    )
                    self._append_log(
                        f"Update check finished. Checked {checked}, updates {update_count}, "
                        f"unknown {unknown_count}, failures {len(failures)}"
                    )

                    summary = (
                        f"{checked} mods checked\n"
                        f"{update_count} updates available\n"
                        f"{unknown_count} unknown source/version\n"
                        f"{len(failures)} failures"
                    )
                    if failures:
                        messagebox.showwarning(
                            "Update Check Complete With Errors",
                            summary + "\n\nFailures:\n" + "\n".join(failures[:10]),
                        )
                    else:
                        messagebox.showinfo("Update Check Complete", summary)

        except queue.Empty:
            pass
        finally:
            self.after(100, self._process_ui_queue)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.recent_logs.append(f"[{timestamp}] {message}")
        self.recent_logs = self.recent_logs[-300:]
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _format_dependency_warning(self, warning: dict) -> str:
        add_text = f"\n  Add: {warning['url']}" if warning.get("url") else ""
        return f"- {warning['display_name']} required by {warning['required_by']}{add_text}"

if __name__ == "__main__":
    app = ValheimModDownloader()
    app.mainloop()
