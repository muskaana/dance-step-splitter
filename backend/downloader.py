"""YouTube video downloader using yt-dlp."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yt_dlp

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

    base_opts = {
        "format": build_format_selector(quality),
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
    cookie_browsers: tuple[str | None, ...] = (
        (cookies_from_browser,) if cookies_from_browser else _COOKIE_BROWSERS
    )
    attempts: list[dict] = []

    # Highest-priority: a Netscape-format cookies.txt that authenticates as a
    # real signed-in YouTube account. This is the cleanest way to dodge YT's
    # cloud-IP throttling on Fly.
    cookies_path = Path(cookies_file) if cookies_file else None
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

    last_error: Exception | None = None
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
            continue

    raise RuntimeError(
        "Could not download video — YouTube blocked every player client and no "
        "usable browser cookies were found. Try signing into YouTube in Safari "
        f"or Chrome on this machine, then retry. Last error: {last_error}"
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
