# ToSort Toolkit

A PyWebView desktop application for cleaning and managing ROM and scene release collections. Includes tools for RomVault integration, DAT file management, Internet Archive uploading, and scene RAR reconstruction — including **RSR**, a capture-time format that guarantees byte-exact reconstruction rather than merely making it possible.

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
├── rsr_tool.py              # RSR capture/rebuild — Reproducible Scene Release
├── misc_tools.py            # Miscellaneous utilities backend
├── letter_filter.py         # Shared A–Z / 0-9 / MISC grouping (packer + both uploaders)
├── upload_profiles.py       # Shared upload profile store for both IA uploaders
├── rescene_guard.py         # Gives each tool window its own private pyReScene copy
├── gui/                     # HTML frontends
│   ├── home.html            # Launcher hub (main entry page)
│   ├── index.html           # ToSort pipeline
│   ├── dat_merger.html      # DAT tools
│   ├── ia_uploader.html     # IA uploader
│   ├── ia_folder_packer.html
│   ├── rclone_gui.html      # RClone IA uploader
│   ├── scene_recreator.html # Scene ZIP recreator
│   ├── srrdb_tool.html      # srrdb scene RAR rebuilder
│   ├── rsr_tool.html        # RSR capture/rebuild
│   └── misc_tools.html      # Miscellaneous utilities
├── apps/                    # Drop tool binaries here (gitignored)
│   ├── rar.exe
│   ├── 7z.exe
│   ├── 7za.exe
│   ├── UnRAR.exe
│   ├── chdman.exe
│   ├── xdms.exe
│   ├── winrar_pack-4.20/    # Legacy WinRAR installers for compressed scene RARs
│   └── zip_pack/precomp/windows/precomp.exe   # preflate fallback for ZIP capture
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
| `zip_pack/precomp/windows/precomp.exe` | preflate fallback for RSR ZIP capture | schnaader/precomp-cpp |
| `dosbox/dosbox.exe` | runs the DOS RAR builds for RSR capture | dosbox-staging |
| `dosrar_pack/*.exe` | RAR for DOS, 1.40-2.50 | see **The DOS RAR line** below |

**Notes:**
- For RAR output in the IA pre-processors, `rar.exe` is preferred.
- `7zr.exe` alone cannot create RAR archives — use `7z.exe` or `rar.exe`.
- The `apps/winrar_pack-4.20/` subfolder contains WinRAR setup packages for legacy RAR versions. These are only required for reconstructing compressed scene RARs (rare for video releases). Standard uncompressed scene RARs do not need them — pyReScene handles those natively.
- **WinRAR 7.x is deliberately excluded** from the extracted pack: 7.00 removed RAR4 creation entirely (`Unknown option: ma4`), so 6.24 is the last RAR4-capable build. Versions 5.00–6.24 *are* useful — post-2013 scene releases were made with modern WinRAR in RAR4 mode, and the tools inject `-ma4`.
- `precomp.exe` is only needed for RSR's ZIP capture, and only as a fallback when no deflate setting reproduces a stream. RAR capture never touches it.
- `apps/dosbox/` and `apps/dosrar_pack/` are only needed for pre-2002 releases packed on MS-DOS. Everything else ignores them, and RSR says so in the log rather than failing when they are absent.

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
ia_uploader.json              # IA uploader saved settings (fixdat path, letter filter etc.)
ia_folder_packer.json         # Folder packer saved settings
rclone_ia.json                # RClone uploader saved settings (fixdat path, letter filter etc.)
ia_upload_profiles.json       # Saved upload profiles, shared by both IA uploaders
scene_recreator.json          # Scene recreator saved settings
srrdb_tool.json               # srrdb rebuilder saved settings
srrdb_results.json/.csv/.xlsx # srrdb results DB — wall cache, tried builds, locked recipes
srrdb_extras.db               # srrdb local extras store — CRC index of your extras folders (auto-generated)
srr_cache/                    # srrdb persistent SRR download cache
rsr_tool.json                 # RSR scanner saved settings
rsr_index.db                  # RSR index — captures, misses, recipe priors (auto-generated)
rsr_store/                    # Captured .rsr files and their extras (your data)
rsr_store_bak-*/              # Store backups taken before a risky change
apps/dosbox/                  # dosbox-staging, for the DOS RAR line
apps/dosrar_pack/             # RAR for DOS binaries + auto-generated _caps.json
reference_fingerprint_db.json # Scene recreator fingerprint DB (auto-generated, can be large)
excluded_references.txt       # Scene recreator exclusion list (local)
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

