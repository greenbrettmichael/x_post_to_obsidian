import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import httpx
from openai import OpenAI
from .models import Config, Research


def markdown_urls(text):
    return list(dict.fromkeys(u.rstrip('.,;') for u in re.findall(r'https?://[^\s<>"\)\]]+', text)))


def response_text(response):
    if response.status != "completed":
        raise ValueError(f"Model response did not complete: {response.status}; increase max_output_tokens if truncated")
    if not response.output_text:
        raise ValueError("Model returned no text (possibly refused)")
    return response.output_text


class Provider:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        if cfg.provider != "codex":
            key = os.environ.get(cfg.api_key_env)
            if cfg.provider == "openai" and not key:
                raise ValueError(f"Set {cfg.api_key_env} to an API key, or choose provider='codex'/'compatible'")
            self.client = OpenAI(api_key=key or "local", base_url=cfg.base_url, timeout=cfg.timeout_seconds, max_retries=2)
        elif not shutil.which("codex"):
            raise ValueError("Codex CLI not found; install and sign in with `codex login`")
        if cfg.provider == "compatible" and not os.environ.get("TAVILY_API_KEY"):
            raise ValueError("Compatible providers need TAVILY_API_KEY for live web search")

    def codex(self, instructions, prompt, images, schema=None, require_search=False):
        with tempfile.TemporaryDirectory(prefix="x2o-inference-") as directory:
            output = Path(directory) / "result.txt"
            args = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--json", "--color", "never", "-m", self.cfg.model,
                    "-c", 'approval_policy="never"', "-c", 'web_search="live"',
                    "-c", f'model_reasoning_effort="{self.cfg.reasoning_effort or "medium"}"',
                    "-C", directory, "-o", str(output)]
            if schema:
                schema_path = Path(directory) / "schema.json"
                schema_path.write_text(json.dumps(schema))
                args += ["--output-schema", str(schema_path)]
            for path in images:
                args += ["--image", str(path.resolve())]
            args += ["-"]
            result = subprocess.run(args, input=instructions + "\n\n" + prompt,
                                    capture_output=True, text=True, timeout=self.cfg.timeout_seconds)
            if result.returncode or not output.exists():
                # Avoid printing full auth/debug logs to the user.
                raise ValueError(f"Codex inference failed (exit {result.returncode}); verify `codex login status` and model access")
            events = []
            for line in result.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            self.codex_events = events
            if require_search and not any(e.get("type") == "item.completed" and
                    e.get("item", {}).get("type") in {"web_search", "web_search_call"} for e in events):
                raise ValueError("Codex did not execute web search; no notes written")
            return output.read_text()

    def content(self, prompt, images):
        content = [{"type": "input_text", "text": prompt}]
        for path in images:
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append({"type": "input_text", "text": f"Image evidence: {path.resolve()}"})
            content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "high"})
        return [{"role": "user", "content": content}]

    def chat(self, instructions, prompt, images):
        content = [{"type": "text", "text": prompt}]
        for path in images:
            content.append({"type": "text", "text": f"Image evidence: {path.resolve()}"})
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()}})
        response = self.client.chat.completions.create(model=self.cfg.model,
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": content}],
            max_tokens=self.cfg.max_output_tokens)
        if response.choices[0].finish_reason != "stop":
            raise ValueError("Compatible model response truncated or refused")
        return response.choices[0].message.content or ""

    def research(self, instructions, prompt, images, post):
        if self.cfg.provider == "codex":
            text = self.codex(instructions, prompt, images, require_search=True)
            return {"text": text, "urls": markdown_urls(text), "raw": self.codex_events, "provider": "codex"}
        if self.cfg.provider == "compatible":
            query_text = self.chat("Return only a JSON array of 3 web search queries: official project/paper, "
                                   "independent opinions, and criticism/reproduction.", post.text, [])
            queries = json.loads(query_text.strip().removeprefix('```json').removesuffix('```').strip())
            if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
                raise ValueError("Model returned invalid search queries")
            results = []
            with httpx.Client(timeout=60) as client:
                for query in queries[:3]:
                    response = client.post("https://api.tavily.com/search", json={
                        "api_key": os.environ["TAVILY_API_KEY"], "query": query, "search_depth": "advanced", "max_results": 5})
                    response.raise_for_status()
                    results += response.json().get("results", [])
            return {"text": self.chat(instructions, prompt + "\nLIVE SEARCH EVIDENCE:\n" + json.dumps(results), images),
                    "urls": list(dict.fromkeys(r["url"] for r in results)), "provider": "compatible"}
        args = dict(model=self.cfg.model, instructions=instructions, input=self.content(prompt, images),
                    tools=[{"type": "web_search"}], tool_choice="required",
                    include=["web_search_call.action.sources"], max_output_tokens=self.cfg.max_output_tokens, store=False)
        if self.cfg.reasoning_effort:
            args["reasoning"] = {"effort": self.cfg.reasoning_effort}
        response = self.client.responses.create(**args)
        text = response_text(response)
        raw = response.model_dump(mode="json")
        urls = []
        for item in raw["output"]:
            if item["type"] == "web_search_call":
                urls += [s["url"] for s in (item.get("action") or {}).get("sources", []) if s.get("url")]
            for part in item.get("content", []):
                urls += [a["url"] for a in part.get("annotations", []) if a.get("type") == "url_citation"]
        if not any(i["type"] == "web_search_call" for i in raw["output"]):
            raise ValueError("Model did not execute web search; no notes written")
        return {"text": text, "urls": list(dict.fromkeys(urls)), "raw": raw, "provider": "openai"}

    def synthesize(self, instructions, prompt, images):
        schema = Research.model_json_schema()
        # URI format is validated by Pydantic locally, but is not in the API JSON-schema subset.
        schema["$defs"]["Source"]["properties"]["url"].pop("format", None)
        if self.cfg.provider == "codex":
            return Research.model_validate_json(self.codex(instructions, prompt, images, schema))
        if self.cfg.provider == "compatible":
            text = self.chat(instructions + "\nJSON schema:\n" + json.dumps(schema), prompt, images)
            return Research.model_validate_json(text.strip().removeprefix('```json').removesuffix('```').strip())
        args = dict(model=self.cfg.model, instructions=instructions, input=self.content(prompt, images),
                    text={"format": {"type": "json_schema", "name": "research", "strict": True, "schema": schema}},
                    max_output_tokens=self.cfg.max_output_tokens, store=False)
        if self.cfg.reasoning_effort:
            args["reasoning"] = {"effort": self.cfg.reasoning_effort}
        return Research.model_validate_json(response_text(self.client.responses.create(**args)))
