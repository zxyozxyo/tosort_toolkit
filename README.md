# ToSort Toolkit

A PyWebView desktop application for cleaning and managing ROM collections with RomVault, managing DAT files, and uploading collections to the Internet Archive.

---

## Requirements

### Python
Python 3.10 or later recommended.

### Python Libraries
Install all required libraries with:

```
pip install pywebview py7zr rarfile zstandard internetarchive requests
```

| Library | Purpose |
|---|---|
| `pywebview` | Desktop GUI framework |
| `py7zr` | 7z archive extraction |
| `rarfile` | RAR archive extraction |
| `zstandard` | ZSTD compressed file support |
| `internetarchive` | Internet Archive upload API |
| `requests` | HTTP for IA uploads |

### Optional Tool Binaries
Place these in the same folder as `main.py`:

| File | Purpose | Where to get |
|---|---|---|
| `UnRAR.exe` | RAR extraction (required for RAR files) | rarlab.com |
| `7z.exe` | 7z extraction + RAR/ZIP creation for prepper | 7-zip.org |
| `7za.exe` | Standalone 7z (alternative to 7z.exe) | 7-zip.org |
| `7zr.exe` | Minimal 7z (handles .7z only, cannot create RAR) | 7-zip.org |
| `rar.exe` | Native RAR creation for prepper | rarlab.com (WinRAR) |
| `chdman.exe` | CHD file handling | MAME project |
| `xdms.exe` | Amiga DMS extraction | Various Amiga sources |

**Note:** For the IA Prepper RAR output, you need either `rar.exe` or `7z.exe`/`7za.exe`. `7zr.exe` alone cannot create RAR archives.

---

## Running

```
python main.py
```

---

## Windows Files (gitignored)
These files are gitignored and must be added manually to the app folder:

```
settings.json               # Auto-saved settings
ia_credentials.json         # IA S3 API keys
tosort_settings_export.json # Exported settings backup
7zr.exe
UnRAR.exe
chdman.exe
xdms.exe
7z.exe
7za.exe
rar.exe
```

---

## Features

### Main Pipeline (index.html)

**Module 1 — Archive Extractor**
Recursively extracts archives from a source folder.
- Supported formats: ZIP, RAR, 7z, ZSTD, GZ/TAR.GZ, TGZ, TAR, ISO, CHD, DMS
- Nested archive detection and extraction
- Bad archive → `_BadArchives` folder
- Password-protected archives → configurable `_Passworded` folder (falls back to `_BadArchives`)
- Multi-part RAR support (.part01.rar or .rar + .r00 style)
- ZSTD ZIP support via 7z.exe fallback
- BCJ2/complex 7z filter support via 7z.exe fallback

**Module 2 — File Sorter**
Sorts extracted files into destination buckets by extension.
- Single destination or two-destination mode (General + ROM)
- ROM extension awareness
- Skips archives (left for extractor)
- MAME file awareness (.u01, .s00 etc. not treated as RAR parts unless .rar sibling exists)
- Per-folder progress logging
- Skip recount option for faster starts on large destinations

**Watch Mode**
Monitors source folder and runs pipeline automatically on new files.

**Settings**
- Export settings to `tosort_settings_export.json` in app folder
- Import settings from JSON file
- Auto-saves to `settings.json`

---

### DAT Tools (dat_merger.html)
Nine-tab DAT management suite:
- DAT Merger — combine multiple DAT files
- DAT Splitter — split DATs by platform/region
- DAT Cleaner — remove unwanted entries
- DAT Rebuilder — rebuild ROM sets from DATs
- DAT Creator (File-based) — create DATs from files
- DAT Creator (Folder-based) — create scene-format DATs from folder structure
- Header Editor — edit DAT metadata
- Diff Tool — compare two DATs
- Batch Rename — rename files by DAT

---

### Internet Archive Uploader (ia_uploader.html)
Upload collections to archive.org.

**Setup**
1. Get your S3 API keys from: `archive.org/account/s3.php`
2. Enter keys in the Credentials panel and click Save

**Pre-process Panel**
Groups loose archives into letter-named RAR/ZIP files before upload — ideal for large collections like TOSEC where thousands of loose files would stall IA folders.
- Source folder → recursively finds leaf folders containing archives
- Groups by first character: A-Z, 0-9, MISC
- Splits into A, A2, A3 etc. if group exceeds size limit
- Output format: RAR (requires rar.exe or 7z.exe) or ZIP
- Copy mode: mirrors structure to destination, originals untouched
- Move mode: works in-place, originals deleted after archiving

**Upload Panel**
- Upload to new or existing IA items
- Fetch existing metadata with the Fetch button
- 1-12 concurrent upload threads
- Skip detection: pre-fetches IA file list, skips already-uploaded files (by filename)
- Stall detection: aborts files with no progress for 30 seconds
- Rate limit handling: auto-waits 60s and retries on 503/429
- Bucket deleted detection: stops upload if IA removes the item
- Stop options: graceful (finish current) or instant (abort all)
- Live thread count adjustment mid-upload

**IA Upload Tips**
- IA throttles each connection to ~500 KB/s — use multiple threads for better throughput
- New items: start with 1 thread to avoid rate limiting during item creation
- Existing items: 4-8 threads typically gives best throughput
- A VPN can improve per-connection speeds from some regions
- Queue derive OFF recommended for ROM/archive collections

---

## Internet Archive Identifier Rules
- 5-100 characters
- Letters, numbers, dots, hyphens, underscores only
- Must start with a letter or number
- Must be globally unique on archive.org
- Check availability: `archive.org/details/your-identifier`

---

## Themes
Five themes available via the dropdown in the main titlebar:
Dark (default), Amber, Slate Blue, Red, Light

Theme preference is saved to localStorage and restored on next launch including DAT Tools and IA Uploader windows.

---

## Architecture

```
main.py              Entry point, opens all windows
api.py               Main pipeline backend
dat_merger.py        DAT tools backend  
ia_uploader.py       IA uploader backend
ia_prepper.py        IA pre-processor backend
gui/index.html       Main pipeline GUI
gui/dat_merger.html  DAT tools GUI
gui/ia_uploader.html IA uploader + pre-processor GUI
```

---

## Changelog highlights
- Multi-part RAR extraction and cleanup
- ZSTD ZIP and BCJ2 7z fallback via 7z.exe
- DMS extraction with multiple argument patterns
- MAME file detection (.u01/.s00 etc.)
- IA uploader with concurrent uploads and progress tracking
- IA pre-processor for grouping loose archives
- Password archive folder separation
- Skip recount toggle for faster sorter starts
