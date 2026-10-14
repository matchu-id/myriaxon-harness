#!/usr/bin/env python3
"""Verify the rebuilt seed's badge rows on the VPS, returning ASCII-only output.

Reading emoji-bearing rows back through this console raises UnicodeEncodeError on cp1252, so all parsing
happens remotely and only ASCII verdicts come back.
"""
import re
import subprocess
import sys

HOST = "admin-certivo@139.190.97.11"

REMOTE = r'''
import re, collections
path = "/srv/certivo/db/seed.postgres.sql"
src = open(path, encoding="utf-8").read()

pat = re.compile(r"^INSERT INTO badges \(id, track_id, name, description, icon_emoji, criteria_type, criteria_value_json, sort_order\) VALUES \((.*)\) ON CONFLICT \(id\) DO UPDATE SET .*;$", re.M)

# Split each VALUES tuple on commas that sit outside single-quoted strings (SQL escapes ' as '').
def split_tuple(s):
    out, cur, in_str = [], [], False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "'":
                if i + 1 < len(s) and s[i+1] == "'":
                    cur.append("''"); i += 2; continue
                in_str = False; cur.append(c)
            else:
                cur.append(c)
        else:
            if c == "'":
                in_str = True; cur.append(c)
            elif c == ",":
                out.append("".join(cur).strip()); cur = []
            else:
                cur.append(c)
        i += 1
    out.append("".join(cur).strip())
    return out

rows = []
for m in pat.finditer(src):
    v = split_tuple(m.group(1))
    rows.append(v)

print("parsed badge rows:", len(rows))
bad_cols = [r for r in rows if len(r) != 8]
print("rows with wrong column count:", len(bad_cols))

def unq(x):
    return x[1:-1].replace("''", "'") if x.startswith("'") and x.endswith("'") else x

ids = [unq(r[0]) for r in rows]
orders = [int(r[7]) for r in rows]
types = collections.Counter(unq(r[5]) for r in rows)

dupe_ids = [k for k, c in collections.Counter(ids).items() if c > 1]
dupe_orders = [k for k, c in collections.Counter(orders).items() if c > 1]
print("duplicate ids:", len(dupe_ids), dupe_ids[:10])
print("duplicate sort_orders:", len(dupe_orders), sorted(dupe_orders)[:10])
print("sort_order range:", min(orders), "..", max(orders))

ALLOWED = {
 "level_complete","first_code_pass","perfect_score","track_complete","first_try_pass",
 "attempts_to_pass","speed_pass","skill_star","skill_count_at_star","levels_completed",
 "tracks_completed","code_tests_perfect","level_streak",
}
unknown = sorted(set(types) - ALLOWED)
print("criteria types NOT in BADGES.md:", unknown)
print("criteria type counts:", dict(sorted(types.items())))

NEW = ["sharpshooter","flawless-five","flawless-ten","clean-sweep","clean-run-five","unbroken-ten",
 "comeback","persistent","indomitable","quick-study","speedrun","blitz","adept","expert","master",
 "grandmaster","polymath","explorer","generalist","polymath-tracks","completionist","test-passer",
 "all-green","polyglot"]
missing = [i for i in NEW if ids.count(i) != 1]
print("new platform badges found:", len(NEW) - len(missing), "of", len(NEW), "missing:", missing)

by_id = {unq(r[0]): r for r in rows}
print()
print("--- criteria_value_json for every new platform badge ---")
for i in NEW:
    r = by_id.get(i)
    if r is None:
        print(i, "MISSING"); continue
    print("%-16s type=%-20s value=%s sort=%s" % (i, unq(r[5]), unq(r[6]), r[7]))

print()
print("--- track_id NULL count (platform badges) ---")
print("null track_id rows:", sum(1 for r in rows if r[1] == "NULL"))
print()
print("--- sample level badge rows ---")
for i in ["data-analyst.L01", "data-analyst.L20", "ai-governance.L20", "hr-generalist.L11"]:
    r = by_id.get(i)
    print(i, "->", ("MISSING" if r is None else "type=%s value=%s sort=%s track=%s" % (unq(r[5]), unq(r[6]), r[7], unq(r[1]))))

print()
print("--- frozen originals unchanged ---")
for i, want in [("first-steps","level_complete"),("code-warrior","first_code_pass"),("perfect-score","perfect_score")]:
    r = by_id.get(i)
    print(i, "->", "MISSING" if r is None else "type=%s value=%s sort=%s" % (unq(r[5]), unq(r[6]), r[7]), "(want %s)" % want)
print()
print("--- graduate badges ---")
grads = [i for i in ids if i.endswith(".graduate")]
print("graduate ids:", len(grads), "all track_complete:", all(unq(by_id[i][5]) == "track_complete" for i in grads))
print("level badge ids:", len([i for i in ids if re.search(r"\.L\d\d$", i)]))
'''

r = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST, "python3 -"],
    input=REMOTE.encode("utf-8"), capture_output=True, timeout=600,
)
out = (r.stdout or b"").decode("utf-8", "replace")
err = (r.stderr or b"").decode("utf-8", "replace")
print(out, end="")
if err.strip():
    print("STDERR:", err, file=sys.stderr)
sys.exit(r.returncode)
