import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from x2o.collect import canonical, parse_post, urls_from_file
from x2o.models import Config, Research, Topic, Source
from x2o.pipeline import research_post
from x2o.provider import Provider
from x2o.network import public_url
from x2o.cli import main

RAW = {"tweet": {"id": "123", "text": "Paper", "author": {"screen_name": "alice", "name": "Alice"},
                  "created_timestamp": 1735024763, "media": {"all": []}}}


def result(url="https://arxiv.org/abs/2412.17612"):
    return Research(title="Paper", summary=f"[Evidence]({url})", findings="Facts", opinions="None found",
        limitations="Limits", media_analysis="No media", topics=[Topic(name="Topic", overview="Intro", contribution="Facts", related=[])],
        sources=[Source(title="Paper", url=url, kind="primary")])


def test_urls_and_exports(tmp_path):
    assert canonical("https://twitter.com/alice/status/123?s=20")[1] == "123"
    assert canonical("https://x.com/i/web/status/123")[1] == "123"
    for bad in ["https://x.com/alice", "https://evil.com/alice/status/123", "https://x.com.evil.com/alice/status/123"]:
        with pytest.raises(ValueError):
            canonical(bad)
    file = tmp_path / "bookmarks.json"
    file.write_text(json.dumps({"a": "https://x.com/alice/status/123?s=20", "b": "https://x.com/alice/status/123"}))
    assert len(urls_from_file(file)) == 1


def test_post_identity():
    assert parse_post(RAW, "123").author == "alice"
    with pytest.raises(ValueError, match="does not match"):
        parse_post(RAW, "456")


def test_dry_plan_no_vault_writes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = Config(vault=vault, cache=tmp_path / "cache")
    supplied = tmp_path / "post.json"
    supplied.write_text(json.dumps(RAW))
    with patch("x2o.pipeline.Provider") as mock, patch("x2o.pipeline.collect_sources", return_value=[]):
        mock.return_value.research.return_value = {"text": "Evidence", "urls": ["https://arxiv.org/abs/2412.17612"]}
        mock.return_value.synthesize.return_value = result()
        output = research_post("https://x.com/alice/status/123", cfg, supplied)
    assert list(vault.iterdir()) == []
    assert output["changes"]
    assert (cfg.cache / "123/plan.json").exists()


def test_unsupported_citation_refuses(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = Config(vault=vault, cache=tmp_path / "cache")
    supplied = tmp_path / "post.json"
    supplied.write_text(json.dumps(RAW))
    with patch("x2o.pipeline.Provider") as mock, patch("x2o.pipeline.collect_sources", return_value=[]):
        mock.return_value.research.return_value = {"text": "Evidence", "urls": ["https://arxiv.org/abs/2412.17612"]}
        mock.return_value.synthesize.return_value = result("https://invented.example/paper")
        with pytest.raises(ValueError, match="outside the retrieved"):
            research_post("https://x.com/alice/status/123", cfg, supplied)
    assert list(vault.iterdir()) == []


def test_missing_credentials_actionable(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Provider(Config(vault=tmp_path))


@pytest.mark.parametrize("citation,accepted", [
    ("https://video.twimg.com/known.mp4?tag=14", True),
    ("https://video.twimg.com/unseen.mp4", False),
])
def test_media_citation_provenance(tmp_path, citation, accepted):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = Config(vault=vault, cache=tmp_path / "cache")
    supplied = tmp_path / "post.json"
    supplied.write_text(json.dumps(RAW))
    with patch("x2o.pipeline.Provider") as mock, \
            patch("x2o.pipeline.collect_sources", return_value=[]), \
            patch("x2o.pipeline.process_media", return_value=([
                {"url": "https://video.twimg.com/known.mp4?tag=14", "type": "video", "frames": []}
            ], [])):
        mock.return_value.research.return_value = {"text": "Evidence", "urls": []}
        mock.return_value.synthesize.return_value = result(citation)
        if accepted:
            assert research_post("https://x.com/alice/status/123", cfg, supplied)["changes"]
        else:
            with pytest.raises(ValueError, match="outside the retrieved"):
                research_post("https://x.com/alice/status/123", cfg, supplied)
    assert list(vault.iterdir()) == []


def test_openai_requires_search_and_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    cfg = Config(vault=tmp_path)
    with patch("x2o.provider.OpenAI") as api:
        provider = Provider(cfg)
        response = SimpleNamespace(status="completed", output_text="Research", model_dump=lambda **_: {
            "output": [{"type": "web_search_call", "action": {"sources": [{"url": "https://arxiv.org/abs/2412.17612"}]} }]})
        api.return_value.responses.create.return_value = response
        evidence = provider.research("instructions", "prompt", [], parse_post(RAW, "123"))
        assert evidence["urls"] == ["https://arxiv.org/abs/2412.17612"]
        assert api.return_value.responses.create.call_args.kwargs["tool_choice"] == "required"
        api.return_value.responses.create.return_value = SimpleNamespace(status="completed", output_text=result().model_dump_json())
        provider.synthesize("instructions", "prompt", [])
        assert api.return_value.responses.create.call_args.kwargs["text"]["format"]["strict"]


def test_private_sources_blocked():
    with pytest.raises(ValueError, match="Non-public"):
        public_url("http://127.0.0.1/private")


def test_batch_failure_returns_nonzero_and_continues(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'vault = "{tmp_path}"')
    with patch("x2o.cli.research_post", side_effect=[ValueError("bad post"), None]) as work:
        assert main(["--config", str(cfg), "ingest", "https://x.com/a/status/123", "https://x.com/b/status/456"]) == 1
        assert work.call_count == 2


def test_citation_fragment_supported(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = Config(vault=vault, cache=tmp_path / "cache")
    supplied = tmp_path / "post.json"
    supplied.write_text(json.dumps(RAW))
    with patch("x2o.pipeline.Provider") as mock, patch("x2o.pipeline.collect_sources", return_value=[]):
        mock.return_value.research.return_value = {"text": "Evidence", "urls": ["https://arxiv.org/abs/2412.17612"]}
        mock.return_value.synthesize.return_value = result("https://arxiv.org/abs/2412.17612#abstract")
        assert research_post("https://x.com/alice/status/123", cfg, supplied)["changes"]


def test_arxiv_html_falls_back_to_pdf_and_skips_icons():
    from x2o.collect import collect_sources
    def read(url):
        if '/html/' in url:
            raise ValueError('HTML unavailable')
        return {'url':url, 'text':'paper', 'links': ['https://arxiv.org/favicon.ico']}
    with patch('x2o.collect.read_source', side_effect=read) as reader:
        docs = collect_sources(['https://arxiv.org/abs/2412.17612'], 5)
    assert any('/pdf/' in d['url'] for d in docs)
    assert not any('favicon' in d['url'] for d in docs)
