"""Video downloader using yt-dlp.

Supports YouTube, Instagram Reels, TikTok, and anything else yt-dlp's default
extractors handle. YouTube needs an aggressive player-client fallback chain
because YT periodically breaks individual clients; other hosts use yt-dlp's
default behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yt_dlp


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith("youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")


def _host_label(url: str) -> str:
    """Short, user-facing host name for error messages ("Instagram", "TikTok", …)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "this host"
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "YouTube"
    if host.endswith("instagram.com"):
        return "Instagram"
    if host.endswith("tiktok.com"):
        return "TikTok"
    return host or "this host"

Quality = Literal["480p", "720p", "1080p"]

_HEIGHT_BY_QUALITY: dict[Quality, int] = {"480p": 480, "720p": 720, "1080p": 1080}


def build_format_selector(quality: Quality) -> str:
    """Build a yt-dlp format selector string for the given quality.

    Prefers merging separate high-quality video + audio streams (requires
    ffmpeg). Falls back to a single combined file only if merging isn't
    possible — that fallback path on modern YouTube usually means ~360p,
    which is why ffmpeg is strongly recommended.
    """
    height = _HEIGHT_BY_QUALITY[quality]
    return (
        # 1. Best video+audio at target height, merged via ffmpeg (MP4 + M4A).
        f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
        # 2. Any codec, still merged.
        f"bestvideo[height<={height}]+bestaudio/"
        # 3. Single combined file (modern YouTube → usually low res).
        f"best[ext=mp4][height<={height}]/"
        f"best[height<={height}]"
    )


# YouTube periodically breaks individual player clients. Trying several in
# sequence dramatically reduces 403/"unable to download video data" errors.
_PLAYER_CLIENT_FALLBACKS: tuple[tuple[str, ...], ...] = (
    ("ios", "web"),
    ("android", "web"),
    ("tv", "web"),
    ("mweb",),
    ("web",),
)

