"""Persistent dashboard preferences.

Stored as JSON in `$MYRIAXON_CONFIG_DIR/dashboard_settings.json` (the same directory as the config), so
the Harness remembers how you like it across restarts and server launches.

Why these and not others: each one changes observable behaviour of the agent or the UI. Values that
would only make the server do something surprising (a bind address, a workspace path) deliberately
live outside this file -- the workspace is per-invocation, and the bind address is per-invocation.

Every setter is validated against the same constants the CLI uses, so an invalid value cannot be
persisted and then fail confusingly at request time.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Workflow breadth. `multi` lets the agent delegate to sub-agents; `single` withholds those tools so
# one agent does all the work (cheaper on context, which matters for small models).
WORKFLOWS = ("single", "multi")
WORKFLOW_LABELS = {
    "single": "Single agent — one agent does all the work",
    "multi": "Multi-workflow — enables sub-agent delegation (task / parallel_tasks)",
}

THEMES = ("dark", "light")


@dataclass
class DashboardSettings:
    # workflow
    workflow: str = "single"
    # agent behaviour
    thinking: bool = False
    effort: str = ""            # "" = provider default
    max_tokens: int = 0         # 0 = let the provider/model decide
    yolo: bool = True           # the dashboard cannot answer permission prompts, so default on
    # appearance
    theme: str = "dark"
    density: str = "comfortable"   # comfortable | compact
    show_thinking: bool = True
    # turn behaviour
    auto_compact: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        """Serializable view without the free-form extras."""
        data = self.to_dict()
        data.pop("extra", None)
        return data


class SettingsStore:
    """Loads, validates and saves the dashboard settings."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._settings = self._load()

    # -- persistence ---------------------------------------------------------------
    def _load(self) -> DashboardSettings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return DashboardSettings()
        settings = DashboardSettings()
        for key, value in (raw or {}).items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return self._validated(settings)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._settings.to_dict(), indent=2)
            temp = self._path.with_suffix(".json.tmp")
            try:
                temp.write_text(payload, encoding="utf-8")
                temp.replace(self._path)
            except OSError:
                # Direct write fallback: some managed home directories refuse to create a new file
                # but allow modifying an existing one.
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                self._path.write_text(payload, encoding="utf-8")
        except OSError:
            pass  # settings are a convenience; never break a running server over them

    # -- validation ----------------------------------------------------------------
    @staticmethod
    def _validated(settings: DashboardSettings) -> DashboardSettings:
        from ..constants import EFFORT_LEVELS

        if settings.workflow not in WORKFLOWS:
            settings.workflow = "single"
        if settings.theme not in THEMES:
            settings.theme = "dark"
        if settings.density not in ("comfortable", "compact"):
            settings.density = "comfortable"
        if settings.effort and settings.effort not in EFFORT_LEVELS:
            settings.effort = ""
        try:
            settings.max_tokens = max(0, int(settings.max_tokens))
        except (TypeError, ValueError):
            settings.max_tokens = 0
        for boolean in ("thinking", "yolo", "show_thinking", "auto_compact"):
            setattr(settings, boolean, bool(getattr(settings, boolean)))
        return settings

    # -- api -----------------------------------------------------------------------
    def get(self) -> DashboardSettings:
        with self._lock:
            return self._settings

    def update(self, changes: dict) -> DashboardSettings:
        """Apply validated changes and persist. Unknown keys are ignored rather than stored."""
        with self._lock:
            for key, value in (changes or {}).items():
                if key == "extra" or not hasattr(self._settings, key):
                    continue
                setattr(self._settings, key, value)
            self._settings = self._validated(self._settings)
            self._save()
            return self._settings

    def schema(self) -> dict:
        """Field metadata for the settings UI, so the form is generated rather than hand-written."""
        from ..constants import EFFORT_LEVELS

        return {
            "workflow": {"type": "choice", "label": "Workflow", "options": list(WORKFLOWS),
                         "labels": WORKFLOW_LABELS,
                         "help": "Multi enables sub-agent delegation tools."},
            "thinking": {"type": "bool", "label": "Show reasoning",
                         "help": "Where the provider streams reasoning, display it."},
            "effort": {"type": "choice", "label": "Reasoning effort",
                       "options": ["", *EFFORT_LEVELS], "labels": {"": "provider default"},
                       "help": "Maps to the provider's own effort/reasoning control."},
            "max_tokens": {"type": "int", "label": "Max reply tokens", "min": 0,
                           "help": "0 lets the provider and model decide."},
            "yolo": {"type": "bool", "label": "Automatic approval",
                     "help": "The dashboard cannot answer permission prompts; off may make tools fail."},
            "auto_compact": {"type": "bool", "label": "Automatic compaction",
                             "help": "Summarise older turns when the context window fills."},
            "theme": {"type": "choice", "label": "Theme", "options": list(THEMES),
                      "labels": {"dark": "Dark", "light": "Light"}},
            "density": {"type": "choice", "label": "Density", "options": ["comfortable", "compact"],
                        "labels": {"comfortable": "Comfortable", "compact": "Compact"}},
            "show_thinking": {"type": "bool", "label": "Show reasoning panel",
                              "help": "Hide to keep the transcript compact."},
        }
