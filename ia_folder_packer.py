"""
IA Folder Packer
Packs each leaf folder containing archives into a single RAR or ZIP.
Intelligently finds the deepest folder containing archives and packs
the whole folder as one archive, preserving the relative structure.

Example:
  TOSEC/Commodore/C64/Games/[D64]/archives  ->  [D64].rar
  TOSEC/Commodore/[D64]/archives            ->  [D64].rar
"""

import os
import re
import json
import shutil
import threading
import subprocess
from pathlib import Path

import webview

ARCHIVE_EXTS = {
    '.zip', '.rar', '.7z', '.gz', '.tar', '.tgz', '.tar.gz',
    '.lzh', '.lha', '.arj', '.ace', '.z', '.bz2',
    '.d64', '.t64', '.g64', '.d71', '.d81', '.tap', '.prg',
    '.nib', '.dfi', '.dmp', '.lbr', '.sda', '.sfx', '.lnx',
    '.crt', '.bin', '.ark', '.arc', '.nbz', '.p00',
}


def find_leaf_folders(root: Path) -> list:
    """Find all folders that contain archive files directly."""
    leaves = []
    for folder in sorted(root.rglob('*')):
        if not folder.is_dir():
            continue
        has_archives = any(
            f.suffix.lower() in ARCHIVE_EXTS or
            ''.join(f.suffixes[-2:]).lower() in ARCHIVE_EXTS
            for f in folder.iterdir() if f.is_file()
        )
        if has_archives:
            leaves.append(folder)
    return leaves


