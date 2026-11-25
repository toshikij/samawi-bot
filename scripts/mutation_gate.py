"""Fail unless mutmut's kill rate is at least the given percentage: python scripts/mutation_gate.py 90"""

import json
import subprocess
import sys

threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
out = subprocess.run(["mutmut", "export-cicd-stats"], capture_output=True, text=True)
try:
    stats = json.load(open("mutants/mutmut-cicd-stats.json"))
except OSError:
    sys.exit(f"mutmut stats not found: {out.stderr}")
killed = stats.get("killed", 0) + stats.get("timeout", 0)
total = killed + stats.get("survived", 0) + stats.get("suspicious", 0)
rate = 100.0 * killed / total if total else 0.0
print(f"mutation kill rate {rate:.1f}% ({killed}/{total})")
sys.exit(0 if rate >= threshold else 1)
