# ToSort Toolkit

A PyWebView desktop application for cleaning and managing ROM and scene release collections. Includes tools for RomVault integration, DAT file management, Internet Archive uploading, and scene RAR reconstruction.

---

## Requirements

### Python
Python 3.10 or later recommended.

### Python Libraries
```
pip install pywebview py7zr rarfile zstandard internetarchive requests
pip install pyReScene
```

> **Note:** `pyReScene` is not available under the name `rescene`. Install using the exact command above or via the direct wheel if PyPI fails:
> ```
> pip install https://github.com/srrDB/pyrescene/releases/download/0.7/pyReScene-0.7-py3-none-any.whl
> ```

| Library | Purpose |
|---|---|
| `pywebview` | Desktop GUI framework |
| `py7zr` | 7z archive extraction |
| `rarfile` | RAR archive extraction |
| `zstandard` | ZSTD compressed file support |
| `internetarchive` | Internet Archive upload API |
| `requests` | HTTP for IA uploads |
| `pyReScene` | Scene SRR parsing and RAR reconstruction |

---

## Folder Structure

```
tosort_toolkit/
├── main.py                  # Entry point — launcher hub
├── api.py                   # ToSort pipeline backend
├── dat_merger.py            # DAT tools backend
├── ia_uploader.py           # IA uploader backend
├── ia_prepper.py            # IA archive pre-processor
├── ia_folder_packer.py      # IA folder packer
├── rclone_gui.py            # RClone uploader backend
├── scene_recreator.py       # Scene ZIP recreator/repair tool
├── srrdb_tool.py            # srrdb.com scene RAR rebuilder
├── misc_tools.py            # Miscellaneous utilities backend
├── gui/                     # HTML frontends
│   ├── home.html            # Launcher hub (main entry page)
│   ├── index.html           # ToSort pipeline
│   ├── dat_merger.html      # DAT tools
│   ├── ia_uploader.html     # IA uploader
│   ├── ia_folder_packer.html
│   ├── rclone_gui.html      # RClone IA uploader
│   ├── scene_recreator.html # Scene ZIP recreator
│   ├── srrdb_tool.html      # srrdb scene RAR rebuilder
│   └── misc_tools.html      # Miscellaneous utilities
├── apps/                    # Drop tool binaries here (gitignored)
│   ├── rar.exe
│   ├── 7z.exe
│   ├── 7za.exe
│   ├── UnRAR.exe
│   ├── chdman.exe
│   ├── xdms.exe
│   └── winrar_pack-4.20/    # Legacy WinRAR installers for compressed scene RARs
└── rclone/                  # Drop rclone files here (gitignored)
    ├── rclone.exe
    └── rclone.conf          # Auto-created when saving credentials
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
- For RAR output in the IA pre-processors, `rar.exe` is preferred.
- `7zr.exe` alone cannot create RAR archives — use `7z.exe` or `rar.exe`.
- The `apps/winrar_pack-4.20/` subfolder contains WinRAR setup packages for legacy RAR versions. These are only required for reconstructing compressed scene RARs (rare for video releases). Standard uncompressed scene RARs do not need them — pyReScene handles those natively.

---

## RClone Setup

Place `rclone.exe` in the `rclone/` subfolder (created manually). The `rclone.conf` file is auto-created there when you save credentials in the RClone GUI.

---

## Running

```
python main.py
```

The app opens to the **hub launcher page** — click any card to open that tool. Each tool opens in its own window; the hub stays open as a menu.

---

## Gitignored Files
These are never committed and must be set up locally:

```
apps/                         # All tool binaries
rclone/                       # rclone.exe and rclone.conf
settings.json                 # Auto-saved pipeline settings
ia_credentials.json           # IA S3 keys for Python uploader
ia_uploader.json              # IA uploader saved settings (fixdat path etc.)
ia_folder_packer.json         # Folder packer saved settings
rclone_ia.json                # RClone uploader saved settings (fixdat path etc.)
scene_recreator.json          # Scene recreator saved settings
srrdb_tool.json               # srrdb rebuilder saved settings
reference_fingerprint_db.json # Scene recreator fingerprint DB (auto-generated, can be large)
tosort_settings_export.json
```

---

## Features

---

### Launcher Hub (home.html)

The main entry point — a card-based launcher that opens each tool in its own window. Hover over a card for a description. Supports five themes (Dark, Amber, Slate, Red, Light) via the Theme button; the selected theme persists across sessions and is shared across all tool windows.

---

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
- Spaces in identifiers are automatically converted to underscores

**Fixdat Filter**
Load a RomVault fixdat XML to exclude incomplete ROM sets from the upload queue. Files listed in the fixdat (incomplete) are skipped; files not listed (complete sets) upload normally. The fixdat path is saved between sessions (`ia_uploader.json`). Excluded files are shown greyed out in the file list before uploading.

**IA Pre-processor (within IA Uploader)**
Groups loose archives into letter-named RAR/ZIP files before upload — ideal for large TOSEC sets.
- Groups by first character: A–Z, 0–9, MISC
- Splits into `A`, `A_2`, `A_3` etc. when group exceeds size limit
- Copy mode or move mode
- RAR or ZIP output
- Full Unicode filename support

---

### IA Folder Packer (ia_folder_packer.html)

Packs each leaf folder containing archives into a single archive.

Finds the deepest folder containing archives and packs it as one file, preserving relative structure.

**Example:**
```
TOSEC/Commodore/C64/Games/[D64]/  →  [D64].rar
TOSEC/Commodore/[D64]/            →  [D64].rar
```

- Copy mode: output mirrors structure under destination, originals untouched
- Move mode: archives created alongside source folder, originals deleted
- RAR or ZIP output
- Unicode filename support

---

### RClone IA Uploader (rclone_gui.html)

Standalone rclone wrapper for IA uploads.

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
- Spaces in identifiers are automatically converted to underscores

**Fixdat Filter**
Same RomVault fixdat filtering as the IA Uploader. Loads a fixdat XML and passes matching filenames to rclone as `--exclude` flags so they are skipped server-side. The fixdat path is saved between sessions (`rclone_ia.json`).

**Speed:** rclone typically achieves significantly higher throughput than the Python uploader due to more efficient connection handling.

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

**Reference fingerprinting:** Supply folders of known-good DAT-verified scene ZIPs. The tool learns each release group's packer fingerprint (compression level/strategy, entry ordering, header metadata, expected file set) and tries the learned fingerprint first — typically 1–4 attempts instead of dozens. Also catches injected files by direct comparison, even when name-pattern heuristics would miss them.

**Settings saved to `scene_recreator.json` (gitignored).**

---

### srrdb Scene Rebuilder (srrdb_tool.html)

Downloads SRR files from [srrdb.com](https://www.srrdb.com) and reconstructs byte-perfect original scene RAR releases from unpacked content files.

**Requires:** `pip install pyReScene` (the tool shims pyReScene 0.7's Python 3.12+ incompatibilities automatically — `time.clock`, `distutils`, `locale.format`)

#### What is an SRR?

An SRR (Scene Rebuilder Resource) file stores all original RAR block headers, file metadata and stored scene files (NFO, SFV, SRS) without the actual content. Combined with the original content file (the video, ISO etc.), pyReScene can reconstruct the exact original RAR set byte-for-byte.

#### Finding the release — search strategies

The tool identifies releases in this order, so badly named folders and files still work:

1. **Name search** — NFO/SFV filename stem preferred over the folder name, with progressive
   trimming (drops group suffix and trailing tokens) and dots↔underscores variants
2. **Auto-match scoring** — each candidate's expected file list (srrdb details API) is scored
   against your content by filename **and exact file size** (renamed files still match)
3. **Content CRC lookup** — when names give nothing, the largest media file is hashed (CRC32)
   and looked up via srrdb's `archive-crc` search: an exact, name-independent match.
   A completely scrambled folder with an untouched original file still resolves.

API calls are throttled (~1/s), cached for the session, and back off automatically on
rate-limit responses.

#### Usage — Single Folder

1. Select the **Source** folder containing your unpacked content file(s) (e.g. `movie.avi`)
2. The app auto-detects the release name (see search strategies above)
3. Click **Search** to query srrdb.com — select the correct result from the list
4. Set an **output folder**
5. Click **Process**

#### Usage — Batch (Subfolders)

Switch to **Batch** mode and select a folder containing multiple release subfolders. The app scans each subfolder, auto-detects release names, and builds a queue. Click **Auto-Search All** to verify and fill release names from srrdb.com, then **Process** to run them in sequence. Ambiguous entries are resolved at run time by auto-match scoring and the CRC fallback.

#### What gets rebuilt

- **RAR volumes** — reconstructed byte-perfect from the content file. Renamed content is
  located automatically by exact size + extension.
- **Multi-set SRRs** (movie + Subs vobsub sets) — each RAR set is reconstructed independently;
  sets whose sources are missing (subtitle data is *not* inside the video file) are skipped
  without taking the movie set down.
- **Nested subs SRRs** — `Subs/*.subs.srr` sets are rebuilt when the idx/sub sources are
  present in the content folder.
- **Stored files** — NFO, SFV, Proof/, etc. are extracted and placed in the release folder
  with their original paths.
- **Sample** — rebuilt from the SRS + full video and CRC-verified. If a file matching the
  SRS sample size is already in the content folder, it is CRC-verified and copied into place.

#### Output Structure

```
output_folder/
└── Release.Name-GRP/
    ├── release.name-grp.nfo             ← from SRR stored files
    ├── release.name-grp.sfv             ← from SRR stored files
    ├── release.name-grp.rar             ← reconstructed
    ├── release.name-grp.r00 …           ← reconstructed
    ├── Proof/release.name-grp.proof.jpg ← from SRR stored files
    ├── Subs/release.name-grp.subs.rar   ← reconstructed (if sources present)
    └── Sample/
        └── release.name-grp-sample.mkv  ← rebuilt from SRS + content, CRC-verified
```

#### Sample rebuild — what works and what can't

| Sample type | Rebuildable? |
|---|---|
| MKV / AVI / MP4 / WMV cut from the movie | ✓ yes, CRC-verified |
| Complete Blu-ray (remuxed M2TS cut, STREAM-type SRS) | ✗ never — the sample's bytes don't exist on the disc |
| Sample containing extra tracks (group intro etc.) | ✗ the extra data has no source |
| Usenet-sourced SRR (`sample.mkv.txt` placeholder) | ✗ no SRS data stored |

The log states the exact reason whenever a sample can't be rebuilt.

**Options:**
- **Extract M2TS from ISO** — for format-aware SRS types, extracts the main Blu-ray stream
  before sample creation (needs disk space ≈ stream size). STREAM-type SRS skips this and
  scans the ISO directly.
- **NON-SCENE preview clip** — when the scene sample is provably unrebuildable (Blu-ray
  remuxed samples), optionally carve a playable preview from the disc's main stream, sized
  like the real sample. The file is named `NONSCENE-…-preview.m2ts` and is **not** a scene
  file — it will never CRC-match the SRS. Off by default.

#### Notes

- **No Rar.exe required** for the vast majority of scene releases. Standard uncompressed video scene RARs are reconstructed natively by pyReScene in pure Python.
- **Compressed RARs** (mostly game releases): require the exact original Rar.exe version. Use the **Setup RAR versions** button to extract correctly named executables from installers in `apps/winrar_pack-4.20/`.
- **RAR5 releases** (WinRAR 5+) cannot be reconstructed by pyReScene 0.7 — detected and skipped upfront.
- SRR downloads are cached in the output folder — re-running the same release skips the download.

---

### Miscellaneous Tools (misc_tools.html)

A collection of small utility scripts.

#### Move to Folder

Moves each file in a selected folder into a subfolder named after it (without the extension). Useful when preparing fixdats for RomVault to avoid filename collisions.

**Example:**
```
Before:  games/bobgame.zip
After:   games/bobgame/bobgame.zip
```

#### Extras Collector

Recursively scans a source folder tree and **copies** scene extras into a mirrored structure at the destination. The source files are never moved or modified.

**Collected file types:** `.nfo`, `.diz`, `.jpg`, `.jpeg`, `.png`, `.sfv`, `.nzb`

**Collected subfolders:** `Proof/`, `Sample/`

The full folder structure from the source root is preserved at the destination:
```
Source/3DS_2011/bob_game/bob_game.nfo  →  Destination/3DS_2011/bob_game/bob_game.nfo
Source/3DS_2011/bob_game/Proof/        →  Destination/3DS_2011/bob_game/Proof/
```

**Pack output (optional):** Each collected game folder can optionally be compressed into a ZIP or RAR archive (requires `7z.exe` or `Rar.exe` in `apps/`). The original folder is removed after successful packing.

---

## Internet Archive Identifier Rules
- 5–100 characters
- Letters, numbers, dots, hyphens, underscores only
- Must start with a letter or number
- Globally unique on archive.org
- Spaces are automatically converted to underscores when typed
- Check availability: `archive.org/details/your-identifier`

---

## Themes

Dark (default), Amber, Slate Blue, Red, Light — selectable from the Theme button on the launcher hub. Choice is saved to browser localStorage and applied across all tool windows.
