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
        Entries with no ROMs get a name-based key so they
        are still deduplicated correctly (not passed through all).
        """
        if not self.roms:
            # No ROM data — use name as key so duplicates are caught
            return f"__norom__{self.name.lower().strip()}"
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
        self.forcepacking = ""  # "unzip" to tell RomVault not to repack files


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
    Streaming CLRMamePro parser — processes line by line without loading
    the entire file into memory. Safe for DAT files of any size.
    Returns (DatHeader, list[DatEntry]).
    """
    header = DatHeader()
    entries = []
    source = filepath.name
    file_size = filepath.stat().st_size

    # State machine
    in_header = False
    in_game   = False
    in_rom    = False
    depth     = 0

    # Accumulators
    hdr_lines   = []
    game_lines  = []
    rom_lines   = []
    bytes_read  = 0

    def parse_block_fields(lines):
        """Extract fields from a list of lines."""
        block = "\n".join(lines)
        return block

    def finish_game(game_lines):
        block = "\n".join(game_lines)
        name = _extract_field(block, "name")
        desc = _extract_field(block, "description") or name

        roms = []
        rp = 0
        while True:
            rm = re.search(r"\brom\s*\(", block[rp:], re.IGNORECASE)
            if not rm: break
            rs = rp + rm.end()
            rd = 1; ri = rs
            while ri < len(block) and rd > 0:
                if block[ri] == "(": rd += 1
                elif block[ri] == ")": rd -= 1
                ri += 1
            rom_block = block[rs:ri-1]
            rp = ri
            rom_entry = {
                "name": _extract_field(rom_block, "name"),
                "size": _extract_field(rom_block, "size"),
                "crc":  _extract_field(rom_block, "crc"),
                "md5":  _extract_field(rom_block, "md5"),
                "sha1": _extract_field(rom_block, "sha1"),
            }
            roms.append(rom_entry)

        if name and name.strip():
            if roms or len(name) > 2:
                return DatEntry(name.strip(), desc.strip(), roms, source)
        return None

    def finish_header(hdr_lines):
        block = "\n".join(hdr_lines)
        header.name        = _extract_field(block, "name")
        header.description = _extract_field(block, "description")
        header.version     = _extract_field(block, "version")
        header.author      = _extract_field(block, "author")
        header.homepage    = _extract_field(block, "homepage")
        header.url         = _extract_field(block, "url")
        header.category    = _extract_field(block, "category")
        header.date        = _extract_field(block, "date")

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            bytes_read += len(raw_line.encode("utf-8", errors="replace"))
            line = raw_line.rstrip("\n\r")
            stripped = line.strip().lower()

            # Detect block openings
            if not in_header and not in_game:
                if re.match(r"clrmamepro\s*\(", stripped, re.IGNORECASE):
                    in_header = True
                    depth = 1
                    hdr_lines = []
                    continue
                if re.match(r"(?:game|resource|machine)\s*\(", stripped, re.IGNORECASE):
                    in_game = True
                    depth = 1
                    game_lines = []
                    continue

            elif in_header:
                depth += line.count("(") - line.count(")")
                if depth <= 0:
                    finish_header(hdr_lines)
                    in_header = False
                else:
                    hdr_lines.append(line)

            elif in_game:
                depth += line.count("(") - line.count(")")
                if depth <= 0:
                    entry = finish_game(game_lines)
                    if entry:
                        entries.append(entry)
                    in_game = False
                    game_lines = []
                else:
                    game_lines.append(line)

    return header, entries


