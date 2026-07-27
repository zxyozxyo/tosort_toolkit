#!/usr/bin/env python3
"""WinRAR pack gap audit — shows exactly which WinRAR builds your rescene pack
is missing, so you know precisely what to hunt down and add.

Run:  python winrar_pack_audit.py
It scans apps/winrar_pack-4.20/ for the extracted <date>_rar<ver>[b<n>].exe
files and compares them against the full list of RAR4-capable finals (2.50 ->
6.24). Betas are reported as coverage (which versions have none) — the exact
per-version beta count lives on the ReScene wiki (see the printout).

Adding a build later: drop the installer (wrar<ver>[b<n>].exe, e.g. wrar500b3.exe,
or winrar-x64-<ver>[b<n>].exe) into apps/winrar_pack-4.20/ and run the GUI's
"Setup RAR versions" — it extracts + date-names it automatically. 7.x is
auto-skipped (WinRAR 7 removed RAR4 creation, so it's useless for rebuilding).
"""
import re
import sys
from pathlib import Path

# Windows consoles default to cp1252 and choke on the box/arrow glyphs — force
# UTF-8 so the report never crashes mid-print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PACK = Path(__file__).parent / "apps" / "winrar_pack-4.20"

# Every RAR4-capable FINAL (ver_key -> release date). Mirrors _WINRAR_DATES in
# srrdb_tool.py. 6.24 (2023) is the last that can still create RAR4 archives.
FINALS = {
    "250": "1999-08-02", "260": "1999-10-21", "270": "2000-11-30",
    "271": "2001-01-11", "272": "2001-01-29", "280": "2001-06-01",
    "281": "2001-07-19", "290": "2001-08-29", "291": "2001-11-29",
    "293": "2002-08-14", "300": "2002-05-14", "301": "2003-06-04",
    "302": "2003-10-01", "310": "2003-11-27", "311": "2004-02-01",
    "320": "2004-05-20", "330": "2004-07-20", "340": "2004-09-10",
    "341": "2004-11-04", "342": "2005-02-22", "350": "2005-08-22",
    "351": "2005-09-26", "360": "2005-11-21", "361": "2006-04-05",
    "362": "2006-05-30", "370": "2007-06-07", "371": "2007-07-05",
    "380": "2008-09-22", "390": "2009-09-23", "391": "2010-07-19",
    "392": "2010-09-28", "393": "2010-12-23", "400": "2011-03-09",
    "401": "2011-06-14", "410": "2012-01-17", "411": "2012-03-15",
    "420": "2012-06-09", "500": "2013-10-12", "501": "2013-12-05",
    "510": "2014-04-16", "511": "2014-05-21", "520": "2014-12-18",
    "521": "2015-06-11", "530": "2015-08-10", "531": "2016-04-21",
    "540": "2016-10-25", "550": "2017-05-16", "560": "2018-02-05",
    "561": "2018-06-05", "570": "2019-05-06", "571": "2019-08-15",
    "580": "2020-01-14", "590": "2020-05-07", "591": "2020-07-27",
    "600": "2020-12-08", "601": "2021-01-25", "602": "2021-07-08",
    "610": "2021-12-27", "611": "2022-03-03", "620": "2023-02-28",
    "621": "2023-05-01", "622": "2023-08-01", "623": "2023-08-30",
    "624": "2023-10-04",
}

# The window most console-scene (3DS/NDS/Wii/PSP) releases fall in — prioritise
# filling beta gaps here first, it's where the "No good RAR version found"
# walls cluster.
SCENE_ERA = ("2004-01-01", "2017-01-01")


def ver_label(k):
    return f"{k[0]}.{k[1:]}"


def main():
    if not PACK.is_dir():
        print(f"Pack folder not found: {PACK}")
        return
    have_finals, have_betas = set(), {}
    rx = re.compile(r"^\d{4}-\d{2}-\d{2}_rar(\d{3})(b\d)?\.exe$", re.I)
    for f in PACK.iterdir():
        m = rx.match(f.name)
        if not m:
            continue
        ver, beta = m.group(1), (m.group(2) or "").lower()
        if beta:
            have_betas.setdefault(ver, set()).add(beta)
        else:
            have_finals.add(ver)

    finals_by_date = sorted(FINALS.items(), key=lambda kv: kv[1])
    missing_finals = [(k, d) for k, d in finals_by_date if k not in have_finals]
    no_beta = [(k, d) for k, d in finals_by_date
               if k not in have_betas]
    era_no_beta = [(k, d) for k, d in no_beta if SCENE_ERA[0] <= d < SCENE_ERA[1]]

    print("=" * 70)
    print(" WinRAR pack audit")
    print("=" * 70)
    print(f" Finals present : {len(have_finals)}/{len(FINALS)}")
    print(f" Betas present  : {sum(len(v) for v in have_betas.values())} "
          f"across {len(have_betas)} version(s)")
    print()

    if missing_finals:
        print(f"- MISSING FINALS ({len(missing_finals)}) — get these first, "
              "they're the base builds:")
        for k, d in missing_finals:
            print(f"    {d}   WinRAR {ver_label(k):<5}  -> wrar{k}.exe")
        print()
    else:
        print("- All RAR4 finals present. OK\n")

    print(f"- VERSIONS WITH NO BETA in the pack ({len(no_beta)} of "
          f"{len(FINALS)}):")
    print("   Scene groups often packed with a BETA, so these are the likely")
    print("   'No good RAR version found' culprits. Priority = console-scene era")
    print(f"   ({SCENE_ERA[0][:4]}-{SCENE_ERA[1][:4]}):")
    print()
    print("   * PRIORITY (scene era, no beta yet):")
    for k, d in era_no_beta:
        print(f"       {d}   WinRAR {ver_label(k):<5}  -> hunt wrar{k}b*.exe")
    print()
    others = [(k, d) for k, d in no_beta if (k, d) not in era_no_beta]
    if others:
        print("   · other eras (lower priority):")
        print("     " + ", ".join(ver_label(k) for k, _ in others))
    print()
    print("=" * 70)
    print(" WHERE TO GET THEM")
    print("   - Finals:  rarlab.com/rar/wrar<ver>.exe   (many old finals live)")
    print("   - Betas :  ReScene wiki 'rar-versions' (curated set 2.00->5.30b4+),")
    print("              FileHippo 'winrar-beta/history', or Wayback snapshots")
    print("              of rarlab.com/download.htm from the target year.")
    print("   - Best  :  the ReScene community's all-in-one pyrescene pack")
    print("              (rescene.wikidot.com — forum has a curated WinRAR set).")
    print(" Then: drop wrar<ver>[b<n>].exe into apps/winrar_pack-4.20/ and run")
    print(" the GUI 'Setup RAR versions'.")
    print("=" * 70)


if __name__ == "__main__":
    main()
