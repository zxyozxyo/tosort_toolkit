#!/usr/bin/env python3
"""Re-file captured releases whose SYSTEM classification has changed.

Run:  python migrate_store_system.py                 # dry run, changes nothing
      python migrate_store_system.py --apply         # do it

The store is SYSTEM/YEAR/RELEASE. A release is filed under whatever
`_release_system` said at capture time, so when that function learns a new
platform every release already captured stays where it was put. Adding the
Dreamcast tag reclassified 1,251 names that had all been stored as `Unknown`;
this walks the store, asks `_release_system` again, and moves the folders that
now disagree.

Deliberately narrow:

  * Only folders whose stored system is WRONG by today's classifier move. A
    release already filed correctly is never touched.
  * `Unknown` is never a TARGET. If the classifier has somehow got worse for a
    name, that is a regression to look at, not a move to make -- so a release
    that would go from a real platform back to Unknown is reported and skipped.
  * A target that already exists is reported and skipped. Nothing is merged,
    nothing is overwritten.
  * The index is backed up before the first write, next to the three
    rsr_index.db.bak-* files already there, and `releases.rsr_path` is updated
    in the same pass -- a moved folder with a stale path in the index is worse
    than not moving it.
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
STORE = HERE / "rsr_store"
DB = HERE / "rsr_index.db"
# Housekeeping trees that are not systems.
SKIP = ("_backup", "_index_backups", "_removed", "_")


def plan(store: Path):
    """[(release, old_system, new_system, src, dst)] for every folder that is
    filed under the wrong system today."""
    sys.path.insert(0, str(HERE))
    from rsr_tool import _release_system

    out, odd = [], []
    for sysdir in sorted(p for p in store.iterdir() if p.is_dir()):
        if sysdir.name.startswith(SKIP):
            continue
        for yeardir in sorted(p for p in sysdir.iterdir() if p.is_dir()):
            for rel in sorted(p for p in yeardir.iterdir() if p.is_dir()):
                now = _release_system(rel.name)
                if now == sysdir.name:
                    continue
                if now == "Unknown":
                    odd.append((rel.name, sysdir.name, now))
                    continue
                out.append((rel.name, sysdir.name, now, rel,
                            store / now / yeardir.name / rel.name))
    return out, odd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually move (default is a dry run)")
    ap.add_argument("--store", default=str(STORE))
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    store, db = Path(args.store), Path(args.db)
    if not store.is_dir():
        print(f"no such store: {store}")
        return 1

    moves, odd = plan(store)
    if not moves and not odd:
        print("Every captured release is filed under the system the classifier "
              "gives it today. Nothing to do.")
        return 0

    by_route = {}
    for _, old, new, _, _ in moves:
        by_route[(old, new)] = by_route.get((old, new), 0) + 1
    print(f"{'APPLYING' if args.apply else 'DRY RUN — nothing will change'}\n")
    print(f"{len(moves):,} release(s) are filed under the wrong system:\n")
    for (old, new), n in sorted(by_route.items(), key=lambda kv: -kv[1]):
        print(f"   {old:<12} -> {new:<12} {n:>6,}")
    print("\n  examples:")
    for rel, old, new, src, dst in moves[:5]:
        print(f"   {old}/{src.parent.name}/{rel[:48]}")
        print(f"      -> {new}/{dst.parent.name}/")

    if odd:
        print(f"\n{len(odd)} release(s) would go from a known system BACK to "
              f"Unknown — skipped, and worth a look:")
        for rel, old, _ in odd[:8]:
            print(f"   {old:<10} {rel[:60]}")

    blocked = [m for m in moves if m[4].exists()]
    if blocked:
        print(f"\n{len(blocked)} target(s) already exist — skipped:")
        for rel, _, _, _, dst in blocked[:8]:
            print(f"   {dst.relative_to(store)}")

    todo = [m for m in moves if not m[4].exists()]
    if not args.apply:
        print(f"\nWould move {len(todo):,} folder(s) and update the same number "
              f"of rsr_path rows.\nRe-run with --apply to do it.")
        return 0

    if db.is_file():
        bak = db.with_name(db.name + ".bak-before-system-migration-"
                           + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(db, bak)
        print(f"\nindex backed up to {bak.name}")

    con = sqlite3.connect(db) if db.is_file() else None
    moved = failed = 0
    for rel, old, new, src, dst in todo:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except Exception as e:
            print(f"   FAILED {rel[:50]}: {type(e).__name__}: {e}")
            failed += 1
            continue
        moved += 1
        if con is not None:
            con.execute("UPDATE releases SET rsr_path=? WHERE name=?",
                        (str(dst / f"{rel}.rsr"), rel))
    if con is not None:
        con.commit()
        con.close()
    # Year folders left empty by the move are noise; the system folders stay.
    pruned = 0
    for sysdir in (p for p in store.iterdir() if p.is_dir()):
        if sysdir.name.startswith(SKIP):
            continue
        for yeardir in (p for p in sysdir.iterdir() if p.is_dir()):
            try:
                if not any(yeardir.iterdir()):
                    yeardir.rmdir()
                    pruned += 1
            except OSError:
                pass
    print(f"\nmoved {moved:,}, failed {failed}, empty year folder(s) "
          f"pruned {pruned}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
