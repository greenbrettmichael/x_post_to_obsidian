from pathlib import Path
import tomllib
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Topic(StrictModel):
    name: str
    overview: str
    contribution: str
    related: list[str]


class Source(StrictModel):
    title: str
    url: HttpUrl
    kind: Literal["primary", "commentary", "machine-review"]


class Research(StrictModel):
    title: str
    summary: str
    findings: str
    opinions: str
    limitations: str
    media_analysis: str
    topics: list[Topic] = Field(min_length=1, max_length=12)
    sources: list[Source] = Field(min_length=1)


class Post(BaseModel):
    id: str
    url: str
    author: str
    author_name: str
    published: str
    text: str
    links: list[str] = []
    media: list[dict] = []
    raw: dict = {}


class Config(StrictModel):
    vault: Path
    provider: str = "openai"  # openai, compatible, codex
    model: str = "gpt-6.1-sol"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    reasoning_effort: str | None = "medium"
    transcription: str = "openai"  # openai, local, off
    transcription_model: str = "gpt-transcribe"
    transcription_base_url: str = "https://api.openai.com/v1"
    transcription_key_env: str = "OPENAI_API_KEY"
    local_whisper_model: str = "base"
    research_prompt: Path | None = None
    synthesis_prompt: Path | None = None
    cache: Path = Path(".x2o-cache")
    max_sources: int = Field(default=10, ge=1, le=30)
    max_frames: int = Field(default=12, ge=1, le=30)
    max_media: int = Field(default=4, ge=0, le=10)
    max_video_seconds: int = Field(default=600, ge=1, le=3600)
    max_download_mb: int = Field(default=100, ge=1, le=500)
    max_output_tokens: int = Field(default=16000, ge=2000, le=64000)
    timeout_seconds: int = Field(default=600, ge=30, le=1800)

    @classmethod
    def load(cls, path: Path):
        data = tomllib.loads(path.read_text())
        for key in ("vault", "cache", "research_prompt", "synthesis_prompt"):
            if data.get(key):
                p = Path(data[key]).expanduser()
                data[key] = (path.resolve().parent / p).resolve() if not p.is_absolute() else p
        config = cls.model_validate(data)
        if config.provider not in {"openai", "compatible", "codex"}:
            raise ValueError("provider must be openai, compatible, or codex")
        if config.transcription not in {"openai", "local", "off"}:
            raise ValueError("transcription must be openai, local, or off")
        if not config.vault.is_dir():
            raise ValueError(f"Vault directory does not exist: {config.vault}")
        return config
