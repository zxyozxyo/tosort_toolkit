# ToSort Toolkit — RomVault Cleaning Pipeline

A desktop GUI application for keeping your RomVault ToSort folder clean.

## Folder Structure

```
tosort_toolkit/
├── main.py          ← Entry point, run this
├── api.py           ← Python backend logic (add your scripts here)
├── requirements.txt
├── gui/
│   └── index.html   ← The GUI frontend
└── README.md
```

## Setup

1. Install Python 3.9+ if you haven't already.

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
   
   For RAR support you also need unrar on your PATH:
   - Windows: download UnRAR.exe from rarlab.com and add to PATH
   - Or install WinRAR (rarfile will find it automatically)

3. Run the app:
   ```
   python main.py
   ```

## Adding Your Scripts (Modules 2, 3, 4)

Open `api.py` and find the stub methods:

- `_run_sorter()`  — Module 2: File Sorter by extension
- `_run_empty()`   — Module 3: Empty folder deleter  
- `_run_dupes()`   — Module 4: Duplicate file finder

Paste your script logic into each method. Use these helpers to communicate
back to the GUI:

```python
self._log("Some message")           # plain log line
self._log("Warning!", "warn")       # coloured: ok / warn / err / info / dim
self._progress(mod_idx, 50, "...")  # update progress bar (0-100)
```

Check stop/skip signals inside loops:
```python
if self._stop_flag.is_set() or self._skip_flag.is_set():
    return
```

Config values arrive in the `cfg` dict — keys match the GUI fields:
- mod1 cfg: src, out, misc_catch, rename_dupe
- mod2 cfg: src, recurse, dry_run
- mod3 cfg: src, hash_md5, hash_sha1, size_pre, dry_run

## Module 1 — Archive Extractor (fully implemented)

Supports: .zip, .rar, .7z, .zst (Zstandard)

Workflow:
1. Scans source folder recursively for archives
2. Any archive containing nested archives → moved to Nested Archive Dest (not extracted)
3. All clean archives → extracted into subfolders in Output folder
4. Optionally deletes source archives after extraction
