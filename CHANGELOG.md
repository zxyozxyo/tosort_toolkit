# ToSort Toolkit — Changelog

All notable changes to this project are documented here.
This changelog was reconstructed from the full build history.

---

## v1.0 — Current Release (2025)
**This is the first formally versioned release. All prior work is documented below as build history.**

### Full Feature Set
- **Module 1: Archive Extractor** — .zip, .rar, .7z, .zst, .iso, .chd, .dms
- **Module 2: File Sorter + ROM Sorter** — single scan, two destinations
- **Module 3: Empty Folder Cleanup** — multi-folder, smart recursive delete
- **Module 4: Duplicate File Finder** — multi-folder, two-phase hash dedupe
- **DAT Merger & Deduper** — separate window, CLRMamePro/Logiqx XML parser
- **Watch Mode** — timed auto-run with configurable interval
- **Persistent Settings** — saved/restored on close/open
- **Pipeline Summary Reports** — timestamped .txt after each run

---

## Build History (Pre-v1.0)

### Build 1 — Initial GUI Shell
- Created PyWebView application with dark industrial theme
- Module 1 (Extractor) fully simulated in the interactive preview
- All 4 module panels with config fields and progress bars
- START / STOP buttons with skip-to-next and terminate-all options
- Verbose toggle and timestamped colour-coded log output
- Sidebar module enable/disable checkboxes

### Build 2 — PyWebView Desktop App
- Converted from inline chat widget to standalone desktop application
- Python backend (api.py) with PyWebView js_api bridge
- Module 1 Archive Extractor fully implemented in Python
- Supports .zip, .rar, .7z, .zst (ZSTD) extraction
- Nested archive detection — moves archives-within-archives to separate folder
- Native folder browse dialogs
- Real progress reporting and log output

### Build 3 — Panel Sizing Fix
- Fixed module panels being clipped — panels now auto-size to content
- Main panel area scrolls vertically
- Footer pinned to bottom regardless of scroll

### Build 4 — Browse Button Fix
- Fixed `webview` import missing in api.py (NameError on browse)
- Added `import webview` to api.py top-level imports

### Build 5 — Module 2: File Sorter
- Full file sorter implementation integrated from user's existing script
- Letter subfolder structure: A/ark/, D/d64/, MISC/#3/ etc.
- Non-alpha extensions routed to MISC/ folder
- 10,000 file cap per folder with automatic spillover (_1, _2 etc.)
- Destination folder recounted on each startup for accuracy across sessions
- Name collision handling — append _1, _2 etc.
- GUI panel updated with structure info and green status

### Build 6 — All Modules + Watch + Settings
- **Module 3: Empty Folder Deleter** — recursive bottom-up, multi-pass
- **Module 4: Duplicate Finder** — user's two-phase dedupe script integrated
  - Size grouping → partial MD5 (1MB) → full MD5 confirmation
  - ThreadPoolExecutor parallelism
  - Progress file for resume across interrupted runs
- **Persistent Settings** — settings.json auto-saved on every change
- **Watch Mode** — configurable interval timer, arm/disarm, countdown display
- Dependency check on startup (warns about missing py7zr, rarfile, zstandard)
- Log text selection enabled (user-select: text)

### Build 7 — RAR Auto-Detection
- Auto-detects UnRAR.exe in common Windows locations
  - C:\Program Files\WinRAR\
  - Next to main.py (drop-in)
- Clear actionable error messages when UnRAR binary not found
- Startup dependency status reporting

### Build 8 — Module 5: ROM Sorter + Multi-Folder
- **Module 5: ROM Sorter** with 142 known ROM/emulator extensions
- Separate destination for ROM files (_RomVault_Sorted)
- **Multi-folder support** for Module 3 (Empty Folders) and Module 4 (Dupes)
  - Add/Remove folder rows in GUI
  - All folders processed in sequence
- Settings persistence updated for new fields

