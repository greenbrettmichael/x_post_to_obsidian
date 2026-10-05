"""Deterministic, append-oriented vault updates with conflict detection and backups."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import yaml
from .models import Post, Research


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    value = unicodedata.normalize("NFKC", name).strip()
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|\[\]#^]', "-", value).strip(' .-')[:100]
    if not value or value in {".", ".."}:
        raise ValueError("Invalid topic name")
    return value


def path_in(vault: Path, relative: str):
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Path escapes vault")
    target = vault / relative
    if not target.resolve().is_relative_to(vault.resolve()):
        raise ValueError("Path escapes vault through symlink")
    # Reject symlink components even when they currently resolve inside the vault.
    current = vault
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink path blocked: {relative}")
    return target


def inventory(vault: Path):
    entries = []
    for path in sorted(vault.rglob("*.md")):
        relative = path.relative_to(vault)
        if any(part.startswith('.') for part in relative.parts) or relative.parts[0] in {"Posts", "Media"}:
            continue
        if path.is_symlink():
            continue
        try:
            text = path.read_text()
            match = re.match(r"^---\n(.*?)\n---", text, re.S)
            meta = yaml.safe_load(match[1]) if match else {}
            meta = meta if isinstance(meta, dict) else {}
            aliases = meta.get("aliases", [])
            aliases = [aliases] if isinstance(aliases, str) else aliases
            entries.append({"name": path.stem, "path": relative.as_posix(),
                            "aliases": [str(x) for x in aliases or []], "excerpt": text[:1800]})
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return entries


def resolve_topics(research, entries):
    lookup = {}
    for entry in entries:
        for name in [entry["name"], *entry["aliases"]]:
            lookup.setdefault(name.casefold(), []).append(entry)
    mapping = {}
    used = set()
    for topic in research.topics:
        name = safe_name(topic.name)
        matches = lookup.get(topic.name.casefold(), []) or lookup.get(name.casefold(), [])
        if len(matches) > 1:
            raise ValueError(f"Ambiguous existing topic: {topic.name}. Give notes unique aliases.")
        relative = matches[0]["path"] if matches else f"Topics/{name}.md"
        if relative.casefold() in used:
            raise ValueError(f"Duplicate topics resolve to {relative}")
        used.add(relative.casefold())
        mapping[topic.name] = relative
    for entry in entries:
        for name in [entry["name"], *entry["aliases"]]:
            if len(lookup.get(name.casefold(), [])) == 1:
                mapping.setdefault(name, entry["path"])
    return mapping


def link(path, label=None):
    target = path.removesuffix('.md')
    return f"[[{target}|{label or Path(target).name}]]"


def frontmatter(data):
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True) + "---\n\n"


def block(text, key, content):
    start, end = f"<!-- x2o:{key}:start -->", f"<!-- x2o:{key}:end -->"
    replacement = start + "\n" + content.rstrip() + "\n" + end
    if start in text or end in text:
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
            raise ValueError(f"Damaged generated block {key}; no notes changed")
        return re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: replacement, text, flags=re.S)
    return text.rstrip() + "\n\n" + replacement + "\n"


def plan(vault: Path, post: Post, research: Research, provider: str, model: str, media: list[dict]):
    entries = inventory(vault)
    mapping = resolve_topics(research, entries)
    post_path = f"Posts/{post.id}.md"
    changes = []
    topic_links = [link(mapping[t.name], t.name) for t in research.topics]
    meta = {"type": "x-post", "post_id": post.id, "source": post.url, "author": post.author,
            "author_name": post.author_name, "published": post.published, "date": post.published[:10],
            "processed": now(), "research_provider": provider, "model": model,
            "topics": topic_links, "tags": ["x-bookmark"], "status": "researched"}
    evidence = "\n".join(f"- [{s.title}]({s.url}) — {s.kind}" for s in research.sources)
    body = (f"# {research.title}\n\n[Original X post]({post.url}) · @{post.author} · {post.published}\n\n"
            f"Topics: {', '.join(topic_links)}\n\n## Summary\n\n{research.summary}\n\n"
            f"## Findings and context\n\n{research.findings}\n\n## Opinions and discussion\n\n{research.opinions}\n\n"
            f"## Limitations and open questions\n\n{research.limitations}\n\n## Media analysis\n\n{research.media_analysis}\n\n"
            f"## Sources\n\n{evidence}\n\n## Original post text\n\n{post.text}\n")
    if media:
        body += "\n## Media processing record\n\n"
        for item in media:
            if item.get("url"):
                body += f"- [Original {item.get('type', 'media')}]({item['url']})\n"
            body += f"- Sampled frames: {len(item.get('frames', []))}; audio status: {item.get('audio_status', 'transcribed' if item.get('transcript') else 'unavailable or no speech')}.\n"
            for key in ("error", "audio_error", "transcript_note"):
                if item.get(key):
                    body += f"- {item[key]}\n"
            if item.get("truncated"):
                body += f"- Analysis limited to first {item['analyzed_seconds']} of {item['duration_seconds']} seconds.\n"
            if item.get("transcript"):
                body += "\n### Speech transcript\n\n" + item["transcript"] + "\n"

    def add(relative, initial, key, content):
        target = path_in(vault, relative)
        old = target.read_bytes() if target.exists() else None
        text = old.decode() if old is not None else initial
        if relative == post_path and old is not None and "<!-- x2o:post:start -->" in text:
            match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.S)
            if match:
                existing_meta = yaml.safe_load(match[1]) or {}
                if not isinstance(existing_meta, dict):
                    raise ValueError("Post metadata is not a mapping")
                old_tags = existing_meta.get("tags", [])
                old_tags = [old_tags] if isinstance(old_tags, str) else old_tags
                existing_meta.update(meta)
                existing_meta["tags"] = list(dict.fromkeys([*(old_tags or []), *meta["tags"]]))
                text = frontmatter(existing_meta) + text[match.end():].lstrip('\n')
        new = block(text, key, content)
        changes.append({"path": relative, "before_sha256": digest(old) if old is not None else None, "content": new})

    add(post_path, frontmatter(meta), "post", body)
    for topic in research.topics:
        relative = mapping[topic.name]
        related = [link(mapping[n], n) for n in topic.related if n in mapping and mapping[n] != relative]
        initial = frontmatter({"type": "topic", "tags": ["topic"]}) + f"# {safe_name(topic.name)}\n\n{topic.overview}\n"
        contribution = (f"## {research.title} ({post.published[:10]})\n\n"
                        f"Source note: {link(post_path, research.title)} · [X post]({post.url})\n\n"
                        f"{topic.contribution}\n\nRelated: {', '.join(related) or ', '.join(l for l in topic_links if l != link(relative, topic.name))}\n")
        add(relative, initial, post.id, contribution)
    index_path = "X Bookmarks.md"
    index_initial = frontmatter({"type": "index"}) + "# X Bookmarks\n\nResearch notes and linked topics. Obsidian's native Search can filter `[author:handle]`, `[date:YYYY-MM-DD]`, and `tag:#x-bookmark`.\n"
    add(index_path, index_initial, post.id, f"- {link(post_path, research.title)} — @{post.author}, {post.published[:10]} — {', '.join(topic_links)}")
    active_paths = [c["path"] for c in changes]
    state = path_in(vault, ".x2o/processed.json")
    previous = json.loads(state.read_text()).get(post.id, {}) if state.exists() else {}
    for relative in previous.get("paths", []):
        if relative in active_paths or not relative.endswith('.md'):
            continue
        target = path_in(vault, relative)
        if not target.exists():
            continue
        old = target.read_bytes()
        text = old.decode()
        start, end = f"<!-- x2o:{post.id}:start -->", f"<!-- x2o:{post.id}:end -->"
        if start in text:
            # Validate markers through the same replacement routine, then remove this block.
            block(text, post.id, "")
            new = re.sub(re.escape(start) + r".*?" + re.escape(end), "", text, flags=re.S)
            changes.append({"path": relative, "before_sha256": digest(old), "content": new})
    return {"version": 1, "vault": str(vault.resolve()), "post_id": post.id, "created": now(),
            "changes": changes, "active_paths": active_paths}


@contextmanager
def locked(vault):
    state = path_in(vault, ".x2o")
    state.mkdir(exist_ok=True)
    lock = path_in(vault, ".x2o/write.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Vault write is locked. If a prior process crashed, verify it stopped then remove .x2o/write.lock.") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".x2o-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def apply_plan(plan_data, vault):
    if plan_data.get("version") != 1 or plan_data.get("vault") != str(vault.resolve()):
        raise ValueError("Plan version or target vault mismatch")
    changes = plan_data["changes"]
    if len({c["path"] for c in changes}) != len(changes):
        raise ValueError("Plan contains duplicate paths")
    # Only Markdown notes can be applied; arbitrary JSON cannot write executable/config files.
    for change in changes:
        if not change["path"].endswith('.md') or any(p.startswith('.') for p in Path(change["path"]).parts):
            raise ValueError("Plan may write visible Markdown notes only")
        path_in(vault, change["path"])
    with locked(vault):
        originals = {}
        for change in changes:
            target = path_in(vault, change["path"])
            old = target.read_bytes() if target.exists() else None
            if (digest(old) if old is not None else None) != change["before_sha256"]:
                raise ValueError(f"Note changed after plan creation: {change['path']}. Generate a new plan.")
            originals[change["path"]] = old
        backup_name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup = path_in(vault, f".x2o/backups/{backup_name}")
        backup.mkdir(parents=True)
        for relative, old in originals.items():
            if old is not None:
                atomic_write(backup / relative, old)
        atomic_write(backup / "manifest.json", json.dumps({"created_files": [p for p, b in originals.items() if b is None],
                     "paths": list(originals)}, indent=2).encode())
        written = []
        try:
            for change in changes:
                target = path_in(vault, change["path"])
                atomic_write(target, change["content"].encode())
                written.append(change["path"])
            state = path_in(vault, ".x2o/processed.json")
            processed = json.loads(state.read_text()) if state.exists() else {}
            post_file = path_in(vault, f"Posts/{plan_data['post_id']}.md")
            processed[plan_data["post_id"]] = {"processed": now(), "paths": plan_data.get("active_paths", [c["path"] for c in changes]),
                "post_sha256": digest(post_file.read_bytes()) if post_file.is_file() else None}
            atomic_write(state, json.dumps(processed, indent=2).encode())
        except Exception:
            for relative in reversed(written):
                target = path_in(vault, relative)
                if originals[relative] is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, originals[relative])
            raise
    return [str(vault / c["path"]) for c in changes]


def already_processed(vault, post_id):
    state = path_in(vault, ".x2o/processed.json")
    if not state.exists():
        return False
    entry = json.loads(state.read_text()).get(post_id)
    return bool(entry and all(path_in(vault, p).exists() for p in entry["paths"]))
