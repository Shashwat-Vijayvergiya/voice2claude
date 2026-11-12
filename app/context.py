"""Background for the compression call: your notes and recent commands.

Layout (under the data dir, ``~/.local/share/voice-to-claude/`` by default):

    context/global.md                    your languages, style, common terms
    context/projects/<name>/project.md   goal, stack, key rules for one project
    context/projects/<name>/recent.json  last 5 commands for that project

Notes are written by you; the app never scans your code. Everything here is
sent as *background*: the prompt tells the model it must never override what
you actually said.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.models import CompressionContext

log = logging.getLogger(__name__)

RECENT_LIMIT = 5
MAX_NOTES_CHARS = 2000      # notes are background; keep calls small and cheap
MAX_RECENT_CHARS = 500      # per remembered command
DEFAULT_PROJECT = "default"

REPO_GLOBAL_TEMPLATE = Path(__file__).resolve().parent.parent / "context_data" / "global.md"

PROJECT_TEMPLATE = """# {name}

<!-- Background for compression. Keep it short. Example:
Goal: voice app that turns speech into prompts for Claude.
Stack: Python 3.12, PySide6, Groq API.
Rules: tests with pytest; no new dependencies without asking.
-->
"""

_NAME_RE = re.compile(r"[^a-z0-9_-]+")


class ContextError(Exception):
    pass


def slugify(name: str) -> str:
    slug = _NAME_RE.sub("-", name.strip().lower()).strip("-")[:40]
    if not slug:
        raise ContextError("Project name needs at least one letter or number.")
    return slug


@dataclass
class RecentCommand:
    item_id: str
    original: str
    command: str
    ts: str


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


class ContextStore:
    def __init__(self, root: Path):
        self.root = root
        self.projects_dir = root / "projects"

    # ---- setup --------------------------------------------------------------

    def ensure(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if not self.global_file.exists():
            template = REPO_GLOBAL_TEMPLATE.read_text(encoding="utf-8") if REPO_GLOBAL_TEMPLATE.exists() else "# Global context\n"
            self.global_file.write_text(template, encoding="utf-8")
        self.create_project(DEFAULT_PROJECT)

    @property
    def global_file(self) -> Path:
        return self.root / "global.md"

    def project_dir(self, name: str) -> Path:
        return self.projects_dir / slugify(name)

    # ---- projects -------------------------------------------------------------

    def list_projects(self) -> list[str]:
        if not self.projects_dir.exists():
            return [DEFAULT_PROJECT]
        names = sorted(p.name for p in self.projects_dir.iterdir() if p.is_dir())
        return names or [DEFAULT_PROJECT]

    def create_project(self, name: str) -> str:
        slug = slugify(name)
        folder = self.projects_dir / slug
        folder.mkdir(parents=True, exist_ok=True)
        notes = folder / "project.md"
        if not notes.exists():
            notes.write_text(PROJECT_TEMPLATE.format(name=name.strip() or slug), encoding="utf-8")
        return slug

    def exists(self, name: str) -> bool:
        try:
            return (self.projects_dir / slugify(name)).is_dir()
        except ContextError:
            return False

    # ---- notes ------------------------------------------------------------------

    def read_global(self) -> str:
        return self.global_file.read_text(encoding="utf-8") if self.global_file.exists() else ""

    def write_global(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.global_file, text)

    def read_project(self, name: str) -> str:
        f = self.project_dir(name) / "project.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def write_project(self, name: str, text: str) -> None:
        self.create_project(name)
        _atomic_write(self.project_dir(name) / "project.md", text)

    # ---- recent commands ---------------------------------------------------------

    def _recent_file(self, name: str) -> Path:
        return self.project_dir(name) / "recent.json"

    def recent(self, name: str) -> list[RecentCommand]:
        f = self._recent_file(name)
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return [RecentCommand(**item) for item in data][-RECENT_LIMIT:]
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            log.warning("Ignoring unreadable %s: %s", f, exc)
            return []

    def add_command(self, name: str, item_id: str, original: str, command: str) -> None:
        """Remember a finished command. Re-compressing the same item replaces it."""
        if not command.strip():
            return
        items = [r for r in self.recent(name) if r.item_id != item_id]
        items.append(RecentCommand(item_id, original, command, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.create_project(name)
        _atomic_write(self._recent_file(name), json.dumps([asdict(r) for r in items[-RECENT_LIMIT:]], indent=1))

    def reset_recent(self, name: str) -> None:
        f = self._recent_file(name)
        if f.exists():
            f.unlink()

    # ---- what the compressor gets -------------------------------------------------

    def build(self, name: str, exclude_item: str | None = None) -> CompressionContext:
        """Notes (global + project, comments stripped) and recent commands, oldest first.

        ``exclude_item`` leaves out the command being re-compressed, so the model
        doesn't read the previous version of it as an earlier request.
        """
        parts = []
        global_notes = _strip_comments(self.read_global())
        if global_notes and global_notes != "# Global context":
            parts.append(global_notes)
        project_notes = _strip_comments(self.read_project(name))
        if project_notes and not re.fullmatch(r"#[^\n]*", project_notes):
            parts.append(project_notes)
        recent = [
            _truncate(r.command, MAX_RECENT_CHARS)
            for r in self.recent(name)
            if r.item_id != exclude_item
        ]
        return CompressionContext(
            project_notes=_truncate("\n\n".join(parts), MAX_NOTES_CHARS),
            recent_commands=recent,
        )


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
