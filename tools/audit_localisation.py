#!/usr/bin/env python3
"""Measure the data-analyst track's Indonesian coverage on the VPS, as the pattern to follow.

Three attempts were needed to get a real number here, and the failures are worth recording because each one
produced a plausible-looking result rather than an error.

1. The probe ran `python3` inside `certivo-app-1`, which has Node and not Python. The command failed and the
   empty output was reported as "0 file(s)" -- indistinguishable from a track with no content.
2. The second version embedded a multi-line Node program in an `ssh` command and then in a `node -e`
   argument, so the shell ate the quoting. Also reported nothing.
3. This version writes the probe to a temporary file on the *host*, pipes it into `node` through stdin inside
   the container, and passes the slug as argv. No nested quoting, no heredoc, no shell escaping.

The lesson is the same one this project keeps re-learning about its tooling: an empty result and a broken
command look identical, so a probe has to be able to say "I failed" rather than "there is nothing there".
This one prints an explicit error when the track directory is absent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOST = "admin-certivo@139.190.97.11"
CONTAINER = "certivo-app-1"
LOCAL_ROOT = Path(r"C:\Users\arian\OneDrive\ドキュメント\Myriaxon\certivo-rework")

PROBE = r"""
const fs = require('fs');
const slug = process.argv[2];
const dir = '/app/content/tracks/' + slug + '/levels';
let files;
try {
  files = fs.readdirSync(dir).filter((f) => f.endsWith('.json')).sort();
} catch (error) {
  process.stdout.write(JSON.stringify({ error: 'no such track: ' + dir }) + '\n');
  process.exit(0);
}
const counts = {};
const bump = (field, present) => {
  if (!counts[field]) counts[field] = [0, 0];
  counts[field][1] += 1;
  if (present) counts[field][0] += 1;
};
let questions = 0;
for (const file of files) {
  const data = JSON.parse(fs.readFileSync(dir + '/' + file, 'utf8'));
  const cs = data.caseStudy || {};
  bump('case.titleId', !!cs.titleId);
  bump('case.scenarioMarkdownId', !!cs.scenarioMarkdownId);
  for (const q of data.questions || []) {
    questions += 1;
    bump('q.promptId', !!q.promptId);
    bump('q.explanationId', !!q.explanationId);
    for (const o of q.options || []) bump('opt.textId', !!o.textId);
    if (q.code) bump('code.instructionsMarkdownId', !!q.code.instructionsMarkdownId);
    if (q.essay) bump('essay.rubricMarkdownId', !!q.essay.rubricMarkdownId);
  }
}
process.stdout.write(JSON.stringify({ files: files.length, questions, counts }) + '\n');
"""


def probe(slug: str) -> dict:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", HOST,
         f"docker exec -i {CONTAINER} node - {slug}"],
        input=PROBE.encode("utf-8"), capture_output=True, timeout=300,
    )
    out = (result.stdout or b"").decode("utf-8", "replace")
    err = (result.stderr or b"").decode("utf-8", "replace")
    for line in reversed((out + "\n" + err).splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"error": f"probe returned nothing (exit {result.returncode}): {err.strip()[:200]}"}


def local(slug: str) -> dict:
    root = LOCAL_ROOT / slug / "levels"
    if not root.is_dir():
        return {"error": "not in the workspace"}
    counts: dict[str, list[int]] = {}

    def bump(field: str, present: bool) -> None:
        counts.setdefault(field, [0, 0])
        counts[field][1] += 1
        if present:
            counts[field][0] += 1

    files = sorted(root.glob("*.json"))
    questions = 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        cs = data.get("caseStudy", {})
        bump("case.titleId", bool(cs.get("titleId")))
        bump("case.scenarioMarkdownId", bool(cs.get("scenarioMarkdownId")))
        for question in data.get("questions", []):
            questions += 1
            bump("q.promptId", bool(question.get("promptId")))
            bump("q.explanationId", bool(question.get("explanationId")))
            for option in question.get("options", []):
                bump("opt.textId", bool(option.get("textId")))
            if question.get("code"):
                bump("code.instructionsMarkdownId", bool(question["code"].get("instructionsMarkdownId")))
            if question.get("essay"):
                bump("essay.rubricMarkdownId", bool(question["essay"].get("rubricMarkdownId")))
    return {"files": len(files), "questions": questions, "counts": counts}


def show(label: str, payload: dict) -> None:
    if "error" in payload or not payload:
        print(f"    {label}: {payload.get('error', 'nothing returned')}")
        return
    print(f"    {label}: {payload['files']} file(s), {payload['questions']} question(s)")
    for field, (have, total) in sorted(payload.get("counts", {}).items()):
        pct = (100 * have / total) if total else 0
        mark = "OK " if have == total else "   "
        print(f"      {mark}{field:<30} {have:>5} / {total:<5} {pct:>5.1f}%")


def main() -> int:
    slugs = sys.argv[1:] or ["data-analyst", "ai-ml-engineer", "fullstack-developer", "data-scientist"]
    for slug in slugs:
        print(f"  === {slug} ===")
        show("on the VPS", probe(slug))
        show("workspace", local(slug))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
