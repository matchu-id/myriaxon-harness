import re
from pathlib import PurePath

# Only extensions common enough in real code/docs that a hallucinated reference is worth
# flagging -- deliberately NOT a bare "\w+\.\w+" pattern, which would false-positive constantly
# on things like "e.g.", version numbers ("3.12"), or model names ("gpt-4.1").
_CODE_LIKE_EXTENSIONS = {
    "py", "ts", "tsx", "js", "jsx", "json", "md", "toml", "yaml", "yml", "txt",
    "go", "rs", "java", "cs", "cpp", "c", "h", "hpp", "rb", "php", "sql", "sh", "ps1",
}
_PATH_TOKEN_RE = re.compile(r"[\w][\w./\\-]{1,}\.[A-Za-z]{1,5}\b")


def find_unverified_paths(reply_text: str, touched_paths: set[str]) -> list[str]:
    """Conservative, opt-in heuristic (see ToolContext.grounding_enabled): flags path-like
    tokens in the assistant's reply that don't match (by substring/basename) any path a tool
    call actually touched this turn. False negatives are expected and fine -- this is a nudge to
    double-check, not a guarantee of correctness. False positives are the thing to actively avoid,
    hence the narrow extension allowlist and substring/basename (not exact-path) matching against
    touched paths, which tolerates the model writing a relative path when the tool call used an
    absolute one, or vice versa."""
    candidates: dict[str, None] = {}  # dict-as-ordered-set: de-dup while preserving first-seen order
    for match in _PATH_TOKEN_RE.finditer(reply_text):
        token = match.group(0)
        ext = token.rsplit(".", 1)[-1].lower()
        if ext not in _CODE_LIKE_EXTENSIONS:
            continue
        candidates[token] = None

    if not candidates:
        return []

    touched_norms = {t.replace("\\", "/").lower() for t in touched_paths if t}
    unverified = []
    for token in candidates:
        token_norm = token.replace("\\", "/").lower()
        token_basename = PurePath(token_norm).name
        matched = any(
            token_norm in touched or touched.endswith(token_norm) or PurePath(touched).name == token_basename
            for touched in touched_norms
        )
        if not matched:
            unverified.append(token)
    return unverified
