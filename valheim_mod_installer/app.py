import json
import queue
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

import customtkinter as ctk
import requests
from tkinter import filedialog, messagebox

from .core.dependencies import detect_missing_dependencies, loose_match_key
from .core.downloader import CHUNK_SIZE, REQUEST_TIMEOUT
from .core.install_history import save_installed_files
from .core.thunderstore import UnsupportedURL, parse_thunderstore_package_url, resolve_download_url
from .models.mod import INITIAL_MODS
from .utils.files import guess_download_filename, sanitize_filename


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

        self.mods = [mod.copy() for mod in INITIAL_MODS]
        self.ui_queue: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self.is_busy = False
        self.selected_bepinex_path: Optional[Path] = None

        self.mod_name_var = ctk.StringVar()
        self.mod_url_var = ctk.StringVar()
        self.status_var = ctk.StringVar(value="Ready")
        self.bepinex_path_var = ctk.StringVar(value="No BepInEx folder selected")
        self.install_mode_var = ctk.StringVar(value="gather_dlls")

        self._build_ui()
        self._render_mods()
        self.after(100, self._process_ui_queue)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header.grid_columnconfigure(3, weight=2)

        ctk.CTkLabel(header, text="Mod Name").grid(row=0, column=0, padx=(16, 8), pady=16)
        self.name_entry = ctk.CTkEntry(header, textvariable=self.mod_name_var, placeholder_text="Better Portals")
        self.name_entry.grid(row=0, column=1, padx=(0, 12), pady=16, sticky="ew")

        ctk.CTkLabel(header, text="Thunderstore Page / Direct URL").grid(row=0, column=2, padx=(0, 8), pady=16)
        self.url_entry = ctk.CTkEntry(
            header,
            textvariable=self.mod_url_var,
            placeholder_text="https://thunderstore.io/c/valheim/p/Tekla/AutoRepair/",
        )
        self.url_entry.grid(row=0, column=3, padx=(0, 12), pady=16, sticky="ew")

        self.add_button = ctk.CTkButton(header, text="Add to List", command=self.add_mod)
        self.add_button.grid(row=0, column=4, padx=(0, 16), pady=16)

        toolbar = ctk.CTkFrame(self, corner_radius=0)
        toolbar.grid(row=1, column=0, sticky="ew", padx=16, pady=(16, 8))
        toolbar.grid_columnconfigure(2, weight=1)

        self.export_button = ctk.CTkButton(toolbar, text="Export List", command=self.export_list)
        self.export_button.grid(row=0, column=0, padx=(0, 10), pady=12)

        self.import_button = ctk.CTkButton(toolbar, text="Import List", command=self.import_list)
        self.import_button.grid(row=0, column=1, padx=(0, 10), pady=12)

        self.count_label = ctk.CTkLabel(toolbar, text="")
        self.count_label.grid(row=0, column=3, padx=12, pady=12)

        bepinex_bar = ctk.CTkFrame(self, corner_radius=0)
        bepinex_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        bepinex_bar.grid_columnconfigure(1, weight=1)

        self.select_bepinex_button = ctk.CTkButton(
            bepinex_bar,
            text="Select BepInEx Folder",
            command=self.select_bepinex_folder,
        )
        self.select_bepinex_button.grid(row=0, column=0, padx=(0, 10), pady=10)

        self.bepinex_path_label = ctk.CTkLabel(
            bepinex_bar,
            textvariable=self.bepinex_path_var,
            anchor="w",
            text_color="#facc15",
        )
        self.bepinex_path_label.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=10)

        self.mod_frame = ctk.CTkScrollableFrame(self, label_text="Current Mod List")
        self.mod_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=8)
        self.mod_frame.grid_columnconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(self, height=120, wrap="word")
        self.log_box.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.log_box.configure(state="disabled")

        footer = ctk.CTkFrame(self, corner_radius=0)
        footer.grid(row=5, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self.install_mode_selector = ctk.CTkSegmentedButton(
            footer,
            values=["Download only", "Download + Gather DLLs", "Full BepInEx Install"],
            command=self.on_install_mode_changed,
        )
        self.install_mode_selector.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
        self.install_mode_selector.set("Download + Gather DLLs")

        self.main_action_button = ctk.CTkButton(
            footer,
            text="Download and Gather DLLs",
            height=42,
            command=self.run_selected_mode,
        )
        self.main_action_button.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        self.progress = ctk.CTkProgressBar(footer)
        self.progress.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.progress.set(0)

        self.status_label = ctk.CTkLabel(footer, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))

    def _render_mods(self) -> None:
        for child in self.mod_frame.winfo_children():
            child.destroy()

        for index, mod in enumerate(self.mods):
            enabled = mod.get("enabled", True)
            text_color = "#d1d5db" if enabled else "#6b7280"
            url_color = "#9ca3af" if enabled else "#4b5563"

            row = ctk.CTkFrame(self.mod_frame)
            row.grid(row=index, column=0, sticky="ew", padx=6, pady=6)
            row.grid_columnconfigure(1, weight=1)

            enabled_var = ctk.BooleanVar(value=enabled)
            enabled_checkbox = ctk.CTkCheckBox(
                row,
                text="",
                variable=enabled_var,
                width=28,
                command=lambda idx=index: self.toggle_mod_enabled(idx),
            )
            enabled_checkbox.grid(row=0, column=0, rowspan=2, padx=(12, 4), pady=10)

            label = ctk.CTkLabel(row, text=mod["name"], anchor="w", text_color=text_color)
            label.grid(row=0, column=1, sticky="ew", padx=12, pady=10)

            url_label = ctk.CTkLabel(row, text=mod["url"], anchor="w", text_color=url_color)
            url_label.grid(row=1, column=1, sticky="ew", padx=12, pady=(0, 10))

            status = mod.get("status", "Ready")
            status_label = ctk.CTkLabel(row, text=status, width=92, anchor="center", text_color=self._status_color(status))
            status_label.grid(row=0, column=2, rowspan=2, padx=(6, 0), pady=10)

            delete_button = ctk.CTkButton(
                row,
                text="Delete",
                fg_color="#b91c1c",
                hover_color="#991b1b",
                width=88,
                command=lambda idx=index: self.delete_mod(idx),
            )
            delete_button.grid(row=0, column=3, rowspan=2, padx=12, pady=10)

        self.count_label.configure(text=f"{len(self.mods)} mod{'s' if len(self.mods) != 1 else ''} loaded")

    def _status_color(self, status: str) -> str:
        colors = {
            "Ready": "#d1d5db",
            "Downloading": "#60a5fa",
            "Downloaded": "#93c5fd",
            "Extracting": "#facc15",
            "Installing": "#fb923c",
            "Installed": "#4ade80",
            "Failed": "#f87171",
            "Disabled": "#6b7280",
        }
        return colors.get(status, "#d1d5db")

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.add_button,
            self.export_button,
            self.import_button,
            self.main_action_button,
            self.select_bepinex_button,
            self.install_mode_selector,
        ):
            button.configure(state=state)

    def on_install_mode_changed(self, selected_label: str) -> None:
        label_to_mode = {
            "Download only": "download_only",
            "Download + Gather DLLs": "gather_dlls",
            "Full BepInEx Install": "full_install",
        }
        mode_to_button_text = {
            "download_only": "Download Mods",
            "gather_dlls": "Download and Gather DLLs",
            "full_install": "Download and Install to BepInEx",
        }

        mode = label_to_mode[selected_label]
        self.install_mode_var.set(mode)
        self.main_action_button.configure(text=mode_to_button_text[mode])

    def select_bepinex_folder(self) -> None:
        if self.is_busy:
            return

        selected_target = filedialog.askdirectory(title="Choose BepInEx Folder or Valheim Folder")
        if not selected_target:
            return

        try:
            bepinex_dir = self._resolve_bepinex_folder(Path(selected_target))
        except ValueError as exc:
            self.selected_bepinex_path = None
            self.bepinex_path_label.configure(text_color="#f87171")
            self.bepinex_path_var.set(f"Invalid BepInEx folder: {selected_target}")
            self._queue_log(f"Invalid BepInEx selection: {exc}")
            messagebox.showerror("Invalid BepInEx Folder", str(exc))
            return

        self.selected_bepinex_path = bepinex_dir
        self.bepinex_path_label.configure(text_color="#4ade80")
        self.bepinex_path_var.set(f"Valid BepInEx folder: {bepinex_dir}")
        self._queue_log(f"Selected valid BepInEx folder: {bepinex_dir}")

    def _queue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def delete_mod(self, index: int) -> None:
        if self.is_busy:
            return
        del self.mods[index]
        self._render_mods()

    def toggle_mod_enabled(self, index: int) -> None:
        if self.is_busy or not (0 <= index < len(self.mods)):
            return

        enabled = not self.mods[index].get("enabled", True)
        self.mods[index]["enabled"] = enabled
        self.mods[index]["status"] = "Ready" if enabled else "Disabled"
        self._render_mods()
        self._queue_log(f"{'Enabled' if enabled else 'Disabled'} {self.mods[index]['name']}")

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
        self._render_mods()
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
        mods_snapshot = [mod.copy() for mod in self.mods]

        worker = threading.Thread(
            target=self._download_worker,
            args=(mods_snapshot, Path(directory), mode, bepinex_dir),
            daemon=True,
        )
        worker.start()

    def _resolve_bepinex_folder(self, selected_path: Path) -> Path:
        """Accept either a Valheim folder containing BepInEx or the BepInEx folder itself."""
        selected_path = selected_path.resolve()
        standard_children = ("plugins", "config", "patchers")

        if selected_path.name.lower() == "bepinex" or any((selected_path / child).exists() for child in standard_children):
            bepinex_dir = selected_path
        else:
            bepinex_dir = selected_path / "BepInEx"

        if not bepinex_dir.exists():
            raise ValueError(
                "Could not find a BepInEx folder. Select your Valheim folder containing BepInEx, "
                "or select the BepInEx folder directly."
            )
        if not bepinex_dir.is_dir():
            raise ValueError("The detected BepInEx path is not a folder.")

        # The installer can create these standard BepInEx folders if they are missing,
        # but the parent BepInEx folder must already exist to avoid installing elsewhere.
        for folder_name in standard_children:
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
                        self.ui_queue.put(("mod_package_id_resolved", original_index, mod["package_id"]))
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

                elif event_name == "downloads_complete":
                    _, successes, gathered_dlls, installed_files, backed_up_files, history_path, failures, dependency_warnings = event
                    self._set_busy(False)
                    self.progress.set(1 if successes else 0)
                    self.status_var.set(
                        f"Finished: {successes} downloaded, {installed_files} installed, "
                        f"{gathered_dlls} DLLs gathered, {backed_up_files} backed up, {len(failures)} failed"
                    )
                    self._append_log(
                        f"Finished. Downloaded {successes}, installed {installed_files}, "
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
                        self._append_log("Missing dependencies detected:")
                        for warning in dependency_warnings:
                            self._append_log(self._format_dependency_warning(warning))

                    if failures:
                        messagebox.showwarning(
                            "Downloads Complete With Errors",
                            f"Downloaded {successes} files. Installed {installed_files} files. "
                            f"Gathered {gathered_dlls} DLL files into ready_plugins. "
                            f"Backed up {backed_up_files} files.\n\n"
                            "Some operations failed:\n\n"
                            + "\n".join(failures[:10])
                            + history_text
                            + dependency_text,
                        )
                    elif dependency_warnings:
                        messagebox.showwarning(
                            "Missing Dependencies Detected",
                            f"Downloaded {successes} files. Installed {installed_files} files. "
                            f"Gathered {gathered_dlls} DLL files into ready_plugins. "
                            f"Backed up {backed_up_files} files."
                            + history_text
                            + dependency_text,
                        )
                    else:
                        messagebox.showinfo(
                            "Downloads Complete",
                            f"Downloaded {successes} files. Installed {installed_files} files. "
                            f"Gathered {gathered_dlls} DLL files into ready_plugins. "
                            f"Backed up {backed_up_files} files."
                            + history_text,
                        )

                    if dependency_warnings:
                        self._offer_to_add_missing_dependencies(dependency_warnings)

        except queue.Empty:
            pass
        finally:
            self.after(100, self._process_ui_queue)

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _format_dependency_warning(self, warning: dict) -> str:
        add_text = f"\n  Add: {warning['url']}" if warning.get("url") else ""
        return f"- {warning['display_name']} required by {warning['required_by']}{add_text}"

    def _offer_to_add_missing_dependencies(self, dependency_warnings: List[dict]) -> None:
        addable = []
        seen_urls = set()
        for warning in dependency_warnings:
            url = warning.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            addable.append(warning)

        if not addable:
            return

        should_add = messagebox.askyesno(
            "Add Missing Dependencies?",
            "Add missing dependencies to the mod list?\n\n"
            + "\n".join(f"- {warning['display_name']}" for warning in addable[:10]),
        )
        if not should_add:
            return

        existing_urls = {str(mod.get("url", "")).lower() for mod in self.mods}
        existing_names = {loose_match_key(str(mod.get("name", ""))) for mod in self.mods}
        added = 0

        for warning in addable:
            if warning["url"].lower() in existing_urls:
                continue
            if loose_match_key(warning["display_name"]) in existing_names:
                continue

            package_id = ""
            package_parts = parse_thunderstore_package_url(warning["url"])
            if package_parts:
                package_id = f"{package_parts[0]}-{package_parts[1]}"

            self.mods.append(
                {
                    "name": warning["display_name"],
                    "url": warning["url"],
                    "enabled": True,
                    "status": "Ready",
                    **({"package_id": package_id} if package_id else {}),
                }
            )
            added += 1

        if added:
            self._render_mods()
            self._append_log(f"Added {added} missing dependencies to the mod list")


if __name__ == "__main__":
    app = ValheimModDownloader()
    app.mainloop()
