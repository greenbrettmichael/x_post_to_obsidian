"""Bounded public URL retrieval. Source content never becomes instructions."""
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx

USER_AGENT = "x-post-to-obsidian/0.1 (personal bookmark research)"


def public_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) source URLs are supported")
    for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"Non-public source URL blocked: {parsed.hostname}")
    return url


def download(url: str, limit: int = 8 * 1024 * 1024) -> tuple[bytes, str, str]:
    # Validate each redirect, including shortened links. No authentication headers.
    with httpx.Client(timeout=45, headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(8):
            public_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"Download exceeded {limit // 1024 // 1024} MB")
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", ""), str(response.url)
    raise ValueError("Too many redirects")
