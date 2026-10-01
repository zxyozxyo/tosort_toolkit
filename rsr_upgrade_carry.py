"""Carry, inside existing .rsr files, the non-game files they demand as content.

Captures made before 2026-10-01 treat the largest file as rebuild CONTENT even
when it is a cover jpg, a PPF, a guide pdf or an nfo -- files no DAT holds, so
those releases cannot rebuild once their source folder is gone. This reads the
missing files from the original releases (RomVault's copies, read-only),
checks each against the SHA-256 the capture recorded, and adds them to the
.rsr. The recipe is untouched. Every replaced .rsr is backed up first.

    python rsr_upgrade_carry.py G:\\RomRoot [--list rsr_recapture_2026-10-01.txt]
                                [--backup DIR] [--limit N]

Safe to stop and re-run: an upgraded .rsr no longer qualifies, so it is skipped.
"""
import argparse, json, sqlite3, sys, time, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rsr_tool as R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("romroot", nargs="+", help="folders laid out <System Scene YEAR>/<release>")
    ap.add_argument("--list", default=str(HERE / "rsr_recapture_2026-10-01.txt"))
    ap.add_argument("--backup", default=str(HERE / "rsr_upgrade_backup"))
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    api = R.RsrToolAPI()
    log = open(HERE / "rsr_upgrade_carry.log", "a", encoding="utf-8")
    api._log = lambda msg, kind="", *x, **k: (log.write(f"[{kind}] {msg}\n"), log.flush())
    api._emit = lambda *x, **k: None
    wanted = [l.strip() for l in Path(a.list).read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.startswith("#")]
    print(f"indexing {', '.join(a.romroot)} ...", flush=True)
    roots = {}
    for root in a.romroot:
        for p in Path(root).glob("*/*"):
            if p.is_dir():
                roots.setdefault(p.name, p)
    con = sqlite3.connect(f"file:{api._db_path}?mode=ro", uri=True)
    done = fail = nofolder = 0
    t0 = time.monotonic()
    for i, rel in enumerate(wanted, 1):
        if a.limit and i > a.limit:
            break
        row = con.execute("SELECT rsr_path FROM releases WHERE name=?", (rel,)).fetchone()
        if not row or not Path(row[0]).is_file():
            print(f"[{i}/{len(wanted)}] {rel}: no .rsr", flush=True)
            fail += 1
            continue
        rsr = Path(row[0])
        m = json.loads(zipfile.ZipFile(rsr).read("manifest.json"))
        orig = roots.get(Path(m.get("source_folder", "")).name)
        if orig is None:
            nofolder += 1
            print(f"[{i}/{len(wanted)}] {rel}: original not found", flush=True)
            continue
        r = api.upgrade_carry(rsr, orig, Path(a.backup))
        if r.get("ok") and r.get("changed"):
            done += 1
        elif not r.get("ok"):
            fail += 1
        print(f"[{i}/{len(wanted)}] {rel}: {r}", flush=True)
        log.write(f"{rel}: {r}\n")
    print(f"done in {time.monotonic() - t0:,.0f}s: {done} upgraded, {fail} failed, "
          f"{nofolder} original not found", flush=True)


if __name__ == "__main__":
    main()
