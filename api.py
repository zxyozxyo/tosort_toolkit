"""
ToSort Toolkit - Python API Backend
Exposes methods to the JS frontend via PyWebView's js_api bridge.
"""

import os
import shutil
import threading
import hashlib
import zipfile
# Monkey-patch zipfile to support ZSTD compression (method 93)
try:
    import zipfile_zstd  # noqa: F401 — import for side effect only
except ImportError:
    pass
import tarfile
import time
import json
from pathlib import Path
import webview


def fast_move(src: Path, dst: Path):
    """
    Move src to dst as fast as possible.
    Same drive: os.rename (instant, just a directory entry update).
    Cross drive: shutil.copy2 + unlink (must physically copy bytes).
    Falls back to shutil.move if anything unexpected happens.
    """
    try:
        os.rename(str(src), str(dst))
        return
    except OSError:
        pass
    # Cross-device or other rename failure - copy then delete
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        os.unlink(str(src))
    except Exception:
        # Last resort fallback
        shutil.move(str(src), str(dst))


# Optional imports - gracefully handle missing libs
try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False

try:
    import rarfile as _rarfile_mod
    # Auto-detect UnRAR.exe in common Windows locations
    _UNRAR_CANDIDATES = [
        r'C:\Program Files\WinRAR\UnRAR.exe',
        r'C:\Program Files (x86)\WinRAR\UnRAR.exe',
        r'C:\Program Files\WinRAR\Rar.exe',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'UnRAR.exe'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'unrar.exe'),
    ]
    for _c in _UNRAR_CANDIDATES:
        if os.path.isfile(_c):
            _rarfile_mod.UNRAR_TOOL = _c
            break
    _rarfile_mod.tool_setup()
    import rarfile
    HAS_RAR = True
    RAR_ERROR = None
except ImportError:
    HAS_RAR = False
    RAR_ERROR = 'rarfile not installed. Run: pip install rarfile'
except Exception as _rar_ex:
    HAS_RAR = False
    RAR_ERROR = (
        'UnRAR binary not found. Easiest fix:\n'
        '  1. Download UnRAR.exe from: https://www.rarlab.com/rar/unrarw64.exe\n'
        '  2. Drop UnRAR.exe into the tosort_toolkit folder (next to main.py)\n'
        '  3. Restart the app and it will be detected automatically.\n'
        '  OR just install WinRAR and it will be found automatically.'
    )

try:
    import zstandard as zstd
    HAS_ZST = True
except ImportError:
    HAS_ZST = False


def _find_tool(names: list) -> str:
    """Search app folder then PATH for a list of executable names."""
    import shutil as _sh
    app_dir = os.path.dirname(os.path.abspath(__file__))
    for name in names:
        local = os.path.join(app_dir, name)
        if os.path.isfile(local):
            return local
        found = _sh.which(name)
        if found:
            return found
    return None


def extract_iso(src: Path, dest_dir: Path) -> list:
    """
    Extract/copy an ISO image.
    ISOs are disc images — we just move/copy them rather than extracting contents.
    Returns the destination path in a list for consistency.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = safe_dest(dest_dir / src.name)
    shutil.copy2(str(src), str(dest))
    return [str(dest)]


def extract_chd(src: Path, dest_dir: Path) -> list:
    """
    Convert CHD back to its source format using chdman.
    chdman.exe must be in the app folder or on PATH.
    Falls back to copy if chdman not found.
    """
    import subprocess
    dest_dir.mkdir(parents=True, exist_ok=True)
    chdman = _find_tool(['chdman.exe', 'chdman'])
    if not chdman:
        # No chdman — just copy the CHD as-is
        dest = safe_dest(dest_dir / src.name)
        shutil.copy2(str(src), str(dest))
        return [str(dest)]
    # Try to detect CHD type and extract to appropriate format
    # First run info to determine type
    info = subprocess.run([chdman, 'info', '-i', str(src)],
                          capture_output=True, text=True)
    chd_type = 'cdrom'  # default
    for line in info.stdout.splitlines():
        if 'CD-ROM' in line or 'cdrom' in line.lower():
            chd_type = 'cdrom'
        elif 'Hard Disk' in line or 'hd' in line.lower():
            chd_type = 'hd'
        elif 'DVD' in line:
            chd_type = 'dvd'
    ext_map = {'cdrom': '.cue', 'hd': '.img', 'dvd': '.iso'}
    out_ext  = ext_map.get(chd_type, '.bin')
    out_stem = src.stem
    out_path = safe_dest(dest_dir / (out_stem + out_ext))
    cmd_map  = {'cdrom': 'extractcd', 'hd': 'extracthd', 'dvd': 'extractdvd'}
    cmd = [chdman, cmd_map.get(chd_type, 'extractcd'),
           '-i', str(src), '-o', str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'chdman failed: {result.stderr[:200]}')
    extracted = [str(out_path)]
    # chdman extractcd also creates a .bin alongside the .cue
    bin_path = out_path.with_suffix('.bin')
    if bin_path.exists():
        extracted.append(str(bin_path))
    return extracted


def extract_dms(src: Path, dest_dir: Path) -> list:
    """
    Extract DMS (Amiga Disk Masher) files using xdms or dmstools.
    xdms.exe / dmstools must be in the app folder or on PATH.
    Falls back to copy if tool not found.
    """
    import subprocess
    dest_dir.mkdir(parents=True, exist_ok=True)
    tool = _find_tool(['xdms.exe', 'xdms', 'dmstools.exe', 'dmstools'])
    if not tool:
        # No tool — just copy the DMS as-is
        dest = safe_dest(dest_dir / src.name)
        shutil.copy2(str(src), str(dest))
        return [str(dest)]
    out_path = safe_dest(dest_dir / src.with_suffix('.adf').name)
    result = subprocess.run(
        [tool, 'u', str(src), str(out_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f'xdms failed: {result.stderr[:200]}')
    return [str(out_path)]



# ---------------------------------------------------------------------------
# Helper: safe rename on collision (appends _1, _2, etc.)
# ---------------------------------------------------------------------------
def safe_dest(dest_path: Path) -> Path:
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".zst", ".tar", ".tar.gz", ".tgz"}

def is_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_EXTS or \
           "".join(path.suffixes[-2:]).lower() in {".tar.gz", ".tar.bz2"}

def archive_contains_archive(path: Path) -> bool:
    """Return True if the archive itself contains nested archives."""
    try:
        suf = path.suffix.lower()
        if suf == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                return any(
                    is_archive(Path(n)) for n in zf.namelist()
                )
        elif suf == ".7z" and HAS_7Z:
            with py7zr.SevenZipFile(path, mode="r") as sz:
                return any(is_archive(Path(n)) for n in sz.getnames())
        elif suf == ".rar":
            if not HAS_RAR:
                return False
            with rarfile.RarFile(path) as rf:
                return any(is_archive(Path(n)) for n in rf.namelist())
    except Exception:
        pass
    return False

def _find_unrar_binary() -> str:
    """Find UnRAR executable - checks app folder first, then common locations."""
    import shutil as _sh
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "UnRAR.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "unrar.exe"),
        r"C:\Program Files\WinRAR\UnRAR.exe",
        r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
        r"C:\Program Files\WinRAR\Rar.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return _sh.which("unrar") or _sh.which("UnRAR") or None


def extract_rar_native(src: Path, dest_dir: Path, overwrite: bool = False) -> list:
    """
    Extract RAR using the UnRAR binary directly.
    Massively faster than rarfile for large archives - whole archive in one native call.
    Falls back to rarfile if binary not found.
    """
    import subprocess
    binary = _find_unrar_binary()

    if not binary:
        # Fallback to rarfile
        if not HAS_RAR:
            raise RuntimeError(RAR_ERROR or "RAR support unavailable.")
        with rarfile.RarFile(src) as rf:
            rf.extractall(dest_dir)
            return [str(dest_dir / m.filename) for m in rf.infolist()]

    dest_dir.mkdir(parents=True, exist_ok=True)
    overwrite_flag = "-o+" if overwrite else "-o-"
    cmd = [
        binary, "x",
        "-inul",
        "-y",
        overwrite_flag,
        "-p-",
        str(src),
        str(dest_dir) + os.sep,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # UnRAR exit codes: 0=OK, 1=warning, 3=CRC error, others=failure
    if result.returncode in (0, 1):
        extracted = []
        for root, dirs, files in os.walk(dest_dir):
            for f in files:
                extracted.append(os.path.join(root, f))
        return extracted
    elif result.returncode == 3:
        raise RuntimeError("CRC error - archive is corrupt")
    else:
        stderr = (result.stderr or result.stdout or "").strip()
        if "password" in stderr.lower() or "encrypted" in stderr.lower():
            raise RuntimeError("password protected")
        raise RuntimeError(f"UnRAR exit code {result.returncode}: {stderr[:200]}")


def extract_archive(src: Path, dest_dir: Path, overwrite: bool = False) -> list:
    """
    Extract src archive into dest_dir.
    Returns list of extracted file paths (strings).
    RAR files use native UnRAR binary for full speed.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    suf = src.suffix.lower()

    if suf == ".zip":
        with zipfile.ZipFile(src, "r") as zf:
            for member in zf.infolist():
                out = dest_dir / member.filename
                if out.exists() and not overwrite:
                    out = safe_dest(out)
                zf.extract(member, dest_dir)
                extracted.append(str(dest_dir / member.filename))

    elif suf == ".7z":
        if not HAS_7Z:
            raise RuntimeError("py7zr not installed - cannot extract .7z files.")
        with py7zr.SevenZipFile(src, mode="r") as sz:
            sz.extractall(path=dest_dir)
            extracted = [str(dest_dir / n) for n in sz.getnames()]

    elif suf == ".rar":
        extracted = extract_rar_native(src, dest_dir, overwrite=overwrite)

    elif suf == ".zst":
        if not HAS_ZST:
            raise RuntimeError("zstandard not installed - cannot extract .zst files.")
        out_name = src.stem
        out_path = dest_dir / out_name
        if out_path.exists() and not overwrite:
            out_path = safe_dest(out_path)
        dctx = zstd.ZstdDecompressor()
        with open(src, "rb") as f_in, open(out_path, "wb") as f_out:
            dctx.copy_stream(f_in, f_out)
        extracted.append(str(out_path))

    elif suf == ".iso":
        extracted = extract_iso(src, dest_dir)

    elif suf == ".chd":
        extracted = extract_chd(src, dest_dir)

    elif suf == ".dms":
        extracted = extract_dms(src, dest_dir)

    else:
        raise RuntimeError(f"Unsupported format: {suf}")

    return extracted


