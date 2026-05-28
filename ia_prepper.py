"""
IA Prepper — Pre-processes folders for Internet Archive upload.
Groups loose archives into letter-named RAR/ZIP files of a set size limit.
"""

import os
import re
import shutil
import threading
import time
from pathlib import Path


# Archive extensions to group
ARCHIVE_EXTS = {
    '.zip', '.rar', '.7z', '.zst', '.gz', '.tgz', '.tar',
    '.iso', '.chd', '.nrg', '.img', '.bin', '.cue',
    '.dms', '.adf', '.lha', '.lzh',
}


def get_letter_group(filename: str) -> str:
    """Return letter group for a filename."""
    name = Path(filename).stem.strip()
    if not name:
        return 'MISC'
    first = name[0].upper()
    if first.isalpha():
        return first
    elif first.isdigit():
        return '0-9'
    else:
        return 'MISC'


def find_archive_folders(root: Path) -> list:
    """
    Recursively find all leaf-level folders containing archives.
    Returns list of Path objects.
    """
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        archives = [f for f in filenames
                    if Path(f).suffix.lower() in ARCHIVE_EXTS]
        if archives:
            result.append(dp)
    return result


class IAPrepperAPI:
    def __init__(self):
        self._window = None
        self._stop_flag = threading.Event()
        self._running = False

    def set_window(self, window):
        self._window = window

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j
            payload = _j.dumps(data, ensure_ascii=True).replace("'", "\\'")
            self._window.evaluate_js(
                f"window.prepEvent('{event}', JSON.parse('{payload}'))"
            )
        except Exception:
            pass

    def _log(self, msg: str, cls: str = "info"):
        self._emit("prepLog", {"msg": msg, "cls": cls})

    def _progress(self, pct: int, label: str = ""):
        self._emit("prepProgress", {"pct": pct, "label": label})

    def stop_prep(self):
        self._stop_flag.set()
        self._log("Stopping...", "warn")

    def start_prep(self, config: dict):
        if self._running:
            self._log("Already running.", "warn")
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._run, args=(config,), daemon=True)
        t.start()
        return True

    def _run(self, config: dict):
        self._running = True
        self._emit("prepStatus", {"state": "running"})

        src_dir    = Path(config.get("src", "")).expanduser()
        dst_dir    = Path(config.get("dst", "")).expanduser() if config.get("dst") else None
        mode       = config.get("mode", "copy")  # "copy" or "move"
        size_limit = int(config.get("size_limit", 2 * 1024**3))  # bytes
        fmt        = config.get("format", "zip").lower()  # "rar" or "zip"

        if not src_dir.is_dir():
            self._log(f"ERROR: Source folder not found: {src_dir}", "err")
            self._running = False
            self._emit("prepStatus", {"state": "error"})
            return

        if mode == "copy" and not dst_dir:
            self._log("ERROR: Copy mode requires a destination folder.", "err")
            self._running = False
            self._emit("prepStatus", {"state": "error"})
            return

        # Find RAR/ZIP tool
        _rar = self._find_tool(["rar.exe", "rar", "WinRAR.exe"])
        _7z  = self._find_tool(["7z.exe", "7za.exe", "7zr.exe", "7z", "7za"])

        if fmt == "rar" and not _rar:
            if _7z:
                self._log("rar.exe not found — falling back to 7z for RAR creation.", "warn")
                _rar = None
            else:
                self._log("ERROR: No archiver found. Drop rar.exe or 7z.exe in app folder.", "err")
                self._running = False
                self._emit("prepStatus", {"state": "error"})
                return

        if fmt == "zip" and not _7z and not _rar:
            self._log("ERROR: No archiver found. Drop 7z.exe or rar.exe in app folder.", "err")
            self._running = False
            self._emit("prepStatus", {"state": "error"})
            return

        self._log(f"Scanning: {src_dir}", "info")
        archive_folders = find_archive_folders(src_dir)
        self._log(f"Found {len(archive_folders)} folder(s) containing archives", "dim")

        if not archive_folders:
            self._log("No archive folders found.", "warn")
            self._running = False
            self._emit("prepStatus", {"state": "done"})
            return

        total = len(archive_folders)
        n_done = 0
        n_created = 0
        n_errors = 0

        for folder in archive_folders:
            if self._stop_flag.is_set():
                break

            pct = int((n_done / total) * 100)
            self._progress(pct, f"Processing: {folder.name}/")
            self._log(f"\nFolder: {folder}", "info")

            # Relative path from source root
            try:
                rel = folder.relative_to(src_dir)
            except ValueError:
                rel = Path(folder.name)

            # Determine working dir
            # Include the source root folder name so structure is preserved
            if mode == "copy":
                work_dir = dst_dir / src_dir.name / rel
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                work_dir = folder

            # Get all archives in this folder
            archives = sorted([
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS
            ], key=lambda x: x.name.lower())

            if not archives:
                n_done += 1
                continue

            self._log(f"  {len(archives)} archive(s) — grouping by letter...", "dim")

            # Group by letter
            groups: dict = {}  # letter -> list of Path
            for arc in archives:
                letter = get_letter_group(arc.name)
                groups.setdefault(letter, []).append(arc)

            # Process each letter group
            for letter in sorted(groups.keys()):
                if self._stop_flag.is_set():
                    break

                files = groups[letter]
                # Split into size-limited batches
                batches = self._split_by_size(files, size_limit)

                for batch_idx, batch in enumerate(batches):
                    if self._stop_flag.is_set():
                        break

                    # Determine archive name
                    suffix = str(batch_idx + 1) if batch_idx > 0 else ""
                    arc_stem = f"{letter}{suffix}"
                    arc_name = f"{arc_stem}.{fmt}"
                    arc_path = work_dir / arc_name

                    batch_size = sum(f.stat().st_size for f in batch)
                    self._log(
                        f"  Creating {arc_name}  "
                        f"({len(batch)} files, {self._fmt_size(batch_size)})", "dim"
                    )

                    # Copy files to work_dir first if copy mode
                    files_to_archive = []
                    if mode == "copy":
                        for f in batch:
                            dst_f = work_dir / f.name
                            if not dst_f.exists():
                                shutil.copy2(f, dst_f)
                            files_to_archive.append(dst_f)
                    else:
                        files_to_archive = batch

                    # Create archive
                    try:
                        ok = self._create_archive(
                            arc_path, files_to_archive, fmt, _rar, _7z
                        )
                        # arc_path may have changed to .zip by fallback
                        actual_arc = arc_path.with_suffix(".zip") \
                            if not arc_path.exists() and arc_path.with_suffix(".zip").exists() \
                            else arc_path
                        if ok and actual_arc.exists():
                            n_created += 1
                            self._log(f"  ✓ {actual_arc.name}  ({self._fmt_size(actual_arc.stat().st_size)})", "ok")
                            # Delete source files after archiving (both modes)
                            for f in files_to_archive:
                                try:
                                    if f.exists():
                                        f.unlink()
                                except Exception:
                                    pass
                        else:
                            n_errors += 1
                            self._log(f"  ✗ Failed: {arc_name} — check archiver is in app folder", "err")
                    except Exception as e:
                        n_errors += 1
                        self._log(f"  ✗ Error creating {arc_name}: {e}", "err")

            n_done += 1

        self._log(
            f"\nDone — {n_created} archive(s) created, {n_errors} error(s)", 
            "ok" if not n_errors else "warn"
        )
        self._progress(100, "Complete")
        self._running = False
        self._emit("prepStatus", {
            "state": "done", "created": n_created, "errors": n_errors
        })

    def _split_by_size(self, files: list, limit: int) -> list:
        """Split file list into batches not exceeding size limit."""
        batches = []
        current = []
        current_size = 0
        for f in files:
            fsize = f.stat().st_size
            if current and current_size + fsize > limit:
                batches.append(current)
                current = [f]
                current_size = fsize
            else:
                current.append(f)
                current_size += fsize
        if current:
            batches.append(current)
        return batches if batches else [[]]

    def _create_archive(self, arc_path: Path, files: list,
                        fmt: str, rar_bin: str, z_bin: str) -> bool:
        """Create a RAR or ZIP archive containing the given files."""
        import subprocess
        if not files:
            return False

        # All files should be in the same directory
        work_dir = files[0].parent
        file_names = [f.name for f in files]

        if fmt == "rar" and rar_bin:
            cmd = [rar_bin, "a", "-ep", "-m0", str(arc_path)] + file_names
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(work_dir), stdin=subprocess.DEVNULL
            )
            return result.returncode == 0
        elif z_bin:
            # 7zr.exe cannot create RAR — only 7z.exe/7za.exe can
            # If only 7zr available and format is RAR, fall back to ZIP
            z_name = Path(z_bin).name.lower()
            actual_fmt = fmt
            if fmt == "rar" and "7zr" in z_name:
                self._log("  7zr.exe cannot create RAR — using ZIP instead (drop 7z.exe for RAR)", "warn")
                actual_fmt = "zip"
                arc_path = arc_path.with_suffix(".zip")
            if actual_fmt == "rar":
                cmd = [z_bin, "a", "-trar", "-mx0", str(arc_path)] + file_names
            else:
                cmd = [z_bin, "a", "-tzip", "-mx0", str(arc_path)] + file_names
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(work_dir), stdin=subprocess.DEVNULL
            )
            if result.returncode != 0:
                self._log(f"  7z error: {result.stderr[:200] or result.stdout[:200]}", "err")
                # Fall back to Python zip
                import zipfile as _zf
                zip_path = arc_path.with_suffix(".zip")
                with _zf.ZipFile(zip_path, "w", _zf.ZIP_STORED) as zf:
                    for f_path in [work_dir / fn for fn in file_names]:
                        if f_path.exists():
                            zf.write(f_path, f_path.name)
                self._log(f"  Fell back to Python ZIP: {zip_path.name}", "warn")
                return zip_path.exists()
            return True
        else:
            # Python zipfile fallback — no external tool
            import zipfile as _zf
            zip_path = arc_path.with_suffix(".zip")
            with _zf.ZipFile(zip_path, "w", _zf.ZIP_STORED) as zf:
                for f_path in [work_dir / fn for fn in file_names]:
                    if f_path.exists():
                        zf.write(f_path, f_path.name)
            return zip_path.exists()

    @staticmethod
    def _find_tool(candidates: list) -> str:
        import shutil as _sh
        app_dir = Path(__file__).parent
        for name in candidates:
            local = app_dir / name
            if local.exists():
                return str(local)
            found = _sh.which(name)
            if found:
                return found
        return ""

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory()
            root.destroy()
            return path if path else ""
        except Exception:
            return ""