Ten-tab DAT management suite: Merger, Splitter, Cleaner, Rebuilder, DAT Creator (file-based), DAT Creator (folder-based), Header Editor, Diff Tool, Batch Rename, and **Strip MIA** (remove all MIA-flagged entries from a single DAT or a folder of DATs, leaving the DAT usable).

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

- Upload profiles — save and recall a whole set of fields; the profile store is shared with the RClone uploader

**Fixdat Filter**
Load a RomVault fixdat XML to exclude incomplete ROM sets from the upload queue. Files listed in the fixdat (incomplete) are skipped; files not listed (complete sets) upload normally. The fixdat path is saved between sessions (`ia_uploader.json`). Excluded files are shown greyed out in the file list before uploading.

**Letter Filter**
An IA item is capped at 1 TB, so an oversized set (REDUMP AUDIO CD and friends) has to go up in chunks. Rather than hand-copying files into `0 - C` / `D - G` staging folders, point the uploader at the whole set and tick the letters this run should carry.

- Tick boxes for `A`–`Z`, `0-9` (filenames starting with a digit) and `MISC` (`#`, brackets, symbols), plus All / None
- Nothing ticked = upload everything, exactly as before
- Only matching files are added by **+ Folder**; hand-picked files outside the range are refused with a log line
- **Re-apply to list** re-filters a queue built under a different selection, leaving already-uploaded rows alone
- The upload thread re-checks the filter itself, so a selection changed mid-session can't smuggle an out-of-range file into the item
- Stacks with the fixdat filter — letter first, fixdat on top, so nothing incomplete ever slips through
- Selection persists between sessions (`ia_uploader.json`)

The groups are the same ones the Folder Packer names its batches after (`letter_filter.py` is shared by all three tools), so a letter means the same thing everywhere.

**IA Pre-processor (within IA Uploader)**
Groups loose archives into letter-named RAR/ZIP files before upload — ideal for large TOSEC sets.
- Groups by first character: A–Z, 0–9, MISC
- Splits into `A`, `A_2`, `A_3` etc. when group exceeds size limit
- Copy mode or move mode
- RAR or ZIP output
- Full Unicode filename support

---

### IA Folder Packer (ia_folder_packer.html)

One tool, three selectable packing strategies for preparing folders of archives for upload. All three share the same source/destination/format/copy-or-move options, the same archiver discovery, and a preview that shows what a run would produce before it does it.

**LEAF** — finds the deepest folder that directly contains archives and packs each one as a single file, named after the folder, preserving relative structure.

```
TOSEC/Commodore/C64/Games/[D64]/  →  [D64].rar
TOSEC/Commodore/[D64]/            →  [D64].rar
```

**LETTER** — groups loose archives by first character (`A`–`Z`, `0-9`, `MISC`) and splits each group into batches not exceeding a size limit, named `A.rar`, `A_2.rar`, `A_3.rar`. This is the original IA Prepper behaviour, and it uses the same grouping as the uploaders' Letter Filter.

**DEPTH** — treats every folder at a fixed number of levels below the source root as one packing unit (everything beneath it is included, whether or not it directly contains archives) and size-splits it the same way LETTER does, named `FOLDERNAME.rar`, `FOLDERNAME_2.rar`.

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
Same RomVault fixdat filtering as the IA Uploader. Loads a fixdat XML; matching files are held back from the transfer. The fixdat path is saved between sessions (`rclone_ia.json`).

**Letter Filter**
Same letter selection as the IA Uploader (see above) — tick the letters this run should carry so an oversized set can be split across several IA items without staging copies on disk.

Both filters are expressed to rclone as **one `--files-from` list** of exactly the files that should go up, rather than a `--exclude` per unwanted file. A full ROM set can push the unwanted list into the thousands, which would blow past the Windows command-line length limit; a list file also states the intent (upload precisely these) instead of leaving it implied, and covers subfolders rather than just the top level. If that list can't be written the upload aborts rather than running unfiltered. Held-back files appear in the queue dimmed and marked `✗ fixdat` or `✗ letter`.

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

#### Compressed RAR reconstruction (game releases)

Game scene RARs (3DS, NDS, etc.) are usually **compressed**, which makes the rebuild sensitive to the exact WinRAR build and settings the original packer used. Rather than write a near-miss off, the tool applies a layered rescue stack — each layer only ever runs *after* the normal rebuild has failed, so releases that rebuild today are untouched:

