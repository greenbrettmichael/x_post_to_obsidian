import json
from pathlib import Path
from unittest.mock import patch
import pytest
from x2o.models import Post, Research, Topic, Source
from x2o.vault import plan, apply_plan, already_processed


@pytest.fixture
def post():
    return Post(id="1871455615752847763", url="https://x.com/janusch_patas/status/1871455615752847763",
                author="janusch_patas", author_name="MrNeRF", published="2024-12-24T07:19:23+00:00", text="Test post")


@pytest.fixture
def research():
    return Research(title="CoSurfGS", summary="Summary", findings="Findings", opinions="No independent opinion found",
                    limitations="Unverified claims", media_analysis="No media supplied",
                    topics=[Topic(name="Gaussian splatting", overview="Introduction", contribution="Evidence", related=["Surface reconstruction"]),
                            Topic(name="Surface reconstruction", overview="Intro", contribution="More evidence", related=["Gaussian splatting"])],
                    sources=[Source(title="Paper", url="https://arxiv.org/abs/2412.17612", kind="primary")])


def test_linked_notes_and_yaml(tmp_path, post, research):
    import yaml
    changes = plan(tmp_path, post, research, "test", "test-model", [])
    apply_plan(changes, tmp_path)
    content = (tmp_path / f"Posts/{post.id}.md").read_text()
    meta = yaml.safe_load(content.split('---')[1])
    assert meta["author"] == post.author and meta["date"] == "2024-12-24"
    assert "[[Topics/Gaussian splatting|Gaussian splatting]]" in content
    assert f"[[Posts/{post.id}|CoSurfGS]]" in (tmp_path / "Topics/Gaussian splatting.md").read_text()
    assert already_processed(tmp_path, post.id)


def test_existing_alias_and_user_text_preserved(tmp_path, post, research):
    note = tmp_path / "My Concepts" / "3DGS.md"
    note.parent.mkdir()
    note.write_text('---\naliases: [Gaussian splatting]\ncustom: mine\n---\n\nMy handwritten content.\n')
    changes = plan(tmp_path, post, research, "test", "test", [])
    apply_plan(changes, tmp_path)
    assert "My handwritten content." in note.read_text()
    assert "custom: mine" in note.read_text()
    assert not (tmp_path / "Topics/Gaussian splatting.md").exists()
    first = note.read_text()
    apply_plan(plan(tmp_path, post, research, "test", "test", []), tmp_path)
    assert note.read_text() == first
    assert first.count('## CoSurfGS') == 1


def test_plan_conflict_refuses_all_note_changes(tmp_path, post, research):
    changes = plan(tmp_path, post, research, "test", "test", [])
    existing = tmp_path / "Topics/Surface reconstruction.md"
    existing.parent.mkdir()
    existing.write_text("User edited this")
    with pytest.raises(ValueError, match="Note changed"):
        apply_plan(changes, tmp_path)
    assert not (tmp_path / f"Posts/{post.id}.md").exists()
    assert existing.read_text() == "User edited this"


def test_rollback_on_write_failure(tmp_path, post, research):
    import x2o.vault as vault
    existing = tmp_path / "Topics/Gaussian splatting.md"
    existing.parent.mkdir()
    existing.write_text("User content")
    changes = plan(tmp_path, post, research, "test", "test", [])
    original = vault.atomic_write
    calls = 0
    def fail(path, data):
        nonlocal calls
        if path.parent == tmp_path / "Topics":
            calls += 1
            if calls == 1:
                raise OSError("Disk failure")
        original(path, data)
    with patch.object(vault, "atomic_write", fail):
        with pytest.raises(OSError):
            apply_plan(changes, tmp_path)
    assert existing.read_text() == "User content"
    assert not (tmp_path / f"Posts/{post.id}.md").exists()
    assert not already_processed(tmp_path, post.id)


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md", ".obsidian/config.md", "run.sh"])
def test_rejects_unsafe_plan_paths(tmp_path, path):
    malicious = {"version": 1, "vault": str(tmp_path), "post_id": "123", "changes": [
        {"path": path, "before_sha256": None, "content": "bad"}]}
    with pytest.raises(ValueError):
        apply_plan(malicious, tmp_path)


def test_rejects_symlink(tmp_path, post, research):
    outside = tmp_path.parent / "outside-vault"
    outside.mkdir(exist_ok=True)
    (tmp_path / "Topics").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        plan(tmp_path, post, research, "test", "test", [])


def test_ambiguous_topic_refuses(tmp_path, post, research):
    for name in ["A", "B"]:
        (tmp_path / f"{name}.md").write_text('---\naliases: [Gaussian splatting]\n---\n')
    with pytest.raises(ValueError, match="Ambiguous"):
        plan(tmp_path, post, research, "test", "test", [])


def test_refresh_updates_managed_metadata_and_preserves_custom(tmp_path, post, research):
    import yaml
    apply_plan(plan(tmp_path, post, research, "test", "old-model", []), tmp_path)
    file = tmp_path / f"Posts/{post.id}.md"
    file.write_text(file.read_text().replace('type: x-post', 'custom: mine\ntype: x-post') + '\nMy annotation\n')
    apply_plan(plan(tmp_path, post, research, "test", "new-model", []), tmp_path)
    text = file.read_text()
    meta = yaml.safe_load(text.split('---')[1])
    assert meta['model'] == 'new-model'
    assert meta['custom'] == 'mine'
    assert 'My annotation' in text


def test_refresh_removes_obsolete_contribution_only(tmp_path, post, research):
    apply_plan(plan(tmp_path, post, research, "test", "test", []), tmp_path)
    file = tmp_path / 'Topics/Surface reconstruction.md'
    file.write_text(file.read_text() + '\nHandwritten content\n')
    research.topics = research.topics[:1]
    changes = plan(tmp_path, post, research, "test", "test", [])
    apply_plan(changes, tmp_path)
    assert f'<!-- x2o:{post.id}:start -->' not in file.read_text()
    assert 'Handwritten content' in file.read_text()
    assert 'Topics/Surface reconstruction.md' not in json.loads((tmp_path/'.x2o/processed.json').read_text())[post.id]['paths']


def test_refresh_keeps_user_tags(tmp_path, post, research):
    import yaml
    apply_plan(plan(tmp_path, post, research, "test", "test", []), tmp_path)
    file = tmp_path / f"Posts/{post.id}.md"
    file.write_text(file.read_text().replace('tags:\n- x-bookmark', 'tags:\n- my-tag\n- x-bookmark'))
    apply_plan(plan(tmp_path, post, research, "test", "new", []), tmp_path)
    assert 'my-tag' in yaml.safe_load(file.read_text().split('---')[1])['tags']