class IAFolderPackerAPI:
    def __init__(self):
        self._window = None
        self._running = False
        self._stop_flag = threading.Event()

    def set_window(self, w):
        self._window = w

    def _emit(self, event: str, data: dict):
        if not self._window:
            return
        try:
            import json as _j, base64 as _b64
            b64 = _b64.b64encode(
                _j.dumps(data, ensure_ascii=False).encode('utf-8')
            ).decode('ascii')
            js = "(function(){var d=JSON.parse(atob('"+b64+"'));window.fpEvent('"+event+"',d);})()"
            self._window.evaluate_js(js)
        except Exception:
            pass

    def _log(self, msg: str, cls: str = 'info'):
        self._emit('log', {'msg': msg, 'cls': cls})

    def _progress(self, pct: int, label: str = ''):
        self._emit('progress', {'pct': pct, 'label': label})

    def save_config(self, cfg: dict) -> bool:
        try:
            p = Path(__file__).parent / 'ia_folder_packer.json'
            with open(p, 'w') as f:
                json.dump(cfg, f, indent=2)
            return True
        except Exception:
            return False

    def load_config(self) -> dict:
        try:
            p = Path(__file__).parent / 'ia_folder_packer.json'
            if p.exists():
                with open(p) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def browse_folder(self) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askdirectory()
            root.destroy()
            return path if path else ''
        except Exception:
            return ''

    def start_pack(self, config: dict) -> bool:
        if self._running:
            self._log('Already running.', 'warn')
            return False
        self._stop_flag.clear()
        t = threading.Thread(target=self._run, args=(config,), daemon=True)
        t.start()
        return True

    def stop_pack(self):
        self._stop_flag.set()
        self._log('Stopping...', 'warn')

    def _run(self, config: dict):
        self._running = True
        self._emit('status', {'state': 'running'})

        src_dir  = Path(config.get('src', '')).expanduser()
        dst_dir  = Path(config.get('dst', '')).expanduser() if config.get('dst') else None
        mode     = config.get('mode', 'copy')   # copy or move
        fmt      = config.get('format', 'zip').lower()

        if not src_dir.is_dir():
            self._log(f'ERROR: Source not found: {src_dir}', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        if mode == 'copy' and not dst_dir:
            self._log('ERROR: Copy mode requires a destination.', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        _rar = self._find_tool(['rar.exe', 'rar', 'WinRAR.exe'])
        _7z  = self._find_tool(['7z.exe', '7za.exe', '7z', '7za'])
        if not _7z:
            _7z = self._find_tool(['7zr.exe', '7zr'])

        if not _rar and not _7z:
            self._log('ERROR: No archiver found. Drop rar.exe or 7z.exe in apps/ folder.', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        self._log(f'Scanning: {src_dir}', 'info')
        leaves = find_leaf_folders(src_dir)
        self._log(f'Found {len(leaves)} folder(s) to pack', 'dim')

        if not leaves:
            self._log('No archive folders found.', 'warn')
            self._running = False
            self._emit('status', {'state': 'done'})
            return

        total = len(leaves)
        n_ok = 0
        n_err = 0

        for idx, folder in enumerate(leaves):
            if self._stop_flag.is_set():
                break

            pct = int((idx / total) * 100)
            self._progress(pct, folder.name)

            try:
                # Relative path from source root
                try:
                    rel = folder.relative_to(src_dir)
                except ValueError:
                    rel = Path(folder.name)

                # Output archive name = folder name
                arc_name = f'{folder.name}.{fmt}'

                # Destination: mirror structure under dst, or alongside folder
                if mode == 'copy' and dst_dir:
                    out_dir = dst_dir / src_dir.name / rel.parent
                    out_dir.mkdir(parents=True, exist_ok=True)
                else:
                    out_dir = folder.parent

                arc_path = out_dir / arc_name

                # Count files
                files = [f for f in folder.iterdir() if f.is_file()
                         and f.suffix.lower() in ARCHIVE_EXTS]
                total_size = sum(f.stat().st_size for f in files)
                self._log(
                    f'\nPacking: {folder.name}  '
                    f'({len(files)} files, {self._fmt_size(total_size)})', 'info'
                )

                ok = self._pack_folder(folder, arc_path, fmt, _rar, _7z)

                if ok and arc_path.exists():
                    n_ok += 1
                    self._log(
                        f'  ✓ {arc_name}  ({self._fmt_size(arc_path.stat().st_size)})', 'ok'
                    )
                    # Move mode: delete source folder after packing
                    if mode != 'copy':
                        try:
                            shutil.rmtree(folder)
                        except Exception as e:
                            self._log(f'  Warning: could not remove source: {e}', 'warn')
                else:
                    n_err += 1
                    self._log(f'  ✗ Failed: {arc_name}', 'err')

            except Exception as e:
                n_err += 1
                self._log(f'  ERROR: {folder.name}: {e!r}', 'err')

        self._log(
            f'\nDone — {n_ok} packed, {n_err} error(s)',
            'ok' if not n_err else 'warn'
        )
        self._progress(100, 'Complete')
        self._running = False
        self._emit('status', {'state': 'done', 'ok': n_ok, 'errors': n_err})

    def _pack_folder(self, folder: Path, arc_path: Path,
                     fmt: str, rar_bin: str, z_bin: str) -> bool:
        """Pack an entire folder into one archive."""
        import subprocess

        # Write list file of all files in the folder
        files = sorted([
            f for f in folder.iterdir() if f.is_file()
        ], key=lambda x: x.name.lower())

        if not files:
            return False

        file_names = [f.name for f in files]

        # Write list file locally (not on network share) to avoid permission errors
        # rar.exe needs full path when list file is not in cwd
        import tempfile
        tmp_dir = Path(tempfile.gettempdir())
        list_file = tmp_dir / f'_fp_{folder.name[:40]}.lst'
        list_file.write_bytes(
            b'\xff\xfe' + '\n'.join(file_names).encode('utf-16-le')
        )

        try:
            if fmt == 'rar' and rar_bin:
                cmd = [rar_bin, 'a', '-ep', '-m0', str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(folder), stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
                if result.returncode != 0 or not arc_path.exists():
                    err = (result.stderr or result.stdout or b'')[:200].decode('utf-8', 'replace')
                    self._log(f'  rar error rc={result.returncode}: {err}', 'err')
                    return False
                return True

            elif z_bin:
                z_name = Path(z_bin).name.lower()
                actual_fmt = fmt
                if fmt == 'rar' and '7zr' in z_name:
                    self._log('  7zr cannot create RAR — using ZIP', 'warn')
                    actual_fmt = 'zip'
                    arc_path = arc_path.with_suffix('.zip')
                if actual_fmt == 'rar':
                    cmd = [z_bin, 'a', '-trar', '-mx0', str(arc_path), f'@{list_file}']
                else:
                    cmd = [z_bin, 'a', '-tzip', '-mx0', str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(folder), stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == 'nt' else 0
                )
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or b'')[:200].decode('utf-8', 'replace')
                    self._log(f'  7z error: {err}', 'err')
                    return False
                return arc_path.exists()

            else:
                import zipfile as _zf
                zip_path = arc_path.with_suffix('.zip')
                with _zf.ZipFile(zip_path, 'w', _zf.ZIP_STORED) as zf:
                    for fn in file_names:
                        fp = folder / fn
                        if fp.exists():
                            zf.write(fp, fn)
                return zip_path.exists()

        finally:
            try:
                list_file.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if n < 1024:
                return f'{n:.1f} {unit}'
            n /= 1024
        return f'{n:.1f} PB'

    @staticmethod
    def _find_tool(candidates: list) -> str:
        import shutil as _sh
        app_dir = Path(__file__).parent
        search_dirs = [
            app_dir,
            app_dir / 'apps',
            app_dir / 'rclone',
        ]
        for name in candidates:
            for d in search_dirs:
                local = d / name
                if local.exists():
                    return str(local)
            found = _sh.which(name)
            if found:
                return found
        return ''


def main():
    api = IAFolderPackerAPI()
    window = webview.create_window(
        title='IA Folder Packer',
        url=str(Path(__file__).parent / 'gui' / 'ia_folder_packer.html'),
        js_api=api,
        width=800,
        height=800,
        min_size=(600, 500),
        background_color='#0d0f12',
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == '__main__':
    main()
