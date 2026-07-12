"""
random_name_muxer.py — testing helper for the srrdb Scene Rebuilder.

Drop this script into a folder and run it. It recursively renames every file
and folder beneath it to a random gibberish name. File EXTENSIONS are kept
(so .mkv stays .mkv — media detection still works, names give nothing away).

The script itself and its log file are left untouched. Old→new mappings are
written to _rename_log.txt so you can trace what anything used to be.

Run:  python random_name_muxer.py        (or double-click)
"""

import os
import random
import string
import sys

CHARS = string.ascii_lowercase
SELF = os.path.abspath(__file__)
ROOT = os.path.dirname(SELF)
LOG = os.path.join(ROOT, "_rename_log.txt")


def random_name(existing: set) -> str:
    while True:
        name = "".join(random.choices(CHARS, k=random.randint(3, 12)))
        if name not in existing:
            existing.add(name)
            return name


def main():
    targets_files = []
    targets_dirs = []
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=False):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) in (SELF, os.path.abspath(LOG)):
                continue
            targets_files.append(full)
        for dn in dirnames:
            targets_dirs.append(os.path.join(dirpath, dn))

    if not targets_files and not targets_dirs:
        print("Nothing to rename here.")
        input("Press Enter to close…")
        return

    print(f"Folder: {ROOT}")
    print(f"About to rename {len(targets_files)} file(s) and {len(targets_dirs)} folder(s).")
    answer = input("Proceed? (y/n): ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    used: set = set()
    log_lines = []

    # Files first (paths still valid), then folders deepest-first
    # (targets_dirs is already deepest-first thanks to topdown=False).
    for full in targets_files:
        d, fn = os.path.split(full)
        ext = os.path.splitext(fn)[1]  # keep extension so tools still work
        new = random_name(used) + ext.lower()
        try:
            os.rename(full, os.path.join(d, new))
            log_lines.append(f"{full}  ->  {new}")
        except OSError as e:
            print(f"  ! failed: {fn}: {e}")

    for full in targets_dirs:
        d, dn = os.path.split(full)
        new = random_name(used)
        try:
            os.rename(full, os.path.join(d, new))
            log_lines.append(f"{full}  ->  {new}")
        except OSError as e:
            print(f"  ! failed: {dn}: {e}")

    with open(LOG, "a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")

    print(f"Done — {len(log_lines)} item(s) renamed. Map saved to _rename_log.txt")
    input("Press Enter to close…")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