### Build 9 — Merged Sorter (Module 2 + 5 Combined)
- Combined file sorter and ROM sorter into single Module 2
- Single scan, two destinations — general output + ROM output
- "Enable ROM Sorting" toggle to disable ROM splitting
- Companion files (.cue, .m3u, .xml etc.) routed to ROM output
- Module 5 removed from sidebar — back to 4 modules
- Log shows [ROM], [COMP], [GEN] tags per file in verbose mode

### Build 10 — Sidebar Layout Fix
- Fixed stray </div> breaking sidebar layout
- Verified div balance across all panels

### Build 11 — Bad Archives + Summary Report
- **Bad archive handling** — corrupt and password-protected archives moved to _BadArchives/
- Configurable Bad Archive Dest folder picker in Module 1
- Detection of CRC errors, corrupt headers, and password encryption
- **Pipeline Summary Report** — timestamped .txt saved after every run
  - Per-module stats, timings, error counts, source/dest paths
  - Configurable report output folder

### Build 12 — Native RAR Extraction
- **RAR extraction via native UnRAR binary** — subprocess call instead of rarfile
- Massively faster for large archives (whole archive in one native call)
- Fallback to rarfile if binary not found
- Exit code handling for corrupt/password detection

### Build 13 — Speed + Move Optimisations
- **fast_move()** — os.rename first (instant same-drive), copy+delete fallback
- Replaced all shutil.move calls with fast_move
- **ROM extension database expanded** to 348 extensions
  - All TOSEC DAT endings added
  - 63 music/chiptune/VGM formats (SID, AY, NSF, SPC, VGM, MOD etc.)
- Generated ROM_Extensions_Reference.txt (alphabetical with system labels)

### Build 14 — ISO/CHD/DMS Extraction
- **.iso** extraction support (copy as-is)
- **.chd** extraction via chdman.exe (auto-detect CD/HD/DVD, extract to cue/img/iso)
- **.dms** extraction via xdms.exe (Amiga Disk Masher → ADF)
- Tools auto-detected in app folder or PATH, fallback to copy
- **"Never delete" toggles** — keep .iso/.chd/.dms even with delete-after enabled
- New checkboxes in Module 1 panel for each format

### Build 15 — Speed + False Positives + Recursive Extraction
- **Sorter speed** — verbose logging throttled to every 100 files, progress bar every 50
- **False bad archive fix** — only genuinely corrupt/password archives moved to _BadArchives
  - Other extraction failures logged as SKIP, archive left in place
  - Scan-phase errors no longer auto-moved to bad
- **Output folder default fixed** — blank Extract Output now correctly uses Source Folder
- **Recursive Nested Extraction** — new feature in Module 1
  - Extracts repeatedly (up to 10 passes) until no archives remain
  - Moves flattened results to configurable final destination
- unique_path → unique_path_fast reference fix (Module 2 crash fix)

### Build 16 — DAT Merger & Deduper
- **New DAT Merger tool** — separate window accessible via titlebar button
- Parses both CLRMamePro (.dat) and Logiqx XML (.xml/.dat) formats
- Auto-format detection
- Add individual files or scan entire folders recursively
- Merge multiple DATs into one, deduplicate by ROM hash (SHA1/CRC+size)
- Output always CLRMamePro format (.dat) — fully RomVault compatible
- Configurable DAT name, description, author
- Results stats panel (input/dupes removed/final count)
- File list with DAT/XML badges, individual remove, clear all
- Independent window — runs alongside main pipeline
- .gitignore added for GitHub readiness

---

## File Structure
```
tosort_toolkit/
├── main.py                      ← Entry point
├── api.py                       ← Main pipeline backend (1584 lines)
├── dat_merger.py                ← DAT merger backend (522 lines)
├── requirements.txt             ← Python dependencies
├── .gitignore                   ← Git ignore rules
├── README.md                    ← Setup guide
├── ROM_Extensions_Reference.txt ← 348 ROM extensions reference
└── gui/
    ├── index.html               ← Main pipeline GUI
    └── dat_merger.html          ← DAT merger GUI
```
