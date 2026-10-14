import base64
import re
from pathlib import Path

from .constants import ATTACHMENT_SIZE_CAP_BYTES, PROVIDER_ANTHROPIC

_ATTACH_RE = re.compile(r'@"([^"]+)"|@(\S+)')

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def parse_attachments(user_input: str) -> tuple[str, list[Path]]:
    """Extract @path / @"quoted path" tokens from a message, replacing each with a short
    [attached: filename] marker. Returns the cleaned text and the resolved path list."""
    paths: list[Path] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(1) or match.group(2)
        path = Path(raw).expanduser()
        paths.append(path)
        return f"[attached: {path.name}]"

    cleaned = _ATTACH_RE.sub(_replace, user_input)
    return cleaned, paths


def build_attachment_block(path: Path, provider: str) -> tuple[dict | None, str | None]:
    """Returns (content_block, error). Exactly one of the two is not None."""
    if not path.exists():
        return None, f"Attachment not found: {path}"
    if not path.is_file():
        return None, f"Attachment is not a file: {path}"

    size = path.stat().st_size
    if size > ATTACHMENT_SIZE_CAP_BYTES:
        return None, f"Attachment too large ({size} bytes, cap is {ATTACHMENT_SIZE_CAP_BYTES}): {path}"

    suffix = path.suffix.lower()
    try:
        data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        return None, f"Could not read attachment {path}: {exc}"

    if suffix == ".pdf":
        if provider == PROVIDER_ANTHROPIC:
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data_b64},
            }, None
        return {
            "type": "file",
            "file": {"filename": path.name, "file_data": f"data:application/pdf;base64,{data_b64}"},
        }, None

    if suffix in _IMAGE_MEDIA_TYPES:
        media_type = _IMAGE_MEDIA_TYPES[suffix]
        if provider == PROVIDER_ANTHROPIC:
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data_b64},
            }, None
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{data_b64}"},
        }, None

    return None, f"Unsupported attachment type ({suffix or 'no extension'}): {path}"
