"""
ToSort Toolkit - DAT Merger & Deduper Backend
Parses CLRMamePro (.dat) and Logiqx XML (.xml/.dat) format DAT files.
Merges multiple DATs, deduplicates by ROM hash, outputs RomVault-compatible
CLRMamePro format .dat files.
"""

import os
import re
import xml.etree.ElementTree as ET
import json
import threading
import time
from pathlib import Path
import webview


class DatEntry:
    """Represents a single game/set entry with its ROMs."""
    __slots__ = ('name', 'description', 'roms', 'source_dat')

    def __init__(self, name: str, description: str = "", roms: list = None,
                 source_dat: str = ""):
        self.name = name
        self.description = description or name
        self.roms = roms or []
        self.source_dat = source_dat

    def hash_key(self) -> str:
        """
        Unique key based on sorted ROM hashes.
        Uses SHA1 if available, falls back to CRC+size.
        This determines what counts as a 'duplicate'.
        """
        parts = []
        for rom in sorted(self.roms, key=lambda r: r.get('name', '')):
            if rom.get('sha1'):
                parts.append(rom['sha1'].lower())
            elif rom.get('crc') and rom.get('size'):
                parts.append(f"{rom['crc'].lower()}_{rom['size']}")
            elif rom.get('md5'):
                parts.append(rom['md5'].lower())
            else:
                parts.append(rom.get('name', 'unknown'))
        return "|".join(parts)


class DatHeader:
    """DAT file header information."""
    def __init__(self):
        self.name = ""
        self.description = ""
        self.version = ""
        self.author = ""
        self.homepage = ""
        self.url = ""
        self.category = ""
        self.date = ""


# ═══════════════════════════════════════════════════════════════════════
# PARSERS
# ═══════════════════════════════════════════════════════════════════════

