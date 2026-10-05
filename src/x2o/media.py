import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import imageio_ffmpeg
from openai import OpenAI
from .models import Config, Post
from .network import download


def ffmpeg():
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def run(args):
    result = subprocess.run([ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr[-2000:] or "ffmpeg failed")


def transcribe(path: Path, cfg: Config):
    if cfg.transcription == "off":
        raise ValueError("Audio transcription disabled in configuration")
    if cfg.transcription == "local":
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ValueError('Install local audio support: pip install -e ".[local-audio]"') from exc
        model = WhisperModel(cfg.local_whisper_model, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(path), vad_filter=True)
        return "\n".join(f"[{s.start:.1f}s–{s.end:.1f}s] {s.text}" for s in segments)
    key = os.environ.get(cfg.transcription_key_env)
    if not key:
        raise ValueError(f"Audio transcription needs {cfg.transcription_key_env}, or set transcription='local'")
    client = OpenAI(api_key=key, base_url=cfg.transcription_base_url, timeout=cfg.timeout_seconds)
    with path.open("rb") as audio:
        return client.audio.transcriptions.create(model=cfg.transcription_model, file=audio).text


def process_media(post: Post, cfg: Config, cache: Path):
    records, images = [], []
    for i, item in enumerate(post.media[:cfg.max_media]):
        record = {"url": item.get("url", ""), "type": item.get("type", "unknown"), "frames": []}
        folder = cache / f"media-{i}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if record["type"] == "photo":
                data, _, _ = download(record["url"], cfg.max_download_mb * 1024 * 1024)
                original = folder / "photo-original"
                original.write_bytes(data)
                target = folder / "photo.jpg"
                run(["-y", "-i", str(original), "-frames:v", "1", "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease", str(target)])
                images.append(target)
                record["frames"].append({"seconds": None, "path": str(target.resolve())})
            elif record["type"] in {"video", "gif"}:
                video = folder / "video.mp4"
                if not video.exists():
                    data, _, _ = download(record["url"], cfg.max_download_mb * 1024 * 1024)
                    video.write_bytes(data)
                # ffmpeg's reader obtains duration without requiring a separate ffprobe binary.
                reader = imageio_ffmpeg.read_frames(str(video))
                try:
                    metadata = next(reader)
                finally:
                    reader.close()
                duration = float(metadata.get("duration", 0)) or float(item.get("duration", 0))
                if duration <= 0 or not math.isfinite(duration):
                    raise ValueError("Could not determine video duration")
                sampled_duration = min(duration, cfg.max_video_seconds)
                record.update(duration_seconds=duration, analyzed_seconds=sampled_duration,
                              truncated=duration > sampled_duration)
                for n in range(cfg.max_frames):
                    seconds = (sampled_duration - min(0.2, sampled_duration / 2)) * n / max(1, cfg.max_frames - 1)
                    target = folder / f"frame-{n:02d}.jpg"
                    run(["-y", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
                         "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease", str(target)])
                    if target.exists():
                        images.append(target)
                        record["frames"].append({"seconds": round(seconds, 2), "path": str(target.resolve())})
                if record["type"] != "gif":
                    # At 48 kbps a one-hour clip is under the API's 25 MB upload limit.
                    audio = folder / "audio.mp3"
                    try:
                        run(["-y", "-i", str(video), "-t", str(sampled_duration), "-vn", "-ac", "1",
                             "-ar", "16000", "-b:a", "48k", str(audio)])
                        transcript_path = folder / f"transcript-{cfg.transcription}-{cfg.transcription_model if cfg.transcription == 'openai' else cfg.local_whisper_model}.txt"
                        # Do not allow a configured model name to create arbitrary filesystem paths.
                        transcript_path = folder / ("transcript-" + hashlib.sha256(str(transcript_path.name).encode()).hexdigest()[:16] + ".txt")
                        if transcript_path.exists():
                            transcript = transcript_path.read_text()
                        else:
                            transcript = transcribe(audio, cfg)
                            transcript_path.write_text(transcript)
                        record["transcript"] = transcript
                        record["audio_status"] = "transcribed" if transcript.strip() else "no_speech_recognized"
                        record["transcript_note"] = "Speech recognition only; no assessment of music, sound effects, or tone. " + ("Local transcript includes segment timestamps." if cfg.transcription == "local" else "API transcript has no word timestamps.")
                    except Exception as exc:
                        record["audio_status"] = "failed"
                        record["audio_error"] = f"{type(exc).__name__}: {exc}"
            else:
                record["error"] = "Unsupported media type"
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)
    if len(post.media) > cfg.max_media:
        records.append({"error": f"{len(post.media) - cfg.max_media} media items omitted by max_media"})
    return records, images
