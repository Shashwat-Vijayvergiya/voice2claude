import json

import pytest

from app.context import RECENT_LIMIT, ContextError, ContextStore, slugify


@pytest.fixture
def store(tmp_path):
    s = ContextStore(tmp_path / "context")
    s.ensure()
    return s


def test_ensure_creates_global_and_default(store):
    assert store.global_file.exists()
    assert store.list_projects() == ["default"]
    assert (store.projects_dir / "default" / "project.md").exists()


def test_template_comments_are_not_sent(store):
    # Fresh files hold only headings and <!-- --> examples: nothing to send.
    assert store.build("default").project_notes == ""


def test_notes_are_combined_and_comments_stripped(store):
    store.write_global("# Global\nI use Python 3.12.\n<!-- private scratch -->")
    store.write_project("default", "# default\nStack: FastAPI, PostgreSQL.")
    notes = store.build("default").project_notes
    assert "Python 3.12" in notes and "FastAPI" in notes
    assert "private scratch" not in notes
    assert notes.index("Python 3.12") < notes.index("FastAPI")


def test_notes_are_capped(store):
    store.write_project("default", "x" * 5000)
    assert len(store.build("default").project_notes) <= 2002


def test_recent_keeps_last_five_oldest_first(store):
    for i in range(7):
        store.add_command("default", f"id{i}", f"orig {i}", f"cmd {i}")
    assert store.build("default").recent_commands == [f"cmd {i}" for i in range(2, 7)]
    assert len(store.recent("default")) == RECENT_LIMIT


def test_recompress_replaces_same_item(store):
    store.add_command("default", "a", "first", "cmd a")
    store.add_command("default", "b", "second", "cmd b v1")
    store.add_command("default", "b", "second edited", "cmd b v2")
    assert store.build("default").recent_commands == ["cmd a", "cmd b v2"]


def test_exclude_item_when_recompressing(store):
    store.add_command("default", "a", "x", "cmd a")
    store.add_command("default", "b", "y", "cmd b")
    assert store.build("default", exclude_item="b").recent_commands == ["cmd a"]


def test_empty_command_not_remembered(store):
    store.add_command("default", "a", "x", "   ")
    assert store.recent("default") == []


def test_projects_are_separate(store):
    store.create_project("Voice App")
    store.add_command("voice-app", "a", "x", "voice cmd")
    store.add_command("default", "b", "y", "default cmd")
    assert store.build("voice-app").recent_commands == ["voice cmd"]
    assert store.list_projects() == ["default", "voice-app"]


def test_recent_survives_new_store_instance(store, tmp_path):
    store.add_command("default", "a", "x", "remembered")
    again = ContextStore(tmp_path / "context")
    assert again.build("default").recent_commands == ["remembered"]


def test_reset_recent(store):
    store.add_command("default", "a", "x", "cmd")
    store.reset_recent("default")
    assert store.recent("default") == []
    store.reset_recent("default")  # no error when already empty


def test_corrupt_recent_file_is_ignored(store):
    (store.projects_dir / "default" / "recent.json").write_text("{not json")
    assert store.recent("default") == []
    store.add_command("default", "a", "x", "cmd")  # and it recovers
    assert len(json.loads((store.projects_dir / "default" / "recent.json").read_text())) == 1


@pytest.mark.parametrize("name,slug", [("Voice App", "voice-app"), ("../../etc", "etc"), ("my_proj-2", "my_proj-2")])
def test_slugify(name, slug):
    assert slugify(name) == slug


def test_slugify_rejects_empty():
    with pytest.raises(ContextError):
        slugify("  !!! ")


def test_path_traversal_stays_inside(store):
    store.create_project("../../escape")
    assert (store.projects_dir / "escape").is_dir()
    assert not (store.root.parent.parent / "escape").exists()
