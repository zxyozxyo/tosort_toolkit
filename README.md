# ToSort Toolkit — RomVault Cleaning Pipeline

A desktop GUI application for keeping your RomVault ToSort folder clean.
Extracts, sorts, deduplicates, and organises ROM files in an automated pipeline,
plus a full DAT file management suite with scene release support.

---

## Quick Start

1. Install Python 3.9+
2. Install required packages:
   ```
   pip install -r requirements.txt
   ```
3. For ZSTD-compressed ZIP support (recommended for modern scene collections):
   ```
   pip install zipfile-zstd
   ```
4. Drop optional tool binaries into the app folder (see below)
5. Run:
   ```
   python main.py
   ```

---

## File Structure

```
tosort_toolkit/
├── main.py                      ← Run this
├── api.py                       ← Main pipeline backend
├── dat_merger.py                ← DAT tools backend
├── requirements.txt             ← Python dependencies
├── .gitignore
├── README.md
├── CHANGELOG.md
├── ROM_Extensions_Reference.txt ← 348 ROM extensions reference
│
├── gui/
│   ├── index.html               ← Main pipeline GUI
│   └── dat_merger.html          ← DAT tools GUI (9 tabs)
│
│   Optional tools — drop these next to main.py:
├── UnRAR.exe     ← RAR extraction + DAT creator archive scanning
│                    Download: https://www.rarlab.com/rar/unrarw64.exe
│                    Or install WinRAR — detected automatically
├── chdman.exe    ← CHD extraction (CUE/BIN/IMG/ISO)
│                    From MAME tools: https://www.mamedev.org/release.html
├── xdms.exe      ← DMS extraction (Amiga Disk Masher → ADF)
│                    Search: "xdms windows binary"
└── 7z.exe        ← Required for split archives (.7z.001, .zip.001)
                     Download: https://www.7-zip.org/
```

---

## Python Dependencies

| Package | Purpose | Required |
|---|---|---|
| pywebview | Desktop GUI window | **Yes** |
| py7zr | .7z extraction and archive scanning | Optional |
| rarfile | .rar extraction fallback (also needs UnRAR.exe) | Optional |
| zstandard | .zst extraction | Optional |
| zipfile-zstd | ZSTD-compressed .zip files (method 93) | **Strongly recommended** |

