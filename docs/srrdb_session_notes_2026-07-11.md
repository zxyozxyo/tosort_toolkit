# srrdb Scene Rebuilder — Session Notes (2026-07-11)

Working state of the srrdb module (`srrdb_tool.py` / `gui/srrdb_tool.html`) and what was
fixed/learned in the sessions up to and including 2026-07-11. Written as a reference so we can
pick up where we left off.

---

## Confirmed working

- **Uncompressed RAR4 reconstruction** — pure Python via `rescene.main.reconstruct()`, no rar.exe
  needed. Verified end-to-end: `Majo_to_Yuusha` (7 files), `Babylon.2022.COMPLETE.BLURAY-RiSEHD`
  (98 volumes from a 48 GB ISO).
- **RAR pack setup** — `setup_rar_executables()` extracts rar.exe from `wrar*.exe` installers in
  `apps/winrar_pack-4.20/` (recursive search), naming them in rescene's required format:
  `YYYY-MM-DD_rar<MAJOR><MINOR>[b<N>].exe` (e.g. `2013-06-15_rar420.exe`). 62 versions currently
  in the pack. Wrongly-named/dated leftovers are cleaned up on re-run.
- **RAR5 detection** — `_srr_list()` reads SRR blocks and checks stored RAR headers against the
  RAR5 marker (`Rar!\x1a\x07\x01\x00`). RAR5 releases are skipped upfront with a clear message
  (pyReScene 0.7 cannot reconstruct RAR5).
- **Progressive search** — `search_srrdb_progressive()` retries with the group suffix removed,
  then progressively trims dot-separated tokens (up to 4), each attempt also trying the
  underscore variant. CRC-based exact search from SFV files runs first when an SFV is present.
- **Batch mode** — auto-matching candidates with confidence %, per-release processing, subfolder
  scanning (NFO/SFV/media detection one level deep), double-nesting descent when a release
  folder contains only a single subfolder.

## Fixed today (2026-07-11) — sample creation

Sample creation had never produced a file despite reporting "Sample created ✓". Three separate
bugs, all in or around `_srs_create_sample()`:

1. **`time.clock()` crash** — `resample/srs.py` (pyReScene 0.7) uses `time.clock()`, removed in
   Python 3.8. On Python 3.13 every srs run crashed instantly. Fixed by launching srs through a
   `python -c` wrapper that sets `time.clock = time.perf_counter` before importing
   `resample.srs`.
2. **`distutils` crash** — `resample/fpcalc.py` imports `distutils.spawn.find_executable`;
   distutils was removed in Python 3.12. The same wrapper injects a stub `distutils.spawn`
   module into `sys.modules` with `find_executable = shutil.which`.
3. **False success reporting** — the old code treated exit code 1 as success and never checked
   whether a file existed. Now: success = at least one file actually created in the output dir;
   all srs stdout/stderr lines are logged (`srs: ...` prefix); `-y` flag added so the overwrite
   prompt can't block on stdin.

4. **`locale.format()` crash (2026-07-12)** — removed in Python 3.12, used by rescene's `sep()`
   number formatter which runs in the rebuild results display — *after* the sample was rebuilt
   to a `.tmp` file but *before* the CRC check and rename. This stranded finished samples as
   `.tmp` files. Shimmed with `locale.format = locale.format_string`. Additionally the wrapper
   now recovers lingering `.tmp` files by renaming them to the intended sample name.

## The GUI silent-log bug (major, fixed 2026-07-12)

`_emit()` passed `json.dumps(...)` into a **single-quoted JS string** without escaping
backslashes. Any log message containing a Windows path (`B:\...`) failed `JSON.parse` in the
browser and was silently dropped. Every "Reconstruct ERROR: file does not exist" message was
being lost — making real failures invisible. Fix: escape `\` then `'` before embedding.

## Sample rebuild landscape (verified through testing)

| Sample source | SRS type | Rebuild? |
|---|---|---|
| MKV/AVI/MP4/WMV cut from the movie | format-aware (MKV etc.) | ✓ works, CRC-verified |
| Complete Blu-ray (remuxed m2ts cut) | STREAM (raw bytes) | ✗ never — the 256-byte signature is remux-tool output, not on the disc |
| Sample with extra tracks (e.g. `V_MS/VFW/FOURCC` group intro) | MKV | ✗ extra track has no source in the movie |
| Usenet-sourced SRR | none (`sample.mkv.txt` placeholder) | ✗ no SRS data at all |
| Sample file already in content folder | any | ✓ detected by size, CRC-verified, copied into place |

## Reconstruction fixes (2026-07-12)

- **Multi-set SRRs** (movie + Subs vobsub sets): rescene's `reconstruct()` was all-or-nothing —
  a missing subs source killed the movie RARs too. Now each RAR set is checked for source
  availability and reconstructed individually via `srr_part="prefix.*"`; unsourceable sets are
  skipped with a clear log line.
- **Renamed content files**: `auto_locate_renamed=True` — when the stored filename isn't on
  disk, rescene matches by exact size + extension (fixes hash-named files and year-mismatch
  names like GECKOS 2013/2014).
- **Nested SRR reconstruction**: stored `Subs/*.subs.srr` files are now attempted after the
  main set — rebuilds vobsub RARs when the idx/sub sources are present in the content folder.
  Note: nested subs SRRs are headers-only (~441 B); subtitle data is NOT in the database, so
  subs can only be rebuilt if the original Subs/ files are kept with the content.

## Known limitations / open items

- **Compressed RAR4** — `Chromehounds.PAL.XBOX360-DNL` fails with "No good RAR version found"
  after trying all 62 pack versions. The exact WinRAR build used by DNL isn't in the pack.
  Backtrack testing with more/older versions still pending. Game testing resumes after media
  testing wraps.
- **RAR5** — hard limit of pyReScene 0.7; detected and skipped.
- **BD ISO samples** — confirmed unrebuildable (STREAM-type SRS of remuxed cuts); tool now
  scans the ISO directly (skips the pointless M2TS extraction) and explains the failure.
- **~75% of MKV batch reconstructed + sampled cleanly** as of 2026-07-12; remaining failures
  all had identifiable causes (above), none tool bugs.

## Key internals (quick reference)

- rescene rar.exe name regex: `^\d{4}-\d{2}-\d{2}_rar\d+(?:b\d)?\.(exe)?$`
- `_WINRAR_DATES` maps version → release date for correct naming (2.60 through 6.02).
- `COMPR_STORING = 0x30` = uncompressed; `0x31–0x35` = compressed (needs exact rar.exe).
- `rescene.main.reconstruct()` returns `False` (not an exception) for "no RAR executables" /
  "no good RAR version" — both paths surfaced with actionable messages.
- srrdb API: `search/{query}`, `search/archive-crc:{crc}`, download at
  `www.srrdb.com/download/srr/{release}`.
- srs is invoked as: `python -c "<time.clock + distutils shims>; from resample.srs import main;
  main(sys.argv[1:])" <file.srs> <video> -o <out_dir> -y`
