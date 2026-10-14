from pathlib import Path

_BOM = chr(0xFEFF)


def parse_frontmatter_doc(text: str, source: Path) -> tuple[dict, str] | None:
    """Parse a `---`-fenced YAML-ish frontmatter block followed by a body, the shared shape
    used by skills, custom commands, and subagent-type files. Returns (frontmatter, body) or
    None if the document doesn't start with a frontmatter fence."""
    # Strip a leading UTF-8 BOM -- common Windows tools (Notepad, PowerShell's
    # Set-Content -Encoding utf8) write it by default, and plain "utf-8" decoding
    # (unlike "utf-8-sig") leaves it in the string, silently breaking the fence check below.
    text = text.lstrip(_BOM)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    frontmatter: dict[str, str] = {}
    body_start = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = i + 1
            break
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()

    if body_start is None:
        return None

    body = "\n".join(lines[body_start:]).strip()
    return frontmatter, body
