"""Scan every module for annotations that cannot be evaluated.

On Python 3.14 (this venv) PEP 649 defers annotation evaluation, so a missing
`from typing import Optional` is silent. On <=3.13 the same code is a hard
ImportError at module load. This finds every such latent breakage repo-wide.
"""
import importlib, sys, traceback, typing, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pkgs = ["research", "src", "services", "config"]
mods = []
for pkg in pkgs:
    for p in (ROOT / pkg).rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        rel = p.relative_to(ROOT).with_suffix("")
        mods.append(".".join(rel.parts))

import_errors, ann_errors, ok = [], [], 0
for m in sorted(set(mods)):
    if m.endswith(".__init__"):
        m = m[:-9]
    try:
        mod = importlib.import_module(m)
    except Exception as e:
        import_errors.append((m, type(e).__name__, str(e)[:80]))
        continue
    ok += 1
    for name, obj in list(vars(mod).items()):
        if isinstance(obj, type) and getattr(obj, "__module__", "") == m:
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                ann_errors.append((m, name, type(e).__name__, str(e)[:80]))
        elif callable(obj) and getattr(obj, "__module__", "") == m:
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                ann_errors.append((m, name + "()", type(e).__name__, str(e)[:80]))

print("=" * 100)
print("REPO-WIDE ANNOTATION AUDIT  (python %s)" % sys.version.split()[0])
print("=" * 100)
print("modules scanned : %d" % len(set(mods)))
print("imported OK     : %d" % ok)
print("import FAILURES : %d" % len(import_errors))
for m, t, e in import_errors:
    print("   FAIL %-48s %s: %s" % (m, t, e))
print()
print("annotation objects that FAIL to evaluate: %d" % len(ann_errors))
for m, n, t, e in ann_errors:
    print("   %-46s %-24s %s: %s" % (m, n, t, e))
print()
if not ann_errors:
    print("  -> No latent annotation breakages found in THIS snapshot.")
    print("     The reported Optional/Tuple errors therefore come from a different")
    print("     snapshot than this working tree.")