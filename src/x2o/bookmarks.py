"""Export X bookmarks and remove only imports whose archived notes can be verified."""
import json
import os
from pathlib import Path
import re
import httpx
from .collect import canonical, urls_from_file
from .vault import atomic_write, digest, path_in, now, apply_plan


class XBookmarksAPI:
    """Official X user-auth endpoints. Never extracts browser cookies or private API tokens."""
    def __init__(self, token=None, transport=None):
        token = token or os.environ.get("X_USER_ACCESS_TOKEN")
        if not token:
            raise ValueError("Set X_USER_ACCESS_TOKEN to an X OAuth user access token. A Codex/OpenAI key or app-only X token is insufficient.")
        self.client = httpx.Client(base_url="https://api.x.com/2", timeout=45,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "x-post-to-obsidian/0.1"}, transport=transport)

    def close(self):
        self.client.close()

    def request(self, method, route, **kwargs):
        response = self.client.request(method, route, **kwargs)
        if not response.is_success:
            # Do not echo token-bearing requests or arbitrary server content.
            hint = {401: "expired/invalid user token", 403: "missing bookmark scopes or account/API access",
                    429: "X rate limit reached; retry later"}.get(response.status_code, "X API request failed")
            raise ValueError(f"X API HTTP {response.status_code}: {hint}")
        data = response.json()
        if data.get("errors"):
            raise ValueError("X API reported partial errors; export/cleanup stopped")
        return data

    def identity(self):
        data = self.request("GET", "/users/me")["data"]
        if not str(data.get("id", "")).isdigit():
            raise ValueError("X returned an invalid account identity")
        return {"id": str(data["id"]), "username": data.get("username", "")}

    def list_bookmarks(self, account_id):
        urls, seen_tokens, token = [], set(), None
        while True:
            params = {"max_results": 100}
            if token:
                params["pagination_token"] = token
            page = self.request("GET", f"/users/{account_id}/bookmarks", params=params)
            for item in page.get("data", []):
                post_id = str(item.get("id", ""))
                if not post_id.isdigit():
                    raise ValueError("X returned a bookmark without a valid post ID")
                urls.append(f"https://x.com/i/web/status/{post_id}")
            token = page.get("meta", {}).get("next_token")
            if not token:
                return list(dict.fromkeys(urls))
            if token in seen_tokens:
                raise ValueError("X pagination repeated a token; export stopped")
            seen_tokens.add(token)

    def remove(self, account_id, post_id):
        response = self.request("DELETE", f"/users/{account_id}/bookmarks/{post_id}")
        if response.get("data", {}).get("bookmarked") is not False:
            raise ValueError("X did not confirm bookmark removal")


def sidecar(file):
    return Path(str(file) + ".json")


def export_urls(urls, file, account, source, overwrite=False):
    file = file.expanduser().resolve()
    manifest_path = sidecar(file)
    if (file.exists() or manifest_path.exists()) and not overwrite:
        raise ValueError(f"Export already exists: {file}; choose a new path or use --overwrite")
    if not re.fullmatch(r"[0-9]+", str(account.get("id", ""))) and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account.get("username", "")):
        raise ValueError("An account ID or handle is required for a cleanup-capable export")
    unique = {}
    for url in urls:
        normalized, post_id = canonical(url)
        unique.setdefault(post_id, normalized)
    content = "".join(url + "\n" for url in unique.values()).encode()
    manifest = {"version": 1, "exported": now(), "source": source, "account": account,
                "file_sha256": digest(content), "bookmarks": [{"id": key, "url": url} for key, url in unique.items()]}
    # Invalidation-first: an interrupted replacement cannot authorize cleanup using an old manifest.
    manifest_path.unlink(missing_ok=True)
    atomic_write(file, content)
    atomic_write(manifest_path, json.dumps(manifest, indent=2).encode())
    return manifest


def export_api(api, file, overwrite=False):
    account = api.identity()
    return export_urls(api.list_bookmarks(account["id"]), file, account, "x-api", overwrite)


def load_export(file):
    manifest = json.loads(sidecar(file).read_text())
    if manifest.get("version") != 1 or digest(file.read_bytes()) != manifest.get("file_sha256"):
        raise ValueError("Bookmark export changed or manifest is invalid; create a new export")
    account = manifest.get("account", {})
    if not str(account.get("id", "")).isdigit() and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account.get("username", "")):
        raise ValueError("Export lacks a valid account identity")
    actual = list(dict.fromkeys(canonical(url)[1] for url in urls_from_file(file)))
    stored = [entry["id"] for entry in manifest["bookmarks"]]
    if actual != stored:
        raise ValueError("Bookmark list does not match export manifest")
    for item in manifest["bookmarks"]:
        if canonical(item["url"])[1] != item["id"]:
            raise ValueError("Bookmark URL/ID mismatch in manifest")
    return manifest


