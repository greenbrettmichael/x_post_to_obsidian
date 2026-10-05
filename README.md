# X posts → Obsidian

`x2o` researches bookmarked X posts and builds a cited, linked knowledge base in an existing Obsidian vault. It is a Python command-line application; Obsidian plugins are optional.

Each URL produces a post note with the original link, author handle and display name, publication date, original text, summary, technical context, outside opinions, limitations, and media observations. Research is added to shared topic pages, with links in both directions. Topic names and Obsidian aliases are reused across the existing vault, including notes outside the `Topics` folder.

## Start here

Requires Python 3.11 or newer. The project in this workspace already has `.venv` installed, including local audio support, and `x2o.toml` points at `/home/brett/Documents/x_bookmarks`.

For a fresh installation:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
x2o init --vault /path/to/your/existing/vault
```

Set `OPENAI_API_KEY` in your environment, then run:

```bash
source .venv/bin/activate
x2o doctor
x2o ingest 'https://x.com/janusch_patas/status/1871455615752847763?s=20'
```

The default is `gpt-6.1-sol` through the OpenAI Responses API, with mandatory live web search. Audio is transcribed separately with `gpt-transcribe`; timestamped video frames and the transcript are given to the research model. Model calls, transcription, and search can incur provider charges. This tool uses two LLM stages per post: research and structured synthesis.

To review notes before writing them:

```bash
x2o ingest 'https://x.com/author/status/123456789' --dry-run
x2o apply .x2o-cache/123456789/plan.json
```

A dry run performs research and saves proposed Markdown in a JSON plan, but does not modify the vault. `apply` refuses if a target note changed since the plan was created. Config selection goes before the subcommand: `x2o --config other.toml ingest URL`.

## Process bookmarks in batches

```bash
x2o ingest --file bookmarks.txt
x2o ingest URL1 URL2 URL3
```

Files can be plain text, CSV, JSON, bookmark HTML, or an X archive JavaScript file containing status URLs. Query strings are stripped and repeated post IDs are deduplicated within an export. This ingestion command reads exported links. For account export and optional cleanup, use the bookmark commands below. Batch processing continues after an individual failure and returns a nonzero exit code if any post fails.

Processed posts are skipped before making model calls. Use `--force` to redo research and replace that post's generated sections. Metadata and downloaded videos are cached; delete a post's cache directory if you need fresh metadata or media. Search and synthesis run again on every forced refresh. Evidence remains available if synthesis fails, but the CLI does not yet resume halfway through model stages.

## Export and optional bookmark removal

Codex computer use can capture bookmarks from a signed-in desktop browser. The workflow is in [docs/browser-bookmarks-workflow.md](docs/browser-bookmarks-workflow.md). This shell tool does not attach to Codex's desktop browser session. Its unattended account integration uses X's official API, with an OAuth user token supplied through `X_USER_ACCESS_TOKEN`.

```bash
x2o bookmarks export --output bookmarks.txt
x2o ingest --file bookmarks.txt
x2o bookmarks cleanup --file bookmarks.txt             # preview only
x2o bookmarks cleanup --file bookmarks.txt --execute   # remove verified imports
```

One-command script workflow:

```bash
x2o bookmarks sync --output bookmarks.txt --remove-after-import
```

Removal is off by default. Only exported post IDs with successful vault-write receipts and intact notes are eligible. Failed imports and dry runs stay bookmarked. The authenticated X account must match the export. Exported URLs, cleanup reports, and an attempt/result journal support review and recovery. Choose a new export filename or pass `--overwrite` explicitly. API access and token scopes depend on your X developer app; this tool does not enroll the app or generate OAuth credentials.

For links captured by computer use, `x2o bookmarks export --from-file captured-links.txt --account-handle YOUR_HANDLE --output bookmarks.txt` creates the same local manifest without API access. Actual browser removal needs an explicit instruction in the desktop app; API removal needs `--execute` or `--remove-after-import`. Earlier imports without write receipts need a refresh with `--force` before cleanup can consider them.

## Other providers and customization

See `x2o.example.toml` for all settings and limits. Copy it or edit `x2o.toml`; unknown settings produce an error rather than being ignored.

**Existing Codex login:** set `provider = "codex"` and `transcription = "local"`. Install the Codex CLI and run `codex login` if needed. The tool calls `codex exec` with the selected model, live search, attached frames, a read-only sandbox, and a schema for synthesis. Inference runs in a temporary directory without loading your user config, so it does not inherit configured MCP connectors. Audio is handled locally. The CLI must support the flags shown by `codex exec --help`; account/model eligibility is checked by Codex at runtime. This provider uses your Codex account limits rather than the OpenAI API key.

**Other LLMs:** set `provider = "compatible"`, `base_url`, `model`, and `api_key_env` for an OpenAI-compatible Chat Completions service such as Ollama or OpenRouter. Use a vision-capable model for frame analysis. Set `TAVILY_API_KEY` for live search; this path generates three queries and fetches search evidence independently of the LLM provider. It validates the final JSON locally. Set `transcription = "local"`, or configure a separate transcription API URL/key. API keys are read from environment variables, never written into notes or configuration. Provider capabilities differ; unsupported model features fail explicitly.

**Local speech transcription:**

```bash
python -m pip install -e '.[local-audio]'
```

Set `transcription = "local"`. Faster Whisper runs on CPU with voice activity detection and downloads its configured model on first use. `local_whisper_model` defaults to `base`. PyAV is pinned to a version compatible with Faster Whisper's audio decoder. Local transcription supplies segment timestamps. API transcription supplies text without word timestamps. Neither path analyzes music, sound effects, or vocal tone. Silence/music can confuse speech recognition; an empty transcript means no speech was recognized, not that the clip is silent. `transcription = "off"` records missing audio analysis explicitly.

**Custom prompts:**

```bash
x2o prompts ./my-prompts
```

Edit the copied research and synthesis prompts and set `research_prompt` / `synthesis_prompt` in TOML. Your prompt text is appended to the packaged baseline. Change research focus, tone, topic granularity, technical detail, language, or preferred source types. The JSON result shape and filesystem safeguards remain enforced by code.

## Research and media workflow

1. Retrieve post metadata using FxTwitter's public endpoint; cache the original JSON and check its ID. The service is a third party and can be unavailable. Protected/deleted posts may require a manual export. `x2o fetch URL` needs no model credentials. `--post-json export.json` accepts the same FxTwitter response shape for one URL.
2. Download bounded media. A bundled FFmpeg binary is available through `imageio-ffmpeg`, so a system installation is optional. Extract uniformly spaced JPEG frames and speech audio. The default limit is four media items, twelve frames per video, ten minutes of analyzed video, and 100 MB per download. Longer clips are marked as truncated. Audio is compressed to stay below the API upload limit. Photos are also included.
3. Read links in the post, including expanded short links. Search discovers relevant project and paper links when the post itself has none. Read primary sources and look separately for criticism, reproduction attempts, and opinions. Discovered project links lead to repositories and arXiv pages; arXiv abstracts lead to HTML full text, with PDF fallback when HTML is unavailable.
4. Read up to `max_sources` documents in each retrieval stage. HTTP downloads are bounded; extraction is capped at 40,000 characters per document and 60 PDF pages. Fetch errors and truncation are supplied as evidence gaps. The original URLs and extracted text are saved under the cache, along with search provenance and structured research. Existing notes provide naming context; at most 300 inventory entries, with short excerpts, enter the synthesis prompt.
5. Generate structured research and validate it. Source URLs and prose citations must appear in the collected evidence. This catches invented citation URLs; it does not guarantee every claim is correct. Search snippets, unverified claims, and machine-generated reviews are labeled by the prompt. Sparse discussion is recorded honestly.
6. Prepare and apply a deterministic vault plan. Source text and model output are never used as shell commands or arbitrary file paths.

An X thread's replies are not automatically crawled. Web search may discover relevant replies/discussion. The tool does not yet run OCR on every video frame, hear non-speech audio, or download arbitrary additional videos linked from a project website. It analyzes the media attached to the selected X post and reads linked pages/papers.

## Vault layout and editing

```text
Your vault/
  X Bookmarks.md
  Posts/<post-id>.md
  Topics/CoSurfGS.md
  Topics/3D Gaussian Splatting.md
  ...existing notes reused by name/alias...
  .x2o/processed.json
  .x2o/backups/<timestamp>/
