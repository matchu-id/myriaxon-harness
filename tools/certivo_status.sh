#!/bin/bash
# Report the Certivo repo state. Piped over stdin to avoid the quoting problems that come from
# nesting PowerShell, bash and JSON escaping in a single ssh argument.
cd /srv/certivo 2>/dev/null || { echo "  cannot enter /srv/certivo"; exit 1; }

echo "=== repo ==="
git log --oneline -5 2>/dev/null
echo
echo "=== uncommitted work, which is what Claude Code has in flight ==="
git status --porcelain 2>/dev/null | head -30
echo "  changed files total: $(git status --porcelain 2>/dev/null | wc -l)"
echo
echo "=== size ==="
du -sh . 2>/dev/null
echo
echo "=== layout ==="
ls -1 2>/dev/null | head -24
echo
echo "=== tracks ==="
ls -1 content/tracks/ 2>/dev/null
echo
echo "=== data-analyst levels present ==="
ls -1 content/tracks/data-analyst/levels/ 2>/dev/null | tr '\n' ' '
echo
echo
echo "=== code-question languages still in use, per track ==="
for t in content/tracks/*/; do
  name=$(basename "$t")
  js=$(grep -o '"language": *"javascript"' "$t"levels/*.json 2>/dev/null | wc -l)
  sq=$(grep -o '"language": *"sql"' "$t"levels/*.json 2>/dev/null | wc -l)
  py=$(grep -o '"language": *"python"' "$t"levels/*.json 2>/dev/null | wc -l)
  ts=$(grep -o '"language": *"typescript"' "$t"levels/*.json 2>/dev/null | wc -l)
  printf "  %-28s javascript=%-4s sql=%-4s python=%-4s typescript=%s\n" "$name" "$js" "$sq" "$py" "$ts"
done