def archive_status(vault, post_id):
    """Use a successful-write receipt, not merely a note's existence or a dry-run plan."""
    state = path_in(vault, ".x2o/processed.json")
    processed = json.loads(state.read_text()) if state.exists() else {}
    entry = processed.get(post_id)
    if not entry or not entry.get("paths"):
        return False, "not successfully imported"
    if not entry.get("post_sha256"):
        return False, "legacy import lacks a write receipt; refresh with --force"
    expected = f"Posts/{post_id}.md"
    if expected not in entry["paths"]:
        return False, "receipt lacks the post note"
    for relative in entry["paths"]:
        if not path_in(vault, relative).is_file():
            return False, f"archived note missing: {relative}"
    post = path_in(vault, expected)
    if digest(post.read_bytes()) != entry["post_sha256"]:
        return False, "post note changed since successful import"
    # Shared topic files can gain other posts; their exact byte hashes would become stale.
    for relative in entry["paths"]:
        if relative == expected:
            continue
        content = path_in(vault, relative).read_text()
        if f"<!-- x2o:{post_id}:start -->" not in content or f"<!-- x2o:{post_id}:end -->" not in content:
            return False, f"topic contribution missing: {relative}"
    return True, "verified successful import"


def cleanup_plan(file, vault):
    manifest = load_export(file)
    eligible, retained = [], []
    for item in manifest["bookmarks"]:
        safe, reason = archive_status(vault, item["id"])
        (eligible if safe else retained).append({**item, "reason": reason})
    return {"account": manifest["account"], "eligible": eligible, "retained": retained,
            "export_sha256": manifest["file_sha256"], "created": now()}


def cleanup(file, vault, api=None, execute=False, progress=print, excluded_ids=None):
    planned = cleanup_plan(file, vault)
    excluded_ids = set(excluded_ids or [])
    planned["retained"].extend({**item, "reason": "import failed in this sync run"}
                               for item in planned["eligible"] if item["id"] in excluded_ids)
    planned["eligible"] = [item for item in planned["eligible"] if item["id"] not in excluded_ids]
    report_path = Path(str(file) + ".cleanup.json")
    report = {**planned, "executed": execute, "removed": [], "failures": []}
    if execute:
        if api is None:
            raise ValueError("Cleanup execution requires X user authentication")
        identity = api.identity()
        account = planned["account"]
        if (identity["id"] != account["id"] if account.get("id") else
                identity["username"].casefold() != account["username"].casefold()):
            raise ValueError("Signed-in X account differs from the account that exported these bookmarks")
        journal = Path(str(file) + ".removed.jsonl")
        # Ensure the durable journal can be opened before making external changes.
        with journal.open('a', encoding='utf-8'):
            pass
        for index, item in enumerate(planned["eligible"]):
            # Re-check each archive immediately before its corresponding external removal.
            safe, reason = archive_status(vault, item["id"])
            if not safe:
                report["retained"].append({**item, "reason": reason})
                continue
            try:
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({"id": item["id"], "url": item["url"], "account_id": identity["id"], "attempted": now()}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                api.remove(identity["id"], item["id"])
                report["removed"].append(item)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({"id": item["id"], "url": item["url"], "account_id": identity["id"], "removed": now()}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                progress(f"Removed bookmark {item['id']}")
            except Exception as exc:
                report["failures"].append({**item, "error": str(exc)})
                # Stop on rate limits/auth/ambiguous failure; already removed entries stay journaled.
                report["retained"].extend({**pending, "reason": "cleanup stopped; verify ambiguous failure before retrying"}
                    for pending in planned["eligible"][index:] if pending not in report["removed"])
                break
    atomic_write(report_path, json.dumps(report, indent=2).encode())
    progress(f"{'Removed' if execute else 'Would remove'} {len(report['removed']) if execute else len(planned['eligible'])}; retained {len(report['retained'])}; report: {report_path}")
    return report


def sync(api, cfg, file, ingest, remove=False, overwrite=False, progress=print):
    manifest = export_api(api, file, overwrite)
    progress(f"Exported {len(manifest['bookmarks'])} bookmarks to {file}")
    failures = []
    for item in manifest["bookmarks"]:
        try:
            output = ingest(item["url"], cfg, progress=progress)
            if output:
                apply_plan(output, cfg.vault)
        except Exception as exc:
            failures.append({"id": item["id"], "error": str(exc)})
            progress(f"Import failed for {item['id']}; bookmark retained: {exc}")
    report = cleanup(file, cfg.vault, api, execute=remove, progress=progress, excluded_ids=[item["id"] for item in failures])
    return {"imports_failed": failures, "cleanup": report}
