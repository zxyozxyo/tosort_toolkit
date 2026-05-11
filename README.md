# ToSort Toolkit — RomVault Cleaning Pipeline

A desktop GUI application for keeping your RomVault ToSort folder clean.
Extracts, sorts, deduplicates, and organises ROM files in an automated pipeline,
plus a full DAT file management suite.

---

## Quick Start

1. Install Python 3.9+
2. `pip install -r requirements.txt`
3. Drop optional tools into the app folder (see below)
4. `python main.py`

---

## File Structure

```
tosort_toolkit/
├── main.py                      ← Run this
├── api.py                       ← Main pipeline backend
├── dat_merger.py                ← DAT tools backend
├── requirements.txt
├── .gitignore
├── README.md
├── CHANGELOG.md
├── ROM_Extensions_Reference.txt ← 348 ROM extensions reference
│
├── gui/
│   ├── index.html               ← Main pipeline GUI
│   └── dat_merger.html          ← DAT tools GUI
│
│   Optional tools — drop these next to main.py:
├── UnRAR.exe     ← Fast .rar extraction (from rarlab.com)
├── chdman.exe    ← .chd extraction — converts to CUE/BIN/IMG/ISO
│                    Get from: https://www.mamedev.org/release.html
└── xdms.exe      ← .dms extraction — Amiga Disk Masher → ADF
                     Search: "xdms windows binary"
```

If any optional tool is missing the app still works — it warns at startup
and falls back to copying the file as-is.

---

## Main Pipeline Modules

### Module 1 — Archive Extractor
Extracts .zip .rar .7z .zst .iso .chd .dms

- Detects nested archives (archives inside archives) — moves them to a
  separate configurable folder without extracting
- **Recursive nested extraction** — point at the nested archive folder,
  extracts repeatedly (up to 10 passes) until fully flat, moves results
  to a final destination
- Bad/corrupt/password-protected archives → moved to _BadArchives folder
- **Never-delete toggles** for .iso .chd .dms — keeps originals even if
  "Delete after extract" is on
- RAR extraction uses native UnRAR binary for speed (requires UnRAR.exe)

### Module 2 — File Sorter + ROM Sorter
Single scan of source folder, two destinations.

- **General files** → `A/ark/` `D/d64/` `MISC/#3/` etc.
- **Known ROM files** (348 extensions) → separate ROM output root
- Companion files (.cue .m3u .xml .dat .nfo etc.) → ROM output
- 10,000 file cap per folder, spills to `jpg_1/` `jpg_2/` etc.
- Destination recounted on each run — caps respected across sessions
- ROM sorting can be toggled off (sorts everything to general output)

### Module 3 — Empty Folder Cleanup
Multi-folder support. Removes empty directories.

- Smart detection: whole empty subtrees deleted instantly (rmtree)
- Only recurses into folders that contain files somewhere inside
- Dry run mode

### Module 4 — Duplicate File Finder & Deleter
Multi-folder support — finds dupes across all configured folders.

- Phase 1: group by file size
- Phase 2: partial MD5 (1 MB) → full MD5 confirmation
- Progress saved after each size group — interrupted runs resume
- Dry run mode

---

## DAT Tools Window

Click **◆ DAT Tools** in the main titlebar to open the DAT tools window.
Runs independently — you can use it while the main pipeline is running.

Supports input formats: **CLRMamePro (.dat)** and **Logiqx XML (.xml)**
Output format: always **CLRMamePro (.dat)** — fully RomVault compatible

### Tab 1 — Merge & Dedupe
Merge multiple DATs into one, optionally deduplicating by ROM hash.
Add files individually or scan an entire folder.

### Tab 2 — DAT Diff
Compares a master (newest) DAT against one or more older DATs.
Output contains only entries that exist in the master but NOT in any older DAT.
Use case: "What ROMs were added between version X and version Y?"

### Tab 3 — Smart Merge (by keyword)
Scans a folder, filters DATs whose name or filename contains a keyword
(e.g. "Commodore 64"), merges and dedupes only those matches.
Tip: be specific — "Commodore 64" won't match "Commodore Amiga".

### Tab 4 — Extension Merge
Scans a folder, finds all DATs containing ROMs with a specific extension
(e.g. "tap"), merges all matching DATs into one.

### Tab 5 — Internal Dedupe
Load a single DAT and remove duplicate entries within it.
Useful for TOSEC DATs which often contain entries with identical hashes
but different names.

### Tab 6 — Integrity Check
Scans DATs for problems:
- Duplicate game names within a single DAT
- Entries with no ROMs
- ROMs missing CRC/MD5/SHA1 hash
- ROMs missing size attribute
- ROMs with size = 0
- Parse errors

**Auto-fix option**: automatically removes invalid entries and internal
duplicates. Can overwrite originals or save fixed copies to a separate folder.
Optional .txt report.

### Tab 7 — DAT Creator
Point at a folder, hash every file, generate a CLRMamePro DAT.

**Two modes:**
- **Archives as files** (default): hashes the .zip/.7z itself as a single ROM entry.
  Fast. Use when you want to track archives as whole units.
- **Archive contents**: opens each archive and hashes every file inside
  individually, one ROM entry per file. Use when RomVault needs to rebuild
  archives from the DAT — the rebuilder will expect to reconstruct the
  archive from matched individual files.

CRC32 always included. SHA1 recommended for RomVault compatibility.

### Tab 8 — Rebuilder
Loads one or more DATs as reference, scans a source folder, matches files
by hash, and moves/copies matched files to a destination organised by game name.
Unmatched files are left in place.

Options:
- **Match by**: CRC32 (fastest), SHA1 (most accurate), MD5
- **Dry run**: list matches without moving anything
- **Remove source after rebuild**: move matched files (delete source).
  Unticked = copy only, source files left intact.

Works with both loose files and archive contents when archive contents
mode was used during DAT creation.

### Tab 9 — Size Calculator
Reads ROM sizes from DAT entries and totals them.
Add individual DATs or scan a whole folder.
Shows per-DAT breakdown and grand total.
Optional .txt report export.

---

## Other Features

- **Watch Mode** — auto-run the pipeline on a configurable timer
- **Persistent Settings** — all paths and options saved/restored between sessions
- **Settings Export/Import** — save settings to JSON, import on another machine
- **Pipeline Summary Reports** — timestamped .txt report after every run
- **Verbose toggle** — full per-file detail or clean summary
- **Start/Stop** — skip to next module or terminate everything
- **Startup dependency check** — warns about missing optional libraries/tools
- **Drag and drop** — drop DAT/XML files directly onto file lists in DAT Tools

---

## Python Dependencies

```
pip install -r requirements.txt
```

| Package | Purpose | Required |
|---|---|---|
| pywebview | Desktop GUI window | Yes |
| py7zr | .7z extraction and DAT creator archive scan | Optional |
| rarfile | .rar extraction (also needs UnRAR.exe) | Optional |
| zstandard | .zst extraction | Optional |

---

## Settings & Data Files

| File | Purpose |
|---|---|
| `settings.json` | Auto-saved UI settings (paths, options) |
| `dedupe_progress.json` | Resume file for interrupted dedupe runs |
| `tosort_report_*.txt` | Pipeline summary reports |

These are excluded from git via .gitignore.
