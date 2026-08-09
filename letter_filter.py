"""
First-letter grouping, shared by the Folder Packer and both uploaders.

The Folder Packer uses these groups to name the batches it builds
(A.zip, B.zip, 0-9.zip, MISC.zip). The two IA uploaders use exactly the
same groups to decide which files of an oversized folder go up in this
run — that way "upload the D-G part of REDUMP AUDIO CD" needs no manual
copying into a staging folder, and the letters mean the same thing in
every tool.
"""

from pathlib import Path

# Canonical group order: A..Z, then digits, then everything else.
GROUPS = [chr(c) for c in range(ord('A'), ord('Z') + 1)] + ['0-9', 'MISC']


def get_letter_group(filename: str) -> str:
    """Return the letter group ('A'..'Z', '0-9', 'MISC') for a filename."""
    name = Path(filename).stem.strip()
    if not name:
        return 'MISC'
    first = name[0].upper()
    if first.isalpha():
        return first
    elif first.isdigit():
        return '0-9'
    else:
        return 'MISC'


def normalise_letters(letters) -> set:
    """
    Clean a selection coming from the GUI into a set of canonical group
    names. Returns an empty set for "nothing selected", which every
    caller treats as "no letter filter — take everything"; a selection
    that covers every group is likewise collapsed to empty so no
    pointless filtering work happens.
    """
    if not letters:
        return set()
    sel = {str(x).strip().upper() for x in letters if str(x).strip()}
    sel = {x for x in sel if x in GROUPS}
    if len(sel) == len(GROUPS):
        return set()
    return sel


def letter_allows(filename: str, letters: set) -> bool:
    """True if `filename` belongs to one of the selected groups."""
    if not letters:
        return True
    return get_letter_group(filename) in letters


def describe(letters) -> str:
    """Short human summary of a selection, e.g. 'A-C, 0-9' or 'all letters'."""
    sel = normalise_letters(letters)
    if not sel:
        return 'all letters'
    alpha = sorted(x for x in sel if len(x) == 1 and x.isalpha())
    parts = []
    run_start = None
    prev = None
    for ch in alpha:
        if run_start is None:
            run_start = prev = ch
            continue
        if ord(ch) == ord(prev) + 1:
            prev = ch
            continue
        parts.append(run_start if run_start == prev else f'{run_start}-{prev}')
        run_start = prev = ch
    if run_start is not None:
        parts.append(run_start if run_start == prev else f'{run_start}-{prev}')
    for extra in ('0-9', 'MISC'):
        if extra in sel:
            parts.append(extra)
    return ', '.join(parts)