```

Post YAML properties include `type`, `post_id`, `source`, `author`, `author_name`, `published`, `date`, `processed`, `research_provider`, `model`, `topics`, `tags`, and `status`. Publication timestamps retain UTC; `date` is the UTC calendar date. Search by author/date or use Obsidian Properties, Bases, Graph, and Backlinks. Dataview is optional, for example:

```dataview
TABLE author, date, topics
FROM "Posts"
WHERE type = "x-post"
SORT date DESC
```

Generated sections have HTML comment markers. Put your annotations outside those markers; generated content inside them is replaced on refresh. Existing topic notes retain their text and properties. New topic pages get an overview and attributed contributions from each post, so new bookmarks accumulate evidence on common pages without rewriting your whole note. Shared topic overviews are initial introductions; the tool does not automatically rewrite a global synthesis after every contribution.

If topic selection changes on refresh, the old per-post contribution is removed from previously linked topic notes, while other content is retained. Empty topic notes are not deleted. The post's existing custom YAML properties are preserved; managed post fields are refreshed.

Writes use an exclusive lock, source-content hashes, individual atomic file replacement, backups of preexisting notes, and rollback on caught write failures. An abrupt process termination or power failure can interrupt the multi-file operation; backups and their manifest allow recovery. A stale `.x2o/write.lock` must be removed only after verifying the previous process stopped. A vault is expected to be on a local filesystem; symlink targets are rejected. Hidden Obsidian configuration is left alone. Media/evidence live in the repo cache; notes contain original media links and the transcript, rather than copying large videos into the vault.

## Example and validation

`examples/cosurfgs-research.json` contains a research record prepared in this Codex conversation from retrieved primary sources, inspected video frames, local speech recognition, and attributed outside discussion. The example was written into the supplied vault with the same validated vault writer; it is a demonstration of the note structure, not a claim that an API inference ran here.

Run checks with:

```bash
python -m pytest -q
```

Tests cover source provenance, required OpenAI search, structured response handling, safe paths, existing-note preservation, aliases, idempotent refreshes, metadata updates, changed topic selection, conflict refusal, rollback, batch failures, media limitations, and manual post metadata. Live metadata/source downloads, FFmpeg frame extraction, and local transcription were exercised with the supplied CoSurfGS URL. Paid inference requires configured credentials; mock tests do not verify a provider's live model access.

## References

- [GPT-6.1 Sol model and modalities](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Responses web search and source provenance](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Speech transcription](https://developers.openai.com/api/docs/guides/speech-to-text)
- [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [FxTwitter post endpoint](https://github.com/FxEmbed/FxEmbed/wiki/Status-Fetch-API)
