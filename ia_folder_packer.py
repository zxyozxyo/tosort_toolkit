"""
IA Folder Packer
One tool, three selectable packing strategies for preparing folders of
archives/loose files for Internet Archive upload:

  LEAF       — finds the deepest folder(s) that directly contain archive
               files and packs each whole folder as one archive, named
               after the folder. Use when a leaf folder's contents are
               already individual archives that just need bundling.
               Example: TOSEC/C64/Games/[D64]/*.zip -> [D64].zip

  LETTER     — finds folders containing loose archives, groups them by
               first-letter (A, B, C, ... 0-9, MISC), and splits each
               letter's files into batches not exceeding a size limit,
               naming batches LETTER.ext, LETTER_2.ext, LETTER_3.ext etc.
               This is the original IA Prepper behaviour.

  DEPTH      — looks a fixed number of folder levels down from the
               source root, treats each folder found at exactly that
               depth as one packing unit (regardless of whether it
               directly contains archives — everything under it gets
               included), and splits its contents into size-limited
               batches the same way LETTER mode does, naming output
               FOLDERNAME.ext, FOLDERNAME_2.ext, etc.

All three modes share the same source/destination/format/file_mode
(copy or move) options, the same archiver discovery (rar.exe / 7z.exe /
Python zipfile fallback), and the same logging/progress/stop-flag
machinery.
"""

import os
import json
import shutil
import threading
import tempfile
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


def find_archive_folders(root: Path) -> list:
    """Recursively find all leaf-level folders containing archives (used by LETTER mode)."""
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        archives = [f for f in filenames if Path(f).suffix.lower() in ARCHIVE_EXTS]
        if archives:
            result.append(dp)
    return result


def find_folders_at_depth(root: Path, depth: int) -> list:
    """
    Find every folder sitting at exactly `depth` levels below root
    (depth=1 means root's direct subfolders, depth=2 means their
    subfolders, etc.). A folder only counts if it actually contains at
    least one file somewhere under it — empty branches are skipped.
    """
    if depth < 1:
        return [root] if any(root.rglob('*')) else []

    current_level = [root]
    for _ in range(depth):
        next_level = []
        for folder in current_level:
            try:
                next_level.extend(sorted(p for p in folder.iterdir() if p.is_dir()))
            except Exception:
                continue
        current_level = next_level
        if not current_level:
            break

    return [f for f in current_level if any(p.is_file() for p in f.rglob('*'))]