**Note on ZSTD ZIPs:** Many modern scene/ROM collections use `.zip` files compressed
with Zstandard (method 93). Python 3.13 and earlier do not support this natively.
Install `zipfile-zstd` to enable the DAT Creator to scan inside these archives:
```
pip install zipfile-zstd
```
Without it, ZSTD ZIPs are hashed as whole files (which is correct for scene releases
in archives-as-files mode, but won't work for contents scanning mode).

---

## Main Pipeline Modules

### Module 1 — Archive Extractor
Extracts .zip .rar .7z .zst .iso .chd .dms

- Detects nested archives — moves to configurable folder without extracting
- **Recursive nested extraction** — flattens archives-within-archives up to 10 passes
- Bad/corrupt/password archives → `_BadArchives` folder
- **Never-delete toggles** for .iso .chd .dms originals
- RAR extraction uses native UnRAR binary for speed, falls back to rarfile

### Module 2 — File Sorter + ROM Sorter
Single scan, two destinations.

- **General files** → `A/ark/` `D/d64/` `MISC/#3/` etc.
- **Known ROM files** (348 extensions) → separate ROM output root
- Companion files (.cue .m3u .xml .dat .nfo etc.) → ROM output
- 10,000 file cap per folder with automatic spillover
- ROM sorting toggleable

### Module 3 — Empty Folder Cleanup
Multi-folder. Removes empty directories recursively. Dry run mode.

### Module 4 — Duplicate File Finder & Deleter
Multi-folder deduplication.

- Phase 1: group by file size
- Phase 2: partial MD5 (1MB) → full MD5 confirmation
- Progress saved — interrupted runs resume automatically
- Dry run mode

---

## DAT Tools Window

Click **◆ DAT Tools** in the main titlebar. Runs independently alongside the pipeline.

**Supported input formats:** CLRMamePro (.dat) and Logiqx XML (.xml)
**Output format:** Always CLRMamePro (.dat) — fully RomVault compatible

The streaming parser handles very large DAT files (200MB+) without hanging.

---

### Tab 1 — Merge & Dedupe
Merge multiple DATs, deduplicate by ROM hash (SHA1 preferred, CRC+size fallback).
Add files individually or scan a whole folder recursively.

### Tab 2 — DAT Diff
Set a master (newest) DAT and one or more older DATs.
Output contains only entries in the master that are NOT in any older DAT.
Use case: "What ROMs were added between version X and Y?"

### Tab 3 — Smart Merge
Scan a folder, filter DATs by keyword in name or filename, merge and dedupe matches.
Supports multiple comma-separated keywords: `Sinclair ZX Spectrum, Commodore 64`
Tip: be specific — "Commodore 64" will not match "Commodore Amiga".

### Tab 4 — Extension Merge
Scan a folder, find all DATs containing ROMs with specific extensions, merge them.
Supports multiple comma-separated extensions: `tap, d64, tzx`

### Tab 5 — Internal Dedupe
Load a single DAT, remove duplicate entries within it.
Useful for TOSEC DATs which often have entries with identical hashes.

### Tab 6 — Integrity Check
Scans DATs for problems:
- Duplicate game names
- Entries with no ROMs
- ROMs missing CRC/MD5/SHA1
- ROMs missing size or with size=0
- Parse errors

**Auto-fix option:** strips invalid entries and internal duplicates.
Save fixed copies to a separate folder or overwrite originals.
Optional .txt report.

### Tab 7 — DAT Creator
Scans a folder and generates a RomVault-compatible CLRMamePro DAT.

**How it works — folder-based (scene format):**
Each subfolder becomes one game entry. Every file inside that folder becomes
a ROM entry within that game. This matches the format used by scene DAT sites:

```
game (
    name "2024-01-02-Some_Release_3DS-GROUP"
    rom ( name "group.nfo" size 2904 crc 891CFD55 sha1 ... )
    rom ( name "release.zip" size 851755 crc 3DB29116 sha1 ... )
    rom ( name "file_id.diz" size 220 crc 42CEF5E1 sha1 ... )
)
```

The output DAT includes `forcepacking "unzip"` in the header, which tells
RomVault not to repack files into archives during rebuilding.

**Options:**
- Scan subfolders (recursive) — on by default
- Include SHA1 — recommended for RomVault compatibility
- Include MD5 — optional additional hash

**CRC32** is always included. Every file is hashed as a complete file
(not scanned internally) — correct for scene releases where you want to
track and rebuild the actual archive files themselves.

**Important — RomVault scan mode for ZIP files:**
When using this DAT with RomVault to rebuild scene ZIP releases, set the
RomVault folder scan mode to **"File Only"**. This tells RomVault to match
files by their whole-file CRC rather than trying to open archives and match
internal contents. Can be set per-folder or globally in RomVault settings.
RAR files work in normal scan mode. NFO/SFV files work in both modes.

### Tab 8 — Rebuilder
Loads DATs as reference, scans a source folder, matches files by hash,
moves or copies matched files to destination organised by game name.

**Options:**
- Match by CRC32 (fastest), SHA1, or MD5
- Dry run — list matches without moving
- Remove source after rebuild (move) or leave source intact (copy)

Folder structure from the DAT is preserved in the destination.

### Tab 9 — Size Calculator
Totals ROM sizes declared in DAT entries.
Add individual DATs or scan a whole folder.
Per-DAT breakdown and grand total. Optional .txt report.

---

## Multi-Part Archive Support (DAT Creator)

The DAT Creator correctly handles all scene multi-part archive formats.
In archives-as-files mode, **every file gets its own DAT entry** including
all continuation parts — this is the correct behaviour for scene releases.

| Format | First part | Continuations |
|---|---|---|
| Old-style RAR | `.rar` | `.r00`-`.r99` |
| New-style RAR | `.part1.rar` | `.part2.rar`+ |
| Extended RAR | `.rar` | `.s00-.s99`, `.t00-.t99`, `.u00-.u99` |
| Numeric | `.000` | `.001`-`.999` |
| Split 7z | `.7z.001` | `.7z.002`+ (needs 7z.exe) |
| Split zip | `.zip.001` | `.zip.002`+ (needs 7z.exe) |

Note: continuation skipping only applies in **scan inside archives** mode,
where only the first part needs to be opened to read contents.
In **archives as files** mode (default), every file is hashed individually.

---

## Other Features

- **Watch Mode** — auto-run pipeline on a configurable timer
- **Persistent Settings** — all paths and options saved between sessions
- **Settings Export/Import** — backup settings to JSON, restore on another machine
- **Pipeline Summary Reports** — timestamped .txt report after every run
- **Verbose toggle** — full per-file detail or clean summary
- **Start/Stop** — skip to next module or terminate everything
- **Startup dependency check** — warns about missing libraries/tools
- **Drag and drop** — drop DAT/XML files onto file lists in DAT Tools

---

## Settings & Data Files

| File | Purpose |
|---|---|
| `settings.json` | Auto-saved UI settings (excluded from git) |
| `dedupe_progress.json` | Resume file for interrupted dedupe runs |
| `tosort_report_*.txt` | Pipeline summary reports |