# ---------------------------------------------------------------------------
# Main API class exposed to JS
# ---------------------------------------------------------------------------
class ToSortAPI:

    def __init__(self):
        self._window = None
        self._stop_flag = threading.Event()
        self._skip_flag = threading.Event()
        self._running = False

    def set_window(self, window):
        self._window = window

    # ------------------------------------------------------------------ #
    #  JS -> Python bridge helpers                                         #
    # ------------------------------------------------------------------ #
    def _emit(self, event: str, data: dict):
        """Send a JS event to the frontend."""
        if self._window:
            import json
            payload = json.dumps(data).replace("\\", "\\\\").replace("'", "\\'")
            self._window.evaluate_js(f"window.pyEvent('{event}', JSON.parse('{payload}'))")

    def _log(self, msg: str, cls: str = ""):
        self._emit("log", {"msg": msg, "cls": cls})

    def _progress(self, mod: int, pct: int, label: str):
        self._emit("progress", {"mod": mod, "pct": pct, "label": label})

    def _mod_status(self, mod: int, state: str):
        self._emit("modStatus", {"mod": mod, "state": state})

    def _global_status(self, state: str):
        self._emit("globalStatus", {"state": state})

    # ------------------------------------------------------------------ #
    #  Control                                                             #
    # ------------------------------------------------------------------ #
    def stop_current(self):
        """Skip to next module."""
        self._skip_flag.set()

    def stop_all(self):
        """Terminate everything."""
        self._stop_flag.set()
        self._skip_flag.set()


    # ------------------------------------------------------------------ #
    #  Dependency check                                                   #
    # ------------------------------------------------------------------ #
    def get_dependency_status(self) -> dict:
        """Returns which optional dependencies are available."""
        return {
            "py7zr":     HAS_7Z,
            "rarfile":   HAS_RAR,
            "rar_error": RAR_ERROR if not HAS_RAR else None,
            "zstandard": HAS_ZST,
        }

    # ------------------------------------------------------------------ #
    #  Browse dialog                                                       #
    # ------------------------------------------------------------------ #
    def open_dat_merger_window(self):
        """Open the DAT merger window from JS."""
        if hasattr(self, "open_dat_merger") and callable(self.open_dat_merger):
            self.open_dat_merger()

    def browse_folder(self):
        """Open a native folder picker, return selected path."""
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            allow_multiple=False
        )
        if result and len(result) > 0:
            return result[0]
        return None

    # ------------------------------------------------------------------ #
    #  Pipeline entry point (called from JS)                              #
    # ------------------------------------------------------------------ #
    def run_pipeline(self, config: dict):
        """
        config = {
          modules: [bool, bool, bool, bool],   # which are enabled
          verbose: bool,
          mod0: { src, nested_dest, out, fmts:[...], del_after, overwrite },
          mod1: { src, out, misc_catch, rename_dupe },
          mod2: { src, recurse, dry_run },
          mod3: { src, hash_md5, hash_sha1, size_pre, dry_run },
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._pipeline_thread, args=(config,), daemon=True)
        t.start()

    def _pipeline_thread(self, config: dict):
        self._running = True
        self._stop_flag.clear()
        self._skip_flag.clear()
        self._run_stats = {}          # module_index -> dict of stats

        import datetime
        run_start = time.time()
        run_ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._global_status("running")
        self._log("=" * 50, "info")
        self._log("ToSort Toolkit pipeline started", "ok")
        self._log("=" * 50, "info")

        modules = config.get("modules", [True, True, True, True])
        verbose = config.get("verbose", True)

        MODULE_NAMES = [
            "Archive Extractor",
            "File Sorter",
            "Empty Folder Cleanup",
            "Duplicate File Finder",
        ]

        runners = [
            (self._run_extractor, config.get("mod0", {})),
            (self._run_sorter,    config.get("mod1", {})),
            (self._run_empty,     config.get("mod2", {})),
            (self._run_dupes,     config.get("mod3", {})),
        ]

        for i, (fn, cfg) in enumerate(runners):
            if self._stop_flag.is_set():
                break
            if not modules[i]:
                self._mod_status(i, "skip")
                self._log(f"Module {i+1} disabled — skipping", "dim")
                self._run_stats[i] = {"status": "skipped", "elapsed": 0}
                continue

            self._skip_flag.clear()
            self._mod_status(i, "running")
            mod_start = time.time()
            try:
                fn(i, cfg, verbose)
                elapsed = round(time.time() - mod_start, 1)
                if not self._stop_flag.is_set():
                    self._mod_status(i, "done")
                    self._run_stats[i] = {"status": "done", "elapsed": elapsed}
                else:
                    self._run_stats[i] = {"status": "stopped", "elapsed": elapsed}
            except Exception as e:
                elapsed = round(time.time() - mod_start, 1)
                self._mod_status(i, "error")
                self._log(f"Module {i+1} error: {e}", "err")
                self._run_stats[i] = {"status": "error", "error": str(e), "elapsed": elapsed}

        stopped      = self._stop_flag.is_set()
        total_elapsed = round(time.time() - run_start, 1)
        self._global_status("stopped" if stopped else "done")
        self._log("Pipeline stopped by user." if stopped else "Pipeline complete.", "err" if stopped else "ok")

        # Save summary report if configured
        report_dir = config.get("report_dir", "").strip()
        if report_dir:
            self._save_report(
                report_dir, run_ts, total_elapsed, stopped,
                MODULE_NAMES, modules, config
            )

        self._running = False

    # ------------------------------------------------------------------ #
    #  MODULE 1 — Archive Extractor                                        #
    # ------------------------------------------------------------------ #
    def _run_extractor(self, mod_idx: int, cfg: dict, verbose: bool):
        src_dir    = Path(cfg.get("src", "")).expanduser()
        nested_dst = Path(cfg.get("nested_dest", "")).expanduser()
        out_dir    = Path(cfg.get("out") or cfg.get("src", "")).expanduser()
        bad_dst    = Path(cfg.get("bad_dest", "")).expanduser() if cfg.get("bad_dest") else None
        fmt_set      = set(cfg.get("fmts", [".zip", ".rar", ".7z", ".zst"]))
        del_after    = cfg.get("del_after", False)
        overwrite    = cfg.get("overwrite", False)
        # Formats that should never be deleted even if del_after is on
        no_del_exts  = set()
        if cfg.get("no_del_iso", True):  no_del_exts.add(".iso")
        if cfg.get("no_del_chd", True):  no_del_exts.add(".chd")
        if cfg.get("no_del_dms", True):  no_del_exts.add(".dms")

        self._log("=== MODULE 1: Archive Extractor ===", "info")
        if bad_dst:
            self._log(f"Bad archive destination: {bad_dst}", "dim")

        if not src_dir.is_dir():
            self._log(f"Source folder not found: {src_dir}", "err")
            self._progress(mod_idx, 100, "Error")
            return

        # Collect matching archives
        archives = [
            f for f in src_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in fmt_set
        ]

        if not archives:
            self._log("No matching archives found.", "warn")
            self._progress(mod_idx, 100, "Nothing to do")
            return

        self._log(f"Found {len(archives)} archive(s) in: {src_dir}")

        n_ok = n_nested = n_bad = n_pw = 0

        def move_bad(arc: Path, reason: str):
            """Move a problem archive to bad_dst, or just log if no dest set."""
            nonlocal n_bad, n_pw
            tag = "[PW]" if "password" in reason.lower() else "[BAD]"
            if "password" in reason.lower():
                n_pw += 1
            else:
                n_bad += 1
            if bad_dst:
                bad_dst.mkdir(parents=True, exist_ok=True)
                dest = safe_dest(bad_dst / arc.name)
                try:
                    fast_move(arc, dest)
                    self._log(f"  {tag}  {arc.name}  →  _BadArchives/  ({reason})", "warn")
                except Exception as me:
                    self._log(f"  {tag}  {arc.name}  could not move: {me}", "err")
            else:
                self._log(f"  {tag}  {arc.name}  ({reason})  — no bad-archive folder set", "warn")

        def is_password_error(e: Exception) -> bool:
            msg = str(e).lower()
            return any(k in msg for k in ("password", "encrypted", "wrong password",
                                          "bad password", "requires password"))

        def is_corrupt_error(e: Exception) -> bool:
            msg = str(e).lower()
            return any(k in msg for k in ("crc", "corrupt", "bad archive", "invalid",
                                          "unexpected end", "not a zip", "not a rar",
                                          "broken", "truncated", "bad magic"))

        # --- STEP 1: identify and move nested archives ---
        self._log("Scanning for nested archives...", "info")
        nested = []
        clean  = []
        for i, arc in enumerate(archives):
            if self._stop_flag.is_set(): return
            pct = int((i / len(archives)) * 20)
            self._progress(mod_idx, pct, f"Scanning: {arc.name}")
            try:
                if archive_contains_archive(arc):
                    nested.append(arc)
                    if verbose:
                        self._log(f"  NESTED  {arc.name}", "warn")
                else:
                    clean.append(arc)
            except Exception as e:
                # Can't even open it during scan — treat as bad
                # Can't inspect for nested archives — still try to extract later
                clean.append(arc)
                if verbose:
                    self._log(f"  WARN  {arc.name}: couldn't scan for nesting ({e}) — will try extract", "warn")

        if nested:
            self._log(f"Moving {len(nested)} nested archive(s) → {nested_dst}", "warn")
            nested_dst.mkdir(parents=True, exist_ok=True)
            for i, arc in enumerate(nested):
                if self._stop_flag.is_set(): return
                dest = safe_dest(nested_dst / arc.name)
                fast_move(arc, dest)
                self._log(f"  MOVE  {arc.name}  →  {dest.name}", "warn")
                n_nested += 1
                pct = 20 + int(((i + 1) / len(nested)) * 15)
                self._progress(mod_idx, pct, f"Moving: {arc.name}")

        # --- STEP 2: extract clean archives ---
        self._log(f"Extracting {len(clean)} archive(s) → {out_dir}", "info")
        out_dir.mkdir(parents=True, exist_ok=True)

        for i, arc in enumerate(clean):
            if self._stop_flag.is_set() or self._skip_flag.is_set(): return
            base_pct = 35 + int((i / max(len(clean), 1)) * 60)
            self._progress(mod_idx, base_pct, f"Extracting: {arc.name}")

            if verbose:
                self._log(f"  Opening  {arc.name}  ({arc.stat().st_size // 1024} KB)", "dim")

            try:
                arc_out   = out_dir / arc.stem
                extracted = extract_archive(arc, arc_out, overwrite=overwrite)

                if verbose:
                    for f in extracted[:5]:
                        self._log(f"    + {Path(f).name}", "dim")
                    if len(extracted) > 5:
                        self._log(f"    ... and {len(extracted)-5} more files", "dim")

                self._log(f"  OK  {arc.name}  ({len(extracted)} file(s))", "ok")
                n_ok += 1

                if del_after and arc.suffix.lower() not in no_del_exts:
                    arc.unlink()
                    self._log(f"  DEL  {arc.name}", "warn")
                elif del_after and arc.suffix.lower() in no_del_exts:
                    self._log(f"  KEEP {arc.name} (no-delete rule)", "dim")

            except Exception as e:
                if is_password_error(e):
                    move_bad(arc, "password protected")
                elif is_corrupt_error(e):
                    move_bad(arc, f"corrupt/bad: {e}")
                else:
                    # Non-corrupt failure (permissions, disk full, etc)
                    # Log it but do NOT move to bad archives — it may be fine
                    self._log(f"  SKIP  {arc.name}: {e}", "err")
                    self._log(f"        (archive left in place — try manually)", "dim")

            self._progress(mod_idx, 35 + int(((i + 1) / max(len(clean), 1)) * 60), f"Done: {arc.name}")

        self._progress(mod_idx, 100, "Complete")
        self._log(
            f"Extractor done — {n_ok} extracted, {n_nested} nested moved, "
            f"{n_bad} bad, {n_pw} password-protected.",
            "ok" if (n_bad + n_pw) == 0 else "warn"
        )
        # ── STEP 3: Recursive nested extraction ──────────────────────
        recurse_src = cfg.get("recurse_nested_src", "").strip()
        recurse_dst = cfg.get("recurse_nested_dst", "").strip()
        if recurse_src and Path(recurse_src).is_dir():
            self._log("", "")
            self._log("=== Recursive Nested Extraction ===", "info")
            self._log(f"Source: {recurse_src}", "dim")
            if recurse_dst:
                self._log(f"Final output: {recurse_dst}", "dim")
            r_src = Path(recurse_src).expanduser()
            r_dst = Path(recurse_dst).expanduser() if recurse_dst else r_src
            r_dst.mkdir(parents=True, exist_ok=True)
            total_recursive = 0
            max_depth = 10  # safety limit
            depth = 0
            while depth < max_depth:
                if self._stop_flag.is_set() or self._skip_flag.is_set():
                    break
                depth += 1
                # Find all archives in the recursive source
                r_archives = [
                    f for f in r_src.rglob("*")
                    if f.is_file() and f.suffix.lower() in fmt_set
                ]
                if not r_archives:
                    self._log(f"  Pass {depth}: no more archives found — done.", "ok")
                    break
                self._log(f"  Pass {depth}: found {len(r_archives)} archive(s) to extract", "info")
                extracted_any = False
                for r_arc in r_archives:
                    if self._stop_flag.is_set() or self._skip_flag.is_set():
                        break
                    try:
                        r_out = r_src / r_arc.stem
                        result = extract_archive(r_arc, r_out, overwrite=overwrite)
                        total_recursive += len(result)
                        extracted_any = True
                        if verbose and total_recursive % 50 == 0:
                            self._log(f"    Extracted {r_arc.name} ({len(result)} files)", "dim")
                        # Delete the archive after successful extraction
                        r_arc.unlink()
                    except Exception as e:
                        self._log(f"    ERR  {r_arc.name}: {e}", "err")
                if not extracted_any:
                    break
            # Move all resulting files to final destination if different from source
            if r_dst.resolve() != r_src.resolve():
                self._log(f"  Moving extracted files to: {r_dst}", "info")
                move_count = 0
                for root, dirs, files in os.walk(r_src):
                    for f in files:
                        fp = Path(root) / f
                        dest = safe_dest(r_dst / fp.name)
                        try:
                            fast_move(fp, dest)
                            move_count += 1
                        except Exception as me:
                            self._log(f"    ERR moving {fp.name}: {me}", "err")
                self._log(f"  Moved {move_count} file(s) to final destination.", "ok")
            self._log(
                f"Recursive extraction done — {depth} pass(es), "
                f"{total_recursive} file(s) extracted total.",
                "ok"
            )
            n_ok += total_recursive

        # Store stats for summary report
        if not hasattr(self, '_run_stats'):
            self._run_stats = {}
        self._run_stats[mod_idx] = self._run_stats.get(mod_idx, {})
        self._run_stats[mod_idx].update({
            "extracted": n_ok, "nested": n_nested,
            "bad": n_bad, "password": n_pw
        })

    # ------------------------------------------------------------------ #
    #  MODULE 2 — File Sorter                                             #
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    #  MODULE 2 — File Sorter + ROM Sorter (single scan, two destinations) #
    # ------------------------------------------------------------------ #
    def _run_sorter(self, mod_idx: int, cfg: dict, verbose: bool):
        """
        Single scan of src_dir. Each file is checked against the ROM
        extension database first:
          - Known ROM/emulator ext  -> rom_dir/<letter>/<ext>/
          - Companion file          -> rom_dir/<letter>/<ext>/  (if rom_enable)
          - Everything else         -> general_dir/<letter>/<ext>/
        Both destinations use the same 10k-cap + recount-on-startup rules.
        ROM sorting can be disabled entirely via cfg['rom_enable'].
        """
        import re as _re

        MAX_FILES = 10000

        src_dir     = Path(cfg.get("src",  "")).expanduser()
        gen_dir     = Path(cfg.get("out",  "")).expanduser()
        rom_enable  = cfg.get("rom_enable",  True)
        rom_dir     = Path(cfg.get("rom_out","")).expanduser() if rom_enable else None
        misc_catch  = cfg.get("misc_catch",  True)
        rename_dupe = cfg.get("rename_dupe", True)
        companions  = cfg.get("companions",  True)
        dry_run     = cfg.get("dry_run",     False)

        self._log("=== MODULE 2: File Sorter ===", "info")
        if rom_enable:
            self._log("ROM sorting ENABLED — two-destination mode.", "info")

        if not src_dir.is_dir():
            self._log(f"Source folder not found: {src_dir}", "err")
            self._progress(mod_idx, 100, "Error"); return
        if not gen_dir:
            self._log("No general output folder set.", "err")
            self._progress(mod_idx, 100, "Error"); return
        if rom_enable and not rom_dir:
            self._log("ROM sorting enabled but no ROM output folder set.", "err")
            self._progress(mod_idx, 100, "Error"); return

        gen_dir.mkdir(parents=True, exist_ok=True)
        if rom_enable:
            rom_dir.mkdir(parents=True, exist_ok=True)

        # ── ROM extension database ─────────────────────────────────────
        ROM_EXTS = {
            # ── Atari (2600, 5200, 7800, 8-bit, Lynx, Jaguar, ST) ─────
            "a26","a52","a78","atr","xex","xfd","cas","car","lnx","lyx","jag",
            "atx","abs","cof","bjl","st","stx","stt",
            # ── Nintendo (NES, SNES, GB/GBC/GBA, N64, GC, Wii, Switch, DS, 3DS) ──
            "nes","unf","unif","fds","sfc","smc","fig","gb","gbc","sgb","gba","agb",
            "nds","dsi","3ds","3dsx","cci","cxi","n64","z64","v64","ndd","gcm","gcz",
            "nsp","xci","wad","wbfs","rvz","rpx","wux","dol","gbx",
            # ── Sega (SMS, GG, MD, 32X, CD, Saturn, DC) ────────────────
            "sms","gg","md","gen","32x","scd","sat","ss","dc","gdi",
            # ── Sony (PS1, PSP, PS3/PS4) ───────────────────────────────
            "pbp","cso","dax","pkg",
            # ── NEC / SNK ──────────────────────────────────────────────
            "pce","sgx","ngp","ngc",
            # ── Microsoft Xbox ─────────────────────────────────────────
            "xbe","xiso",
            # ── Commodore 64 / 128 / Amiga ─────────────────────────────
            "d64","t64","g64","d71","d81","prg","p00","nib","crt","c64",
            "adf","dms","ipf","hdf","uae","d40","d80","reu","tap",
            # ── Sinclair ZX Spectrum ────────────────────────────────────
            "tzx","z80","sna","szx","csw","dsk","mdr","mgt","opd","sp","udi",
            # ── Amstrad CPC ────────────────────────────────────────────
            "cdt","cpr",
            # ── BBC Micro / Acorn / Archimedes ─────────────────────────
            "ssd","dsd","uef","bbc","adl","adm","arc","ark","scp",
            # ── MSX ────────────────────────────────────────────────────
            "mx1","mx2","rom","dsk",
            # ── Apple II / IIGS ────────────────────────────────────────
            "2mg","po","hdv","woz","a2r","d13",
            # ── Atari ST / Falcon ──────────────────────────────────────
            "tos","ddp",
            # ── Sharp / Japanese home computers ────────────────────────
            "d77","d88","88d","d2m","d4m","nfd","t88","p6","mzf",
            # ── Thomson / Alice ─────────────────────────────────────────
            "k7","fd","m7","sap","qd",
            # ── Dragon / Tandy CoCo ────────────────────────────────────
            "dgn","vdk","pak","cgd","cas",
            # ── CP/M systems ───────────────────────────────────────────
            "cpm","com",
            # ── Coleco / Intellivision / Vectrex / Odyssey ─────────────
            "col","cv","int","o2","vec",
            # ── WonderSwan / Virtual Boy ───────────────────────────────
            "ws","wsc","vb",
            # ── Arduboy ────────────────────────────────────────────────
            "arduboy","hex",
            # ── Sord M5 / MTX ──────────────────────────────────────────
            "mtx","run",
            # ── Enterprise 128 ─────────────────────────────────────────
            "ep128s","dtf",
            # ── SAM Coupe ──────────────────────────────────────────────
            "sad","sdf",
            # ── Nascom ─────────────────────────────────────────────────
            "nas",
            # ── Camputers Lynx ─────────────────────────────────────────
            "ace",
            # ── Galaksija ──────────────────────────────────────────────
            "gal","gtp",
            # ── Robotron / KC85 ────────────────────────────────────────
            "bkp","cram",
            # ── Soviet/Russian micros ──────────────────────────────────
            "rk5","rx1","ptp","rim",
            # ── DAI Personal Computer ──────────────────────────────────
            "dai",
            # ── VTech / Laser ──────────────────────────────────────────
            "vtp","fbt",
            # ── TRS-80 / Oric / Misc 8-bit ─────────────────────────────
            "dmk","bas","cmd","lbr","wr3","dfi","dmp","mac","caq","cjr","bac",
            "jrc","ihx","asm","a22","tu6","v0","app","asy",
            # ── Elan / Memotech / misc ─────────────────────────────────
            "fpk","pss","imz","cp2","formzt","sol","svt","smu","srn",
            # ── Sharp MZ ───────────────────────────────────────────────
            "mzf","qdf",
            # ── SpectraVideo ───────────────────────────────────────────
            "sda","sfx",
            # ── Sinclair QL / Microdrives ──────────────────────────────
            "mdv",
            # ── Jupiter Ace ────────────────────────────────────────────
            "ace",
            # ── Aamber Pegasus / misc ──────────────────────────────────
            "bee","pop","bs5","ecb","ent","sd","sis","dck",
            # ── Paper tape / teletype ──────────────────────────────────
            "ptp",
            # ── Floppy disk image formats (multi-system) ───────────────
            "hfe","fdi","fsd","td0","imd","dim","xdf","hdm","2d","2hd",
            "dcp","nfd","hxcstream","mfm","pdi","pfdc","pri","psi","tc",
            "1dd","scp","ima","edd","jfd","fdd","baz","wdr",
            # ── Disc images ────────────────────────────────────────────
            "iso","mdf","mds","ccd","sub","ecm","img","bin","chd",
            # ── ZX Microdrive / misc Sinclair ──────────────────────────
            "$c","$b","$u","$z","$t","$w","$x",
            # ── MAME / Arcade ──────────────────────────────────────────
            "chd",
            # ── Emulator saves / states / BIOS ─────────────────────────
            "sav","srm","fla","sta","ss1","ss2","mcr","vmp","vms","bios",
            # ── Misc transport / tape ──────────────────────────────────
            "tape","t77","cmt","wav","aif","raw",
            # ── No-Intro / TOSEC / Redump specific ─────────────────────
            "min","trim","cia","elf","dol","vpk","velf",
            # ── Misc / uncategorised TOSEC ─────────────────────────────
            "lib","orig","pl","prn","out","wmf","gcz",
            # ═══════════════════════════════════════════════════════════
            # ── VGM / Chiptune / ROM Music formats ─────────────────────
            # ═══════════════════════════════════════════════════════════
            # VGM family
            "vgm","vgz","gym","s98",
            # SID (Commodore 64 music)
            "sid","psid","rsid",
            # ZX Spectrum / AY chip music
            "ay","stc","stp","sqt","pt1","pt2","pt3","psc",
            # Amiga tracker music
            "mod","xm","s3m","it","med","oct","dbm","ahx","hvl",
            # Nintendo music rips
            "nsf","nsfe","gbs","hes",
            # SNES music
            "spc","rsn",
            # Sega music
            "kss",
            # PlayStation music rips
            "psf","psf2","minipsf","minipsf2",
            # Saturn music rips
            "ssf","minissf",
            # Dreamcast music rips
            "dsf","minidsf",
            # GBA music rips
            "gsf","minigsf",
            # N64 music rips
            "usf","miniusf",
            # NDS music rips
            "2sf","mini2sf",
            # Atari ST music
            "sndh","sc68","ym",
            # AdLib / OPL chip music
            "hsc","ksm","lds","rad",
            # General tracker / chip
            "stm","669","far","mtm","ult","dmf","tfm","tfe",
            "fc13","fc14",
            # CPC music
            "cpc",
            # Various additional
            "s","l3","l3b","l3c",
        }

        COMPANION_EXTS = {
            "cue","m3u","xml","dat","nfo","txt","md5","sfv",
            "jpg","png","bmp","ccd","sub",
        }

        # ── Shared helpers ─────────────────────────────────────────────
        def letter_bucket(key: str) -> str:
            first = key[0] if key else ""
            return first.upper() if first.isalpha() else "MISC"

        # name_counters[folder_path] = next available collision counter
        # avoids repeated os.path.exists() calls per file
        name_counters: dict = {}

        def unique_path_fast(folder: Path, filename: str) -> Path:
            key = str(folder) + "/" + filename
            candidate = folder / filename
            if key not in name_counters:
                # First time seeing this name in this folder
                if not candidate.exists():
                    name_counters[key] = 1
                    return candidate
                # File already exists - find next free slot
                stem, sfx = os.path.splitext(filename)
                n = 1
                while (folder / f"{stem}_{n}{sfx}").exists():
                    n += 1
                name_counters[key] = n + 1
                return folder / f"{stem}_{n}{sfx}"
            else:
                # We have moved files here this session - use counter directly
                n = name_counters[key]
                name_counters[key] = n + 1
                stem, sfx = os.path.splitext(filename)
                return folder / f"{stem}_{n}{sfx}"

        # Cache of resolved destination folders to avoid repeated mkdir
        folder_cache: dict = {}

        def get_folder(root: Path, ext_key: str, counts: dict) -> Path:
            ext_key = ext_key.lower()
            cache_key = str(root) + ext_key
            if cache_key in folder_cache:
                # Check if we need to roll to next slot
                if counts.get(ext_key, 0) % MAX_FILES == 0 and counts.get(ext_key, 0) > 0:
                    del folder_cache[cache_key]  # force recalc
                else:
                    return folder_cache[cache_key]
            idx    = counts.get(ext_key, 0) // MAX_FILES
            bucket = letter_bucket(ext_key)
            suffix = f"_{idx}" if idx > 0 else ""
            folder = root / bucket / f"{ext_key}{suffix}"
            folder.mkdir(parents=True, exist_ok=True)
            folder_cache[cache_key] = folder
            return folder

        def recount(root: Path) -> dict:
            """Fast recount using os.scandir instead of rglob."""
            counts: dict = {}
            if not root or not root.is_dir():
                return counts
            try:
                with os.scandir(root) as bucket_it:
                    for bucket in bucket_it:
                        if not bucket.is_dir(follow_symlinks=False): continue
                        with os.scandir(bucket.path) as ext_it:
                            for ed in ext_it:
                                if not ed.is_dir(follow_symlinks=False): continue
                                base = _re.sub(r"_\d+$", "", ed.name)
                                n = sum(1 for _ in os.scandir(ed.path))
                                counts[base] = counts.get(base, 0) + n
            except Exception:
                pass
            return counts

        # ── Recount both destinations ──────────────────────────────────
        self._log("Recounting destination folders...", "info")
        self._progress(mod_idx, 2, "Recounting...")
        gen_counts = recount(gen_dir)
        rom_counts = recount(rom_dir) if rom_enable else {}
        self._log(
            f"  General: {len(gen_counts)} ext bucket(s)  "
            + (f"  ROM: {len(rom_counts)} ext bucket(s)" if rom_enable else ""),
            "dim"
        )

        # ── Collect all files ──────────────────────────────────────────
        self._progress(mod_idx, 5, "Collecting files...")
        all_files = []
        for root, dirs, files in os.walk(src_dir):
            rp = Path(root).resolve()
            if rp == gen_dir.resolve() or str(rp).startswith(str(gen_dir.resolve())):
                dirs.clear(); continue
            if rom_enable and rom_dir:
                if rp == rom_dir.resolve() or str(rp).startswith(str(rom_dir.resolve())):
                    dirs.clear(); continue
            for f in files:
                all_files.append(Path(root) / f)

        if not all_files:
            self._log("No files found in source folder.", "warn")
            self._progress(mod_idx, 100, "Nothing to do"); return

        self._log(f"Found {len(all_files)} file(s) — scanning once for all destinations.", "info")

        total_moved = total_rom = total_comp = total_gen = total_err = 0

        for i, src_path in enumerate(all_files):
            if self._stop_flag.is_set() or self._skip_flag.is_set():
                break

            if i % 50 == 0:  # throttle GUI updates for speed
                pct = 5 + int((i / len(all_files)) * 93)
                self._progress(mod_idx, pct, f"Sorting: {src_path.name} ({i}/{len(all_files)})")

            raw_ext = src_path.suffix.lower()
            ext_key = raw_ext[1:] if raw_ext else "no_ext"

            # Classify
            is_rom  = rom_enable and ext_key in ROM_EXTS
            is_comp = rom_enable and companions and ext_key in COMPANION_EXTS

            if is_rom or is_comp:
                dest_root   = rom_dir
                dest_counts = rom_counts
                tag = "ROM" if is_rom else "COMP"
            else:
                # General sort — apply MISC rule
                bucket = letter_bucket(ext_key)
                if not misc_catch and bucket == "MISC":
                    continue
                dest_root   = gen_dir
                dest_counts = gen_counts
                tag = "GEN"

            dest_folder = get_folder(dest_root, ext_key, dest_counts)
            dest_path   = unique_path_fast(dest_folder, src_path.name) if rename_dupe \
                          else dest_folder / src_path.name

            if verbose and total_moved % 100 == 0 and total_moved > 0:
                self._log(
                    f"  [{tag}]  {src_path.name}  →  "
                    f"{dest_folder.parent.name}/{dest_folder.name}/  "
                    f"({total_moved} moved so far...)", "dim"
                )

            if dry_run:
                dest_counts[ext_key] = dest_counts.get(ext_key, 0) + 1
                total_moved += 1
                continue

            try:
                fast_move(src_path, dest_path)
                dest_counts[ext_key] = dest_counts.get(ext_key, 0) + 1
                total_moved += 1
                if is_rom:   total_rom  += 1
                elif is_comp: total_comp += 1
                else:         total_gen  += 1
            except Exception as e:
                total_err += 1
                self._log(f"  ERR  {src_path.name}: {e}", "err")

        self._progress(mod_idx, 100, "Complete")
        dry_tag = "  [DRY RUN]" if dry_run else ""
        self._log(
            f"File Sorter done{dry_tag} — "
            f"{total_gen} general, {total_rom} ROM, {total_comp} companion, "
            f"{total_err} error(s).  Total: {total_moved}",
            "ok" if total_err == 0 else "warn"
        )
        if not hasattr(self, '_run_stats'): self._run_stats = {}
        self._run_stats[mod_idx] = self._run_stats.get(mod_idx, {})
        self._run_stats[mod_idx].update({
            "general": total_gen, "rom": total_rom,
            "companion": total_comp, "errors": total_err,
            "dry_run": dry_run
        })

    # ------------------------------------------------------------------ #
    #  MODULE 3 — Empty Folder Deleter                                    #
    # ------------------------------------------------------------------ #
    def _run_empty(self, mod_idx: int, cfg: dict, verbose: bool):
        """
        Walks one or more target trees bottom-up, removing every empty directory.
        Multiple passes catch folders that only become empty after children removed.
        Root folders are never deleted.
        Accepts either cfg['src'] (single path string) or cfg['srcs'] (list of paths).
        """
        raw_srcs = cfg.get("srcs") or ([cfg.get("src")] if cfg.get("src") else [])
        src_dirs = [Path(s).expanduser() for s in raw_srcs if s and s.strip()]
        dry_run  = cfg.get("dry_run", False)
        recurse  = cfg.get("recurse", True)

        self._log("=== MODULE 3: Empty Folder Cleanup ===", "info")

        if not src_dirs:
            self._log("No target folders configured.", "err")
            self._progress(mod_idx, 100, "Error")
            return

        valid_dirs = [d for d in src_dirs if d.is_dir()]
        for d in src_dirs:
            if not d.is_dir():
                self._log(f"Folder not found (skipping): {d}", "warn")

        if not valid_dirs:
            self._log("No valid target folders found.", "err")
            self._progress(mod_idx, 100, "Error")
            return

        if dry_run:
            self._log("Dry run — no folders will be deleted.", "warn")

        self._log(f"Processing {len(valid_dirs)} folder(s)...", "info")
        total_deleted = 0

        def folder_is_empty(path: Path) -> bool:
            """True if folder contains zero files anywhere inside it."""
            try:
                for entry in os.scandir(path):
                    if entry.is_file(follow_symlinks=False):
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        if not folder_is_empty(Path(entry.path)):
                            return False
                return True
            except Exception:
                return False

        def clean_dir(path: Path, depth: int = 0) -> int:
            """Recursively remove empty subdirs. Returns count deleted."""
            removed = 0
            try:
                subdirs = [Path(e.path) for e in os.scandir(path)
                           if e.is_dir(follow_symlinks=False)]
            except Exception:
                return 0
            for sub in subdirs:
                if self._stop_flag.is_set() or self._skip_flag.is_set():
                    break
                # Fast check: if the whole subtree has no files, nuke it instantly
                if folder_is_empty(sub):
                    if dry_run:
                        self._log(f"  [DRY] Would remove: {sub}", "warn")
                        removed += 1
                    else:
                        try:
                            shutil.rmtree(sub)
                            removed += 1
                            if verbose:
                                self._log(f"  DEL  {sub}", "warn")
                        except Exception as e:
                            self._log(f"  ERR  {sub}: {e}", "err")
                else:
                    # Has files somewhere inside - recurse to find empty subdirs
                    removed += clean_dir(sub, depth + 1)
            return removed

        for dir_idx, src_dir in enumerate(valid_dirs):
            if self._stop_flag.is_set() or self._skip_flag.is_set():
                break
            self._log(f"  [{dir_idx+1}/{len(valid_dirs)}] Scanning: {src_dir}", "info")
            pct = int((dir_idx / len(valid_dirs)) * 95)
            self._progress(mod_idx, pct, f"Cleaning: {src_dir.name}")
            deleted = clean_dir(src_dir)
            total_deleted += deleted
            self._log(f"  Done: {src_dir.name} — {deleted} folder(s) removed", "dim")

        self._progress(mod_idx, 100, "Complete")
        action = "Would remove" if dry_run else "Removed"
        self._log(
            f"Empty folder cleanup done — {action} {total_deleted} folder(s) across {len(valid_dirs)} location(s).",
            "ok"
        )
        if not hasattr(self, '_run_stats'): self._run_stats = {}
        self._run_stats[mod_idx] = self._run_stats.get(mod_idx, {})
        self._run_stats[mod_idx].update({"deleted": total_deleted, "dry_run": dry_run})

    # ------------------------------------------------------------------ #
    #  MODULE 4 — Duplicate File Finder & Deleter                         #
    # ------------------------------------------------------------------ #
    def _run_dupes(self, mod_idx: int, cfg: dict, verbose: bool):
        """
        Two-phase duplicate detection:
          1. Group files by size (fast, no I/O beyond stat)
          2. Partial MD5 of first 1 MB to filter obvious non-matches
          3. Full MD5 of remaining candidates to confirm duplicates

        Uses ThreadPoolExecutor to parallelise hashing.
        Progress file is written after each size group so a crashed run
        can resume without re-hashing already-processed groups.
        """
        import json
        from collections import defaultdict
        from concurrent.futures import ThreadPoolExecutor, as_completed

        PARTIAL_HASH_SIZE = 1024 * 1024  # 1 MB
        MAX_WORKERS       = 4

        raw_srcs = cfg.get("srcs") or ([cfg.get("src")] if cfg.get("src") else [])
        src_dirs = [Path(s).expanduser() for s in raw_srcs if s and s.strip()]
        dry_run  = cfg.get("dry_run", False)
        size_pre = cfg.get("size_pre", True)
        use_md5  = cfg.get("hash_md5", True)

        # Progress/resume file lives next to main.py
        progress_file = Path(__file__).parent / "dedupe_progress.json"

        self._log("=== MODULE 4: Duplicate File Finder ===", "info")

        if not src_dirs:
            self._log("No scan folders configured.", "err")
            self._progress(mod_idx, 100, "Error")
            return

        valid_dirs = [d for d in src_dirs if d.is_dir()]
        for d in src_dirs:
            if not d.is_dir():
                self._log(f"Folder not found (skipping): {d}", "warn")

        if not valid_dirs:
            self._log("No valid scan folders found.", "err")
            self._progress(mod_idx, 100, "Error")
            return

        if dry_run:
            self._log("Dry run — duplicates will be listed but NOT deleted.", "warn")

        self._log(f"Scanning {len(valid_dirs)} folder(s) for duplicates...", "info")

        # ── load resume progress ────────────────────────────────────────
        if progress_file.exists():
            try:
                with open(progress_file) as f:
                    resume = json.load(f)
                processed_sizes = set(resume.get("processed_sizes", []))
                self._log(f"Resuming — {len(processed_sizes)} size group(s) already done.", "info")
            except Exception:
                processed_sizes = set()
        else:
            processed_sizes = set()

        def _save_progress():
            try:
                with open(progress_file, "w") as f:
                    json.dump({"processed_sizes": list(processed_sizes)}, f)
            except Exception:
                pass

        # ── hashing helpers ─────────────────────────────────────────────
        def partial_hash(path):
            try:
                with open(path, "rb") as f:
                    return hashlib.md5(f.read(PARTIAL_HASH_SIZE)).hexdigest()
            except Exception:
                return None

        def full_hash(path):
            h = hashlib.md5()
            try:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(8192), b""):
                        h.update(chunk)
                return h.hexdigest()
            except Exception:
                return None

        def hash_group_partial(files):
            result = defaultdict(list)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(partial_hash, f): f for f in files}
                for fut in as_completed(futures):
                    h = fut.result()
                    if h:
                        result[h].append(futures[fut])
            return result

        def hash_group_full(files):
            """Returns list of (duplicate_path, original_path)."""
            seen = {}
            dupes = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(full_hash, f): f for f in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    h = fut.result()
                    if not h:
                        continue
                    if h in seen:
                        dupes.append((path, seen[h]))
                    else:
                        seen[h] = path
            return dupes

        # ── STEP 1: scan all file sizes across all src dirs ───────────
        self._log("Scanning file sizes...", "info")
        self._progress(mod_idx, 2, "Scanning files...")

        size_map = defaultdict(list)
        file_count = 0

        for src_dir in valid_dirs:
            self._log(f"  Scanning: {src_dir}", "dim")
            for root, dirs, files in os.walk(src_dir):
                if self._stop_flag.is_set(): return
                for fname in files:
                    fpath = os.path.join(root, fname)
                    file_count += 1
                    if file_count % 5000 == 0:
                        self._log(f"  Scanned {file_count:,} files...", "dim")
                        self._progress(mod_idx, 3, f"Scanning: {file_count:,} files found...")
                    try:
                        size_map[os.path.getsize(fpath)].append(fpath)
                    except Exception:
                        pass

        self._log(f"Scan complete: {file_count:,} files, {len(size_map):,} unique sizes.", "ok")

        # Only keep sizes with 2+ files
        candidate_sizes = {sz: paths for sz, paths in size_map.items()
                           if len(paths) >= 2 and (not size_pre or sz > 0)}

        self._log(f"{len(candidate_sizes)} size group(s) with potential duplicates.", "info")

        if not candidate_sizes:
            self._log("No duplicates found.", "ok")
            self._progress(mod_idx, 100, "No duplicates")
            return

        # ── STEP 2 & 3: hash and compare ───────────────────────────────
        total_dupes   = 0
        total_deleted = 0
        total_errors  = 0
        sizes_list    = list(candidate_sizes.items())

        for idx, (size, files) in enumerate(sizes_list):
            if self._stop_flag.is_set() or self._skip_flag.is_set():
                break

            pct = 5 + int((idx / len(sizes_list)) * 93)
            self._progress(mod_idx, pct, f"Hashing group {idx+1}/{len(sizes_list)} ({len(files)} files)")

            if size in processed_sizes:
                if verbose:
                    self._log(f"  SKIP (already processed)  size={size}", "dim")
                continue

            # Partial hash pass
            partial_map = hash_group_partial(files)

            # Full hash only on partial-hash collisions
            for p_hash, p_files in partial_map.items():
                if len(p_files) < 2:
                    continue
                if self._stop_flag.is_set(): return

                dupes = hash_group_full(p_files)

                for dup_path, orig_path in dupes:
                    total_dupes += 1
                    if verbose:
                        self._log(f"  DUPE  {Path(dup_path).name}", "warn")
                        self._log(f"    ← orig: {Path(orig_path).name}", "dim")
                    else:
                        self._log(f"  DUPE  {Path(dup_path).name}  (keeping {Path(orig_path).name})", "warn")

                    if not dry_run:
                        try:
                            os.remove(dup_path)
                            total_deleted += 1
                            if verbose:
                                self._log(f"    DELETED", "ok")
                        except Exception as e:
                            total_errors += 1
                            self._log(f"    ERR deleting: {e}", "err")
                    else:
                        self._log(f"    [DRY RUN — not deleted]", "dim")

            processed_sizes.add(size)
            _save_progress()

        # Clean up progress file if run completed cleanly
        if not self._stop_flag.is_set() and not self._skip_flag.is_set():
            try:
                if progress_file.exists():
                    progress_file.unlink()
            except Exception:
                pass

        self._progress(mod_idx, 100, "Complete")
        self._log(
            f"Dedupe done — {total_dupes} duplicate(s) found, "
            f"{total_deleted} deleted, {total_errors} error(s).",
            "ok" if total_errors == 0 else "warn"
        )
        if not hasattr(self, '_run_stats'): self._run_stats = {}
        self._run_stats[mod_idx] = self._run_stats.get(mod_idx, {})
        self._run_stats[mod_idx].update({
            "dupes": total_dupes, "deleted": total_deleted,
            "errors": total_errors, "dry_run": dry_run
        })


    # ------------------------------------------------------------------ #
    #  Summary Report                                                     #
    # ------------------------------------------------------------------ #
    def _save_report(self, report_dir: str, run_ts: str, total_elapsed: float,
                     stopped: bool, module_names: list, modules_enabled: list,
                     config: dict):
        """Save a plain-text pipeline summary report."""
        import datetime
        report_path = Path(report_dir)
        report_path.mkdir(parents=True, exist_ok=True)

        safe_ts  = run_ts.replace(":", "-").replace(" ", "_")
        filename = report_path / f"tosort_report_{safe_ts}.txt"

        lines = []
        lines.append("=" * 60)
        lines.append("  ToSort Toolkit — Pipeline Summary Report")
        lines.append("=" * 60)
        lines.append(f"  Run started : {run_ts}")
        lines.append(f"  Total time  : {total_elapsed}s")
        lines.append(f"  Status      : {'STOPPED BY USER' if stopped else 'COMPLETED'}")
        lines.append("")

        for i, name in enumerate(module_names):
            if not modules_enabled[i]:
                lines.append(f"  Module {i+1}: {name}")
                lines.append(f"    Status  : Disabled / Skipped")
                lines.append("")
                continue

            stats   = getattr(self, '_run_stats', {}).get(i, {})
            status  = stats.get("status", "unknown")
            elapsed = stats.get("elapsed", 0)

            lines.append(f"  Module {i+1}: {name}")
            lines.append(f"    Status  : {status.upper()}")
            lines.append(f"    Time    : {elapsed}s")

            if i == 0:  # Extractor
                lines.append(f"    Extracted       : {stats.get('extracted', '?')}")
                lines.append(f"    Nested moved    : {stats.get('nested', '?')}")
                lines.append(f"    Bad/corrupt     : {stats.get('bad', '?')}")
                lines.append(f"    Password-prot.  : {stats.get('password', '?')}")
                cfg0 = config.get("mod0", {})
                lines.append(f"    Source          : {cfg0.get('src','')}")
                lines.append(f"    Output          : {cfg0.get('out','')}")
                if cfg0.get("bad_dest"):
                    lines.append(f"    Bad archive dest: {cfg0.get('bad_dest','')}")

            elif i == 1:  # File Sorter
                lines.append(f"    Moved (general) : {stats.get('general', '?')}")
                lines.append(f"    Moved (ROM)     : {stats.get('rom', '?')}")
                lines.append(f"    Moved (companion): {stats.get('companion', '?')}")
                lines.append(f"    Errors          : {stats.get('errors', '?')}")
                cfg1 = config.get("mod1", {})
                lines.append(f"    Source          : {cfg1.get('src','')}")
                lines.append(f"    General output  : {cfg1.get('out','')}")
                if cfg1.get("rom_enable"):
                    lines.append(f"    ROM output      : {cfg1.get('rom_out','')}")

            elif i == 2:  # Empty folders
                lines.append(f"    Folders removed : {stats.get('deleted', '?')}")
                lines.append(f"    Dry run         : {stats.get('dry_run', False)}")

            elif i == 3:  # Dupes
                lines.append(f"    Duplicates found: {stats.get('dupes', '?')}")
                lines.append(f"    Deleted         : {stats.get('deleted', '?')}")
                lines.append(f"    Errors          : {stats.get('errors', '?')}")
                lines.append(f"    Dry run         : {stats.get('dry_run', False)}")

            if stats.get("error"):
                lines.append(f"    Error detail    : {stats['error']}")
            lines.append("")

        lines.append("=" * 60)
        lines.append(f"  Report saved: {filename}")
        lines.append("=" * 60)

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self._log(f"Summary report saved: {filename}", "ok")
        except Exception as e:
            self._log(f"Could not save report: {e}", "err")

    # ------------------------------------------------------------------ #
    #  SETTINGS — save/load all GUI state to JSON                         #
    # ------------------------------------------------------------------ #
    def load_settings(self) -> dict:
        """Called by JS on startup. Returns saved settings or empty dict."""
        settings_path = Path(__file__).parent / "settings.json"
        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def export_settings(self, path: str) -> bool:
        """Export all settings to a user-chosen JSON file."""
        try:
            import json
            settings = self.load_settings()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception:
            return False

    def import_settings(self, path: str) -> dict:
        """Import settings from a user-chosen JSON file."""
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            # Also save as current settings
            self.save_settings(settings)
            return settings
        except Exception:
            return None

    def browse_save_file(self, default_name: str = "settings.json"):
        """Open save dialog."""
        import webview as _wv
        result = self._window.create_file_dialog(
            _wv.SAVE_DIALOG,
            save_filename=default_name,
            file_types=('JSON Files (*.json)',)
        )
        if result:
            return result if isinstance(result, str) else result[0] if result else None
        return None

    def browse_open_file(self):
        """Open file dialog for JSON."""
        import webview as _wv
        result = self._window.create_file_dialog(
            _wv.OPEN_DIALOG,
            allow_multiple=False,
            file_types=('JSON Files (*.json)',)
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def save_settings(self, settings: dict) -> bool:
        """Called by JS on close / field change. Persists GUI state."""
        settings_path = Path(__file__).parent / "settings.json"
        try:
            with open(settings_path, "w") as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    #  WATCH FOLDER — background timer                                    #
    # ------------------------------------------------------------------ #
    def start_watch(self, config: dict):
        """Start the watch timer. config includes interval_minutes."""
        self._watch_stop = threading.Event()
        t = threading.Thread(
            target=self._watch_thread, args=(config,), daemon=True
        )
        t.start()
        return True

    def stop_watch(self):
        if hasattr(self, "_watch_stop"):
            self._watch_stop.set()
        return True

    def _watch_thread(self, config: dict):
        interval = int(config.get("interval_minutes", 30)) * 60
        mins = config.get("interval_minutes", 30)
        self._log(
            f"Watch mode active — pipeline will run every {mins} minute(s). "
            f"First run starting now...",
            "info"
        )
        while not self._watch_stop.is_set():
            # Run pipeline first
            self._emit("watchFired", {})
            self._log("Watch: starting pipeline run...", "info")
            self._pipeline_thread(config)
            if self._watch_stop.is_set():
                break
            # Then count down until next run
            self._log(f"Watch: pipeline complete — next run in {mins} minute(s).", "info")
            self._emit("watchWaiting", {"total": interval})
            for remaining in range(interval, 0, -1):
                if self._watch_stop.is_set():
                    break
                self._emit("watchTick", {"remaining": remaining, "total": interval})
                time.sleep(1)
