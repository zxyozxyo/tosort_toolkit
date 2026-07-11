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

## Learned today — Blu-ray ISO samples

`Unable to locate track signature for track 1` on Complete Blu-ray releases is **not a bug in
the SRS**: the scene group built the sample SRS from an M2TS stream *inside* the disc
(`BDMV/STREAM/*.m2ts`), so the SRS byte offsets reference that M2TS — not the ISO container.
Passing the ISO gives srs the wrong byte layout.

Handling added:

- Sample media selection now prefers stream files (searched recursively:
  `.avi .mkv .mp4 .m4v .mov .wmv .m2ts .ts .vob`, largest first) over disc images
  (`.iso .img .bin .nrg`).
- New GUI option **"Extract M2TS from ISO for Blu-ray samples"** (off by default, greyed out
  unless "Create sample" is ticked). When enabled: 7z lists `*.m2ts` inside the ISO, extracts
  the largest stream (main feature) to `_iso_m2ts_tmp/` under the output folder, runs srs
  against it, then deletes the temp extract. Config key: `extract_iso_m2ts`. Caveat: needs free
  disk space roughly equal to the main video stream; extraction of a 40 GB stream takes minutes.

## Known limitations / open items

- **Compressed RAR4** — `Chromehounds.PAL.XBOX360-DNL` fails with "No good RAR version found"
  after trying all 62 pack versions. The exact WinRAR build used by DNL isn't in the pack.
  Backtrack testing with more/older versions still pending.
- **RAR5** — hard limit of pyReScene 0.7; detected and skipped.
- **ISO M2TS extraction** — implemented today, not yet tested end-to-end (needs a Blu-ray ISO
  run with the new checkbox ticked).
- **Sample creation on plain video releases** (MKV/AVI content) — should now work after the
  Python 3.13 fixes, needs confirmation on the next media batch.

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
