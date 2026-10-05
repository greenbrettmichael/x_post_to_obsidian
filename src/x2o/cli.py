import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from .collect import canonical, urls_from_file, fetch_post
from .models import Config
from .pipeline import research_post
from .vault import apply_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Research X posts into a linked Obsidian vault")
    parser.add_argument("--config", type=Path, default=Path("x2o.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Write an editable config without touching vault notes")
    init.add_argument("--vault", type=Path, required=True)
    ingest = sub.add_parser("ingest", help="Research URLs and write notes (or prepare a dry-run plan)")
    ingest.add_argument("urls", nargs="*")
    ingest.add_argument("--file", type=Path, help="Bookmark URLs: text, JSON, CSV, HTML or X archive JS")
    ingest.add_argument("--post-json", type=Path, help="Manually exported FxTwitter post data for one URL")
    ingest.add_argument("--dry-run", action="store_true", help="Save plans in cache; do not modify vault")
    ingest.add_argument("--force", action="store_true", help="Refresh research on an already processed post")
    apply = sub.add_parser("apply", help="Apply a reviewed dry-run plan with conflict checks")
    apply.add_argument("plan", type=Path)
    fetch = sub.add_parser("fetch", help="Retrieve post metadata without model credentials")
    fetch.add_argument("url")
    sub.add_parser("doctor", help="Check config, inference credentials, media tools and vault")
    prompts = sub.add_parser("prompts", help="Copy baseline prompts for customization")
    prompts.add_argument("directory", type=Path)
    bookmarks = sub.add_parser("bookmarks", help="Export bookmarks and optionally remove verified imports")
    actions = bookmarks.add_subparsers(dest="bookmark_action", required=True)
    export = actions.add_parser("export", help="Export with X API, or normalize links captured in a browser")
    export.add_argument("--output", type=Path, default=Path("bookmarks.txt"))
    export.add_argument("--from-file", type=Path, help="Links captured by Codex computer use; no API needed")
    export.add_argument("--account-id", help="X account ID for a browser-captured export")
    export.add_argument("--account-handle", help="Signed-in X handle, when account ID is unavailable")
    export.add_argument("--overwrite", action="store_true")
    clean = actions.add_parser("cleanup", help="Preview removal; use --execute to remove verified imports")
    clean.add_argument("--file", type=Path, required=True)
    clean.add_argument("--execute", action="store_true")
    sync = actions.add_parser("sync", help="Export and import, optionally removing successful imports")
    sync.add_argument("--output", type=Path, default=Path("bookmarks.txt"))
    sync.add_argument("--overwrite", action="store_true")
    sync.add_argument("--remove-after-import", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if args.config.exists():
                raise ValueError(f"Config already exists: {args.config}")
            if not args.vault.expanduser().is_dir():
                raise ValueError("Vault directory must already exist")
            args.config.write_text(f'vault = {json.dumps(str(args.vault.expanduser().resolve()))}\nprovider = "openai"\nmodel = "gpt-6.1-sol"\ntranscription = "openai"\n')
            print(f"Created {args.config.resolve()}; configure model credentials before ingesting")
            return 0
        if args.command == "prompts":
            args.directory.mkdir(parents=True, exist_ok=True)
            for source in (Path(__file__).parent / "prompts").glob("*.md"):
                destination = args.directory / source.name
                if destination.exists():
                    raise ValueError(f"Prompt already exists: {destination}")
                shutil.copyfile(source, destination)
            print(f"Copied baseline prompts to {args.directory.resolve()}")
            return 0
        cfg = Config.load(args.config)
        if args.command == "bookmarks":
            from .bookmarks import XBookmarksAPI, export_urls, export_api, cleanup, sync
            if args.bookmark_action == "export" and args.from_file:
                if not args.account_id and not args.account_handle:
                    raise ValueError("--from-file requires --account-id or --account-handle to bind cleanup to the correct X account")
                account = {"id": args.account_id} if args.account_id else {"username": args.account_handle.lstrip('@')}
                manifest = export_urls(urls_from_file(args.from_file), args.output, account, "browser", args.overwrite)
                print(f"Exported {len(manifest['bookmarks'])} links to {args.output}; account identity must be verified before cleanup")
                return 0
            if args.bookmark_action == "cleanup" and not args.execute:
                cleanup(args.file, cfg.vault)
                return 0
            api = XBookmarksAPI()
            try:
                if args.bookmark_action == "export":
                    manifest = export_api(api, args.output, args.overwrite)
                    print(f"Exported {len(manifest['bookmarks'])} bookmarks to {args.output}")
                    return 0
                if args.bookmark_action == "cleanup":
                    report = cleanup(args.file, cfg.vault, api, execute=True)
                    return 1 if report["failures"] else 0
                report = sync(api, cfg, args.output, research_post, remove=args.remove_after_import, overwrite=args.overwrite)
                return 1 if report["imports_failed"] or report["cleanup"]["failures"] else 0
            finally:
                api.close()
        if args.command == "doctor":
            from .media import ffmpeg
            checks = {"vault": str(cfg.vault), "vault_writable": os.access(cfg.vault, os.W_OK),
                      "provider": cfg.provider, "model": cfg.model, "ffmpeg": ffmpeg(),
                      "inference_credential": bool(os.environ.get(cfg.api_key_env)) if cfg.provider == 'openai' else
                          (bool(shutil.which('codex')) if cfg.provider == 'codex' else True),
                      "search_credential": cfg.provider != 'compatible' or bool(os.environ.get('TAVILY_API_KEY')),
                      "audio": cfg.transcription, "audio_credential": cfg.transcription != 'openai' or bool(os.environ.get(cfg.transcription_key_env))}
            print(json.dumps(checks, indent=2))
            return 0 if checks["inference_credential"] and checks["search_credential"] else 1
        if args.command == "fetch":
            _, post_id = canonical(args.url)
            post = fetch_post(args.url, cfg.cache / post_id)
            print(post.model_dump_json(indent=2, exclude={"raw"}))
            return 0
        if args.command == "apply":
            for path in apply_plan(json.loads(args.plan.read_text()), cfg.vault):
                print(f"Wrote {path}")
            return 0
        urls = list(dict.fromkeys(args.urls + (urls_from_file(args.file) if args.file else [])))
        if not urls:
            raise ValueError("Provide at least one X status URL or --file")
        if args.post_json and len(urls) != 1:
            raise ValueError("--post-json requires exactly one URL")
        failures = 0
        for url in urls:
            try:
                output = research_post(url, cfg, args.post_json, args.force, progress=lambda text: print(text, flush=True))
                if output is None:
                    continue
                if args.dry_run:
                    print(f"Plan: {cfg.cache / output['post_id'] / 'plan.json'}")
                    for change in output["changes"]:
                        print(f"  {'update' if change['before_sha256'] else 'create'} {change['path']}")
                else:
                    for path in apply_plan(output, cfg.vault):
                        print(f"Wrote {path}")
            except Exception as exc:
                failures += 1
                print(f"Failed {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1 if failures else 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
