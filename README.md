# ToSort Toolkit

A PyWebView desktop application for cleaning and managing ROM collections with RomVault, managing DAT files, and uploading collections to the Internet Archive.

---

## Requirements

### Python
Python 3.10 or later recommended.

### Python Libraries
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

---

## Folder Structure

```
tosort_toolkit/
├── main.py               # Entry point
├── api.py                # Main pipeline backend
├── dat_merger.py         # DAT tools backend
├── ia_uploader.py        # IA uploader backend
├── ia_prepper.py         # IA archive pre-processor
├── ia_folder_packer.py   # IA folder packer
├── rclone_gui.py         # RClone uploader GUI
├── scene_recreator.py    # Scene ZIP recreator/repair tool
├── gui/                  # HTML frontends
│   ├── index.html
│   ├── dat_merger.html
│   ├── ia_uploader.html
│   ├── ia_folder_packer.html
│   ├── rclone_gui.html
│   └── scene_recreator.html
├── apps/                 # Drop tool binaries here (gitignored)
│   ├── rar.exe
│   ├── 7z.exe
│   ├── 7za.exe
│   ├── UnRAR.exe
│   ├── chdman.exe
│   └── xdms.exe
└── rclone/               # Drop rclone files here (gitignored)
    ├── rclone.exe
    └── rclone.conf       # Auto-created when saving credentials
```

---

## Tool Binaries

Place binaries in the `apps/` subfolder (created manually). All scripts search `apps/` automatically.

| File | Purpose | Where to get |
|---|---|---|
| `rar.exe` | RAR creation (required for RAR output) | rarlab.com |
| `UnRAR.exe` | RAR extraction | rarlab.com |
| `7z.exe` | 7z/ZIP extraction and creation | 7-zip.org |
| `7za.exe` | Standalone 7z (alternative) | 7-zip.org |
| `7zr.exe` | Minimal 7z (.7z only, cannot create RAR) | 7-zip.org |
| `chdman.exe` | CHD file handling | MAME project |
| `xdms.exe` | Amiga DMS extraction | Various Amiga sources |

