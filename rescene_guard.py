"""One process, two tools, one pyReScene — hand each tool its own copy.

main.py runs the srrdb rebuilder and the RSR scanner as windows on a SINGLE
Python process (main.py:19-20), so both reach the same `sys.modules`. pyReScene
is not shareable that way: `srrdb_tool._fresh_rescene()` purges and re-imports
`rescene.main` per reconstruction, then patches `custom_popen` with ITS stop and
skip flags and ITS 30-minute deadline, and subscribes ITS logger. Whatever
imports rescene afterwards silently inherits all of it.

Observed while both tools ran together: an RSR capture writing its legacy .srr
for Monster_High printed

    rescene: Processing file: catrvm2x.rar
    rescene: Don't delete 'em yet!

into the SRRDB window, in the middle of reconstructing a different release
(cat-tqmj). Confusing in the log, but the real hazard is the other direction —
RSR was then running under srrdb's popen patch, so an srrdb Stop, or its
reconstruct deadline expiring, would raise inside RSR's unrelated SRR creation.

`load_private()` gives the caller a module object nobody else holds, and leaves
`sys.modules` exactly as it found it, so a tool that is mid-flight with its own
copy sees nothing change. The lock covers the purge-and-reimport window, which
is the only point where two threads could interleave and leave one of them with
a half-initialised package.
"""

import sys
import threading

LOCK = threading.RLock()


def load_private():
    """Import a pristine `rescene.main` that no other caller has a handle on.

    Returns the module. Raises ImportError like a normal import if pyReScene
    is not installed."""
    with LOCK:
        saved = {n: m for n, m in list(sys.modules.items())
                 if n == "rescene" or n.startswith("rescene.")}
        for n in saved:
            del sys.modules[n]
        try:
            import rescene.main as rm      # noqa: F401  (fresh instance)
            return rm
        finally:
            for n in [x for x in list(sys.modules)
                      if x == "rescene" or x.startswith("rescene.")]:
                del sys.modules[n]
            sys.modules.update(saved)
