"""Write the conversation-researched example with the same vault writer, without inference."""
import argparse
import json
from pathlib import Path
from x2o.collect import parse_post
from x2o.models import Research
from x2o.vault import plan, apply_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path)
    parser.add_argument("--media-record", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        parser.error("Vault must already exist")
    here = Path(__file__).parent
    post = parse_post(json.loads((here / 'cosurfgs-post.json').read_text()), '1871455615752847763')
    result = Research.model_validate_json((here / 'cosurfgs-research.json').read_text())
    media = json.loads(args.media_record.read_text()) if args.media_record else []
    output = plan(vault, post, result, 'codex-conversation', 'conversation-assisted example', media)
    if args.dry_run:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for path in apply_plan(output, vault):
            print(f"Wrote {path}")


if __name__ == '__main__':
    main()
