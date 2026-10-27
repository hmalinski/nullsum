"""Summarise the per-suite --unittest TSVs into the pass/fail table (one row per build/config)."""
import glob
import os
import re
import sys

d = sys.argv[1]
order = ["baseline-debug", "release-dormant", "release-M1misattr", "release-M2skim",
         "release-M1misattr-NETREMOVED", "release-M2skim-NETREMOVED",
         "debug-dormant", "debug-M1misattr", "debug-M2skim",
         "debug-M1misattr-NETREMOVED", "debug-M2skim-NETREMOVED"]
print(f"{'config':32s} suites  clean  fail(n-suites/n-tests)  aborted(SIGABRT)  other-nonzero")
for name in order:
    f = os.path.join(d, name + ".tsv")
    if not os.path.exists(f):
        continue
    clean = failing = aborted = other = tests_failed = 0
    failing_names, aborted_names = [], []
    rows = [l.rstrip("\n").split("\t") for l in open(f)]
    for r in rows:
        suite, rc, summ = r[0], int(r[1]), r[3] if len(r) > 3 else ""
        m = re.search(r"(\d+) failures?", summ)
        nf = int(m.group(1)) if m else 0
        if rc == 134:
            aborted += 1; aborted_names.append(suite)
        elif rc == 0 and nf == 0:
            clean += 1
        elif nf > 0:
            failing += 1; tests_failed += nf; failing_names.append(f"{suite}({nf})")
        else:
            other += 1
    print(f"{name:32s} {len(rows):6d} {clean:6d}  {failing:3d}/{tests_failed:<6d}            {aborted:3d}               {other:3d}")
    if failing_names:
        print("    failing suites : " + ", ".join(failing_names))
    if aborted_names:
        print("    aborted suites : " + ", ".join(aborted_names))
