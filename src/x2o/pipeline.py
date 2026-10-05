import json
from pathlib import Path
from urllib.parse import urldefrag
from pydantic import HttpUrl
from .collect import fetch_post, collect_sources, canonical
from .media import process_media
from .models import Config
from .provider import Provider, markdown_urls
from .vault import inventory, plan, already_processed


def prompt(cfg, kind):
    custom = getattr(cfg, kind + "_prompt")
    baseline = (Path(__file__).parent / "prompts" / f"{kind}.md").read_text()
    return baseline + ("\n\nUser preferences:\n" + custom.read_text() if custom else "")


def research_post(url: str, cfg: Config, supplied=None, force=False, progress=print):
    _, post_id = canonical(url)
    if not force and already_processed(cfg.vault, post_id):
        progress(f"Skipping already processed post {post_id}; use --force to refresh")
        return None
    # Fail before downloading when inference credentials are absent.
    provider = Provider(cfg)
    cache = cfg.cache / post_id
    progress(f"Retrieving post {post_id}")
    post = fetch_post(url, cache, supplied)
    progress("Extracting video frames and audio")
    media, images = process_media(post, cfg, cache)
    (cache / "media.json").write_text(json.dumps(media, indent=2))
    for item in media:
        for key in ("error", "audio_error"):
            if item.get(key):
                progress(f"Media limitation: {item[key]}")
    progress("Reading linked sources")
    linked = collect_sources(post.links, cfg.max_sources)
    evidence = {"post": post.model_dump(exclude={"raw"}), "media": media, "linked_sources": linked}
    progress(f"Searching project, paper, context, and independent opinions with {cfg.model}")
    research = provider.research(prompt(cfg, "research"), json.dumps(evidence, ensure_ascii=False), images, post)
    (cache / "search.json").write_text(json.dumps(research, ensure_ascii=False, indent=2))
    progress("Reading discovered papers, projects, and discussion")
    docs = collect_sources(research["urls"][:cfg.max_sources], cfg.max_sources)
    evidence.update(search=research["text"], discovered_sources=docs, existing_topics=inventory(cfg.vault)[:300])
    (cache / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    progress("Synthesizing cited post and shared topic notes")
    result = provider.synthesize(prompt(cfg, "synthesis"), json.dumps(evidence, ensure_ascii=False), images)
    allowed = {post.url, *post.links, *research["urls"],
               *[item["url"] for item in media if item.get("url")]}
    for doc in linked + docs:
        allowed.add(doc["url"])
        if doc.get("resolved_url"):
            allowed.add(doc["resolved_url"])
    # Reject invented source URLs before any vault changes. This verifies provenance, not truth.
    normalize = lambda u: str(HttpUrl(urldefrag(u)[0])).rstrip('/')
    allowed = {normalize(u) for u in allowed}
    for source in result.sources:
        if normalize(str(source.url)) not in allowed:
            raise ValueError(f"Model cited a source outside the retrieved evidence: {source.url}. Evidence saved in {cache}")
    for field in [result.summary, result.findings, result.opinions, result.limitations, result.media_analysis,
                  *[t.contribution for t in result.topics], *[t.overview for t in result.topics]]:
        for url_found in markdown_urls(field):
            if normalize(url_found) not in allowed:
                raise ValueError(f"Model inserted an unsupported citation: {url_found}. Evidence saved in {cache}")
    (cache / "research.json").write_text(result.model_dump_json(indent=2))
    output = plan(cfg.vault, post, result, cfg.provider, cfg.model, media)
    (cache / "plan.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    return output
