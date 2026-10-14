"""Permission-rule editing for the dashboard.

The policy engine already supports layered `allow` / `ask` / `deny` rules, including argument-aware
ones like `"run_bash:git *"`. The only way to reach it was hand-editing a `settings.json`, which is
why it read as a missing feature. This module gives the UI a structured way in.

Rules are always written to the USER settings file, never to a project or system file:
  * a project file is version-controlled and shared, so a UI click mutating it would surprise people
  * the system file is an administrative lock (`ProgramData`) and must not be editable from a browser
  * the user file is the one that belongs to whoever is sitting at the machine

Precedence is unchanged: system > project > user, so a rule set here cannot weaken a project or
administrative policy. That is reported in the UI rather than hidden.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

DECISIONS = ("allow", "ask", "deny")


class PermissionRules:
    """Read and write the user layer of the permission policy."""

    def __init__(self, user_settings_file: Path, project_root: Path | None = None) -> None:
        self._user_file = user_settings_file
        self._project_root = project_root
        self._lock = threading.Lock()

    # -- reading -------------------------------------------------------------------
    def snapshot(self) -> dict:
        """Everything the panel needs: the effective rules and where each layer came from."""
        from ..settings import load_settings

        settings = load_settings(self._project_root)
        # The user layer is the last one contributed by the user file; recomputed here rather than
        # assumed, so a hand-edited file shows up correctly.
        user_rules = self._read_user_rules()
        effective = {}
        for index, layer in enumerate(settings.permission_layers):
            effective[f"layer{index}"] = layer
        return {
            "user_rules": user_rules,
            "decisions": list(DECISIONS),
            "effective": settings.permissions,
            "layers": settings.permission_layers,
            "sources": settings.sources,
            "user_file": str(self._user_file),
            "higher_priority_active": len(settings.permission_layers) > (1 if user_rules else 0),
        }

    def _read_user_rules(self) -> dict:
        try:
            data = json.loads(self._user_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        perms = data.get("permissions")
        return {str(k): str(v) for k, v in perms.items()} if isinstance(perms, dict) else {}

    # -- writing -------------------------------------------------------------------
    def save(self, rules: dict) -> dict:
        """Replace the user layer's rules. Validates keys and decisions before writing.

        Raises ValueError on invalid input so the caller can report it, rather than writing a file
        whose rules would then be silently ignored at request time.
        """
        from ..policy import validate_rule_key

        cleaned: dict[str, str] = {}
        for key, value in (rules or {}).items():
            key = str(key).strip()
            if not key:
                continue
            reason = validate_rule_key(key)
            if reason:
                raise ValueError(f"{key!r}: {reason}")
            decision = str(value).strip().lower()
            if decision not in DECISIONS:
                raise ValueError(f"{key!r}: decision must be one of {', '.join(DECISIONS)}")
            cleaned[key] = decision

        with self._lock:
            data: dict = {}
            try:
                data = json.loads(self._user_file.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError):
                data = {}
            if cleaned:
                data["permissions"] = cleaned
            else:
                data.pop("permissions", None)

            payload = json.dumps(data, indent=2)
            try:
                self._user_file.parent.mkdir(parents=True, exist_ok=True)
                temp = self._user_file.with_suffix(".json.tmp")
                try:
                    temp.write_text(payload, encoding="utf-8")
                    temp.replace(self._user_file)
                except OSError:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._user_file.write_text(payload, encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"could not write {self._user_file}: {exc}") from exc
        return self.snapshot()