**Notes:**
- For RAR output in the IA pre-processors, `rar.exe` is preferred. `7z.exe` can create RAR but requires the optional RAR plugin (most builds don't include it).
- `7zr.exe` alone cannot create RAR archives — use `7z.exe` or `rar.exe`.

---

## RClone Setup

Place `rclone.exe` in the `rclone/` subfolder (created manually). The `rclone.conf` file is auto-created there when you save credentials in the RClone GUI.

---

## Running

```
python main.py
```

---

## Gitignored Files
These are never committed and must be set up locally:

```
apps/                     # All tool binaries
rclone/                   # rclone.exe and rclone.conf
settings.json             # Auto-saved pipeline settings
ia_credentials.json       # IA S3 keys for Python uploader
ia_folder_packer.json     # Folder packer saved settings
tosort_settings_export.json
```

---

## Features

### Main Pipeline (index.html)

**Module 1 — Archive Extractor**
Recursively extracts archives from a source folder.
- Supported: ZIP, RAR, 7z, ZSTD, GZ/TAR.GZ, TGZ, TAR, ISO, CHD, DMS
- Nested archive detection
- Bad archives → `_BadArchives` folder
- Password archives → configurable `_Passworded` folder
- Multi-part RAR support
- ZSTD ZIP and BCJ2/complex 7z via 7z.exe fallback

**Module 2 — File Sorter**
Sorts extracted files into destination buckets by extension.
- Single or two-destination mode (General + ROM)
- ROM extension awareness
- MAME file detection
- Per-folder progress logging
- Skip recount option for faster starts

**Watch Mode**
Monitors source folder and runs pipeline automatically on new files.

**Settings**
- Export to `tosort_settings_export.json`
- Import from JSON file
- Auto-saves to `settings.json`

---

### DAT Tools (dat_merger.html)
Nine-tab DAT management suite: Merger, Splitter, Cleaner, Rebuilder, DAT Creator (file-based), DAT Creator (folder-based), Header Editor, Diff Tool, Batch Rename.

---

### Internet Archive Uploader (ia_uploader.html)
Upload collections directly to archive.org using the IA S3 API.

**Setup:** Get S3 keys from `archive.org/account/s3.php` and enter in the Credentials panel.

**Features:**
- 1–12 concurrent upload threads
- Skip detection (pre-fetches IA file list)
- Stall detection (30s no-progress abort)
- Rate limit handling (auto-retry on 503/429)
- Graceful or instant stop
- Live thread count adjustment

**IA Pre-processor (within IA Uploader)**
Groups loose archives into letter-named RAR/ZIP files before upload — ideal for large TOSEC sets.
- Groups by first character: A–Z, 0–9, MISC
- Splits into `A`, `A_2`, `A_3` etc. when group exceeds size limit
- Copy mode or move mode
- RAR or ZIP output
- Full Unicode filename support (ø, ü, accented chars etc.)
- Network share safe (list files written to local temp)

---

### IA Folder Packer (ia_folder_packer.html)
Packs each leaf folder containing archives into a single archive.

Intelligently finds the deepest folder containing archives and packs the whole folder as one file, preserving relative structure.

**Example:**
```
TOSEC/Commodore/C64/Games/[D64]/  →  [D64].rar
TOSEC/Commodore/[D64]/            →  [D64].rar
```

- Copy mode: output mirrors structure under destination, originals untouched
- Move mode: archives created alongside source folder, originals deleted
- RAR or ZIP output
- Unicode filename support
- Network share safe

---

### RClone IA Uploader (rclone_gui.html)
Standalone rclone wrapper for IA uploads. Accessible from the main titlebar or run independently with `python rclone_gui.py`.

**Setup:** Enter IA S3 keys and click Save Credentials — writes `rclone/rclone.conf` automatically.

**Features:**
- 1–12 transfer threads
- Derive toggle (written to rclone.conf)
- Verbose selector (-v or -vv)
- Wait-archive timer
- Checksum toggle
- Fetch button (pulls existing IA item metadata)
- Restart button (stop + change settings + resume, rclone skips already-uploaded files)
- Full rclone log output with colour coding

**Speed:** rclone typically achieves significantly higher throughput than the Python uploader due to more efficient connection handling.

---

## Internet Archive Identifier Rules
- 5–100 characters
- Letters, numbers, dots, hyphens, underscores only
- Must start with a letter or number
- Globally unique on archive.org
- Check: `archive.org/details/your-identifier`

---

### Scene ZIP Recreator (scene_recreator.html)

Repairs old scene `.zip` releases to byte-match a DAT-listed CRC32/MD5/SHA1 target without ever touching the original file. All work is performed on a copy in the output folder.

**Matching:** Three-way name matching against DAT entries — exact filename, no-extension, and normalised (strip region/flags). Supports CLRMamePro and Logiqx XML DAT formats.

**Repair techniques attempted in order:**
1. EOCD comment strip — removes topsite tagline grow-appends
2. FAT-front / Unix-tail truncation + EOCD rebuild
3. Line-ending normalisation (CRLF↔LF) on `.nfo`/`.diz` entries
4. Junk-file removal — re-zips without grow-appended entries
5. Compression-setting variations (STORE, DEFLATE levels 0–9) on full rebuild
6. Faithful rebuild — reuses original header fields (version, flags, DOS timestamp, attributes) across all entry orderings and compression levels
7. Heuristic junk removal — flags injected FTP-script/topsite/courier entries by name pattern and folder name (`adverts/` etc.), tries every non-empty subset of candidates
8. Strip to essentials — keeps only the largest entry plus `.nfo`/`.diz`, tries both line-ending variants across full search space
9. Foreign-packer diagnosis — checks internal CRC32 consistency; distinguishes "probably fine, can't byte-match" (different original packer) from "actually broken content"

**Reference fingerprinting:** Supply one or more folders of known-good DAT-verified scene ZIPs. The tool learns each release group's packer fingerprint (compression level/strategy, entry ordering, header metadata, expected file set) and tries the learned fingerprint first — typically 1–4 attempts instead of dozens. Also catches injected files by direct comparison against verified references, even when name-pattern heuristics would miss them.

**Settings saved to `scene_recreator.json` (gitignored):**
- Source folder, DAT folder, reference folders, destination folder
- Dry-run mode, move vs copy mode
- Use fingerprint DB toggle, auto-retry excluding suspect references toggle

**Reference fingerprint database:** `reference_fingerprint_db.json` — auto-built from reference scans, gitignored (can be large).

---

## Themes
Dark (default), Amber, Slate Blue, Red, Light — saved to localStorage.
