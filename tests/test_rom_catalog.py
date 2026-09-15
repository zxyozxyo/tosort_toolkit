"""The information view (Supported systems & what they need) stays truthful.

    python tests/test_rom_catalog.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rom_tools as rt

results = []


def check(name, ok, detail=''):
    results.append(ok)
    print(f'{"PASS" if ok else "FAIL"}  {name}  {detail}')


cat = rt.system_catalog()
convs = [c for s in cat for c in s['conversions']]

missing = sorted(set(rt.CONVERSIONS) - set(rt.CONVERSION_INPUTS))
check('every conversion lists the file types it starts from', not missing, ', '.join(missing))
unnamed = sorted({c['system'] for c in rt.CONVERSIONS.values()} - set(rt.SYSTEM_NAMES))
check('every system has a display name', not unnamed, ', '.join(unnamed))
check('every conversion appears exactly once', len(convs) == len(rt.CONVERSIONS),
      f'{len(convs)} vs {len(rt.CONVERSIONS)}')

known = set(rt.KEY_FILES) | set(rt.EXTERNAL_TOOLS) | set(rt.PYTHON_PACKAGES)
bad = sorted({n for needs in rt.CONVERSION_NEEDS.values() for n, _, _ in needs} - known)
check('every requirement name is a registered key, tool or package', not bad, ', '.join(bad))

ext_tools = [cid for cid, s in rt.CONVERSIONS.items()
             if s['engine'] == rt.ENGINE_EXTERNAL and s.get('requires') in rt.EXTERNAL_TOOLS]
unlisted = [cid for cid in ext_tools
            if rt.CONVERSIONS[cid]['requires'] not in {n for n, _, _ in rt.CONVERSION_NEEDS.get(cid, [])}]
check('external-tool conversions name their program', not unlisted, ', '.join(unlisted))

keyed = [cid for cid, s in rt.CONVERSIONS.items()
         if s['engine'] == rt.ENGINE_KEYED and s.get('fn') and s.get('requires') in rt.KEY_FILES]
unlisted = [cid for cid in keyed
            if rt.CONVERSIONS[cid]['requires'] not in {n for n, _, _ in rt.CONVERSION_NEEDS.get(cid, [])}]
check('keyed conversions name their key file', not unlisted, ', '.join(unlisted))

planned = [c['id'] for c in convs if c['status'] == 'planned']
check('unbuilt engines show as planned, never ready',
      all(rt.CONVERSIONS[cid].get('fn') is None for cid in planned) and
      not [c for c in convs if c['status'] == 'ready' and rt.CONVERSIONS[c['id']].get('fn') is None],
      f'{len(planned)} planned')
liars = [c['id'] for c in convs if c['reversible'] and
         not rt.CONVERSIONS.get(rt.CONVERSIONS[c['id']].get('inverse') or '', {}).get('fn')]
check('"reversible" only where the way back is built', not liars, ', '.join(liars))

# a missing required file must turn a conversion from ready to missing
saved = rt.KEY_FILES['nds_blow']
rt.KEY_FILES['nds_blow'] = (Path('Z:/nowhere/bios7.bin'), saved[1])
try:
    nds = next(s for s in rt.system_catalog() if s['code'] == 'NDS')
    st = {c['id']: c['status'] for c in nds['conversions']}
    check('missing bios7.bin marks NDS decryption as missing',
          st['nds:encrypted->decrypted'] == 'missing' and st['nds:untrimmed->trimmed'] == 'ready', str(st))
finally:
    rt.KEY_FILES['nds_blow'] = saved

api = rt.RomToolsAPI().system_catalog()
check('GUI API returns JSON-serialisable data', api['ok'] and bool(json.dumps(api)))

print('=' * 70)
print('ALL PASS' if all(results) else f'{results.count(False)} FAILED')
sys.exit(0 if all(results) else 1)
