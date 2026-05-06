# ToSort Toolkit — RomVault Cleaning Pipeline

A desktop GUI application for keeping your RomVault ToSort folder clean.
Extracts, sorts, deduplicates, and organises ROM files in an automated pipeline.

## File Structure

```
tosort_toolkit/
├── main.py                      ← Run this to start the app
├── api.py                       ← Main pipeline backend
├── dat_merger.py                ← DAT merger/deduper backend
├── requirements.txt             ← Python dependencies
├── .gitignore
├── README.md
├── CHANGELOG.md                 ← Full version history
├── ROM_Extensions_Reference.txt ← 348 recognised ROM extensions
├── gui/
│   ├── index.html               ← Main pipeline GUI
│   └── dat_merger.html          ← DAT merger GUI
│
│  Optional tools (drop into this folder):
├── UnRAR.exe                    ← For fast RAR extraction
├── chdman.exe                   ← For CHD extraction (from MAME tools)
└── xdms.exe                     ← For DMS extraction (Amiga Disk Masher)
```

## Setup

1. **Install Python 3.9+** if you haven't already.

2. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```

3. **Optional tools** — drop these executables into the `tosort_toolkit` folder
   (next to `main.py`) for full format support:

   - **UnRAR.exe** — Required for .rar extraction.
     Download from: https://www.rarlab.com/rar/unrarw64.exe
     Or just install WinRAR and it will be detected automatically.

   - **chdman.exe** — Required for .chd extraction (converts CHD back to
     CUE/BIN, IMG, or ISO depending on disc type).
     Download from MAME tools: https://www.mamedev.org/release.html
     (included in the full MAME package, or search for standalone chdman builds)

   - **xdms.exe** — Required for .dms extraction (Amiga Disk Masher → ADF).
     Search for "xdms windows binary" — it's a small standalone tool.

   If any tool is missing, the app will still work — it just copies those
   formats as-is instead of extracting them, and warns you at startup.

4. **Run the app:**
   ```
   python main.py
   ```

## Modules

### Module 1 — Archive Extractor
Extracts .zip, .rar, .7z, .zst, .iso, .chd, and .dms archives.
- Detects and moves nested archives (archives inside archives) to a separate folder
- Recursive nested extraction — fully flatten archives-within-archives
- Bad/corrupt/password-protected archives moved to configurable _BadArchives folder
- "Never delete" toggles for .iso, .chd, .dms originals
- RAR extraction uses native UnRAR binary for speed

### Module 2 — File Sorter + ROM Sorter
Single scan, two destinations. Files sorted by extension into lettered subfolders.
- General files → `A/ark/`, `D/d64/`, `MISC/#3/` etc.
- Known ROM files (348 extensions) → separate ROM output folder
- 10,000 file cap per folder with automatic spillover
- Destination folders recounted on each run
- ROM sorting can be toggled on/off
- Companion files (.cue, .m3u, .xml etc.) routed alongside ROMs

### Module 3 — Empty Folder Cleanup
Recursively removes empty directories. Multi-folder support.
- Smart detection: empty subtrees deleted instantly via rmtree
- Only recurses into folders that contain files somewhere inside

### Module 4 — Duplicate File Finder & Deleter
Two-phase deduplication across multiple folders.
- Phase 1: Group by file size
- Phase 2: Partial MD5 (1MB) then full MD5 confirmation
- Progress saved after each size group — interrupted runs resume automatically
- Dry run mode to list duplicates without deleting

### DAT Merger & Deduper
Separate window (click ◆ DAT Merger button in titlebar).
- Parses both CLRMamePro (.dat) and Logiqx XML (.xml) formats
- Merge multiple DATs into one
- Deduplicate by ROM hash (SHA1 preferred, CRC+size fallback)
- Output is always CLRMamePro format — fully RomVault compatible
- Configurable DAT name, description, author

## Other Features

- **Watch Mode** — auto-run the pipeline on a timer (5min to 2hrs, or custom)
- **Persistent Settings** — all folder paths and options saved/restored between sessions
- **Pipeline Summary Reports** — timestamped .txt report after every run
- **Verbose/quiet toggle** — full per-file detail or clean summary only
- **Start/Stop controls** — skip to next module or terminate everything
- **Startup dependency check** — warns about missing optional libraries

## Dependencies

Required:
- pywebview >= 4.4

Optional (for full format support):
- py7zr >= 0.20 (for .7z extraction)
- rarfile >= 4.1 (for .rar — also needs UnRAR.exe)
- zstandard >= 0.21 (for .zst extraction)
