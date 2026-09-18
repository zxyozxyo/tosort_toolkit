#!/usr/bin/env python3
"""Volume-size signature audit — what volume size each group actually packed at,
measured across every captured .rsr rather than predicted from a scene ruleset.

Run:  python volume_signature_audit.py [--csv out.csv]

It reads recipe.volume_bytes out of every manifest in rsr_store/ and reports,
per (system, group), the modal volume size and how pure that mode is.

WHY THIS EXISTS, AND WHAT IT IS *NOT* FOR
-----------------------------------------
This was written to test a theory that turned out to be wrong, and it is kept
so nobody re-runs the idea from scratch. The theory was: scenerules.org
publishes an exact volume-size table per platform and era (2010 NDS mandates
5,000,000-byte parts; 2011 PS3 has seven brackets; 1998 GAMEiSO/ISOaRIP say
15,000,000 for a CD), so a release whose volume size violates its era's table
was not packed to the rules of its stated year -- which would make it cheap,
offline evidence that the local source is a REPACK rather than the scene
original. Three formulations, three nulls (2026-09-18, full corpus):

  1. Ruleset compliance. Falsified by the null check: for PSX/PS1 the MODAL
     volume size among *byte-exactly verified* genuine releases is 20,000,000,
     while the only applicable rulesets say 15,000,000. The rule simply does
     not govern those groups, so "violation" flags the majority of real
     releases.

  2. REPACK correlation, NDS (the one platform with an unambiguous rule).
     REPACK-tagged releases are MORE compliant than the rest, not less:
     97.3% exactly 5,000,000 (n=37) vs 78.7% (n=4,900). Backwards.

  3. Deviation from the group's OWN modal size, all systems. 449 deviants
     carry a 0.45% REPACK rate against a 0.59% corpus base rate. No signal.

A fourth guess -- that volume-size inconsistency marks a group that packed
non-deterministically, and so predicts capture difficulty -- also died: mean
delta-verify share is 52.4% for groups below 90% purity vs 59.4% for those at
or above it, again the wrong way round.

What the audit DOES produce is a measured per-group signature, and one of
those is sharp enough to be worth keeping: NDS group LITE packed 1,000 of
1,001 sets at 5,000,192 bytes and NONE at 5,000,000. 5,000,192 is 4883 * 1024,
i.e. that group spelled the switch `-v4883k` where everyone else wrote
`-v5000000b`. Use this as a capture-integrity check (a LITE release landing on
5,000,000 is the anomaly), never as a sweep prior -- rsr_tool reads the true
volume size straight out of the archive headers, so it never needs to guess.
"""
import argparse
import collections
import csv
import json
import re
import sys
import zipfile
from pathlib import Path

# Windows consoles default to cp1252 and choke on the box glyphs -- force UTF-8
# so the report never crashes mid-print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STORE = Path(__file__).parent / "rsr_store"

# Groups smaller than this have too few sets for a "modal size" to mean
# anything -- one oddity in a group of three reads as 33% impurity.
MIN_SETS = 20


def group_of(release: str) -> str:
    """Scene group = the tail after the final dash. '?' when unparseable."""
    m = re.search(r"-([A-Za-z0-9_.]+)$", release or "")
    return m.group(1).upper() if m else "?"


def read_sets(store: Path):
    """Yield one record per RAR set that names a real volume size.

    Skips the _backup / _index_backups / _removed-* housekeeping trees, and
    skips volume_bytes 0 -- that is a single-volume archive, which has no
    split size to compare and would otherwise swamp every distribution.
    """
    for p in store.rglob("*.rsr"):
        if any(part.startswith("_") for part in p.relative_to(store).parts):
            continue
        try:
            man = json.loads(zipfile.ZipFile(p).read("manifest.json"))
        except Exception:
            continue
        for s in man.get("sets") or []:
            if not str(s.get("format", "")).startswith("RAR"):
                continue
            vb = (s.get("recipe") or {}).get("volume_bytes") or 0
            if not vb:
                continue
            yield {
                "path": str(p),
                "release": man.get("release") or "",
                "system": man.get("system") or "",
                "year": man.get("year") or "",
                "group": group_of(man.get("release")),
                "volume_bytes": int(vb),
                "verify": s.get("verify") or "",
            }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", metavar="PATH",
                    help="also write the per-set rows here")
    ap.add_argument("--store", default=str(STORE), help="rsr_store to scan")
    args = ap.parse_args()

    store = Path(args.store)
    if not store.is_dir():
        print(f"no such store: {store}")
        return 1

    rows = list(read_sets(store))
    if not rows:
        print(f"no RAR sets with a volume size under {store}")
        return 1

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.csv}\n")

    by = collections.defaultdict(list)
    for r in rows:
        by[(r["system"], r["group"])].append(r)

    print(f"{len(rows):,} multi-volume RAR sets across "
          f"{len(by):,} (system, group) pairs\n")
    print(f"groups with >= {MIN_SETS} sets, least consistent first:\n")
    print("{:<9} {:<14} {:>6} {:>8} {:>16} {:>6}".format(
        "system", "group", "sets", "purity", "modal size", "other"))

    stats = []
    for (system, group), v in by.items():
        if len(v) < MIN_SETS:
            continue
        counts = collections.Counter(x["volume_bytes"] for x in v)
        mode, hits = counts.most_common(1)[0]
        stats.append((hits / len(v), system, group, len(v), mode,
                      len(v) - hits, len(counts)))

    for purity, system, group, n, mode, other, distinct in sorted(stats):
        print("{:<9} {:<14} {:>6} {:>7.1f}% {:>16,} {:>6}".format(
            system, group, n, 100 * purity, mode, other))

    shown = sum(s[3] for s in stats)
    print(f"\n{shown:,} sets in reported groups; "
          f"{len(rows) - shown:,} in groups below the {MIN_SETS}-set floor.")
    print("\nA low purity means the group did not pack to one volume size. It "
          "does NOT mean\nthose releases are repacks, and it does not predict "
          "capture difficulty -- both were\ntested and rejected; see this "
          "file's docstring before re-deriving either.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
