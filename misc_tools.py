"""
Miscellaneous Utilities Backend
- Move to Folder: moves each file in a folder into a subfolder named after it
- Extras Collector: copies scene extras (.nfo/.diz/.jpg etc.) into named destination folders
"""

import os
import re
import json
import shutil
import threading
import subprocess
from pathlib import Path

import webview


EXTRA_EXTS   = {'.nfo', '.diz', '.jpg', '.jpeg', '.png', '.sfv', '.nzb'}
EXTRA_DIRS   = {'proof', 'sample'}


class MiscToolsAPI:
    def __init__(self):
        self._window    = None
        self._stop_flag = threading.Event()
        self._running   = False

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j
            payload = _j.dumps(data, ensure_ascii=True).replace("'", "\\'")
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
