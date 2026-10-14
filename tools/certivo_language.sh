#!/bin/bash
# Establish how Certivo handles CONTENT language, before authoring 500 questions in the wrong one.
#
# The UI is already bilingual -- app/src has an i18n resource table with `en` and `id` blocks. What is
# not yet established is whether question content is bilingual too: whether there is one content tree
# with both languages, two trees, or a single tree whose language is whatever the author typed. The
# data-analyst SQL questions mix English prompts with Indonesian instructions, which suggests the last
# of those. Writing 500 English questions into the wrong structure would be unrecoverable work.
cd /srv/certivo || exit 1

echo "=== content directories: is there a language dimension? ==="
find content -maxdepth 2 -type d | head -20
echo
echo "=== any locale-suffixed content or directories? ==="
find content -maxdepth 3 -type d \( -name "*en*" -o -name "*id*" -o -name "*locale*" -o -name "*lang*" \) 2>/dev/null | head -10
echo "  (empty means single-tree content)"
echo
echo "=== does the content schema mention a language field? ==="
grep -n "language\|locale" shared/src/contentSchemas.ts | head -10
echo
echo "=== does the questions table store a language? ==="
grep -n "language\|locale" worker/src/db/schema.ts | head -12
echo
echo "=== is there a translation layer for content? ==="
grep -rln "translateQuestion\|contentTranslation\|questionI18n\|locale" worker/src shared/src app/src 2>/dev/null | grep -v node_modules | head -10
echo
echo "=== how many level files mention Indonesian? sample a few tracks ==="
for t in ai-ml-engineer data-analyst fullstack-developer backend-developer; do
  n=$(grep -l -E "yang|dengan|adalah|untuk|kamu|Tabel|Kembalikan" content/tracks/$t/levels/*.json 2>/dev/null | wc -l)
  echo "  $t: $n of 20 level files contain Indonesian words"
done
echo
echo "=== the app's i18n: is question text in the translation table? ==="
grep -rn "questionsPerAttempt\|rubricMarkdown\|instructionsMarkdown" app/src --include=*.ts --include=*.tsx 2>/dev/null | grep -v node_modules | head -8