def detect_format(filepath: Path) -> str:
    """Detect whether a file is Logiqx XML or CLRMamePro format."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            head = f.read(2000)
        if head.strip().startswith('<?xml') or head.strip().startswith('<datafile') \
                or '<datafile' in head[:1000]:
            return 'xml'
        if 'clrmamepro' in head.lower() or re.search(r'^game\s*\(', head, re.MULTILINE):
            return 'clrmamepro'
        # Try XML parse as fallback
        if '<' in head[:200]:
            return 'xml'
        return 'clrmamepro'
    except Exception:
        return 'clrmamepro'


def parse_logiqx_xml(filepath: Path) -> tuple:
    """
    Parse a Logiqx XML format DAT file.
    Returns (DatHeader, list[DatEntry]).
    """
    header = DatHeader()
    entries = []
    source = filepath.name

    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError:
        # Try with error recovery - strip bad chars
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
        raw = re.sub(r'[^\x09\x0A\x0D\x20-\x7F\x80-\uFFFF]', '', raw)
        root = ET.fromstring(raw)

    # Parse header
    hdr = root.find('header')
    if hdr is not None:
        header.name = (hdr.findtext('name') or '').strip()
        header.description = (hdr.findtext('description') or '').strip()
        header.version = (hdr.findtext('version') or '').strip()
        header.author = (hdr.findtext('author') or '').strip()
        header.homepage = (hdr.findtext('homepage') or '').strip()
        header.url = (hdr.findtext('url') or '').strip()
        header.category = (hdr.findtext('category') or '').strip()
        header.date = (hdr.findtext('date') or '').strip()

    # Parse games/machines
    for game in list(root.iter('game')) + list(root.iter('machine')):
        name = game.get('name', '').strip()
        desc = (game.findtext('description') or name).strip()
        roms = []
        for rom in game.iter('rom'):
            rom_entry = {
                'name': rom.get('name', ''),
                'size': rom.get('size', ''),
                'crc':  rom.get('crc', ''),
                'md5':  rom.get('md5', ''),
                'sha1': rom.get('sha1', ''),
            }
            roms.append(rom_entry)
        if name:
            entries.append(DatEntry(name, desc, roms, source))

    return header, entries


def parse_clrmamepro(filepath: Path) -> tuple:
    """
    Parse a CLRMamePro format DAT file.
    Returns (DatHeader, list[DatEntry]).
    """
    header = DatHeader()
    entries = []
    source = filepath.name

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Parse header block
    hdr_match = re.search(r'clrmamepro\s*\((.*?)\)', content, re.DOTALL | re.IGNORECASE)
    if hdr_match:
        hdr_block = hdr_match.group(1)
        header.name = _extract_field(hdr_block, 'name')
        header.description = _extract_field(hdr_block, 'description')
        header.version = _extract_field(hdr_block, 'version')
        header.author = _extract_field(hdr_block, 'author')
        header.homepage = _extract_field(hdr_block, 'homepage')
        header.url = _extract_field(hdr_block, 'url')
        header.category = _extract_field(hdr_block, 'category')
        header.date = _extract_field(hdr_block, 'date')

    # Parse game blocks — handle nested parentheses properly
    pos = 0
    while True:
        # Find next 'game (' or 'resource ('
        match = re.search(r'(?:game|resource)\s*\(', content[pos:], re.IGNORECASE)
        if not match:
            break
        block_start = pos + match.end()
        # Find matching closing paren (handle nested rom( ) blocks)
        depth = 1
        i = block_start
        while i < len(content) and depth > 0:
            if content[i] == '(':
                depth += 1
            elif content[i] == ')':
                depth -= 1
            i += 1
        block = content[block_start:i - 1]
        pos = i

        name = _extract_field(block, 'name')
        desc = _extract_field(block, 'description') or name

        roms = []
        for rom_match in re.finditer(r'rom\s*\((.*?)\)', block, re.DOTALL):
            rom_block = rom_match.group(1)
            rom_entry = {
                'name': _extract_field(rom_block, 'name'),
                'size': _extract_field(rom_block, 'size'),
                'crc':  _extract_field(rom_block, 'crc'),
                'md5':  _extract_field(rom_block, 'md5'),
                'sha1': _extract_field(rom_block, 'sha1'),
            }
            roms.append(rom_entry)

        if name:
            entries.append(DatEntry(name, desc, roms, source))

    return header, entries


def _extract_field(block: str, field: str) -> str:
    """Extract a field value from a CLRMamePro block."""
    # Try quoted value first
    m = re.search(rf'{field}\s+"([^"]*)"', block, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try unquoted value
    m = re.search(rf'{field}\s+(\S+)', block, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def parse_dat_file(filepath: Path) -> tuple:
    """Auto-detect format and parse a DAT file. Returns (DatHeader, list[DatEntry])."""
    fmt = detect_format(filepath)
    if fmt == 'xml':
        return parse_logiqx_xml(filepath)
    else:
        return parse_clrmamepro(filepath)


# ═══════════════════════════════════════════════════════════════════════
# MERGER / DEDUPER
# ═══════════════════════════════════════════════════════════════════════

def merge_and_dedupe(all_entries: list, dedupe: bool = True) -> tuple:
    """
    Merge entries from multiple DATs and optionally deduplicate.
    Deduplication is by ROM hash — entries with identical ROM hashes
    (SHA1 preferred, fallback CRC+size) are considered duplicates.
    First occurrence wins.

    Returns (merged_entries, stats_dict).
    """
    if not dedupe:
        return all_entries, {
            "total_input": len(all_entries),
            "duplicates_removed": 0,
            "total_output": len(all_entries),
        }

    seen_hashes = {}
    merged = []
    dupes = 0

    for entry in all_entries:
        key = entry.hash_key()
        if key and key in seen_hashes:
            dupes += 1
        else:
            seen_hashes[key] = entry.name
            merged.append(entry)

    return merged, {
        "total_input": len(all_entries),
        "duplicates_removed": dupes,
        "total_output": len(merged),
    }


# ═══════════════════════════════════════════════════════════════════════
# OUTPUT - CLRMamePro DAT (RomVault compatible)
# ═══════════════════════════════════════════════════════════════════════

def _escape_dat_string(s: str) -> str:
    """Escape a string for CLRMamePro format."""
    return s.replace('"', "'")


def write_clrmamepro_dat(filepath: Path, header: DatHeader, entries: list):
    """Write a RomVault-compatible CLRMamePro format DAT file."""
    lines = []

    # Header
    lines.append('clrmamepro (')
    lines.append(f'\tname "{_escape_dat_string(header.name)}"')
    lines.append(f'\tdescription "{_escape_dat_string(header.description)}"')
    if header.version:
        lines.append(f'\tversion "{_escape_dat_string(header.version)}"')
    if header.author:
        lines.append(f'\tauthor "{_escape_dat_string(header.author)}"')
    if header.homepage:
        lines.append(f'\thomepage "{_escape_dat_string(header.homepage)}"')
    if header.url:
        lines.append(f'\turl "{_escape_dat_string(header.url)}"')
    lines.append(')')
    lines.append('')

    # Game entries
    for entry in sorted(entries, key=lambda e: e.name.lower()):
        lines.append('game (')
        lines.append(f'\tname "{_escape_dat_string(entry.name)}"')
        if entry.description and entry.description != entry.name:
            lines.append(f'\tdescription "{_escape_dat_string(entry.description)}"')
        for rom in sorted(entry.roms, key=lambda r: r.get('name', '')):
            parts = [f'name "{_escape_dat_string(rom.get("name", ""))}"']
            if rom.get('size'):
                parts.append(f'size {rom["size"]}')
            if rom.get('crc'):
                parts.append(f'crc {rom["crc"].upper()}')
            if rom.get('md5'):
                parts.append(f'md5 {rom["md5"].lower()}')
            if rom.get('sha1'):
                parts.append(f'sha1 {rom["sha1"].lower()}')
            lines.append(f'\trom ( {" ".join(parts)} )')
        lines.append(')')
        lines.append('')

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ═══════════════════════════════════════════════════════════════════════
# API CLASS - exposed to JS via PyWebView
# ═══════════════════════════════════════════════════════════════════════

class DatMergerAPI:

    def __init__(self):
        self._window = None
        self._running = False
        self._stop = threading.Event()

    def set_window(self, window):
        self._window = window

    def _emit(self, event: str, data: dict):
        if self._window:
            payload = json.dumps(data).replace("\\", "\\\\").replace("'", "\\'")
            self._window.evaluate_js(f"window.pyEvent('{event}', JSON.parse('{payload}'))")

    def _log(self, msg: str, cls: str = ""):
        self._emit("log", {"msg": msg, "cls": cls})

    def _progress(self, pct: int, label: str):
        self._emit("progress", {"pct": pct, "label": label})

    def browse_folder(self):
        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG, allow_multiple=False
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def browse_files(self):
        """Open file picker for .dat and .xml files, allow multiple."""
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=('All Files (*.*)',)
        )
        if result and len(result) > 0:
            return list(result)
        return []

    def browse_save(self, default_name: str = "merged.dat"):
        """Open save dialog for output .dat file."""
        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=default_name,
            file_types=('DAT Files (*.dat)',)
        )
        if result:
            return result if isinstance(result, str) else result[0] if result else None
        return None

    def scan_folder(self, folder_path: str) -> list:
        """Scan a folder for .dat and .xml files, return list of paths."""
        found = []
        try:
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith(('.dat', '.xml')):
                        found.append(os.path.join(root, f))
        except Exception as e:
            self._log(f"Error scanning folder: {e}", "err")
        return found

    def stop(self):
        self._stop.set()

    def run_merge(self, config: dict):
        """
        config = {
            files: [list of .dat/.xml file paths],
            output_path: str,
            dat_name: str,
            dat_description: str,
            dat_author: str,
            dedupe: bool,
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._merge_thread, args=(config,), daemon=True)
        t.start()

    def _merge_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        files = config.get("files", [])
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Merged DAT")
        dat_desc = config.get("dat_description", dat_name)
        dat_author = config.get("dat_author", "ToSort Toolkit")
        dedupe = config.get("dedupe", True)

        self._log("=" * 50, "info")
        self._log(f"DAT Merger started — {len(files)} file(s)", "ok")
        self._log("=" * 50, "info")

        if not files:
            self._log("No DAT files selected.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        if not output:
            self._log("No output path set.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        # Ensure .dat extension
        if not output.lower().endswith('.dat'):
            output += '.dat'

        all_entries = []
        total_parsed = 0
        parse_errors = 0

        # ── Parse all input files ──────────────────────────────────────
        for i, fpath in enumerate(files):
            if self._stop.is_set():
                self._log("Stopped by user.", "warn")
                break

            pct = int((i / len(files)) * 60)
            fname = Path(fpath).name
            self._progress(pct, f"Parsing: {fname}")

            try:
                header, entries = parse_dat_file(Path(fpath))
                all_entries.extend(entries)
                total_parsed += 1
                self._log(
                    f"  Parsed: {fname} — {len(entries)} game(s)"
                    + (f"  [{header.name}]" if header.name else ""),
                    "ok"
                )
            except Exception as e:
                parse_errors += 1
                self._log(f"  ERROR parsing {fname}: {e}", "err")

        if self._stop.is_set():
            self._running = False
            self._emit("done", {"success": False})
            return

        self._log(f"\nTotal entries loaded: {len(all_entries)} from {total_parsed} file(s)", "info")

        if not all_entries:
            self._log("No entries found in any file.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        # ── Merge and deduplicate ──────────────────────────────────────
        self._progress(65, "Deduplicating..." if dedupe else "Merging...")
        self._log("Deduplicating by ROM hash..." if dedupe else "Merging entries...", "info")

        merged, stats = merge_and_dedupe(all_entries, dedupe=dedupe)

        if dedupe:
            self._log(
                f"  Input: {stats['total_input']}  |  "
                f"Duplicates removed: {stats['duplicates_removed']}  |  "
                f"Output: {stats['total_output']}",
                "ok" if stats['duplicates_removed'] > 0 else "info"
            )
        else:
            self._log(f"  Total entries: {stats['total_output']}", "info")

        # ── Write output DAT ──────────────────────────────────────────
        self._progress(85, "Writing output DAT...")
        self._log(f"\nWriting: {output}", "info")

        out_header = DatHeader()
        out_header.name = dat_name
        out_header.description = dat_desc
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, merged)
            file_size = os.path.getsize(output)
            size_str = f"{file_size // 1024} KB" if file_size > 1024 else f"{file_size} bytes"
            self._log(f"  Written: {size_str}", "ok")
        except Exception as e:
            self._log(f"  ERROR writing output: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        # ── Summary ────────────────────────────────────────────────────
        self._progress(100, "Complete")
        self._log("", "")
        self._log("=" * 50, "info")
        self._log(f"Merge complete!", "ok")
        self._log(f"  Files parsed:  {total_parsed} ({parse_errors} errors)", "info")
        self._log(f"  Total entries: {stats['total_input']}", "info")
        if dedupe:
            self._log(f"  Dupes removed: {stats['duplicates_removed']}", "info")
        self._log(f"  Final entries: {stats['total_output']}", "info")
        self._log(f"  Output: {output}", "ok")
        self._log("=" * 50, "info")

        self._running = False
        self._emit("done", {"success": True, "stats": stats})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE 2: DAT DIFF
    # ══════════════════════════════════════════════════════════════════
    def run_diff(self, config: dict):
        """
        config = {
            master_file: str,           # path to the master/newest DAT
            older_files: [str, ...],    # paths to older DATs to diff against
            output_path: str,
            dat_name: str,
            dat_author: str,
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._diff_thread, args=(config,), daemon=True)
        t.start()

    def _diff_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        master_path = config.get("master_file", "")
        older_paths = config.get("older_files", [])
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Diff DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")

        self._log("=" * 50, "info")
        self._log("DAT Diff started", "ok")
        self._log("=" * 50, "info")

        if not master_path or not older_paths or not output:
            self._log("Missing master file, older files, or output path.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        if not output.lower().endswith('.dat'):
            output += '.dat'

        # Parse master DAT
        self._progress(5, "Parsing master DAT...")
        try:
            master_hdr, master_entries = parse_dat_file(Path(master_path))
            self._log(f"  Master: {Path(master_path).name} — {len(master_entries)} entries", "ok")
        except Exception as e:
            self._log(f"  ERROR parsing master: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        # Parse all older DATs and collect their hashes
        older_hashes = set()
        for i, opath in enumerate(older_paths):
            if self._stop.is_set():
                break
            pct = 10 + int((i / len(older_paths)) * 40)
            self._progress(pct, f"Parsing: {Path(opath).name}")
            try:
                _, older_entries = parse_dat_file(Path(opath))
                for entry in older_entries:
                    older_hashes.add(entry.hash_key())
                self._log(f"  Older: {Path(opath).name} — {len(older_entries)} entries", "ok")
            except Exception as e:
                self._log(f"  ERROR parsing {Path(opath).name}: {e}", "err")

        if self._stop.is_set():
            self._running = False
            self._emit("done", {"success": False})
            return

        # Find entries in master that are NOT in any older DAT
        self._progress(60, "Computing diff...")
        self._log("Computing diff (entries in master but not in older DATs)...", "info")

        diff_entries = []
        for entry in master_entries:
            if entry.hash_key() not in older_hashes:
                diff_entries.append(entry)

        self._log(f"  Master entries:   {len(master_entries)}", "info")
        self._log(f"  Older entries:    {len(older_hashes)} unique hashes", "info")
        self._log(f"  Diff (new only):  {len(diff_entries)}", "ok")

        # Write output
        self._progress(80, "Writing diff DAT...")
        out_header = DatHeader()
        out_header.name = dat_name
        out_header.description = f"Diff: {len(diff_entries)} entries not in older DATs"
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, diff_entries)
            self._log(f"  Written: {output}", "ok")
        except Exception as e:
            self._log(f"  ERROR writing: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(100, "Complete")
        self._log("", "")
        self._log(f"Diff complete — {len(diff_entries)} new entries found.", "ok")

        self._running = False
        self._emit("done", {
            "success": True,
            "stats": {
                "total_input": len(master_entries),
                "duplicates_removed": len(master_entries) - len(diff_entries),
                "total_output": len(diff_entries),
            }
        })

    # ══════════════════════════════════════════════════════════════════
    # FEATURE 3: SMART MERGE (by DAT name keyword)
    # ══════════════════════════════════════════════════════════════════
    def scan_dats_in_folder(self, folder_path: str) -> list:
        """Scan folder for DATs and return list of {path, name, filename}."""
        results = []
        try:
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith(('.dat', '.xml')):
                        fpath = os.path.join(root, f)
                        # Quick parse just the header for the DAT name
                        try:
                            hdr, entries = parse_dat_file(Path(fpath))
                            dat_name = hdr.name or f
                            results.append({
                                "path": fpath,
                                "name": dat_name,
                                "filename": f,
                                "entries": len(entries),
                            })
                        except Exception:
                            results.append({
                                "path": fpath,
                                "name": f,
                                "filename": f,
                                "entries": 0,
                            })
        except Exception as e:
            self._log(f"Scan error: {e}", "err")
        return results

    def run_smart_merge(self, config: dict):
        """
        config = {
            folder: str,
            keyword: str,        # filter DAT names containing this keyword
            output_path: str,
            dat_name: str,
            dat_author: str,
            dedupe: bool,
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._smart_merge_thread, args=(config,), daemon=True)
        t.start()

    def _smart_merge_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        folder = config.get("folder", "")
        keyword = config.get("keyword", "").strip().lower()
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Smart Merged DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")
        dedupe = config.get("dedupe", True)

        self._log("=" * 50, "info")
        self._log(f'Smart Merge — keyword: "{keyword}"', 'ok')
        self._log("=" * 50, "info")

        if not folder or not keyword or not output:
            self._log("Missing folder, keyword, or output path.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        if not output.lower().endswith('.dat'):
            output += '.dat'

        # Scan folder for all DATs
        self._progress(5, "Scanning folder...")
        self._log(f"Scanning: {folder}", "info")
        all_dats = self.scan_dats_in_folder(folder)
        self._log(f"  Found {len(all_dats)} DAT/XML file(s) total", "info")

        # Filter by keyword (match against DAT name and filename)
        matched = [
            d for d in all_dats
            if keyword in d['name'].lower() or keyword in d['filename'].lower()
        ]
        self._log(f'  Matched "{keyword}": {len(matched)} file(s)', 'ok')

        if not matched:
            self._log("No DATs matched the keyword.", "warn")
            self._running = False
            self._emit("done", {"success": False})
            return

        for d in matched:
            self._log(f"    {d['filename']}  [{d['name']}]  ({d['entries']} entries)", "dim")

        # Parse and merge matched DATs
        all_entries = []
        for i, d in enumerate(matched):
            if self._stop.is_set():
                break
            pct = 10 + int((i / len(matched)) * 50)
            self._progress(pct, f"Parsing: {d['filename']}")
            try:
                _, entries = parse_dat_file(Path(d['path']))
                all_entries.extend(entries)
            except Exception as e:
                self._log(f"  ERROR: {d['filename']}: {e}", "err")

        if self._stop.is_set():
            self._running = False
            self._emit("done", {"success": False})
            return

        self._log(f"  Total entries loaded: {len(all_entries)}", "info")

        # Dedupe
        self._progress(65, "Deduplicating..." if dedupe else "Merging...")
        merged, stats = merge_and_dedupe(all_entries, dedupe=dedupe)
        if dedupe:
            self._log(f"  Deduped: {stats['duplicates_removed']} duplicates removed", "ok")

        # Write
        self._progress(85, "Writing output...")
        out_header = DatHeader()
        out_header.name = dat_name
        out_header.description = f"Smart merge: {keyword} ({len(merged)} entries)"
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, merged)
            self._log(f"  Written: {output}", "ok")
        except Exception as e:
            self._log(f"  ERROR: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(100, "Complete")
        self._log(f"Smart merge complete — {stats['total_output']} entries.", "ok")
        self._running = False
        self._emit("done", {"success": True, "stats": stats})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE 4: EXTENSION MERGE (merge DATs containing specific ext)
    # ══════════════════════════════════════════════════════════════════
    def run_ext_merge(self, config: dict):
        """
        config = {
            folder: str,
            extension: str,      # e.g. "tap" or ".tap"
            output_path: str,
            dat_name: str,
            dat_author: str,
            dedupe: bool,
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._ext_merge_thread, args=(config,), daemon=True)
        t.start()

    def _ext_merge_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        folder = config.get("folder", "")
        ext = config.get("extension", "").strip().lower().lstrip(".")
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Extension Merged DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")
        dedupe = config.get("dedupe", True)

        self._log("=" * 50, "info")
        self._log(f'Extension Merge — looking for .{ext}', 'ok')
        self._log("=" * 50, "info")

        if not folder or not ext or not output:
            self._log("Missing folder, extension, or output path.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        if not output.lower().endswith('.dat'):
            output += '.dat'

        # Scan and find all DATs
        self._progress(5, "Scanning folder...")
        dat_files = []
        try:
            for root, dirs, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(('.dat', '.xml')):
                        dat_files.append(os.path.join(root, f))
        except Exception as e:
            self._log(f"Scan error: {e}", "err")

        self._log(f"  Found {len(dat_files)} DAT/XML file(s)", "info")

        # Parse each DAT and check if any ROM has the target extension
        matched_entries = []
        matched_count = 0
        for i, fpath in enumerate(dat_files):
            if self._stop.is_set():
                break
            pct = 10 + int((i / max(len(dat_files), 1)) * 50)
            if i % 10 == 0:
                self._progress(pct, f"Scanning: {Path(fpath).name}")
            try:
                _, entries = parse_dat_file(Path(fpath))
                has_ext = False
                for entry in entries:
                    for rom in entry.roms:
                        rname = rom.get('name', '').lower()
                        if rname.endswith('.' + ext):
                            has_ext = True
                            break
                    if has_ext:
                        break
                if has_ext:
                    matched_entries.extend(entries)
                    matched_count += 1
                    self._log(f"  MATCH: {Path(fpath).name} ({len(entries)} entries)", "ok")
            except Exception as e:
                self._log(f"  ERROR: {Path(fpath).name}: {e}", "err")

        if self._stop.is_set():
            self._running = False
            self._emit("done", {"success": False})
            return

        self._log(f"  {matched_count} DAT(s) contain .{ext} files, {len(matched_entries)} total entries", "info")

        if not matched_entries:
            self._log(f"No DATs contain .{ext} files.", "warn")
            self._running = False
            self._emit("done", {"success": False})
            return

        # Dedupe
        self._progress(65, "Deduplicating..." if dedupe else "Merging...")
        merged, stats = merge_and_dedupe(matched_entries, dedupe=dedupe)
        if dedupe:
            self._log(f"  Deduped: {stats['duplicates_removed']} duplicates removed", "ok")

        # Write
        self._progress(85, "Writing output...")
        out_header = DatHeader()
        out_header.name = dat_name
        out_header.description = f"Extension merge: .{ext} ({len(merged)} entries)"
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, merged)
            self._log(f"  Written: {output}", "ok")
        except Exception as e:
            self._log(f"  ERROR: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(100, "Complete")
        self._log(f"Extension merge complete — {stats['total_output']} entries.", "ok")
        self._running = False
        self._emit("done", {"success": True, "stats": stats})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE 5: DAT SIZE CALCULATOR
    # ══════════════════════════════════════════════════════════════════
    def run_size_calc(self, config: dict):
        """
        config = {
            files: [str, ...],    # DAT/XML file paths
            output_txt: str,      # optional: save summary to .txt
        }
        """
        if self._running:
            return
        t = threading.Thread(target=self._size_calc_thread, args=(config,), daemon=True)
        t.start()

    def _size_calc_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        files = config.get("files", [])
        output_txt = config.get("output_txt", "").strip()

        self._log("=" * 50, "info")
        self._log(f"DAT Size Calculator — {len(files)} file(s)", "ok")
        self._log("=" * 50, "info")

        if not files:
            self._log("No files selected.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        results = []
        grand_total = 0

        for i, fpath in enumerate(files):
            if self._stop.is_set():
                break
            pct = int((i / len(files)) * 90)
            self._progress(pct, f"Calculating: {Path(fpath).name}")

            try:
                hdr, entries = parse_dat_file(Path(fpath))
                dat_name = hdr.name or Path(fpath).name
                total_bytes = 0
                rom_count = 0
                for entry in entries:
                    for rom in entry.roms:
                        try:
                            size = int(rom.get('size', 0))
                            total_bytes += size
                            rom_count += 1
                        except (ValueError, TypeError):
                            pass

                grand_total += total_bytes
                size_str = self._format_size(total_bytes)
                results.append({
                    "file": Path(fpath).name,
                    "dat_name": dat_name,
                    "entries": len(entries),
                    "roms": rom_count,
                    "bytes": total_bytes,
                    "size_str": size_str,
                })
                self._log(f"  {dat_name}  =  {size_str}  ({len(entries)} games, {rom_count} ROMs)", "ok")

            except Exception as e:
                self._log(f"  ERROR: {Path(fpath).name}: {e}", "err")
                results.append({
                    "file": Path(fpath).name,
                    "dat_name": "ERROR",
                    "entries": 0,
                    "roms": 0,
                    "bytes": 0,
                    "size_str": "ERROR",
                })

        # Summary
        self._progress(95, "Done")
        self._log("", "")
        self._log("=" * 50, "info")
        self._log(f"Grand total: {self._format_size(grand_total)}", "ok")
        self._log("=" * 50, "info")

        # Save to txt if requested
        if output_txt:
            try:
                lines = []
                lines.append("=" * 65)
                lines.append("  ToSort Toolkit — DAT Size Calculator Report")
                lines.append("=" * 65)
                lines.append(f"  Generated: {time.strftime('%Y-%m-%d %H:%M')}")
                lines.append("")
                lines.append(f"  {'DAT Name':<45} {'Size':>12}  {'Games':>7}  {'ROMs':>7}")
                lines.append(f"  {'-'*43}   {'-'*10}  {'-'*7}  {'-'*7}")
                for r in results:
                    name = r['dat_name'][:43]
                    lines.append(f"  {name:<45} {r['size_str']:>12}  {r['entries']:>7}  {r['roms']:>7}")
                lines.append("")
                lines.append(f"  {'GRAND TOTAL':<45} {self._format_size(grand_total):>12}")
                lines.append("=" * 65)

                if not output_txt.lower().endswith('.txt'):
                    output_txt += '.txt'
                Path(output_txt).parent.mkdir(parents=True, exist_ok=True)
                with open(output_txt, 'w', encoding='utf-8') as f:
                    f.write("\n".join(lines))
                self._log(f"Report saved: {output_txt}", "ok")
            except Exception as e:
                self._log(f"Could not save report: {e}", "err")

        self._progress(100, "Complete")
        self._running = False
        self._emit("done", {
            "success": True,
            "stats": {
                "total_input": len(files),
                "duplicates_removed": 0,
                "total_output": len(results),
            },
            "size_results": results,
            "grand_total": self._format_size(grand_total),
        })

    @staticmethod
    def _format_size(nbytes: int) -> str:
        """Format bytes into human-readable size."""
        if nbytes < 1024:
            return f"{nbytes} B"
        elif nbytes < 1024 * 1024:
            return f"{nbytes / 1024:.1f} KB"
        elif nbytes < 1024 * 1024 * 1024:
            return f"{nbytes / (1024*1024):.1f} MB"
        elif nbytes < 1024 * 1024 * 1024 * 1024:
            return f"{nbytes / (1024*1024*1024):.2f} GB"
        else:
            return f"{nbytes / (1024*1024*1024*1024):.2f} TB"

