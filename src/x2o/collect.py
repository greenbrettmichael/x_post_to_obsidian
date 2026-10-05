import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import trafilatura
from pypdf import PdfReader
from .models import Post
from .network import download

POST_PATTERN = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/(?:[A-Za-z0-9_]+|i/web)/status/([0-9]+)")


def canonical(url: str) -> tuple[str, str]:
    match = POST_PATTERN.fullmatch(url.split("?")[0].split("#")[0].rstrip("/"))
    if not match:
        raise ValueError(f"Expected an X/Twitter status URL: {url}")
    # Stable canonical i/web URL until author is retrieved.
    return f"https://x.com/i/web/status/{match[1]}", match[1]


def urls_from_file(path: Path):
    # Supports plain text, CSV, browser bookmark HTML, JSON, and X archive JS.
    return list(dict.fromkeys(m.group(0) for m in POST_PATTERN.finditer(path.read_text())))


def parse_post(raw: dict, expected_id: str) -> Post:
    tweet = raw.get("tweet") or raw.get("status") or raw
    if str(tweet.get("id")) != expected_id:
        raise ValueError("Post data does not match requested ID")
    author = tweet["author"]
    text = tweet.get("text", "")
    links = re.findall(r'https?://[^\s<>"\)]+', text)
    for facet in tweet.get("raw_text", {}).get("facets", []):
        if facet.get("type") == "url":
            links.append(facet.get("replacement") or facet.get("original"))
    if tweet.get("created_timestamp") is not None:
        date = datetime.fromtimestamp(tweet["created_timestamp"], timezone.utc).isoformat()
    else:
        from email.utils import parsedate_to_datetime
        date = parsedate_to_datetime(tweet["created_at"]).isoformat()
    return Post(id=expected_id, url=f"https://x.com/{author['screen_name']}/status/{expected_id}",
                author=author["screen_name"], author_name=author.get("name", author["screen_name"]),
                published=date, text=text, links=list(dict.fromkeys(filter(None, links))),
                media=tweet.get("media", {}).get("all", []), raw=raw)


def fetch_post(url: str, cache: Path, supplied: Path | None = None):
    _, post_id = canonical(url)
    cache.mkdir(parents=True, exist_ok=True)
    if supplied:
        raw = json.loads(supplied.read_text())
    elif (cache / "post.json").exists():
        raw = json.loads((cache / "post.json").read_text())
    else:
        try:
            data, _, _ = download(f"https://api.fxtwitter.com/status/{post_id}")
            raw = json.loads(data)
        except Exception as exc:
            raise ValueError("Could not retrieve post. Supply --post-json with an FxTwitter-format export "
                             "for private/deleted/unavailable posts.") from exc
    post = parse_post(raw, post_id)
    (cache / "post.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    return post


def read_source(url: str) -> dict:
    data, content_type, resolved = download(url, 20 * 1024 * 1024)
    if "pdf" in content_type or data.startswith(b"%PDF"):
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join(p.extract_text() or "" for p in reader.pages[:60])
        title = str((reader.metadata or {}).get("/Title", url))
        links = []
    else:
        html = data.decode("utf-8", errors="replace")
        text = trafilatura.extract(html, include_links=True, include_tables=True) or ""
        metadata = trafilatura.extract_metadata(html)
        title = metadata.title if metadata and metadata.title else url
        links = [str(urljoin(resolved, href))
                 for href in re.findall(r'href=[\'\"]([^\'\"]+)[\'\"]', html)]
    return {"url": url, "resolved_url": resolved, "title": title, "text": text[:40000],
            "truncated": len(text) > 40000, "links": links[:250]}


def collect_sources(urls: list[str], maximum: int):
    results, seen, queue = [], set(), list(dict.fromkeys(urls))
    while queue and len(results) < maximum:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            doc = read_source(url)
            results.append(doc)
            # Follow project -> paper/repository, and abstract -> full paper.
            if urlsplit(url).hostname == "arxiv.org" and "/abs/" in url:
                queue.insert(0, url.replace("/abs/", "/html/"))
            for link in doc["links"]:
                host = urlsplit(link).hostname or ""
                if (host == "arxiv.org" and re.match(r"^/(abs|html|pdf)/[0-9]", urlsplit(link).path)) or (host == "github.com" and len(urlsplit(link).path.strip('/').split('/')) == 2):
                    if link not in seen and link not in queue:
                        queue.append(link)
        except Exception as exc:
            results.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
            if urlsplit(url).hostname == "arxiv.org" and "/html/" in url:
                fallback = url.replace("/html/", "/pdf/")
                if fallback not in seen:
                    queue.insert(0, fallback)
    return results
