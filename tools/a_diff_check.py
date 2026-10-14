#!/usr/bin/env python3
"""ASCII-safe structured diff between the reconstructed original and the live build-seed.ts."""
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"

REMOTE = r'''
import difflib
a = open("/srv/certivo/content/scripts/build-seed.ts.orig.bak", encoding="utf-8").read().splitlines()
b = open("/srv/certivo/content/scripts/build-seed.ts", encoding="utf-8").read().splitlines()
print("orig.bak lines:", len(a), "live lines:", len(b))
added = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l.startswith("+") and not l.startswith("+++")]
removed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l.startswith("-") and not l.startswith("---")]
print("lines only in live (added by Agent A):", len(added))
print("lines only in orig.bak:", len(removed))
def ascii(s):
    return s.encode("ascii", "replace").decode("ascii")
print()
print("--- added lines (ascii-safe) ---")
for l in added:
    print(ascii(l))
print()
print("--- removed lines (ascii-safe) ---")
for l in removed:
    print(ascii(l))
'''

r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, "python3 -"],
                   input=REMOTE.encode("utf-8"), capture_output=True, timeout=600)
print((r.stdout or b"").decode("utf-8", "replace"), end="")
err = (r.stderr or b"").decode("utf-8", "replace")
if err.strip():
    print("STDERR:", err, file=sys.stderr)
sys.exit(r.returncode)