- **Version sweep** — pyReScene locks the first WinRAR build whose test *piece* matches and never tries another; on low-effort methods (`-m1`) that can be an implausibly old build. When the full archive comes out a few bytes off, every other build in the pack is tried (this group's known-good history and nearest release date first) and CRC-verified against the SFV.
- **Thread-count (`-mt`) rescue** — when the right version reproduces the wrong bytes because the original used a different thread count, the tool sweeps thread counts for the affected stream and re-verifies. Handles the single-file case, embedded proof-JPG near-misses, and small NFO/DIZ files that inherited a large file's thread count.
- **Missing / mismatched extras** — some groups pack a proof JPG or an NFO/DIZ that isn't stored in the SRR, or a copy that differs from the loose one. These are resolved, in order, from: srrdb's "adds", a CRLF↔LF line-ending fix, and the **local extras store** (below). A genuinely unreconstructable extra fast-fails *before* wasting a full content recompress.

**Local extras store.** Point the srrdb tool at one or more folders of scene extras — e.g. a set built with the misc-tools **Extras Collector**, or a pack from a DAT site. A single SQLite index (`srrdb_extras.db`, in the tosort folder for easy backup) is built from them, keyed by **content CRC32** — so it's name-independent and can hold same-named files from different packs with different bytes. When a rebuild needs an exact packed NFO/DIZ/proof the SRR doesn't carry and srrdb has no add for, it's matched by the SRR's exact CRC, **copied** (never moved) into the release, and re-verified. Manage folders and trigger a rescan from the **Extras store** panel; the scan is incremental (only new/changed files are read) and the folder list persists across restarts. Entirely inert until you add a folder.

**Genuine walls** (reported clearly, never silently failed): RAR5 releases (pyReScene 0.7 limit), solid-compressed archives whose exact settings can't be reproduced, and a packed extra that differs from the SRR copy when no matching file exists on srrdb or in your extras store.

#### Results DB — what a re-run reuses

Every processed release is recorded (`srrdb_results.json`, also exported as CSV/XLSX). A re-run reuses three things from it, so a second pass doesn't repeat the first:

- **Version-wall cache** — a release proven to match *no* build in the pack is a pure version wall; only a bigger pack can ever fix it, so it's skipped instantly instead of re-grinding every build. The record is tagged with the pack signature and a cache generation, so **adding WinRAR versions, or a change to the hunt logic, automatically re-opens every wall** for one fresh attempt. A wall recorded under the release-date cap is marked as such and re-opens by itself when the cap is turned off — the cap's subset is never a permanent exclusion.
- **Recipe-sweep verdict** — cached for a hit and for a clean exhaustion. A sweep cut short by the budget or by Stop is deliberately *not* cached, since it proved nothing.
- **Builds a timed-out run already tested** — these are pushed to the **back** of the order on the retry, so a second pass explores new builds instead of grinding the same head of the list into the same timeout. A deadline-truncated run is never recorded as a wall.

Two options control this. **Ignore stored DB history** discards all three and searches from scratch — thorough, but it also throws away the resume list, so a release that only ever times out will restart at the same builds every run. **Ask first (30s pause)** offers the choice per release instead and auto-continues if unanswered, so an unattended batch is unaffected.

Locked recipes are stored per stream as `[file, version, mt]` — the byte-exact packing settings, and the accumulating dataset the RSR format grew out of. The RSR tool can import them as priors.

#### Notes

- **No Rar.exe required** for the vast majority of scene releases. Standard uncompressed video scene RARs are reconstructed natively by pyReScene in pure Python.
- **Compressed RARs** (mostly game releases): require the exact original Rar.exe version. Use the **Setup RAR versions** button to extract correctly named executables from installers in `apps/winrar_pack-4.20/`. See the rescue stack above.
- **RAR5 releases** (WinRAR 5+) cannot be reconstructed by pyReScene 0.7 — detected and skipped upfront.
- SRR downloads are cached persistently in `srr_cache/` — re-running any release (even after a restart) skips the download and never re-hits the rate-limited host.

---

### RSR — Reproducible Scene Release (rsr_tool.html)

A capture-time format that **guarantees** byte-exact archive reconstruction, where a `.srr` can only make it *possible*.

The difference is where the brute force happens. pyReScene reassembles an archive block by block at *rebuild* time, having to infer the WinRAR build and thread count from evidence the SRR never recorded. RSR captures while the **original archives are still on disk**, so it can find the exact recipe, run it, and byte-compare the result against the real thing before writing anything:

```
reconstruction = replay the original `rar a` command
```

That one decision is why RSR handles what the rebuilder cannot:

| | |
|---|---|
| **RAR5** | pyReScene 0.7 can't write it; `rar.exe` always could |
| **Solid archives** | one command packs the whole solid set — no member surgery |
| **`.001`/`.002`** | a volume naming scheme, not a structural problem |
| **ZIP releases** | captured too — a third of a typical NDS corpus |

Anything the replay still gets wrong (header-level: timestamps, attributes, host-OS byte) is stored as a per-volume delta, so a verified `.rsr` is verified in the literal sense: it was run, and the bytes matched. Nothing is ever written to the folder being scanned, and **no `.rsr` is written until a recipe is proved** — there is no such thing as a partial capture.

#### Capture

Point it at a folder of releases (or one release) and a **Store** folder. Several source folders can be scanned in one run. Captures are filed as `SYSTEM/YEAR/RELEASE`.

- **Max -mt** — thread counts swept 0–N. Sweep order is measured rather than assumed (mt8 dominates, then mt1/mt3/mt4) and runs outermost, so common counts cover the whole build pack before a rare one is tried anywhere
- **Embed cap** — packed files under this size are embedded so archive-only extras can be rebuilt; larger ones count as content
- **Also embed a legacy `.srr`** — for compatibility with existing tooling
- **Budget** — minutes per release, 0 for no limit
- **Retry larger dictionary sizes if the header's is wrong**
- **Smallest releases first** — learns recipes cheaply before spending time on the big ones
- **Skip releases already captured** / **Retry releases already swept to exhaustion**
- Live **Skip file**, **Finish this one** (lifts the budget for the release running right now, next release gets the normal budget again) and **Stop**

Outcomes are named rather than lumped together: captured, known wall, damaged, partial, parked, metadata-only, ZIP. A release that ran out of budget is distinguished from one that was searched exhaustively, so only the second gets remembered as a wall — and that wall re-opens automatically once the build pack grows.

#### ZIP capture

ZIP releases are captured through the same prove-it-then-write discipline. The tool learns which deflate implementation a group zips with and seeds those priors from the store rather than relearning them each run. When no deflate setting reproduces a stream, it falls back to **preflate** (via `precomp.exe`), which derives the parameters from the stream itself. Groups only preflate can crack skip the settings grid entirely.

#### The DOS RAR line

**RAR for DOS is a different compressor from WinRAR of the same version**, not a repackaging of it. Measured on one 300 KB file at `-m3`/64K, identical method and `unp_ver` in both headers:

```
DOS RAR 2.50    6,226 bytes
WinRAR  2.50    6,252 bytes
```

That 26-byte gap is why a whole class of 1990s releases was unreachable. `Woody_Woodpecker_Racing_USA-KALISTO` had been swept against all 239 Windows builds twice and written off as a wall; DOS RAR 2.50 reproduces it exactly. On the PSX year-2000 corpus the DOS line produced **110 verified captures in 10 hours** - KALISTO 44, HOOLiGANS 9 - from two groups previously declared unreachable.

**Setting it up.** Both pieces live under `apps/` and both are gitignored:

| Path | What |
|---|---|
| `apps/dosbox/dosbox.exe` | **dosbox-staging** (~105 MB extracted). Plain DOSBox works too; staging is what this was measured against. |
| `apps/dosrar_pack/*.exe` | The DOS RAR binaries, named `YYYY-MM-DD_dosrarNNN.exe` - e.g. `1996-05-08_dosrar200.exe`. |

The DOS RAR releases are the original self-extracting distributions (`rar140.exe` through `rar250.exe`), found in RARLAB's old-version archive and the usual scene-tool mirrors. Rename each to the dated form above - the date is the build date, and RSR uses it to order the sweep by era. Drop them in the folder; nothing else is required.

**They are capability-probed once** and the result cached in `apps/dosrar_pack/_caps.json` (auto-generated, safe to delete). The probe matters because these builds fail in ways that are not obvious:

- 151/152 reject `-s1`/`-ds` - they print their usage screen and pack nothing
- 151/152/153/140 do not understand `-v<N>b` and **do not fail cleanly**; 1.52 was seen splitting a 74-byte `.CUE` across ~1,800 volumes
- **8 of the 24 binaries are OS/2**, not DOS. The SFX installers shipped an OS/2 build beside the DOS one, and it boots DOSBox only to print *"This program must be run under OS/2"*. Detected by header (`MZ` + `LX`/`NE`/`PE`), never by filename. The usable tail is **16 builds**.

**Three gates decide whether DOS can apply at all**, all measured rather than assumed:

| Gate | Rule |
|---|---|
| **Format** | `unp_ver >= 29` rules DOS out completely - RAR 2.9 format arrived with RAR 3.00 (2002) and there was never a DOS RAR 3.x. Read from a *compressed* header: a stored file is stamped 20 by every build ever made. |
| **Filenames** | A name that is not 8.3 rules DOS out completely. RAR 2.50 stores `HLG-MO~1.BIN` for `hlg-monopol.bin`, and forcing DOSBox `lfn=true` changes nothing - it is 16-bit real mode and int21 find-first only ever returns 8.3. |
| **Host** | When the archive's host byte says MS-DOS, the DOS builds are tried **first**. Across 186 captured releases with sources on disk, a Windows build has never reproduced a DOS-host archive. |

Otherwise the DOS tail sits behind everything else: a DOS combo is a DOSBox boot plus a full pack with no prefix probe, and DOSBox runs at roughly **0.5 MB/s** regardless of compression level. Budget hours rather than minutes for a large DOS-line release - rebuilds pay the same ceiling, 10-16 minutes for a 25-volume set against about 1 for a Windows-built one.

DOSBox runs headless (`SDL_VIDEODRIVER=dummy`) - byte-identical output, measured - because otherwise it opens a focus-stealing window for every combo.

> **Only one group in the corpus needs it.** A host-byte survey of PSX 2000 found KALISTO packing 138 of its releases on MS-DOS while every other group of any size was pure Windows - KALISTO alone is 93% of all DOS-packed releases. The line is narrow, but it is the only thing that opens that group.

#### Coder and packer axes

A sweep can only find what it thinks to ask for. Several axes exist because a release proved unreachable without them:

| Axis | Why |
|---|---|
| **`-mm` / `-mmf`** | RAR 2.x's multimedia coder, documented as `mm[f]` - compression *[force]*. Plain `-mm` lets rar choose per block; `-mmf` forces it. They are different coders producing different streams, and neither was ever asked for. On HOOLiGANS this took the group from 3 to 28 of 29 captures, with **26 wins and no ordinary recipes at all**. |
| **`-s1 -ds`** | RAR for Unix does not do WinRAR's store-fallback: where Windows stores a file whose compression came out larger than the input, the Unix build keeps the expanded stream. `-s1` (solid groups of one) suppresses the fallback without giving any file the previous one's context; `-ds` stops solid mode re-sorting the members by extension. |
| **`-mc` (PPM)** | `-m5` compresses with LZSS *or* PPMd and rar chooses per file, so a packer that forced PPM was unreachable at every build. Skipped automatically on RAR 2.0 archives - PPM arrived with RAR 3.0, so no PPM stream can carry a 2.0 stamp. |
| **`-rr`** | The recovery record is a **512-byte sector count, not a percentage**, and RAR 2.x ignores a `p`/`%` suffix entirely. A short record lets more file data into volume one and shifts every later volume boundary. |

The sweep is ordered by capability: builds that could have written the archive's format lead - in their plain, `-s1` and multimedia forms - and the rest follow as a backstop. Nothing is ever removed, so a wall the tool reports is still a real wall.

When a sweep does exhaust without a match, it says how close it got:

```
X no build x -mt reproduces these streams - the exact build is outside the pack.
  closest: 2 of 3 stream(s) matched under 2000-11-30 2.70 b4 -mm
    - hlg-muscle.bin never did.
```

#### Rebuild

Two modes: **Batch** (rebuild everything under a root, optionally deleting content as it goes) and **Single** (one `.rsr` + its content folder → output folder).

#### Database

`rsr_index.db` indexes every capture, miss and recipe prior.

- **Search** by release, packed file name, or CRC32
- **Import srrdb priors** — seed recipe hints from the legacy rebuild results, so the two tools' evidence flows both ways
- **Reindex store** — re-read the Store folder and index any `.rsr` the DB is missing (also notices a Store that has moved)

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