def _extract_field(block: str, field: str) -> str:
    """
    Extract a field value from a CLRMamePro block.
    Handles names with embedded double quotes (e.g. Game "Title" (1984)).
    """
    ef = re.escape(field)
    # Match quoted value including escaped internal quotes
    m = re.search(ef + r'\s+"((?:[^"\\]|\\.)*)"', block, re.IGNORECASE)
    if m:
        return m.group(1).replace('\"'  , '"'  ).strip()
    # Fallback - line-safe quoted match
    m = re.search(ef + r'\s+"([^"]*)"', block, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Unquoted single token
    m = re.search(ef + r'\s+(\S+)', block, re.IGNORECASE)
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
    empty_skipped = 0

    for entry in all_entries:
        key = entry.hash_key()
        # key is now always non-empty (no-ROM entries get name-based key)
        if key in seen_hashes:
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
    """
    Escape a string for CLRMamePro format.
    Only escape embedded double quotes — do NOT double-escape backslashes
    as that causes \\[ to appear in names like [Misc].
    """
    return s.replace('"', '\"')


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
    if getattr(header, 'forcepacking', ''):
        lines.append(f'\tforcepacking "{header.forcepacking}"')
    lines.append(')')
    lines.append('')

    # Game entries
    # Filter: skip empty entries and entries with malformed names and no ROMs
    def _is_valid_entry(e):
        if not e.name or not e.name.strip():
            return False
        n = e.name.strip()
        bad_starts = ('"', "'", '\\')
        if not e.roms and n and n[0] in bad_starts:
            return False
        return True
    valid_entries = [e for e in entries if _is_valid_entry(e)]
    # Dedupe by name within the write — RomVault crashes on duplicate game names
    seen_names = set()
    unique_valid = []
    for e in sorted(valid_entries, key=lambda e: e.name.lower()):
        n = e.name.strip().lstrip('"\\').rstrip()
        if n and n not in seen_names:
            seen_names.add(n)
            unique_valid.append(e)
    for entry in unique_valid:
        lines.append('game (')
        # Strip any leading/trailing quote chars that crept in during parsing
        clean_name = entry.name.strip().lstrip('"\\').rstrip()
        if not clean_name:  # skip if stripping left nothing
            continue
        lines.append(f'\tname "{_escape_dat_string(clean_name)}"')
        if entry.description and entry.description != entry.name:
            lines.append(f'\tdescription "{_escape_dat_string(entry.description)}"')
        # Note: mtime is stored in the sidecar .mtime.json file, NOT in the DAT
        # RomVault rejects unknown fields like "date" and "comment" in game blocks
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
        # Register Python-side drag-drop handler to get full file paths
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e):
                files = e.get('dataTransfer', {}).get('files', [])
                if not files:
                    return
                paths = []
                for f in files:
                    path = f.get('pywebviewFullPath') or f.get('path') or f.get('name', '')
                    if path and (path.lower().endswith('.dat') or path.lower().endswith('.xml')):
                        paths.append(path)
                if paths:
                    import json
                    paths_json = json.dumps(paths)
                    # Tell JS which list to add to based on currently visible tab
                    self._window.evaluate_js(
                        f'window.handlePythonDrop({paths_json})'
                    )

            def on_drag(e):
                pass  # needed to suppress default behaviour

            window.dom.document.events.dragenter += DOMEventHandler(on_drag, True, True)
            window.dom.document.events.dragover  += DOMEventHandler(on_drag, True, True, debounce=200)
            window.dom.document.events.drop      += DOMEventHandler(on_drop, True, True)
        except Exception:
            pass  # DOMEventHandler not available in older pywebview versions

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
        """
        Fast scan — reads only first 8KB of each file to extract the DAT name
        from the header. Does NOT parse entries, so works quickly even on
        very large DAT files.
        """
        results = []
        try:
            for root, dirs, files in os.walk(folder_path):
                for f in files:
                    if f.lower().endswith(('.dat', '.xml')):
                        fpath = os.path.join(root, f)
                        dat_name = f  # default to filename
                        try:
                            with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                                head = fh.read(8192)
                            # Try XML header name first
                            m = re.search(r'<name>([^<]+)</name>', head, re.IGNORECASE)
                            if m:
                                dat_name = m.group(1).strip()
                            else:
                                # Try CLRMamePro header
                                m = re.search(
                                    r'clrmamepro\s*\(.*?name\s+"([^"]+)"',
                                    head, re.DOTALL | re.IGNORECASE
                                )
                                if m:
                                    dat_name = m.group(1).strip()
                        except Exception:
                            pass
                        results.append({
                            "path": fpath,
                            "name": dat_name,
                            "filename": f,
                            "entries": -1,  # -1 = not counted (fast mode)
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
        # Support multiple keywords (list or comma-separated string)
        raw_kw = config.get("keywords", config.get("keyword", ""))
        if isinstance(raw_kw, list):
            keywords = [k.strip().lower() for k in raw_kw if k.strip()]
        else:
            keywords = [k.strip().lower() for k in str(raw_kw).split(",") if k.strip()]
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Smart Merged DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")
        dedupe = config.get("dedupe", True)

        self._log("=" * 50, "info")
        self._log(f"Smart Merge — keywords: {keywords}", "ok")
        self._log("=" * 50, "info")

        if not folder or not keywords or not output:
            self._log("Missing folder, keyword(s), or output path.", "err")
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

        # Filter by ANY keyword (match against DAT name and filename)
        def matches_any(d):
            nl = d["name"].lower()
            fl = d["filename"].lower()
            return any(kw in nl or kw in fl for kw in keywords)

        matched = [d for d in all_dats if matches_any(d)]
        self._log(f"  Matched {len(matched)} DAT(s) from {len(all_dats)} scanned", "ok")

        if not matched:
            self._log("No DATs matched the keyword.", "warn")
            self._running = False
            self._emit("done", {"success": False})
            return

        for d in matched:
            self._log(f"    {d['filename']}", "ok")
            self._log(f"      DAT name: {d['name']}", "dim")

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
                self._log(f"  Parsed: {d['filename']} — {len(entries)} entries added", "dim")
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
        out_header.description = f"Smart merge: {keywords} ({len(merged)} entries)"
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
        # Support multiple extensions (list or comma-separated string)
        raw_ext = config.get("extensions", config.get("extension", ""))
        if isinstance(raw_ext, list):
            exts = [e.strip().lower().lstrip(".") for e in raw_ext if e.strip()]
        else:
            exts = [e.strip().lower().lstrip(".") for e in str(raw_ext).split(",") if e.strip()]
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Extension Merged DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")
        dedupe = config.get("dedupe", True)

        self._log("=" * 50, "info")
        self._log(f"Extension Merge — looking for: {["." + e for e in exts]}", "ok")
        self._log("=" * 50, "info")

        if not folder or not exts or not output:
            self._log("Missing folder, extension(s), or output path.", "err")
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
                matching_exts_found = set()
                for entry in entries:
                    for rom in entry.roms:
                        rname = rom.get('name', '').lower()
                        for ex in exts:
                            if rname.endswith('.' + ex):
                                has_ext = True
                                matching_exts_found.add(ex)
                if has_ext:
                    matched_entries.extend(entries)
                    matched_count += 1
                    self._log(
                        f"  MATCH: {Path(fpath).name} "
                        f"({len(entries)} entries, "
                        f"exts: {["." + x for x in matching_exts_found]})",
                        "ok"
                    )
            except Exception as e:
                self._log(f"  ERROR: {Path(fpath).name}: {e}", "err")

        if self._stop.is_set():
            self._running = False
            self._emit("done", {"success": False})
            return

        self._log(f"  {matched_count} DAT(s) matched, {len(matched_entries)} total entries", "info")

        if not matched_entries:
            self._log(f"No DATs contain those extensions.", "warn")
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
        out_header.description = f"Extension merge: {exts} ({len(merged)} entries)"
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
        auto_fix = config.get("auto_fix", False)
        fix_output_dir = config.get("fix_output_dir", "").strip()

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


    # ══════════════════════════════════════════════════════════════════
    # FEATURE: SINGLE-DAT INTERNAL DEDUPER
    # ══════════════════════════════════════════════════════════════════
    def run_internal_dedupe(self, config: dict):
        """
        config = {
            file: str,           # single DAT/XML path
            output_path: str,
            dat_name: str,       # optional override
            dat_author: str,
        }
        """
        if self._running: return
        t = threading.Thread(target=self._internal_dedupe_thread, args=(config,), daemon=True)
        t.start()

    def _internal_dedupe_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        fpath = config.get("file", "")
        output = config.get("output_path", "")
        dat_name_override = config.get("dat_name", "").strip()
        dat_author = config.get("dat_author", "ToSort Toolkit")

        self._log("=" * 50, "info")
        self._log("Single-DAT Internal Deduper", "ok")
        self._log("=" * 50, "info")

        if not fpath or not output:
            self._log("Missing input file or output path.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        if not output.lower().endswith('.dat'):
            output += '.dat'

        self._progress(10, "Parsing...")
        try:
            header, entries = parse_dat_file(Path(fpath))
            self._log(f"  Loaded: {Path(fpath).name} — {len(entries)} entries", "ok")
        except Exception as e:
            self._log(f"  ERROR: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(40, "Deduplicating internally...")
        seen = {}
        unique = []
        dupes = 0
        for entry in entries:
            key = entry.hash_key()
            if key in seen:
                dupes += 1
                self._log(f"  DUPE: {entry.name}  ==  {seen[key]}", "dim")
            else:
                seen[key] = entry.name
                unique.append(entry)

        self._log(f"  {len(entries)} input, {dupes} internal duplicates, {len(unique)} unique", "ok")

        self._progress(70, "Writing...")
        out_header = DatHeader()
        out_header.name = dat_name_override or header.name or "Deduped DAT"
        out_header.description = header.description or out_header.name
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, unique)
            self._log(f"  Written: {output}", "ok")
        except Exception as e:
            self._log(f"  ERROR: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(100, "Complete")
        self._log(f"Internal dedupe done — {dupes} duplicates removed.", "ok")
        self._running = False
        self._emit("done", {"success": True, "stats": {
            "total_input": len(entries), "duplicates_removed": dupes, "total_output": len(unique)
        }})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE: DAT INTEGRITY CHECK
    # ══════════════════════════════════════════════════════════════════
    def run_integrity_check(self, config: dict):
        """
        config = {
            files: [str, ...],
            output_txt: str,     # optional report path
        }
        """
        if self._running: return
        t = threading.Thread(target=self._integrity_thread, args=(config,), daemon=True)
        t.start()

    def _integrity_thread(self, config: dict):
        self._running = True
        self._stop.clear()

        files = config.get("files", [])
        output_txt = config.get("output_txt", "").strip()
        auto_fix = config.get("auto_fix", False)
        fix_output_dir = config.get("fix_output_dir", "").strip()

        self._log("=" * 50, "info")
        self._log(f"DAT Integrity Check — {len(files)} file(s)", "ok")
        self._log("=" * 50, "info")

        if not files:
            self._log("No files selected.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        all_issues = []
        total_ok = 0

        for fi, fpath in enumerate(files):
            if self._stop.is_set(): break
            pct = int((fi / len(files)) * 90)
            fname = Path(fpath).name
            self._progress(pct, f"Checking: {fname}")

            issues = []
            try:
                fsize = Path(fpath).stat().st_size
                fsize_str = self._format_size(fsize)
                self._log(f"  Parsing: {fname} ({fsize_str}) ...", "dim")
                self._progress(pct, f"Parsing {fname} ({fsize_str})...")
                header, entries = parse_dat_file(Path(fpath))
                dat_name = header.name or fname
                self._log(f"  Loaded: {dat_name} — {len(entries):,} entries", "dim")

                if not header.name:
                    issues.append("WARNING: DAT has no name in header")
                if not entries:
                    issues.append("WARNING: DAT contains zero entries")

                name_counts = {}
                empty_games = 0
                missing_hash = 0
                missing_size = 0
                zero_size = 0
                total_roms = 0
                chunk = max(1, len(entries) // 20)  # progress every 5%

                for ei, entry in enumerate(entries):
                    if ei % chunk == 0:
                        pct2 = int((fi / max(len(files),1)) * 80) + int((ei / max(len(entries),1)) * (80 // max(len(files),1)))
                        self._progress(min(pct2, 88), f"Checking {fname}: {ei}/{len(entries)} entries...")
                    entry = entry  # keep original variable name below
                    # Check duplicate game names
                    name_counts[entry.name] = name_counts.get(entry.name, 0) + 1

                    if not entry.roms:
                        empty_games += 1
                        continue

                    for rom in entry.roms:
                        total_roms += 1
                        has_any_hash = bool(rom.get('crc') or rom.get('md5') or rom.get('sha1'))
                        if not has_any_hash:
                            missing_hash += 1
                        if not rom.get('size'):
                            missing_size += 1
                        elif rom.get('size') == '0':
                            zero_size += 1

                # Duplicate names
                dupe_names = {k: v for k, v in name_counts.items() if v > 1}
                if dupe_names:
                    for name, count in sorted(dupe_names.items()):
                        issues.append(f"DUPLICATE NAME: '{name}' appears {count} times")

                if empty_games > 0:
                    issues.append(f"EMPTY ENTRIES: {empty_games} game(s) with no ROMs")
                if missing_hash > 0:
                    issues.append(f"MISSING HASH: {missing_hash} ROM(s) with no CRC/MD5/SHA1")
                if missing_size > 0:
                    issues.append(f"MISSING SIZE: {missing_size} ROM(s) with no size attribute")
                if zero_size > 0:
                    issues.append(f"ZERO SIZE: {zero_size} ROM(s) with size=0")

                if issues:
                    self._log(f"  {dat_name}: {len(issues)} issue(s) found", "warn")
                    for iss in issues:
                        self._log(f"    {iss}", "warn")
                    all_issues.append({"file": fname, "dat_name": dat_name,
                                       "entries": len(entries), "roms": total_roms,
                                       "issues": issues})
                else:
                    self._log(f"  {dat_name}: OK ({len(entries)} entries, {total_roms} ROMs)", "ok")
                    total_ok += 1

            except Exception as e:
                issues.append(f"PARSE ERROR: {e}")
                self._log(f"  {fname}: PARSE ERROR — {e}", "err")
                all_issues.append({"file": fname, "dat_name": fname,
                                   "entries": 0, "roms": 0, "issues": issues})

        # Auto-fix: if enabled, run each problematic DAT through internal deduper
        if auto_fix and all_issues:
            fix_dir = Path(fix_output_dir) if fix_output_dir else None
            self._log("", "")
            self._log(f"Auto-fixing {len(all_issues)} DAT(s)...", "info")
            for item in all_issues:
                if self._stop.is_set(): break
                fpath = next((f for f in files if Path(f).name == item['file']), None)
                if not fpath: continue
                try:
                    hdr, entries = parse_dat_file(Path(fpath))
                    # Dedupe internally
                    seen = {}
                    unique = []
                    for entry in entries:
                        k = entry.hash_key()
                        if k not in seen:
                            seen[k] = True
                            # Strip ROMs with no hash data
                            clean_roms = [r for r in entry.roms
                                          if r.get('crc') or r.get('sha1') or r.get('md5')]
                            entry.roms = clean_roms
                            # Remove artefact entries: no ROMs + name starts with quote
                            _n = entry.name.strip()
                            _bad = not clean_roms and _n and _n[0] in ('"', "'", '\\')
                            if not _bad:
                                unique.append(entry)
                    # Determine output path
                    if fix_dir:
                        fix_dir.mkdir(parents=True, exist_ok=True)
                        out_path = fix_dir / Path(fpath).name
                    else:
                        out_path = Path(fpath)
                    write_clrmamepro_dat(out_path, hdr, unique)
                    self._log(f"  Fixed: {item['file']} ({len(entries)-len(unique)} issues removed)", "ok")
                except Exception as e:
                    self._log(f"  Fix failed: {item['file']}: {e}", "err")

        # Summary
        self._progress(95, "Done")
        self._log("", "")
        self._log(f"Integrity check: {total_ok} OK, {len(all_issues)} with issues, out of {len(files)} checked.", "ok" if not all_issues else "warn")

        if output_txt:
            try:
                lines = ["=" * 60, "  DAT Integrity Check Report", "=" * 60,
                         f"  Date: {time.strftime('%Y-%m-%d %H:%M')}",
                         f"  Files checked: {len(files)}",
                         f"  Clean: {total_ok}  |  Issues: {len(all_issues)}", ""]
                for item in all_issues:
                    lines.append(f"  {item['dat_name']}  ({item['file']})")
                    lines.append(f"    Entries: {item['entries']}  ROMs: {item['roms']}")
                    for iss in item['issues']:
                        lines.append(f"    - {iss}")
                    lines.append("")
                lines.append("=" * 60)
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
        self._emit("done", {"success": True, "stats": {
            "total_input": len(files), "duplicates_removed": len(all_issues),
            "total_output": total_ok
        }})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE: DAT CREATOR
    # ══════════════════════════════════════════════════════════════════
    def run_create_dat(self, config: dict):
        """
        config = {
            folder: str,         # folder to scan
            output_path: str,
            dat_name: str,
            dat_author: str,
            recursive: bool,
            use_sha1: bool,
            use_md5: bool,
        }
        """
        if self._running: return
        t = threading.Thread(target=self._create_dat_thread, args=(config,), daemon=True)
        t.start()

    def _create_dat_thread(self, config: dict):
        import hashlib
        import re as _re
        self._running = True
        self._stop.clear()

        folder = Path(config.get("folder", "")).expanduser()
        output = config.get("output_path", "")
        dat_name = config.get("dat_name", "Created DAT")
        dat_author = config.get("dat_author", "ToSort Toolkit")
        recursive = config.get("recursive", True)
        use_sha1 = config.get("use_sha1", True)
        use_md5 = config.get("use_md5", False)
        archives_as_files = config.get("archives_as_files", True)
        verbose = config.get("verbose", False)

        self._log("=" * 50, "info")
        self._log("DAT Creator", "ok")
        self._log("=" * 50, "info")

        if not folder.is_dir():
            self._log(f"Folder not found: {folder}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return
        if not output:
            self._log("No output path set.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return
        if not output.lower().endswith('.dat'):
            output += '.dat'

        # ── Group files by parent folder ─────────────────────────────────
        # Each folder = one game entry; all files in it = ROM entries
        # This matches scene DAT format: folder name as game, every file as ROM
        self._progress(5, "Collecting files...")
        import zlib
        from collections import defaultdict
        import re as _re2

        folder_files = defaultdict(list)
        if recursive:
            for root, dirs, files in os.walk(folder):
                rp = Path(root)
                for f in sorted(files):
                    folder_files[rp].append(rp / f)
        else:
            files_in_root = sorted([f for f in folder.iterdir() if f.is_file()])
            if files_in_root:
                folder_files[folder] = files_in_root
            for d in sorted(folder.iterdir()):
                if d.is_dir():
                    for f in sorted(d.iterdir()):
                        if f.is_file():
                            folder_files[d].append(f)

        total_files = sum(len(v) for v in folder_files.values())
        self._log(f"  Found {total_files} file(s) in {len(folder_files)} folder(s)", "info")

        def hash_one_file(fpath):
            crc = 0
            sha1_h = hashlib.sha1() if use_sha1 else None
            md5_h  = hashlib.md5()  if use_md5  else None
            size = 0
            with open(fpath, "rb") as _f:
                while True:
                    chunk = _f.read(65536)
                    if not chunk: break
                    size += len(chunk)
                    crc = zlib.crc32(chunk, crc)
                    if sha1_h: sha1_h.update(chunk)
                    if md5_h:  md5_h.update(chunk)
            result = {"crc": format(crc & 0xFFFFFFFF, "08X"), "size": str(size)}
            if use_sha1: result["sha1"] = sha1_h.hexdigest()
            if use_md5:  result["md5"]  = md5_h.hexdigest()
            return result

        entries = []
        n_processed = 0
        total_folders = len(folder_files)

        for fi, (fdir, ffiles) in enumerate(sorted(folder_files.items())):
            if self._stop.is_set(): break
            pct = 5 + int((fi / max(total_folders, 1)) * 88)
            self._progress(pct, f"Processing: {fdir.name} ({fi+1}/{total_folders})")

            # Game name = folder name relative to scan root
            try:
                rel_dir = str(fdir.relative_to(folder)).replace(os.sep, "/")
            except ValueError:
                rel_dir = fdir.name
            if rel_dir in (".", ""):
                rel_dir = fdir.name

            if verbose:
                self._log(f"  Folder: {rel_dir} ({len(ffiles)} files)", "dim")

            roms = []
            for fpath in ffiles:
                if self._stop.is_set(): break
                suf = fpath.suffix.lower()
                is_arc = suf in {'.zip', '.rar', '.7z', '.gz'}

                if archives_as_files or not is_arc:
                    # Hash the file as-is (always for non-archives,
                    # and for archives when in archives-as-files mode)
                    try:
                        hashes = hash_one_file(fpath)
                        roms.append({"name": fpath.name, **hashes})
                        n_processed += 1
                        if verbose:
                            self._log(f"    {fpath.name}  crc={hashes['crc']}", "dim")
                    except Exception as he:
                        self._log(f"  ERR: {fpath.name}: {he}", "err")
                else:
                    # Contents mode — each archive becomes its OWN game entry
                    # named after the archive (without extension)
                    try:
                        arc_roms = self._hash_archive_contents(fpath, verbose)
                        if arc_roms:
                            # Game name = parent folder path + archive stem
                            arc_game = rel_dir + '/' + fpath.stem if rel_dir not in ('.','') else fpath.stem
                            arc_entry = DatEntry(arc_game, arc_game, arc_roms, str(folder))
                            entries.append(arc_entry)
                            n_processed += len(arc_roms)
                            if verbose:
                                self._log(f"    {fpath.name} -> {len(arc_roms)} internal files", "dim")
                        else:
                            # Fallback: hash as file if unreadable
                            hashes = hash_one_file(fpath)
                            roms.append({"name": fpath.name, **hashes})
                            n_processed += 1
                    except Exception as he:
                        self._log(f"  ERR: {fpath.name}: {he}", "err")

            # Only add folder-level entry if it has roms
            # (in contents mode, archives create their own entries above)
            if roms:
                entry = DatEntry(rel_dir, rel_dir, roms, str(folder))
                entries.append(entry)

        self._log(
            f"  Processed {n_processed} files across {total_folders} folder(s)",
            "ok"
        )

        # Write
        self._progress(92, "Writing DAT...")
        out_header = DatHeader()
        out_header.name = dat_name
        out_header.description = f"Created from {folder.name} ({len(entries)} files)"
        out_header.author = dat_author
        out_header.version = time.strftime("%Y-%m-%d %H:%M")
        # Only set forcepacking unzip for archives-as-files mode
        if archives_as_files:
            out_header.forcepacking = "unzip"

        try:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            write_clrmamepro_dat(Path(output), out_header, entries)
            self._log(f"  Written: {output}", "ok")
        except Exception as e:
            self._log(f"  ERROR: {e}", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        self._progress(100, "Complete")
        self._log(f"DAT created — {len(entries)} entries.", "ok")
        self._running = False
        self._emit("done", {"success": True, "stats": {
            "total_input": n_processed, "duplicates_removed": 0, "total_output": len(entries)
        }})

    # ══════════════════════════════════════════════════════════════════
    # FEATURE: DAT REBUILDER
    # ══════════════════════════════════════════════════════════════════
    def run_rebuild(self, config: dict):
        """
        config = {
            dat_files: [str,...],    # DAT(s) to use as reference
            source_folder: str,      # folder with loose files
            dest_folder: str,        # where matched files are moved
            dry_run: bool,
            match_by: str,           # 'crc', 'sha1', 'md5'
        }
        """
        if self._running: return
        t = threading.Thread(target=self._rebuild_thread, args=(config,), daemon=True)
        t.start()

    def _rebuild_thread(self, config: dict):
        import hashlib, zlib
        self._running = True
        self._stop.clear()

        dat_files = config.get("dat_files", [])
        # Support multiple source folders
        raw_sources = config.get("source_folders", config.get("source_folder", ""))
        if isinstance(raw_sources, list):
            source_folders = [Path(s).expanduser() for s in raw_sources if s.strip()]
        else:
            source_folders = [Path(raw_sources).expanduser()] if raw_sources.strip() else []
        _dest_raw = config.get("dest_folder", "").strip()
        if not _dest_raw:
            self._log("No destination folder set.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return
        dest = Path(_dest_raw).expanduser()
        dry_run = config.get("dry_run", False)
        match_by = config.get("match_by", "crc")
        delete_source = config.get("delete_source", False)
        # rebuild_as_archives: if True, matched files are moved/copied as whole
        # archive files (archives-as-files DATs). If False, files are rebuilt
        # individually by internal hash into dest folder structure.
        rebuild_as_archives = config.get("rebuild_as_archives", True)
        cleanup_empty = config.get("cleanup_empty", True)

        self._log("=" * 50, "info")
        self._log("DAT Rebuilder", "ok")
        self._log("=" * 50, "info")

        if not dat_files:
            self._log("No DAT files selected.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return
        if not source_folders:
            self._log("No source folders set.", "err")
            self._running = False
            self._emit("done", {"success": False})
            return

        # dest_root is the base — each DAT rebuilds into its own named subfolder
        dest_root = dest
        dest_root.mkdir(parents=True, exist_ok=True)
        if dry_run:
            self._log("Dry run — files will be listed but NOT moved.", "warn")

        # Step 1: Build hash lookup from DAT(s)
        self._progress(5, "Building hash index from DATs...")
        hash_index = {}  # hash -> (game_name, rom_name)
        total_dat_roms = 0

        for dpath in dat_files:
            if self._stop.is_set(): break
            try:
                _, entries = parse_dat_file(Path(dpath))
                # Each DAT rebuilds into dest_root / DAT_stem subfolder
                dat_stem = Path(dpath).stem
                dest = dest_root / dat_stem
                dest.mkdir(parents=True, exist_ok=True)
                self._log(f"  Rebuilding to: {dat_stem}/", "dim")
                for entry in entries:
                    for rom in entry.roms:
                        h = None
                        if match_by == 'sha1' and rom.get('sha1'):
                            h = rom['sha1'].lower()
                        elif match_by == 'md5' and rom.get('md5'):
                            h = rom['md5'].lower()
                        elif rom.get('crc'):
                            h = rom['crc'].upper()
                        if h:
                            # Store dest subdir so each DAT rebuilds to correct folder
                            hash_index[h] = (entry.name, rom.get('name', ''), dest)
                            total_dat_roms += 1
                self._log(f"  Loaded: {Path(dpath).name}", "ok")
            except Exception as e:
                self._log(f"  ERROR: {Path(dpath).name}: {e}", "err")

        self._log(f"  Hash index: {len(hash_index)} unique ROM hashes from {total_dat_roms} total", "info")
        # Show first 5 entries in hash index for debugging
        for k,v in list(hash_index.items())[:5]:
            self._log(f"  Index sample: {k} -> {v[1]} [{v[2].name}]", "dim")

        # Step 2: Scan all source folders
        self._progress(30, "Scanning source files...")
        source_files = []  # list of (fpath, internal_name_or_None)
        ARCHIVE_SCAN_EXTS = {".zip", ".rar", ".7z"}
        for source in source_folders:
            if not source.is_dir():
                self._log(f"  WARN: source folder not found: {source}", "warn")
                continue
            for root, dirs, files in os.walk(source):
                rp = Path(root).resolve()
                if rp == dest_root.resolve() or str(rp).startswith(str(dest_root.resolve())):
                    dirs.clear(); continue
                for f in files:
                    fp = Path(root) / f
                    if not rebuild_as_archives and fp.suffix.lower() in ARCHIVE_SCAN_EXTS:
                        # Scan internally — yield each internal file as a candidate
                        try:
                            if fp.suffix.lower() == ".zip":
                                import zipfile as _zf
                                zfm = _zf
                                try:
                                    import zipfile_zstd as zfm
                                except ImportError: pass
                                with zfm.ZipFile(fp, "r") as z:
                                    for iname in z.namelist():
                                        if not z.getinfo(iname).is_dir():
                                            source_files.append((fp, iname))
                            elif fp.suffix.lower() == ".7z":
                                try:
                                    import py7zr
                                    with py7zr.SevenZipFile(fp, mode="r") as sz:
                                        for iname in sz.getnames():
                                            source_files.append((fp, iname))
                                except ImportError: pass
                            elif fp.suffix.lower() == ".rar":
                                try:
                                    import rarfile
                                    with rarfile.RarFile(fp) as rf:
                                        for iname in rf.namelist():
                                            source_files.append((fp, iname))
                                except ImportError: pass
                        except Exception as ae:
                            self._log(f"  WARN: could not scan {fp.name}: {ae}", "warn")
                            source_files.append((fp, None))  # fallback: treat as file
                    else:
                        source_files.append((fp, None))

        self._log(f"  Source files: {len(source_files)}", "info")

        matched = 0
        unmatched = 0
        errors = 0

        for i, (fpath, internal_name) in enumerate(source_files):
            if self._stop.is_set(): break
            if i % 50 == 0:
                pct = 30 + int((i / max(len(source_files), 1)) * 65)
                self._progress(pct, f"Checking: {fpath.name} ({i}/{len(source_files)})")

            try:
                # Hash the file or internal archive entry
                _internal_data = None
                if internal_name and not rebuild_as_archives:
                    # Read from inside archive for hashing
                    try:
                        if fpath.suffix.lower() == ".zip":
                            import zipfile as _zf2
                            zfm2 = _zf2
                            try:
                                import zipfile_zstd as zfm2
                            except ImportError: pass
                            with zfm2.ZipFile(fpath, "r") as z:
                                _internal_data = z.read(internal_name)
                        elif fpath.suffix.lower() == ".7z":
                            import py7zr
                            with py7zr.SevenZipFile(fpath, mode="r") as sz:
                                _internal_data = sz.read([internal_name])[internal_name].read()
                        elif fpath.suffix.lower() == ".rar":
                            import rarfile
                            with rarfile.RarFile(fpath) as rf:
                                _internal_data = rf.read(internal_name)
                    except Exception as ie:
                        self._log(f"  ERR reading {fpath.name}/{internal_name}: {ie}", "err")
                        errors += 1
                        continue
                    if match_by == "sha1":
                        file_hash = hashlib.sha1(_internal_data).hexdigest().lower()
                    elif match_by == "md5":
                        file_hash = hashlib.md5(_internal_data).hexdigest().lower()
                    else:
                        file_hash = format(zlib.crc32(_internal_data) & 0xFFFFFFFF, "08X")
                else:
                    # Hash whole file
                    if match_by == "sha1":
                        h = hashlib.sha1()
                        with open(fpath, "rb") as f:
                            for chunk in iter(lambda: f.read(65536), b""):
                                h.update(chunk)
                        file_hash = h.hexdigest().lower()
                    elif match_by == "md5":
                        h = hashlib.md5()
                        with open(fpath, "rb") as f:
                            for chunk in iter(lambda: f.read(65536), b""):
                                h.update(chunk)
                        file_hash = h.hexdigest().lower()
                    else:  # crc
                        crc = 0
                        with open(fpath, "rb") as f:
                            for chunk in iter(lambda: f.read(65536), b""):
                                crc = zlib.crc32(chunk, crc)
                        file_hash = format(crc & 0xFFFFFFFF, "08X")

                if i < 3:
                    self._log(f"  File hash: {fpath.name} = {file_hash}", "dim")
                if file_hash in hash_index:
                    game_name, rom_name, file_dest = hash_index[file_hash]
                    # Move to dest/game_name/rom_name
                    # rom_name may contain subfolders (e.g. A/de/rom.zip)
                    # Rebuild exact folder structure under file_dest (DAT named subdir)
                    if rom_name and '/' in rom_name:
                        dest_file = file_dest / Path(rom_name)
                    else:
                        game_dir = file_dest / game_name
                        dest_file = game_dir / (rom_name or fpath.name)
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    # Check if an identical file already exists at dest
                    # Use size + CRC comparison — avoids rebuilding same file twice
                    already_exists = False
                    if dest_file.exists():
                        try:
                            # Get the ROM size from hash_index for quick size check
                            dest_size = dest_file.stat().st_size
                            src_size  = fpath.stat().st_size
                            if dest_size == src_size:
                                # Same size — verify by CRC
                                existing_crc = 0
                                with open(dest_file, "rb") as _ef:
                                    for _chunk in iter(lambda: _ef.read(65536), b""):
                                        existing_crc = zlib.crc32(_chunk, existing_crc)
                                existing_crc_str = format(existing_crc & 0xFFFFFFFF, "08X")
                                src_crc = 0
                                with open(fpath, "rb") as _sf:
                                    for _chunk in iter(lambda: _sf.read(65536), b""):
                                        src_crc = zlib.crc32(_chunk, src_crc)
                                src_crc_str = format(src_crc & 0xFFFFFFFF, "08X")
                                if existing_crc_str == src_crc_str:
                                    already_exists = True
                                else:
                                    # Different file — rename
                                    stem, sfx = os.path.splitext(dest_file.name)
                                    n = 1
                                    while dest_file.exists():
                                        dest_file = dest_file.parent / f"{stem}_{n}{sfx}"
                                        n += 1
                            else:
                                # Different size — rename
                                stem, sfx = os.path.splitext(dest_file.name)
                                n = 1
                                while dest_file.exists():
                                    dest_file = dest_file.parent / f"{stem}_{n}{sfx}"
                                    n += 1
                        except Exception:
                            pass

                    if already_exists:
                        # File already correctly rebuilt — just delete source if needed
                        if delete_source and not dry_run:
                            try:
                                os.unlink(str(fpath))
                            except Exception:
                                pass
                        matched += 1
                        continue

                    if not dry_run:
                        try:
                            if _internal_data is not None:
                                # Write extracted internal file to destination
                                # Rebuild into a zip named after the source archive
                                import zipfile as _zwf
                                zip_dest = dest_file.parent / (fpath.stem + ".zip")
                                zip_dest.parent.mkdir(parents=True, exist_ok=True)
                                mode = "a" if zip_dest.exists() else "w"
                                with _zwf.ZipFile(zip_dest, mode, 
                                                  compression=_zwf.ZIP_DEFLATED) as zout:
                                    zout.writestr(Path(internal_name).name, _internal_data)
                            elif delete_source:
                                try:
                                    os.rename(str(fpath), str(dest_file))
                                except OSError:
                                    import shutil
                                    shutil.copy2(str(fpath), str(dest_file))
                                    os.unlink(str(fpath))
                            else:
                                import shutil
                                shutil.copy2(str(fpath), str(dest_file))
                        except Exception as move_err:
                            self._log(f"  ERR moving {fpath.name}: {move_err}", "err")


                    matched += 1
                    if matched % 100 == 0:
                        self._log(f"  Matched {matched} files so far...", "dim")
                else:
                    unmatched += 1

            except Exception as e:
                errors += 1
                self._log(f"  ERR: {fpath.name}: {e}", "err")

        self._progress(100, "Complete")
        if dry_run:
            action = "Would match"
        elif delete_source:
            action = "Moved"
        else:
            action = "Copied"

        # Clean up empty folders in source
        if cleanup_empty and not dry_run and delete_source:
            self._progress(98, "Cleaning empty folders...")
            removed_dirs = 0
            for source in source_folders:
                if not source.is_dir(): continue
                for root, dirs, files in os.walk(str(source), topdown=False):
                    rp = Path(root)
                    if rp == source: continue
                    try:
                        if not any(rp.iterdir()):
                            rp.rmdir()
                            removed_dirs += 1
                    except Exception:
                        pass
            if removed_dirs:
                self._log(f"  Removed {removed_dirs} empty folder(s) from source.", "dim")

        self._log("", "")
        self._log(f"Rebuild done — {action} {matched} matched, {unmatched} unmatched, {errors} errors.", "ok")
        self._running = False
        self._emit("done", {"success": True, "stats": {
            "total_input": len(source_files), "duplicates_removed": unmatched,
            "total_output": matched,
        }})

    def _hash_archive_contents(self, fpath, verbose=False) -> list:
        """Open an archive and hash each file inside. Returns list of rom dicts."""
        import hashlib as _hl, zlib as _zl, zipfile, tempfile, subprocess, os as _os
        roms = []
        suf = fpath.suffix.lower()

        def hash_bytes(data):
            crc = _zl.crc32(data) & 0xFFFFFFFF
            return {
                'crc': format(crc, '08X'),
                'size': str(len(data)),
                'sha1': _hl.sha1(data).hexdigest(),
            }

        try:
            if suf == '.zip':
                zf_mod = zipfile
                try:
                    import zipfile_zstd as zf_mod
                except ImportError:
                    pass
                with zf_mod.ZipFile(fpath, 'r') as zf:
                    for name in zf.namelist():
                        info = zf.getinfo(name)
                        if info.is_dir(): continue
                        data = zf.read(name)
                        h = hash_bytes(data)
                        roms.append({'name': Path(name).name, **h})
                        if verbose:
                            self._log(f"      {Path(name).name}  crc={h['crc']}", 'dim')
            elif suf == '.7z':
                try:
                    import py7zr
                    with py7zr.SevenZipFile(fpath, mode='r') as sz:
                        for name, bio in sz.readall().items():
                            data = bio.read()
                            h = hash_bytes(data)
                            roms.append({'name': Path(name).name, **h})
                except ImportError:
                    pass
            elif suf == '.rar':
                _unrar = _find_tool(['UnRAR.exe','unrar.exe','UnRAR','unrar'])
                if _unrar:
                    with tempfile.TemporaryDirectory() as tmp:
                        subprocess.run(
                            [_unrar, 'x', '-inul', '-y', str(fpath), tmp+_os.sep],
                            capture_output=True
                        )
                        for root, dirs, files in _os.walk(tmp):
                            for fn in files:
                                fp2 = Path(root)/fn
                                data = fp2.read_bytes()
                                h = hash_bytes(data)
                                roms.append({'name': fn, **h})
        except Exception as e:
            self._log(f"  WARN: could not scan {fpath.name}: {e}", 'warn')
        return roms

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