def get_letter_group(filename: str) -> str:
    """Return letter group for a filename (used by LETTER mode)."""
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

    def preview_pack(self, config: dict, limit: int = 10) -> dict:
        """
        Shows what a real run WOULD produce — discovers the same
        folders/batches the real run method would, computes the same
        output archive names and file counts, but never touches the
        archiver, never copies/moves/deletes anything, and never
        requires rar.exe/7z.exe to be present. Synchronous (fast enough
        not to need threading/progress events) — returns a dict with
        the results directly rather than emitting log events, so the
        GUI can render it however it wants (e.g. inline in the log
        panel, or its own preview area).

        Returns: {
          'mode': str, 'total_units': int, 'shown': int,
          'items': [ { 'unit': str (folder name/letter/etc.),
                       'archives': [ {'name': str, 'files': int,
                                      'size': int} ] } ],
          'error': str or None
        }
        """
        mode = config.get('mode', 'leaf')
        src_dir = Path(config.get('src', '')).expanduser()
        fmt = config.get('format', 'zip').lower()

        if not src_dir.is_dir():
            return {'mode': mode, 'total_units': 0, 'shown': 0, 'items': [],
                    'error': f'Source not found: {src_dir}'}

        try:
            if mode == 'letter_split':
                return self._preview_letter_split(config, src_dir, fmt, limit)
            elif mode == 'depth':
                return self._preview_depth(config, src_dir, fmt, limit)
            else:
                return self._preview_leaf(config, src_dir, fmt, limit)
        except Exception as e:
            return {'mode': mode, 'total_units': 0, 'shown': 0, 'items': [],
                    'error': f'Preview failed: {e!r}'}

    def _preview_leaf(self, config, src_dir, fmt, limit):
        dst_dir = Path(config.get('dst', '')).expanduser() if config.get('dst') else None
        file_mode = config.get('file_mode', 'copy')
        skip_existing = bool(config.get('skip_existing', False))

        leaves = find_leaf_folders(src_dir)
        items = []
        for folder in leaves[:limit]:
            files = [f for f in folder.iterdir() if f.is_file()
                     and f.suffix.lower() in ARCHIVE_EXTS]
            total_size = sum(f.stat().st_size for f in files)
            arc_name = f'{folder.name}.{fmt}'

            try:
                rel = folder.relative_to(src_dir)
            except ValueError:
                rel = Path(folder.name)
            if file_mode == 'copy' and dst_dir:
                out_dir = dst_dir / src_dir.name / rel.parent
            else:
                out_dir = folder.parent
            arc_path = out_dir / arc_name
            would_skip = skip_existing and arc_path.exists()

            items.append({
                'unit': str(folder.relative_to(src_dir)) if folder != src_dir else folder.name,
                'archives': [{'name': arc_name, 'files': len(files), 'size': total_size,
                              'skip': would_skip}],
            })
        return {'mode': 'leaf', 'total_units': len(leaves), 'shown': len(items),
                'items': items, 'error': None}

    def _preview_letter_split(self, config, src_dir, fmt, limit):
        size_limit = int(config.get('size_limit', 2 * 1024 ** 3))
        dst_dir = Path(config.get('dst', '')).expanduser() if config.get('dst') else None
        file_mode = config.get('file_mode', 'copy')
        skip_existing = bool(config.get('skip_existing', False))

        archive_folders = find_archive_folders(src_dir)
        items = []
        shown_count = 0
        for folder in archive_folders:
            if shown_count >= limit:
                break
            archives = sorted([
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS
            ], key=lambda x: x.name.lower())
            if not archives:
                continue
            groups: dict = {}
            for arc in archives:
                letter = get_letter_group(arc.name)
                groups.setdefault(letter, []).append(arc)

            try:
                rel = folder.relative_to(src_dir)
            except ValueError:
                rel = Path(folder.name)
            work_dir = (dst_dir / src_dir.name / rel) if file_mode == 'copy' and dst_dir else folder

            folder_archives = []
            for letter in sorted(groups.keys()):
                batches = self._split_by_size(groups[letter], size_limit)
                for batch_idx, batch in enumerate(batches):
                    suffix = f'_{batch_idx + 1}' if batch_idx > 0 else ''
                    arc_name = f'{letter}{suffix}.{fmt}'
                    batch_size = sum(f.stat().st_size for f in batch)
                    would_skip = skip_existing and (work_dir / arc_name).exists()
                    folder_archives.append({
                        'name': arc_name, 'files': len(batch), 'size': batch_size,
                        'skip': would_skip,
                    })
            items.append({
                'unit': str(folder.relative_to(src_dir)) if folder != src_dir else folder.name,
                'archives': folder_archives,
            })
            shown_count += 1
        return {'mode': 'letter_split', 'total_units': len(archive_folders), 'shown': len(items),
                'items': items, 'error': None}

    def _preview_depth(self, config, src_dir, fmt, limit):
        depth = max(1, int(config.get('depth', 2)))
        size_limit = int(config.get('size_limit', 2 * 1024 ** 3))
        dst_dir = Path(config.get('dst', '')).expanduser() if config.get('dst') else None
        file_mode = config.get('file_mode', 'copy')
        skip_existing = bool(config.get('skip_existing', False))

        targets = find_folders_at_depth(src_dir, depth)
        items = []
        for folder in targets[:limit]:
            all_files = [f for f in folder.rglob('*') if f.is_file()]
            if not all_files:
                items.append({
                    'unit': str(folder.relative_to(src_dir)),
                    'archives': [],
                })
                continue

            try:
                rel = folder.relative_to(src_dir)
            except ValueError:
                rel = Path(folder.name)
            work_dir = (dst_dir / src_dir.name / rel.parent) if file_mode == 'copy' and dst_dir else folder.parent

            batches = self._split_by_size(
                sorted(all_files, key=lambda x: str(x.relative_to(folder)).lower()), size_limit
            )
            folder_archives = []
            for batch_idx, batch in enumerate(batches):
                suffix = f'_{batch_idx + 1}' if batch_idx > 0 else ''
                arc_name = f'{folder.name}{suffix}.{fmt}'
                batch_size = sum(f.stat().st_size for f in batch)
                would_skip = skip_existing and (work_dir / arc_name).exists()
                folder_archives.append({
                    'name': arc_name, 'files': len(batch), 'size': batch_size,
                    'skip': would_skip,
                })
            items.append({
                'unit': str(folder.relative_to(src_dir)),
                'archives': folder_archives,
            })
        return {'mode': 'depth', 'total_units': len(targets), 'shown': len(items),
                'items': items, 'error': None}

    # ── Dispatch ──────────────────────────────────────────────────────
    def _run(self, config: dict):
        self._running = True
        self._emit('status', {'state': 'running'})

        mode = config.get('mode', 'leaf')  # 'leaf' | 'letter_split' | 'depth'
        src_dir = Path(config.get('src', '')).expanduser()
        dst_dir = Path(config.get('dst', '')).expanduser() if config.get('dst') else None
        file_mode = config.get('file_mode', 'copy')   # copy or move
        fmt = config.get('format', 'zip').lower()

        if not src_dir.is_dir():
            self._log(f'ERROR: Source not found: {src_dir}', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        if file_mode == 'copy' and not dst_dir:
            self._log('ERROR: Copy mode requires a destination.', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        _rar = self._find_tool(['rar.exe', 'rar', 'WinRAR.exe'])
        _7z = self._find_tool(['7z.exe', '7za.exe', '7z', '7za'])
        if not _7z:
            _7z = self._find_tool(['7zr.exe', '7zr'])

        if not _rar and not _7z:
            self._log('ERROR: No archiver found. Drop rar.exe or 7z.exe in apps/ folder.', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        try:
            if mode == 'letter_split':
                self._run_letter_split(config, src_dir, dst_dir, file_mode, fmt, _rar, _7z)
            elif mode == 'depth':
                self._run_depth(config, src_dir, dst_dir, file_mode, fmt, _rar, _7z)
            else:
                self._run_leaf(config, src_dir, dst_dir, file_mode, fmt, _rar, _7z)
        except Exception as e:
            self._log(f'ERROR: unexpected failure: {e!r}', 'err')
            self._running = False
            self._emit('status', {'state': 'error'})
            return

        self._running = False

    # ── LEAF mode ─────────────────────────────────────────────────────
    def _run_leaf(self, config, src_dir, dst_dir, file_mode, fmt, rar_bin, z_bin):
        """Pack each leaf folder (one that directly contains archives) as one archive."""
        skip_existing = bool(config.get('skip_existing', False))
        self._log(f'Mode: LEAF — Scanning: {src_dir}', 'info')
        leaves = find_leaf_folders(src_dir)
        self._log(f'Found {len(leaves)} folder(s) to pack', 'dim')

        if not leaves:
            self._log('No archive folders found.', 'warn')
            self._emit('status', {'state': 'done'})
            return

        total = len(leaves)
        n_ok = 0
        n_err = 0
        n_skipped = 0

        for idx, folder in enumerate(leaves):
            if self._stop_flag.is_set():
                break

            pct = int((idx / total) * 100)
            self._progress(pct, folder.name)

            try:
                try:
                    rel = folder.relative_to(src_dir)
                except ValueError:
                    rel = Path(folder.name)

                arc_name = f'{folder.name}.{fmt}'

                if file_mode == 'copy' and dst_dir:
                    out_dir = dst_dir / src_dir.name / rel.parent
                    out_dir.mkdir(parents=True, exist_ok=True)
                else:
                    out_dir = folder.parent

                arc_path = out_dir / arc_name

                if skip_existing and arc_path.exists():
                    n_skipped += 1
                    self._log(f'⏭ Skipping {folder.name} — {arc_name} already exists', 'dim')
                    continue

                files = [f for f in folder.iterdir() if f.is_file()
                         and f.suffix.lower() in ARCHIVE_EXTS]
                total_size = sum(f.stat().st_size for f in files)
                self._log(
                    f'\nPacking: {folder.name}  '
                    f'({len(files)} files, {self._fmt_size(total_size)})', 'info'
                )

                ok = self._pack_files(folder, [f.name for f in files], arc_path, fmt, rar_bin, z_bin)

                if ok and arc_path.exists():
                    n_ok += 1
                    self._log(f'  ✓ {arc_name}  ({self._fmt_size(arc_path.stat().st_size)})', 'ok')
                    if file_mode != 'copy':
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

        skip_suffix = f', {n_skipped} skipped (already packed)' if n_skipped else ''
        self._log(f'\nDone — {n_ok} packed, {n_err} error(s){skip_suffix}', 'ok' if not n_err else 'warn')
        self._progress(100, 'Complete')
        self._emit('status', {'state': 'done', 'ok': n_ok, 'errors': n_err, 'skipped': n_skipped})

    # ── LETTER mode (the original IA Prepper) ───────────────────────────
    def _run_letter_split(self, config, src_dir, dst_dir, file_mode, fmt, rar_bin, z_bin):
        """Group loose archives by first letter and split into size-limited batches."""
        size_limit = int(config.get('size_limit', 2 * 1024 ** 3))
        skip_existing = bool(config.get('skip_existing', False))

        self._log(f'Mode: LETTER-SPLIT — Scanning: {src_dir}', 'info')
        archive_folders = find_archive_folders(src_dir)
        self._log(f'Found {len(archive_folders)} folder(s) containing archives', 'dim')

        if not archive_folders:
            self._log('No archive folders found.', 'warn')
            self._emit('status', {'state': 'done'})
            return

        total = len(archive_folders)
        n_done = 0
        n_created = 0
        n_errors = 0
        n_skipped = 0

        for folder in archive_folders:
            if self._stop_flag.is_set():
                break

            try:
                pct = int((n_done / total) * 100)
                self._progress(pct, f'Processing: {folder.name}/')
                self._log(f'\nFolder: {folder}', 'info')

                try:
                    rel = folder.relative_to(src_dir)
                except ValueError:
                    rel = Path(folder.name)

                if file_mode == 'copy':
                    work_dir = dst_dir / src_dir.name / rel
                    work_dir.mkdir(parents=True, exist_ok=True)
                else:
                    work_dir = folder

                archives = sorted([
                    f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS
                ], key=lambda x: x.name.lower())

                if not archives:
                    n_done += 1
                    continue

                self._log(f'  {len(archives)} archive(s) — grouping by letter...', 'dim')

                groups: dict = {}
                for arc in archives:
                    letter = get_letter_group(arc.name)
                    groups.setdefault(letter, []).append(arc)

                for letter in sorted(groups.keys()):
                    if self._stop_flag.is_set():
                        break

                    files = groups[letter]
                    batches = self._split_by_size(files, size_limit)
                    self._log(f'  letter={letter} files={len(files)} batches={len(batches)}', 'dim')

                    for batch_idx, batch in enumerate(batches):
                        if self._stop_flag.is_set():
                            break

                        suffix = f'_{batch_idx + 1}' if batch_idx > 0 else ''
                        arc_stem = f'{letter}{suffix}'
                        arc_name = f'{arc_stem}.{fmt}'
                        arc_path = work_dir / arc_name

                        if skip_existing and arc_path.exists():
                            n_skipped += 1
                            self._log(
                                f'  ⏭ Skipping {arc_name} — already exists '
                                f'(source files left untouched)', 'dim'
                            )
                            continue

                        batch_size = sum(f.stat().st_size for f in batch)
                        self._log(
                            f'  Creating {arc_name}  ({len(batch)} files, {self._fmt_size(batch_size)})',
                            'dim'
                        )

                        files_to_archive = []
                        if file_mode == 'copy':
                            for f in batch:
                                dst_f = work_dir / f.name
                                if not dst_f.exists():
                                    shutil.copy2(f, dst_f)
                                files_to_archive.append(dst_f)
                        else:
                            files_to_archive = batch

                        try:
                            ok = self._pack_files(
                                files_to_archive[0].parent,
                                [f.name for f in files_to_archive],
                                arc_path, fmt, rar_bin, z_bin
                            )
                            actual_arc = arc_path.with_suffix('.zip') \
                                if not arc_path.exists() and arc_path.with_suffix('.zip').exists() \
                                else arc_path
                            if ok and actual_arc.exists():
                                n_created += 1
                                self._log(f'  ✓ {actual_arc.name}  ({self._fmt_size(actual_arc.stat().st_size)})', 'ok')
                                for f in files_to_archive:
                                    try:
                                        if f.exists():
                                            f.unlink()
                                    except Exception:
                                        pass
                            else:
                                n_errors += 1
                                self._log(f'  ✗ Failed: {arc_name} — archiver returned no output', 'err')
                        except Exception as e:
                            n_errors += 1
                            self._log(f'  ✗ Error creating {arc_name}: {e!r}', 'err')

                n_done += 1

            except Exception as folder_err:
                self._log(f'  ERROR processing folder {folder.name}: {folder_err!r}', 'err')
                n_done += 1

        skip_suffix = f', {n_skipped} skipped (already packed)' if n_skipped else ''
        self._log(f'\nDone — {n_created} archive(s) created, {n_errors} error(s){skip_suffix}',
                   'ok' if not n_errors else 'warn')
        self._progress(100, 'Complete')
        self._emit('status', {'state': 'done', 'ok': n_created, 'errors': n_errors, 'skipped': n_skipped})

    # ── DEPTH mode (new) ─────────────────────────────────────────────
    def _run_depth(self, config, src_dir, dst_dir, file_mode, fmt, rar_bin, z_bin):
        """
        Look `depth` folder levels down from src_dir, treat each folder
        found at that exact depth as one packing unit (everything under
        it, recursively — not just direct children, and regardless of
        whether the contents are already archives or loose files), and
        split its contents into size-limited batches named after the
        folder (FOLDERNAME.ext, FOLDERNAME_2.ext, FOLDERNAME_3.ext...),
        the same size-splitting behaviour as LETTER mode but anchored to
        a folder-depth rule instead of a first-letter rule.
        """
        depth = max(1, int(config.get('depth', 2)))
        size_limit = int(config.get('size_limit', 2 * 1024 ** 3))
        skip_existing = bool(config.get('skip_existing', False))

        self._log(f'Mode: DEPTH (level {depth}) — Scanning: {src_dir}', 'info')
        targets = find_folders_at_depth(src_dir, depth)
        self._log(f'Found {len(targets)} folder(s) at depth {depth}', 'dim')

        if not targets:
            self._log(f'No non-empty folders found at depth {depth}.', 'warn')
            self._emit('status', {'state': 'done'})
            return

        total = len(targets)
        n_created = 0
        n_errors = 0
        n_skipped = 0

        for idx, folder in enumerate(targets):
            if self._stop_flag.is_set():
                break

            pct = int((idx / total) * 100)
            self._progress(pct, folder.name)
            self._log(f'\nFolder: {folder}', 'info')

            any_skipped_this_folder = False

            try:
                try:
                    rel = folder.relative_to(src_dir)
                except ValueError:
                    rel = Path(folder.name)

                if file_mode == 'copy':
                    work_dir = dst_dir / src_dir.name / rel.parent
                    work_dir.mkdir(parents=True, exist_ok=True)
                else:
                    work_dir = folder.parent

                # Everything under this folder, recursively — flattened
                # into one packing set. Files are gathered with their
                # path RELATIVE TO `folder` preserved as the archive
                # entry name, so nested structure inside the depth-level
                # folder survives inside the resulting archive(s).
                all_files = sorted(
                    [f for f in folder.rglob('*') if f.is_file()],
                    key=lambda x: str(x.relative_to(folder)).lower()
                )

                if not all_files:
                    continue

                self._log(f'  {len(all_files)} file(s) under {folder.name}/ — splitting by size...', 'dim')
                batches = self._split_by_size(all_files, size_limit)
                self._log(f'  {len(batches)} batch(es)', 'dim')

                for batch_idx, batch in enumerate(batches):
                    if self._stop_flag.is_set():
                        break

                    suffix = f'_{batch_idx + 1}' if batch_idx > 0 else ''
                    arc_name = f'{folder.name}{suffix}.{fmt}'
                    arc_path = work_dir / arc_name

                    if skip_existing and arc_path.exists():
                        n_skipped += 1
                        any_skipped_this_folder = True
                        self._log(f'  ⏭ Skipping {arc_name} — already exists', 'dim')
                        continue

                    batch_size = sum(f.stat().st_size for f in batch)
                    self._log(
                        f'  Creating {arc_name}  ({len(batch)} files, {self._fmt_size(batch_size)})',
                        'dim'
                    )

                    # Archive entry names need to preserve the relative
                    # path under `folder`, not just the bare filename —
                    # otherwise files with the same name in different
                    # nested subfolders would collide inside the archive
                    entry_names = [str(f.relative_to(folder)) for f in batch]

                    try:
                        ok = self._pack_files_with_entries(
                            batch, entry_names, arc_path, fmt, rar_bin, z_bin
                        )
                        actual_arc = arc_path.with_suffix('.zip') \
                            if not arc_path.exists() and arc_path.with_suffix('.zip').exists() \
                            else arc_path
                        if ok and actual_arc.exists():
                            n_created += 1
                            self._log(f'  ✓ {actual_arc.name}  ({self._fmt_size(actual_arc.stat().st_size)})', 'ok')
                        else:
                            n_errors += 1
                            self._log(f'  ✗ Failed: {arc_name} — archiver returned no output', 'err')
                    except Exception as e:
                        n_errors += 1
                        self._log(f'  ✗ Error creating {arc_name}: {e!r}', 'err')

                # Move mode: remove the source folder once every batch
                # for it has been packed successfully. If ANY batch for
                # this folder was skipped (already existed), the source
                # folder is intentionally NOT removed even on an
                # otherwise-clean pass — we can't be sure every file is
                # safely represented in an archive we didn't just create
                # ourselves this run, so leaving the source in place is
                # the safe default. A clear log line explains why.
                if file_mode != 'copy' and not self._stop_flag.is_set():
                    if any_skipped_this_folder:
                        self._log(
                            f'  Source folder kept (some batches were skipped this run) — '
                            f'verify manually before deleting {folder}', 'warn'
                        )
                    else:
                        try:
                            shutil.rmtree(folder)
                        except Exception as e:
                            self._log(f'  Warning: could not remove source: {e}', 'warn')

            except Exception as e:
                n_errors += 1
                self._log(f'  ERROR: {folder.name}: {e!r}', 'err')

        skip_suffix = f', {n_skipped} skipped (already packed)' if n_skipped else ''
        self._log(f'\nDone — {n_created} archive(s) created, {n_errors} error(s){skip_suffix}',
                   'ok' if not n_errors else 'warn')
        self._progress(100, 'Complete')
        self._emit('status', {'state': 'done', 'ok': n_created, 'errors': n_errors, 'skipped': n_skipped})

    # ── Shared archiving helpers ──────────────────────────────────────
    def _pack_files(self, work_dir: Path, file_names: list, arc_path: Path,
                     fmt: str, rar_bin: str, z_bin: str) -> bool:
        """Pack a flat list of files (all directly inside work_dir, bare
        filenames as archive entry names) into one archive."""
        return self._pack_files_with_entries(
            [work_dir / fn for fn in file_names], file_names, arc_path, fmt, rar_bin, z_bin
        )

    def _pack_files_with_entries(self, file_paths: list, entry_names: list,
                                   arc_path: Path, fmt: str, rar_bin: str, z_bin: str) -> bool:
        """
        Pack `file_paths` into one archive, using `entry_names` (same
        length, same order) as each file's name/path INSIDE the
        archive — lets DEPTH mode preserve nested relative paths while
        LEAF/LETTER modes just use bare filenames. Uses a response/list
        file for both rar.exe and 7z.exe to avoid Windows command-line
        length limits with large batches.
        """
        if not file_paths:
            return False

        # If every entry name already equals its own bare filename
        # (LEAF/LETTER mode, the common case), pack directly from each
        # file's existing parent folder. Otherwise (DEPTH mode with
        # real subfolder nesting under the depth-level folder), stage a
        # temp mirror of the relative structure so the archiver
        # naturally captures the right nested entry names.
        needs_staging = any(en != fp.name for fp, en in zip(file_paths, entry_names))

        if not needs_staging:
            work_dir = file_paths[0].parent
            return self._pack_flat(work_dir, entry_names, arc_path, fmt, rar_bin, z_bin)

        with tempfile.TemporaryDirectory(prefix='_fp_stage_') as stage_root_str:
            stage_root = Path(stage_root_str)
            staged_entries = []
            for fp, en in zip(file_paths, entry_names):
                dest = stage_root / en
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(fp, dest)  # hardlink — fast, no extra disk space, same volume only
                except Exception:
                    shutil.copy2(fp, dest)  # fallback across volumes
                staged_entries.append(en)
            return self._pack_flat(stage_root, staged_entries, arc_path, fmt, rar_bin, z_bin, recursive=True)

    def _pack_flat(self, work_dir: Path, entry_names: list, arc_path: Path,
                    fmt: str, rar_bin: str, z_bin: str, recursive: bool = False) -> bool:
        """Invoke the archiver from work_dir, packing the given relative entry_names."""
        if not entry_names:
            return False

        list_file = Path(tempfile.gettempdir()) / f'_fp_{arc_path.stem[:40]}.lst'
        list_file.write_bytes(
            b'\xff\xfe' + '\n'.join(entry_names).encode('utf-16-le')
        )

        try:
            if fmt == 'rar' and rar_bin:
                cmd = [rar_bin, 'a', '-ep', '-m0'] + (['-r'] if recursive else []) + [str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(work_dir), stdin=subprocess.DEVNULL,
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
                tflag = '-trar' if actual_fmt == 'rar' else '-tzip'
                cmd = [z_bin, 'a', tflag, '-mx0'] + (['-r'] if recursive else []) + [str(arc_path), f'@{list_file}']
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(work_dir), stdin=subprocess.DEVNULL,
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
                    for en in entry_names:
                        fp = work_dir / en
                        if fp.exists():
                            zf.write(fp, en)
                return zip_path.exists()

        finally:
            try:
                list_file.unlink(missing_ok=True)
            except Exception:
                pass

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
            app_dir / 'tools',
            app_dir / 'bin',
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
