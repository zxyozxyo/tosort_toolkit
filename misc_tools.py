"""
Miscellaneous Utilities Backend
- Move to Folder: moves each file in a folder into a subfolder named after it
- Extras Collector: copies scene extras (.nfo/.diz/.jpg etc.) into named destination folders
"""

import os
import re
import json
import time
import shutil
import hashlib
import tempfile
import threading
import subprocess
from pathlib import Path

import webview


EXTRA_EXTS   = {'.nfo', '.diz', '.jpg', '.jpeg', '.png', '.sfv', '.nzb'}
EXTRA_DIRS   = {'proof', 'sample'}


class MiscToolsAPI:
    def __init__(self, auto_loop: bool = True):
        self._window    = None
        self._stop_flag = threading.Event()
        self._running   = False
        # Auto-backup timer resumes whenever the Misc Tools window is open.
        # auto_loop=False is used for the launcher's config-only instance (it
        # runs the one-shot startup backup instead, not the periodic loop).
        if auto_loop:
            threading.Thread(target=self._auto_backup_loop, daemon=True).start()

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j
            # Escape backslashes first (Windows paths), then quotes — otherwise
            # JSON.parse fails in the GUI and the log line is silently dropped.
            payload = (_j.dumps(data, ensure_ascii=True)
                       .replace("\\", "\\\\").replace("'", "\\'"))
            self._window.evaluate_js(
                f"window.miscEvent('{event}', JSON.parse('{payload}'))"
            )
        except Exception:
            pass

    def _log(self, msg: str, cls: str = 'info', target: str = 'ext'):
        self._emit('log', {'msg': msg, 'cls': cls, 'target': target})

    # ── Browse ────────────────────────────────────────────────────────────────

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk(); root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askdirectory()
            root.destroy()
            return path if path else ''
        except Exception:
            return ''

    # ── Move to Folder ────────────────────────────────────────────────────────

    def move_to_folder(self, folder: str) -> dict:
        """Move each file in folder into a subfolder named after it (no extension)."""
        folder = folder.strip()
        if not folder or not Path(folder).is_dir():
            return {'ok': False, 'error': 'Folder not found'}
        base = Path(folder)
        moved = 0
        skipped = 0
        errors = []
        self._log(f'Move to Folder: scanning {folder}', 'info', 'mtf')
        try:
            for item in sorted(base.iterdir()):
                if not item.is_file():
                    continue
                target_dir = base / item.stem
                target_dir.mkdir(exist_ok=True)
                dest = target_dir / item.name
                if dest.exists():
                    self._log(f'  SKIP (already exists): {item.name}', 'warn', 'mtf')
                    skipped += 1
                    continue
                try:
                    shutil.move(str(item), str(dest))
                    self._log(f'  Moved: {item.name} → {item.stem}/', 'dim', 'mtf')
                    moved += 1
                except Exception as e:
                    self._log(f'  ERROR: {item.name}: {e}', 'err', 'mtf')
                    errors.append(item.name)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        self._log(f'Done — {moved} moved, {skipped} skipped, {len(errors)} errors', 'ok' if not errors else 'warn', 'mtf')
        return {'ok': True, 'moved': moved, 'skipped': skipped, 'errors': errors}

    # ── Extras Collector ──────────────────────────────────────────────────────

    def start_extras_collect(self, config: dict) -> bool:
        if self._running:
            self._log('Already running.', 'warn')
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._extras_thread, args=(config,), daemon=True)
        t.start()
        return True

    def stop_extras_collect(self):
        self._stop_flag.set()
        self._log('Stopping…', 'warn')

    def _extras_thread(self, config: dict):
        self._running = True
        self._emit('status', {'state': 'running'})

        src       = config.get('src', '').strip()
        dst       = config.get('dst', '').strip()
        pack_mode = config.get('pack_mode', 'none')  # 'none' | 'zip' | 'rar'

        if not src or not Path(src).is_dir():
            self._log('ERROR: Source folder not found.', 'err')
            self._running = False; self._emit('status', {'state': 'error'}); return
        if not dst:
            self._log('ERROR: No destination folder set.', 'err')
            self._running = False; self._emit('status', {'state': 'error'}); return

        dst_path = Path(dst)
        try:
            dst_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._log(f'ERROR: Cannot create destination: {e}', 'err')
            self._running = False; self._emit('status', {'state': 'error'}); return

        self._log(f'Extras Collector starting', 'info')
        self._log(f'  Source : {src}', 'dim')
        self._log(f'  Dest   : {dst}', 'dim')
        self._log(f'  Pack   : {pack_mode}', 'dim')
        self._log('', '')

        found = 0
        packed = 0

        for dirpath, dirnames, filenames in os.walk(src):
            if self._stop_flag.is_set():
                break

            folder    = Path(dirpath)
            # Extra files directly in this folder
            extra_files = [f for f in filenames if Path(f).suffix.lower() in EXTRA_EXTS]
            # Extra subdirs (case-insensitive match)
            extra_subdirs = [d for d in dirnames if d.lower() in EXTRA_DIRS]

            if not extra_files and not extra_subdirs:
                continue

            rel_path   = folder.relative_to(src)
            out_folder = dst_path / rel_path

            # Avoid overwriting an already-packed result
            if (out_folder.parent / (folder.name + '.zip')).exists() or \
               (out_folder.parent / (folder.name + '.rar')).exists():
                self._log(f'  SKIP (already packed): {folder.name}', 'dim')
                continue

            try:
                out_folder.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self._log(f'  ERROR creating {out_folder}: {e}', 'err')
                continue

            # Copy extra files
            for fname in extra_files:
                src_f = folder / fname
                dst_f = out_folder / fname
                if not dst_f.exists():
                    try:
                        shutil.copy2(str(src_f), str(dst_f))
                        self._log(f'  Copied: {folder.name}/{fname}', 'dim')
                    except Exception as e:
                        self._log(f'  ERROR copying {fname}: {e}', 'err')

            # Copy extra subdirectories (Proof/, Sample/)
            for dname in extra_subdirs:
                src_d = folder / dname
                dst_d = out_folder / dname
                try:
                    if dst_d.exists():
                        shutil.rmtree(str(dst_d))
                    shutil.copytree(str(src_d), str(dst_d))
                    self._log(f'  Copied folder: {folder.name}/{dname}/', 'dim')
                except Exception as e:
                    self._log(f'  ERROR copying folder {dname}: {e}', 'err')

            found += 1
            self._log(f'  ✓ {str(rel_path)} — {len(extra_files)} file(s), {len(extra_subdirs)} folder(s)', 'ok')

            # Pack if requested
            if pack_mode in ('zip', 'rar') and not self._stop_flag.is_set():
                ok = self._pack_folder(out_folder, pack_mode, out_folder.parent)
                if ok:
                    packed += 1

        self._log('', '')
        if self._stop_flag.is_set():
            self._log('Stopped by user.', 'warn')
        else:
            msg = f'Done — {found} game folder(s) collected'
            if pack_mode != 'none':
                msg += f', {packed} packed'
            self._log(msg, 'ok')

        self._running = False
        self._emit('status', {'state': 'done', 'found': found})

    def _pack_folder(self, folder: Path, mode: str, dst_root: Path) -> bool:
        """Pack folder into zip or rar next to it, then remove the folder."""
        app_dir = Path(__file__).parent / 'apps'
        out_name = folder.name

        try:
            if mode == 'zip':
                exe = self._find_exe(['7z.exe', '7za.exe'], app_dir)
                if not exe:
                    self._log(f'  SKIP pack: 7z.exe not found in apps/', 'warn')
                    return False
                out_file = dst_root / (out_name + '.zip')
                cmd = [exe, 'a', '-tzip', str(out_file), str(folder / '*')]
            else:  # rar
                exe = self._find_exe(['Rar.exe', 'rar.exe'], app_dir)
                if not exe:
                    self._log(f'  SKIP pack: Rar.exe not found in apps/', 'warn')
                    return False
                out_file = dst_root / (out_name + '.rar')
                cmd = [exe, 'a', '-ep1', str(out_file), str(folder) + os.sep]

            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode in (0, 1):
                shutil.rmtree(str(folder))
                self._log(f'  Packed: {out_name}.{mode}', 'ok')
                return True
            else:
                self._log(f'  Pack FAILED ({r.returncode}): {out_name}', 'err')
                return False
        except Exception as e:
            self._log(f'  Pack error {out_name}: {e}', 'err')
            return False

    @staticmethod
    def _find_exe(names: list, search_dir: Path) -> str:
        for name in names:
            p = search_dir / name
            if p.exists():
                return str(p)
        return ''

    # ── Local Backup ──────────────────────────────────────────────────────────
    # Two modes: "full" (entire toolkit incl. apps/) into timestamped folders,
    # "quick" (json settings/credentials/results + rclone.conf) into a rolling
    # tosort_quick_backup folder. Multiple destinations, backed up in sequence.
    # Settings persist in misc_tools.json; the auto timer resumes on start.

    _CFG_PATH = Path(__file__).parent / "misc_tools.json"

    def _load_cfg(self) -> dict:
        try:
            return json.loads(self._CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cfg(self, cfg: dict):
        try:
            self._CFG_PATH.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        except Exception:
            pass

    def backup_get_settings(self) -> dict:
        b = self._load_cfg().get("backup", {})
        return {
            "dests":       b.get("dests", []),
            "mode":        b.get("mode", "quick"),
            "auto":        bool(b.get("auto", False)),
            "startup":     bool(b.get("startup", False)),
            "interval_h":  float(b.get("interval_h", 6)),
            "last_backup": float(b.get("last_backup", 0)),
        }

    def backup_save_settings(self, settings: dict) -> dict:
        cfg = self._load_cfg()
        prev = cfg.get("backup", {})
        cfg["backup"] = {
            "dests":       [d for d in (settings.get("dests") or []) if d.strip()],
            "mode":        settings.get("mode", "quick"),
            "auto":        bool(settings.get("auto", False)),
            # Preserve the startup toggle + last-backup stamp: this save comes
            # from the Misc Tools form, which doesn't own those fields (the home
            # checkbox sets `startup`; a completed backup writes `last_backup`).
            "startup":     bool(settings.get("startup", prev.get("startup", False))),
            "interval_h":  max(0.25, float(settings.get("interval_h", 6) or 6)),
            "last_backup": float(prev.get("last_backup", 0)),
        }
        self._save_cfg(cfg)
        return {"ok": True}

    def backup_set_startup(self, enabled) -> dict:
        """Toggle the run-at-startup flag (used by the launcher-hub checkbox)
        without touching the other backup settings."""
        cfg = self._load_cfg()
        cfg.setdefault("backup", {})["startup"] = bool(enabled)
        self._save_cfg(cfg)
        return {"ok": True}

    def backup_startup_info(self) -> dict:
        """State for the launcher-hub checkbox: whether startup backup is on,
        the interval + mode + destination count (set in Misc Tools), and when
        the last backup ran."""
        import time as _t
        s = self.backup_get_settings()
        last = s["last_backup"]
        return {
            "startup":         s["startup"],
            "interval_h":      s["interval_h"],
            "mode":            s["mode"],
            "dests":           len(s["dests"]),
            "last_backup":     last,
            "last_backup_str": (_t.strftime("%Y-%m-%d %H:%M", _t.localtime(last))
                                if last else ""),
        }

    def maybe_run_startup_backup(self, log_cb=None) -> dict:
        """Run ONE backup at app launch, but only if the startup option is on,
        destinations are configured, AND at least interval_h has elapsed since
        the last backup — so frequent restarts (e.g. while testing) don't
        trigger a backup every time. Meant to be called from a background
        thread; runs the copy synchronously."""
        import time as _t
        def _say(m, c="info"):
            if log_cb:
                try:
                    log_cb(m, c)
                except Exception:
                    pass
        s = self.backup_get_settings()
        if not s["startup"]:
            return {"ran": False, "reason": "disabled"}
        if not s["dests"]:
            _say("Startup backup is on but no destinations are set "
                 "(configure them in Misc Tools → Local Backup).", "warn")
            return {"ran": False, "reason": "no_dests"}
        elapsed = _t.time() - s["last_backup"]
        interval = s["interval_h"] * 3600
        if s["last_backup"] and elapsed < interval:
            _say(f"Startup backup skipped — last backup was {elapsed/3600:.1f}h "
                 f"ago (interval {s['interval_h']:g}h).", "dim")
            return {"ran": False, "reason": "not_due"}
        _say(f"Startup backup running ({s['mode']}, "
             f"{len(s['dests'])} destination(s))…", "info")
        self._backup_thread(s["dests"], s["mode"], True)
        _say("Startup backup complete.", "ok")
        return {"ran": True}

    def _quick_files(self, src: Path) -> list:
        files = sorted(src.glob("*.json"))
        for extra in ("srrdb_results.csv", "srrdb_results.xlsx"):
            p = src / extra
            if p.exists():
                files.append(p)
        rconf = src / "rclone" / "rclone.conf"
        if rconf.exists():
            files.append(rconf)
        return files

    def backup_now(self) -> dict:
        """Run a backup with the SAVED settings (all destinations, in sequence)."""
        s = self.backup_get_settings()
        if not s["dests"]:
            return {"ok": False, "error": "No destinations configured"}
        if self._running:
            return {"ok": False, "error": "Another operation is running"}
        threading.Thread(target=self._backup_thread,
                         args=(s["dests"], s["mode"], False), daemon=True).start()
        return {"ok": True}

    def _backup_thread(self, dests: list, mode: str, auto: bool):
        self._running = True
        L = lambda m, c="info": self._log(m, c, "bak")
        src = Path(__file__).parent
        tag = "AUTO " if auto else ""
        import time as _t
        try:
            if mode == "quick":
                files = self._quick_files(src)
                L(f"{tag}Quick backup — {len(files)} settings/data file(s) "
                  f"→ {len(dests)} destination(s)", "info")
            else:
                files = [f for f in src.rglob("*")
                         if f.is_file() and "__pycache__" not in
                         f.relative_to(src).parts]
                L(f"{tag}Full backup — {len(files)} file(s) "
                  f"→ {len(dests)} destination(s)", "info")

            for dest in dests:
                dst_root = Path(dest)
                try:
                    if src in [dst_root, *dst_root.parents]:
                        L(f"  SKIP {dest} — inside the toolkit folder", "err")
                        continue
                    dst_root.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    L(f"  SKIP {dest} — {e}", "err")
                    continue
                if mode == "quick":
                    target = dst_root / "tosort_quick_backup"   # rolling latest
                else:
                    target = dst_root / ("tosort_toolkit_backup_"
                                         + _t.strftime("%Y%m%d-%H%M"))
                copied = errors = 0
                total = 0
                for f in files:
                    rel = f.relative_to(src)
                    out = target / rel
                    try:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(f), str(out))
                        copied += 1
                        total += f.stat().st_size
                        if copied % 250 == 0:
                            L(f"  {dest}: {copied} files…", "dim")
                    except Exception as e:
                        errors += 1
                        L(f"  ERROR: {rel}: {e}", "err")
                L(f"  ✓ {target} — {copied} file(s), {total:,} B"
                  + (f", {errors} error(s)" if errors else ""),
                  "ok" if not errors else "warn")
            L(f"{tag}Backup run complete.", "ok")
            # Stamp completion so the startup auto-backup knows whether a fresh
            # one is due (prevents a backup on every quick restart).
            try:
                c = self._load_cfg()
                c.setdefault("backup", {})["last_backup"] = _t.time()
                self._save_cfg(c)
            except Exception:
                pass
        except Exception as e:
            L(f"Backup FAILED: {e}", "err")
        finally:
            self._running = False

    def _auto_backup_loop(self):
        """Runs for the lifetime of the Misc Tools window. Sleeps the configured
        interval, then performs a backup with the saved settings if enabled."""
        import time as _t
        while True:
            s = self.backup_get_settings()
            if not (s["auto"] and s["dests"]):
                _t.sleep(30)
                continue
            _t.sleep(max(900, s["interval_h"] * 3600))
            s = self.backup_get_settings()   # re-read: user may have toggled
            if s["auto"] and s["dests"] and not self._running:
                self._backup_thread(s["dests"], s["mode"], True)

    # ── RAR Inspector ─────────────────────────────────────────────────────────

    _RAR4_SIG = b"Rar!\x1a\x07\x00"
    _RAR5_SIG = b"Rar!\x1a\x07\x01\x00"
    _METHODS  = {0x30: "Store (uncompressed, -m0)", 0x31: "Fastest (-m1)",
                 0x32: "Fast (-m2)", 0x33: "Normal (-m3)",
                 0x34: "Good (-m4)", 0x35: "Best (-m5)"}
    _HOST_OS  = {0: "MS-DOS", 1: "OS/2", 2: "Windows", 3: "Unix", 4: "Mac", 5: "BeOS"}
    _EXTRACT_VER = {
        15: "RAR 1.5x (pre-1996)",
        20: "RAR 2.0–2.5x era (1996–2000)",
        26: "RAR 2.6 era (2000)",
        29: "RAR 2.9–4.20 era (2001–2013)",
        36: "RAR 3.6 alternative (rare)",
        50: "RAR 5.x+ (2013 or later)",
    }
    _DICT_KB  = {0: 64, 1: 128, 2: 256, 3: 512, 4: 1024, 5: 2048, 6: 4096}

    def analyze_rar(self, folder: str) -> dict:
        """Inspect every RAR set under a folder: format, compression method,
        minimum extract version (creation-era hint), dictionary size, archive
        flags — and a reconstruction verdict for pyReScene."""
        L = lambda m, c="info": self._log(m, c, "rai")
        base = Path((folder or "").strip())
        if not base.is_dir():
            return {"ok": False, "error": "Folder not found"}
        try:
            import rarfile
        except ImportError:
            L("rarfile library not installed — pip install rarfile", "err")
            return {"ok": False, "error": "rarfile not installed"}

        # Set heads: every .rar that is not a .partN (N>1) continuation volume
        heads = [
            p for p in sorted(base.rglob("*.rar"))
            if not re.search(r"\.part0*(?!1\b)\d+\.rar$", p.name, re.IGNORECASE)
            or re.search(r"\.part0*1\.rar$", p.name, re.IGNORECASE)
        ]
        zips = sorted(base.rglob("*.zip"))
        sevens = sorted(base.rglob("*.7z"))
        if not heads and not zips and not sevens:
            L("No .rar / .zip / .7z files found under this folder.", "warn")
            return {"ok": True, "sets": 0}

        L(f"Found {len(heads)} RAR set(s), {len(zips)} ZIP(s), "
          f"{len(sevens)} 7z under {base}", "info")
        for head in heads:
            L("", "")
            L(f"══ {head.relative_to(base)} ══", "info")
            try:
                self._analyze_one_set(head, rarfile, L)
            except Exception as e:
                L(f"  ERROR: {e}", "err")
        for z in zips:
            L("", "")
            L(f"══ {z.relative_to(base)} ══", "info")
            try:
                self._analyze_zip(z, L)
            except Exception as e:
                L(f"  ERROR: {e}", "err")
        for s in sevens:
            L("", "")
            L(f"══ {s.relative_to(base)} ══", "info")
            try:
                self._analyze_7z(s, L)
            except Exception as e:
                L(f"  ERROR: {e}", "err")
        L("", "")
        L(f"Analysis complete — {len(heads) + len(zips) + len(sevens)} archive(s).", "ok")
        return {"ok": True, "sets": len(heads) + len(zips) + len(sevens)}

    _ZIP_METHODS = {0: "Store", 8: "Deflate", 9: "Deflate64", 12: "BZip2",
                    14: "LZMA", 93: "Zstandard", 95: "XZ", 98: "PPMd", 99: "AES-encrypted"}
    _ZIP_SYSTEMS = {0: "MS-DOS/Windows", 3: "Unix", 7: "Macintosh",
                    10: "Windows NTFS", 19: "OS X"}

    def _analyze_zip(self, path, L):
        import zipfile
        with zipfile.ZipFile(str(path)) as zf:
            infos = zf.infolist()
            comment = zf.comment or b""
            methods, systems, versions = set(), set(), set()
            encrypted = False
            tot_u = tot_c = 0
            for i in infos:
                methods.add(i.compress_type)
                systems.add(i.create_system)
                versions.add(i.create_version)
                if i.flag_bits & 0x1:
                    encrypted = True
                tot_u += i.file_size
                tot_c += i.compress_size
            L("  Format: ZIP", "dim")
            L(f"  Entries: {len(infos)}  ({tot_u:,} B → {tot_c:,} B, "
              f"{(100 * tot_c / tot_u) if tot_u else 100:.1f}%)", "dim")
            for i in infos[:10]:
                m = self._ZIP_METHODS.get(i.compress_type, str(i.compress_type))
                ratio = (100 * i.compress_size / i.file_size) if i.file_size else 100
                L(f"    {i.filename}  {i.file_size:,} B → {i.compress_size:,} B "
                  f"({ratio:.1f}%)  [{m}]", "dim")
            if len(infos) > 10:
                L(f"    … and {len(infos) - 10} more", "dim")
            L("  Created on: " + ", ".join(
                self._ZIP_SYSTEMS.get(s, f"system {s}") for s in sorted(systems)), "dim")
            L("  Creator version(s): " + ", ".join(
                f"{v / 10:.1f}" for v in sorted(versions)), "dim")
            if comment:
                L(f"  ⚠ EOCD comment present ({len(comment)} B) — often a topsite "
                  "tagline grow-append (Scene ZIP Recreator can strip it)", "warn")
            if encrypted:
                L("  ⚠ Password protected", "warn")
            L("  Note: scene ZIP repair/byte-matching is handled by the "
              "Scene ZIP Recreator module.", "dim")

    def _analyze_7z(self, path, L):
        try:
            import py7zr
        except ImportError:
            L("  py7zr not installed — pip install py7zr", "err")
            return
        with py7zr.SevenZipFile(str(path), mode="r") as z:
            info = z.archiveinfo()
            entries = z.list()
            L("  Format: 7z", "dim")
            L(f"  Entries: {len(entries)}  (uncompressed {info.uncompressed:,} B, "
              f"archive {Path(path).stat().st_size:,} B)", "dim")
            L(f"  Method(s): {', '.join(info.method_names)}", "dim")
            L(f"  Solid: {'YES' if info.solid else 'no'}   Blocks: {info.blocks}", "dim")
            for e in entries[:10]:
                L(f"    {e.filename}  {e.uncompressed:,} B", "dim")
            if len(entries) > 10:
                L(f"    … and {len(entries) - 10} more", "dim")
            if z.needs_password():
                L("  ⚠ Password protected", "warn")

    def _analyze_one_set(self, head: Path, rarfile, L):
        # Format from magic bytes
        with open(head, "rb") as f:
            magic = f.read(8)
            # Main archive header flags (RAR4): marker(7) + CRC(2) type(1) flags(2)
            main_flags = 0
            if magic[:7] == self._RAR4_SIG:
                f.seek(7)
                hdr = f.read(7)
                if len(hdr) == 7 and hdr[2] == 0x73:
                    main_flags = int.from_bytes(hdr[3:5], "little")

        if magic[:8] == self._RAR5_SIG:
            fmt = "RAR5"
        elif magic[:7] == self._RAR4_SIG:
            fmt = "RAR4"
        elif magic[:2] == b"MZ":
            fmt = "SFX (self-extracting)"
        else:
            fmt = "unknown"
        L(f"  Format: {fmt}", "dim")

        if fmt == "RAR4" and main_flags:
            fl = []
            if main_flags & 0x0001: fl.append("multi-volume")
            if main_flags & 0x0008: fl.append("SOLID")
            if main_flags & 0x0010: fl.append("new-style naming (.partN)")
            if main_flags & 0x0040: fl.append("recovery record")
            if main_flags & 0x0080: fl.append("encrypted headers")
            if main_flags & 0x0004: fl.append("locked")
            if fl:
                L(f"  Archive flags: {', '.join(fl)}", "dim")

        rf = rarfile.RarFile(str(head))
        try:
            vols = rf.volumelist()
            vol_bytes = sum(Path(v).stat().st_size for v in vols if Path(v).exists())
            L(f"  Volumes: {len(vols)} ({vol_bytes:,} B total)", "dim")

            infos = [i for i in rf.infolist() if i.is_file()]
            methods, vers = set(), set()
            solid_any = bool(main_flags & 0x0008)
            dict_kbs = set()
            for i in infos:
                methods.add(i.compress_type)
                if i.extract_version:
                    vers.add(i.extract_version)
                if fmt == "RAR4" and i.flags is not None:
                    if i.flags & 0x10:
                        solid_any = True
                    d = (i.flags >> 5) & 7
                    if d in self._DICT_KB:
                        dict_kbs.add(self._DICT_KB[d])

            L(f"  Packed files ({len(infos)}):", "dim")
            for i in infos[:12]:
                m = self._METHODS.get(i.compress_type, hex(i.compress_type or 0))
                ratio = (100 * (i.compress_size or 0) / i.file_size) if i.file_size else 100
                crc = getattr(i, "CRC", None)
                crc_s = f"  CRC={crc & 0xFFFFFFFF:08X}" if crc is not None else ""
                dt = getattr(i, "date_time", None)
                ts = ("  %04d-%02d-%02d %02d:%02d:%02d" % dt) if dt else ""
                # Exact packed bytes are the single most diagnostic field on a
                # near-miss: same packed_size + wrong CRC → a byte-level (build/
                # header/comment) diff, NOT version/-mt; different packed_size →
                # a compression diff (version/-mt/dict).
                L(f"    {i.filename}  {i.file_size:,} B → "
                  f"{i.compress_size:,} B ({ratio:.1f}%)  [{m}]{crc_s}{ts}", "dim")
            if len(infos) > 12:
                L(f"    … and {len(infos) - 12} more", "dim")
            # Comments change the archive hash and must be reproduced exactly —
            # a hidden comment is a classic silent near-miss cause.
            cmt = getattr(rf, "comment", None)
            if cmt:
                L(f"  ⚠ Archive comment present ({len(cmt)} B) — affects the "
                  "archive hash; must be reproduced exactly to rebuild.", "warn")

            for v in sorted(vers):
                era = self._EXTRACT_VER.get(v, f"unknown (version byte {v})")
                L(f"  Min. version to extract: {v / 10:.1f}  →  created with {era}", "info")
            if dict_kbs:
                L(f"  Dictionary size: {', '.join(f'{k} KB' for k in sorted(dict_kbs))}"
                  + ("  (-md switch must match for rebuild)" if len(dict_kbs) == 1 else ""),
                  "dim")
            host = {i.host_os for i in infos if i.host_os is not None}
            if host:
                L(f"  Created on: {', '.join(self._HOST_OS.get(h, str(h)) for h in sorted(host))}", "dim")
            if rf.needs_password():
                L("  ⚠ Password protected", "warn")

            # Reconstruction verdict
            compressed = any(m != 0x30 for m in methods)
            if fmt == "RAR5":
                L("  ⛔ Verdict: RAR5 — pyReScene 0.7 cannot reconstruct this format.", "err")
            elif not compressed:
                L("  ✓ Verdict: uncompressed (stored) — pyReScene rebuilds natively, "
                  "no rar.exe needed.", "ok")
            else:
                mnames = ", ".join(self._METHODS.get(m, hex(m)) for m in sorted(methods))
                L(f"  ⚠ Verdict: COMPRESSED ({mnames}) — rebuild needs the exact "
                  "original rar.exe version.", "warn")
                if 29 in vers:
                    L("    Era hint: version byte 29 spans WinRAR 2.90–4.20 "
                      "(2001–2013). rescene will try each pack version in date "
                      "order; make sure that whole range is in the pack.", "dim")
                elif 20 in vers or 26 in vers:
                    L("    Era hint: WinRAR 2.0x–2.6x (1996–2000) — needs very "
                      "old rar.exe versions in the pack.", "dim")
                if solid_any:
                    L("    ⚠ SOLID archive — all files compressed as one stream; "
                      "reconstruction needs every source file present and is "
                      "more version-sensitive.", "warn")
        finally:
            rf.close()

    # ── Recipe Probe ──────────────────────────────────────────────────────────
    # Find the exact (WinRAR build, -mt) that reproduces an ORIGINAL rar's
    # compressed streams byte-for-byte — the recipe an SRR can't store. This is
    # the diagnostic that turns "stubborn near-miss" into a definitive answer,
    # and the core capture step for the future .srr2 format.

    _MD_LETTER = {64: "A", 128: "B", 256: "C", 512: "D",
                  1024: "E", 2048: "F", 4096: "G"}

    def stop_probe(self) -> dict:
        """Signal the recipe probe to stop after the current attempt."""
        self._stop_flag.set()
        return {"ok": True}

    def _probe_progress(self, msg: str):
        """Update the single transient progress line (overwrites in place, so the
        live counter never fills the log). Milestones use _log (permanent)."""
        self._emit("progress", {"msg": msg, "target": "rai"})

    def probe_recipe(self, folder: str, max_mt: int = 16) -> dict:
        """Launch the recipe probe in the background (it runs many rar.exe
        compresses, so it must not block the GUI thread). Progress + result go
        to the log; a 'probe_done' event re-enables the buttons when finished."""
        if getattr(self, "_probe_running", False):
            return {"ok": False, "error": "A probe is already running"}
        self._stop_flag.clear()

        def _bg():
            self._probe_running = True
            try:
                self._probe_run(folder, max_mt)
            except Exception as e:
                self._log(f"Probe error: {e}", "err", "rai")
            finally:
                self._probe_running = False
                self._emit("progress", {"msg": "", "target": "rai"})
                self._emit("probe_done", {})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "started": True}

    def _probe_run(self, folder: str, max_mt: int = 16) -> dict:
        """For an ORIGINAL rar set in `folder`, recompress its own content across
        pack build × thread-count and find which combo reproduces every packed
        stream BYTE-EXACT (compared via RarStream, header-independent). Reports
        the winning (build, -mt) — or proves no pack combo matches (a genuine
        wall). Builds are DE-DUPED by output family (RAR3/4 point releases emit
        identical bytes), so 232 exes collapse to ~a couple dozen real tries."""
        L = lambda m, c="info": self._log(m, c, "rai")
        self._stop_flag.clear()
        base = Path((folder or "").strip())
        if not base.is_dir():
            L("Folder not found.", "err"); return {"ok": False}
        try:
            import rarfile
            from rescene.rarstream import RarStream  # type: ignore
        except ImportError as e:
            L(f"Missing dependency: {e} (need rarfile + rescene).", "err")
            return {"ok": False}
        pack = Path(__file__).parent / "apps" / "winrar_pack-4.20"
        exes = sorted(pack.glob("*_rar*.exe")) if pack.is_dir() else []
        if not exes:
            L("WinRAR pack not found (apps/winrar_pack-4.20/*.exe).", "err")
            return {"ok": False}

        heads = [p for p in sorted(base.rglob("*.rar"))
                 if not re.search(r"\.part0*(?!1\b)\d+\.rar$", p.name, re.I)
                 or re.search(r"\.part0*1\.rar$", p.name, re.I)]
        if not heads:
            L("No .rar set found under this folder.", "err"); return {"ok": False}
        head = heads[0]
        L("", ""); L(f"══ PROBE: {head.relative_to(base)} ══", "info")

        rf = rarfile.RarFile(str(head))
        try:
            infos = [i for i in rf.infolist() if i.is_file()]
            if not infos:
                L("  No packed files.", "warn"); return {"ok": False}
            # Compression recipe to replicate (from the first compressed file).
            comp = next((i for i in infos if (i.compress_type or 0x30) != 0x30),
                        infos[0])
            level = (comp.compress_type or 0x30) - 0x30
            dkb = 4096
            if comp.flags is not None:
                dkb = self._DICT_KB.get((comp.flags >> 5) & 7, 4096)
            solid = any((i.flags or 0) & 0x10 for i in infos)
            md = self._MD_LETTER.get(dkb, "G")
            L(f"  Recipe to match: -m{level} -md{md} ({dkb} KB) "
              f"{'-s (SOLID)' if solid else '-s-'} · {len(infos)} file(s)", "dim")

            # Target: each packed stream's exact bytes (header-independent).
            targets = {}
            for i in infos:
                try:
                    with RarStream(str(head), packed_file_name=i.filename,
                                   compressed=True) as rs:
                        data = rs.read()
                    targets[i.filename] = (len(data), hashlib.sha1(data).digest())
                except Exception as e:
                    L(f"  ⚠ couldn't read original stream {i.filename}: {e}", "warn")
            if not targets:
                L("  Couldn't read any original compressed streams — abort.", "err")
                return {"ok": False}

            # Extract the sources from the original (via a pack rar.exe, so no
            # unrar backend is needed), preserving names.
            work = Path(tempfile.mkdtemp(prefix="probe-"))
            src = work / "src"; src.mkdir()
            xr = subprocess.run(
                [str(exes[-1]), "x", "-y", "-o+", str(head), str(src) + os.sep],
                capture_output=True)
            order = [i.filename for i in infos]
            src_files = [str(src / n) for n in order]
            if not all(Path(f).is_file() for f in src_files):
                L(f"  Extraction incomplete (rc={xr.returncode}) — abort.", "err")
                shutil.rmtree(work, ignore_errors=True); return {"ok": False}
            # Family-dedup must key on a COMPRESSED file — a stored extra (e.g. an
            # EXiMiUS proof jpg) compresses identically on every build and would
            # wrongly collapse all families into one. Use the SMALLEST compressed
            # file as the discriminator, and only dedup when it's small enough
            # that 232 test-compresses are cheap.
            comp_idx = [k for k, i in enumerate(infos)
                        if (i.compress_type or 0x30) != 0x30]
            disc_idx = min(comp_idx, key=lambda k: infos[k].file_size,
                           default=None)
            dedup = (disc_idx is not None
                     and infos[disc_idx].file_size <= 8 * 1024 * 1024)
        finally:
            rf.close()

        def _families(exes):
            """Collapse builds that emit identical output into one representative
            — compress the SMALLEST compressed file at -mt1 and hash it."""
            seen, reps = {}, []
            probe = work / "fam.rar"
            dname = order[disc_idx]
            total = len(exes)
            for idx, ex in enumerate(exes, 1):
                if self._stop_flag.is_set():
                    break
                try:
                    probe.unlink(missing_ok=True)
                except Exception:
                    pass
                cmd = [str(ex), "a", f"-m{level}", f"-md{md}",
                       "-s" if solid else "-s-", "-ds", "-mt1", "-o+", "-ep",
                       "-idcd", str(probe), src_files[disc_idx]]
                subprocess.run(cmd, capture_output=True)
                try:
                    with RarStream(str(probe), packed_file_name=dname,
                                   compressed=True) as rs:
                        sig = hashlib.sha1(rs.read()).digest()
                except Exception:
                    sig = ("ERR", ex.name)
                if sig not in seen:
                    seen[sig] = ex
                    reps.append(ex)
                if idx % 8 == 0 or idx == total:
                    self._probe_progress(
                        f"fingerprinting builds… {idx}/{total}  "
                        f"({len(reps)} distinct so far)")
            return reps

        if dedup:
            L(f"  De-duping {len(exes)} pack builds by output family "
              f"(via {order[disc_idx]})…", "dim")
            reps = _families(exes)
            L(f"  → {len(reps)} distinct build famil"
              f"{'y' if len(reps) == 1 else 'ies'} to probe × up to -mt{max_mt}.",
              "dim")
        else:
            reps = exes
            L(f"  No small compressed file to dedup on — probing all {len(exes)} "
              f"builds × up to -mt{max_mt} (early-exit on match).", "dim")

        mts = [n for n in (8, 4, 2, 1, 3, 6, 5, 7, 0,
                           *range(9, max_mt + 1)) if n <= max_mt]
        probe = work / "probe.rar"
        tried = 0
        try:
            for fi, ex in enumerate(reps, 1):
                if self._stop_flag.is_set():
                    L("  Stopped.", "warn"); break
                ver = self._exe_label(ex.name)
                for n in mts:
                    if self._stop_flag.is_set():
                        break
                    tried += 1
                    self._probe_progress(
                        f"probing {ver} -mt{n}…  (build {fi}/{len(reps)}, "
                        f"{tried} combos tried)")
                    try:
                        probe.unlink(missing_ok=True)
                    except Exception:
                        pass
                    cmd = [str(ex), "a", f"-m{level}", f"-md{md}",
                           "-s" if solid else "-s-", "-ds", f"-mt{n}",
                           "-o+", "-ep", "-idcd", str(probe), *src_files]
                    subprocess.run(cmd, capture_output=True)
                    ok = True
                    for name, (tlen, tsha) in targets.items():
                        try:
                            with RarStream(str(probe), packed_file_name=name,
                                           compressed=True) as rs:
                                d = rs.read()
                        except Exception:
                            ok = False; break
                        if len(d) != tlen or hashlib.sha1(d).digest() != tsha:
                            ok = False; break
                    if ok:
                        L(f"  ✓ RECIPE FOUND: {ver}  -mt{n}  "
                          f"(-m{level} -md{md} {'-s' if solid else '-s-'}) — "
                          f"reproduces all {len(targets)} stream(s) byte-exact.",
                          "ok")
                        return {"ok": True, "version": ver, "mt": n,
                                "level": level, "dict_kb": dkb, "solid": solid,
                                "tried": tried}
            L(f"  ✗ No pack build × -mt (0–{max_mt}) reproduces this archive "
              f"({tried} combos tried across {len(reps)} families). Genuine wall "
              "— the exact build/setting is outside the pack (a .srr2 case).",
              "err")
            return {"ok": False, "wall": True, "tried": tried}
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _exe_label(fname: str) -> str:
        """'2012-03-15_rar411.exe' → '2012-03-15 4.11' (matches srrdb_tool)."""
        m = re.match(r"(\d{4}-\d{2}-\d{2})_rar(\d)(\d\d)(b\d)?", fname)
        if not m:
            return fname
        d, maj, mnr, beta = m.groups()
        return f"{d} {maj}.{mnr}" + (f" {beta}" if beta else "")


def main():
    api = MiscToolsAPI()
    window = webview.create_window(
        title='Miscellaneous Tools — ToSort Toolkit',
        url=str(Path(__file__).parent / 'gui' / 'misc_tools.html'),
        js_api=api,
        width=820,
        height=820,
        min_size=(600, 500),
        background_color='#0d0f12',
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == '__main__':
    main()
