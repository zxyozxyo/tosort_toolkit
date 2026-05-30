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
            import json as _j, base64 as _b64
            b64 = _b64.b64encode(
                _j.dumps(data, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            js = "(function(){var d=JSON.parse(atob('"+b64+"'));window.prepEvent('"+event+"',d);})()"
            self._window.evaluate_js(js)
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
        mode       = config.get("mode", "copy")
        size_limit = int(config.get("size_limit", 2 * 1024**3))
        fmt        = config.get("format", "zip").lower()

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

        _rar = self._find_tool(["rar.exe", "rar", "WinRAR.exe"])
        # Prefer 7z.exe/7za.exe over 7zr.exe — 7zr cannot create RAR
        _7z  = self._find_tool(["7z.exe", "7za.exe", "7z", "7za"])
        if not _7z:  # fall back to 7zr only if nothing else found
            _7z = self._find_tool(["7zr.exe", "7zr"])

        if fmt == "rar":
            if _rar:
                # Use rar.exe — 7z RAR creation requires optional plugin
                self._log(f"Using rar.exe for RAR creation: {_rar}", "dim")
                _7z_for_rar = None  # signal to use rar path
            elif _7z:
                self._log(f"rar.exe not found — trying 7z for RAR: {_7z}", "warn")
                _7z_for_rar = _7z
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

            try:
                pct = int((n_done / total) * 100)
                self._progress(pct, f"Processing: {folder.name}/")
                self._log(f"\nFolder: {folder}", "info")

                # Relative path from source root
                try:
                    rel = folder.relative_to(src_dir)
                except ValueError:
                    rel = Path(folder.name)

                # Determine working dir
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
                groups: dict = {}
                for arc in archives:
                    letter = get_letter_group(arc.name)
                    groups.setdefault(letter, []).append(arc)

                # Process each letter group
                for letter in sorted(groups.keys()):
                    if self._stop_flag.is_set():
                        break

                    files = groups[letter]
                    batches = self._split_by_size(files, size_limit)
                    self._log(f"  letter={letter} files={len(files)} batches={len(batches)}", "dim")

                    for batch_idx, batch in enumerate(batches):
                        if self._stop_flag.is_set():
                            break

                        suffix = f"_{batch_idx + 1}" if batch_idx > 0 else ""
                        arc_stem = f"{letter}{suffix}"
                        arc_name = f"{arc_stem}.{fmt}"
                        arc_path = work_dir / arc_name

                        batch_size = sum(f.stat().st_size for f in batch)
                        self._log(
                            f"  Creating {arc_name}  "
                            f"({len(batch)} files, {self._fmt_size(batch_size)})", "dim"
                        )

                        files_to_archive = []
                        if mode == "copy":
                            for f in batch:
                                dst_f = work_dir / f.name
                                if not dst_f.exists():
                                    shutil.copy2(f, dst_f)
                                files_to_archive.append(dst_f)
                        else:
                            files_to_archive = batch

                        try:
                            ok = self._create_archive(
                                arc_path, files_to_archive, fmt, _rar, _7z
                            )
                            actual_arc = arc_path.with_suffix(".zip") \
                                if not arc_path.exists() and arc_path.with_suffix(".zip").exists() \
                                else arc_path
                            if ok and actual_arc.exists():
                                n_created += 1
                                self._log(f"  ✓ {actual_arc.name}  ({self._fmt_size(actual_arc.stat().st_size)})", "ok")
                                for f in files_to_archive:
                                    try:
                                        if f.exists():
                                            f.unlink()
                                    except Exception:
                                        pass
                            else:
                                n_errors += 1
                                self._log(f"  ✗ Failed: {arc_name} — archiver returned no output", "err")
                        except Exception as e:
                            n_errors += 1
                            self._log(f"  ✗ Error creating {arc_name}: {e!r}", "err")

                n_done += 1

            except Exception as folder_err:
                self._log(f"  ERROR processing folder {folder.name}: {folder_err!r}", "err")
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
        """Create archive using response file to avoid Windows cmd line length limits."""
        import subprocess
        if not files:
            return False

        work_dir = files[0].parent
        file_names = [f.name for f in files]

        # Write list file to local temp dir — avoids permission errors on
        # network shares and keeps network folders clean
        import tempfile as _tf
        _tmp = Path(_tf.gettempdir())
        list_file = _tmp / f"_prep_{arc_path.stem[:40]}.lst"
        list_file.write_bytes(
            b'\xff\xfe' + '\n'.join(file_names).encode('utf-16-le')
        )

        try:
            if fmt == 'rar' and rar_bin:
                # rar.exe @listfile syntax
                cmd = [rar_bin, 'a', '-ep', '-m0', str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(work_dir), stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
                if result.returncode != 0 or not arc_path.exists():
                    err = (result.stderr or result.stdout or b'')[:200].decode('utf-8', 'replace')
                    self._log(f'  rar.exe rc={result.returncode}: {err}', 'err')
                    return False
                return True

            elif z_bin:
                z_name = Path(z_bin).name.lower()
                actual_fmt = fmt
                if fmt == 'rar' and '7zr' in z_name:
                    self._log('  7zr.exe cannot create RAR — using ZIP instead', 'warn')
                    actual_fmt = 'zip'
                    arc_path = arc_path.with_suffix('.zip')
                # 7z also supports @listfile
                if actual_fmt == 'rar':
                    cmd = [z_bin, 'a', '-trar', '-mx0', str(arc_path), f'@{list_file}']
                else:
                    cmd = [z_bin, 'a', '-tzip', '-mx0', str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(work_dir), stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or b'')[:200].decode('utf-8', 'replace')
                    self._log(f'  7z error: {err}', 'err')
                    import zipfile as _zf
                    zip_path = arc_path.with_suffix('.zip')
                    with _zf.ZipFile(zip_path, 'w', _zf.ZIP_STORED) as zf:
                        for fn in file_names:
                            fp = work_dir / fn
                            if fp.exists():
                                zf.write(fp, fn)
                    self._log(f'  Fell back to Python ZIP: {zip_path.name}', 'warn')
                    return zip_path.exists()
                return True

            else:
                import zipfile as _zf
                zip_path = arc_path.with_suffix('.zip')
                with _zf.ZipFile(zip_path, 'w', _zf.ZIP_STORED) as zf:
                    for fn in file_names:
                        fp = work_dir / fn
                        if fp.exists():
                            zf.write(fp, fn)
                return zip_path.exists()

        finally:
            try:
                list_file.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _find_tool(candidates: list) -> str:
        import shutil as _sh
        app_dir = Path(__file__).parent
        # Search app dir and common subfolders
        search_dirs = [
            app_dir,
            app_dir / "apps",
            app_dir / "app",
            app_dir / "tools",
            app_dir / "bin",
        ]
        for name in candidates:
            for d in search_dirs:
                local = d / name
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