# Browsers to try reading cookies from when the unauthenticated attempts fail.
# yt-dlp's cookies_from_browser makes requests look like a signed-in session,
# which fixes most "Forbidden" / "unable to download video data" errors.
_COOKIE_BROWSERS: tuple[str, ...] = ("safari", "chrome", "firefox", "edge", "brave")

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def download_video(
    url: str,
    output_dir: str | Path = "downloads",
    quality: Quality = "720p",
    filename_template: str = "%(id)s.%(ext)s",
    cookies_from_browser: str | None = None,
    cookies_file: str | Path | None = None,
) -> tuple[Path, dict]:
    """Download a YouTube video as MP4 at the requested quality.

    Tries multiple YouTube player clients in sequence — YouTube frequently
    breaks one or two at a time, and rotating through ios/android/tv/web
    fixes most 403 / "unable to download video data" errors without code
    changes.

    Args:
        url: YouTube video URL.
        output_dir: Directory to save the downloaded file.
        quality: Target resolution, "480p" or "720p".
        filename_template: yt-dlp output template (relative to output_dir).

    Returns:
        Path to the downloaded MP4 file.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    is_youtube = _is_youtube_url(url)
    # YouTube has DASH-style separate video+audio streams that we must merge to
    # get >360p, so we use a strict selector there. Other hosts (IG, TikTok)
    # mostly serve single combined MP4s, but Instagram occasionally splits
    # audio and video into separate streams — "best" alone picks the best
    # single file, which for those splits is a silent video. Prefer merged
    # video+audio first, fall back to combined-best, fall back to anything.
    format_selector = (
        build_format_selector(quality)
        if is_youtube
        else "bestvideo*+bestaudio/best/bestvideo*"
    )

    base_opts = {
        "format": format_selector,
        "outtmpl": str(out_dir / filename_template),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": {"User-Agent": _BROWSER_UA},
        "retries": 3,
        "fragment_retries": 3,
    }

    # Build the rung list:
    #   1. Anonymous attempts across each player-client combo.
    #   2. Cookie-authenticated attempts using each available local browser.
    # If the caller pinned a specific browser, we try only that one (after the
    # anonymous attempts) — usually because they already know it works.
    # On Linux (Fly's image) we skip the browser-cookie rungs entirely — none
    # of those browsers ship in the container, so every attempt would just
    # raise "could not find <X> cookies database" and bury the real error.
    if cookies_from_browser:
        cookie_browsers: tuple[str | None, ...] = (cookies_from_browser,)
    elif sys.platform == "darwin":
        cookie_browsers = _COOKIE_BROWSERS
    else:
        cookie_browsers = ()
    attempts: list[dict] = []

    cookies_path = Path(cookies_file) if cookies_file else None

    if is_youtube:
        # Highest-priority: a Netscape-format cookies.txt that authenticates as a
        # real signed-in YouTube account. This is the cleanest way to dodge YT's
        # cloud-IP throttling on Fly.
        if cookies_path and cookies_path.exists():
            attempts.append(
                {
                    "cookiefile": str(cookies_path),
                    "extractor_args": {
                        "youtube": {"player_client": ["web", "ios", "android"]}
                    },
                }
            )
        for clients in _PLAYER_CLIENT_FALLBACKS:
            attempts.append(
                {"extractor_args": {"youtube": {"player_client": list(clients)}}}
            )
        # Browser cookies (only useful locally — Fly can't read your Mac browser).
        for browser in cookie_browsers:
            attempts.append(
                {
                    "cookiesfrombrowser": (browser,),
                    "extractor_args": {
                        "youtube": {"player_client": ["web", "ios", "android"]}
                    },
                }
            )
    else:
        # Non-YouTube (Instagram, TikTok, etc.): yt-dlp's default extractors
        # handle these without per-client juggling. Still try cookies if
        # available, since IG/TikTok also gate some content behind login.
        if cookies_path and cookies_path.exists():
            attempts.append({"cookiefile": str(cookies_path)})
        attempts.append({})
        for browser in cookie_browsers:
            attempts.append({"cookiesfrombrowser": (browser,)})

    # `last_error` is whatever raised most recently; `first_download_error`
    # is the first error from an attempt that actually reached yt-dlp's
    # extractor (i.e. not a "cookies database missing" setup failure).
    # We prefer the latter for the user-facing message — it's the real cause.
    last_error: Exception | None = None
    first_download_error: Exception | None = None
    for extra in attempts:
        opts = {**base_opts, **extra}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                downloaded = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
                if not downloaded.exists():
                    candidate = Path(ydl.prepare_filename(info))
                    if candidate.exists():
                        downloaded = candidate
                if downloaded.exists():
                    # Keep just the small subset we need downstream (titles for
                    # the library, source URL); the full info dict is huge.
                    summary = {
                        "id": info.get("id"),
                        "title": info.get("title"),
                        "uploader": info.get("uploader"),
                        "duration": info.get("duration"),
                        "webpage_url": info.get("webpage_url") or url,
                        # Actual quality served — diagnose silent downgrades.
                        "height": info.get("height"),
                        "width": info.get("width"),
                        "format_note": info.get("format_note"),
                        "player_client": (
                            extra.get("extractor_args", {})
                            .get("youtube", {})
                            .get("player_client")
                        ),
                        "used_cookies": "cookiesfrombrowser" in extra,
                    }
                    return downloaded, summary
        except Exception as e:
            # Includes DownloadError plus cookies_from_browser errors when a
            # given browser isn't installed / locked / unreadable.
            last_error = e
            msg = str(e).lower()
            is_cookie_setup_error = (
                "could not find" in msg and "cookies database" in msg
            ) or "operation not permitted" in msg
            if first_download_error is None and not is_cookie_setup_error:
                first_download_error = e
            continue

    real_error = first_download_error or last_error
    host_label = _host_label(url)
    has_cookies_file = cookies_path is not None and cookies_path.exists()
    cookies_hint = (
        f"Add signed-in {host_label} cookies to the cookies.txt on the Fly "
        "volume (cat them into /persistent/data/cookies.txt)."
        if not has_cookies_file
        else f"The cookies.txt on the volume doesn't include valid {host_label} "
        "credentials, or the session has expired — re-export and replace it."
    )
    if is_youtube:
        raise RuntimeError(
            f"Could not download YouTube video. YouTube blocked every player "
            f"client and cookie fallback. {cookies_hint} Real error: {real_error}"
        )
    raise RuntimeError(
        f"Could not download from {host_label}. Anonymous cloud-IP requests are "
        f"usually rate-limited or login-walled here. {cookies_hint} "
        f"(The post may also be private, deleted, or region-locked.) "
        f"Real error: {real_error}"
    )


def get_video_metadata(url: str) -> dict:
    """Fetch metadata for a YouTube video without downloading it."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "fps": info.get("fps"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Download a YouTube video as MP4.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("-o", "--output-dir", default="downloads")
    parser.add_argument("-q", "--quality", choices=["480p", "720p"], default="720p")
    args = parser.parse_args()

    path, info = download_video(args.url, args.output_dir, args.quality)
    print(f"Downloaded: {path}  ({info.get('title')})")
