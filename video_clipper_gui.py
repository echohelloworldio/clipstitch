#!/usr/bin/env python3
"""
video_clipper_gui.py (ClipStitch)

A simple desktop GUI for clipping a video at given timestamps and joining
the clips into one final video - from a local file or directly from a URL.

Two source modes:
    1. Local file  - drag & drop a video onto the app, or click to browse.
    2. URL         - paste a YouTube (or other yt-dlp supported site) link.
                      Only the timestamped sections are downloaded directly
                      from the source, so you're not downloading the whole
                      video just to throw most of it away.

Requirements:
    - ffmpeg and ffprobe. This script will automatically use copies of
      ffmpeg.exe / ffprobe.exe placed in the same folder as this script
      (or the same folder as the built .exe) if present. Otherwise it
      falls back to whatever "ffmpeg"/"ffprobe" resolve to on your PATH.
      Download: https://ffmpeg.org/download.html
    - yt-dlp (only needed for URL mode): pip install yt-dlp
    - tkinterdnd2 (optional, enables drag & drop): pip install tkinterdnd2
      Without it, the app still works fully via click-to-browse.

Run it with:
    python video_clipper_gui.py

How to use:
    1. Drag a video onto the drop zone (or click it to browse), OR switch
       to "From URL" and paste a link.
    2. Type your timestamps (one pair per line, e.g. 00:00:10,00:00:45),
       or drag a .txt file of timestamps onto the drop zone.
    3. Click "Clip It".
    4. Once done, you'll be asked where to save the final video and what
       to name it.

A note on legality: only download/clip video you own, have permission to
use, or that is licensed for reuse (e.g. Creative Commons). Downloading
other people's videos may violate the source site's Terms of Service.
"""

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# ----------------------------------------------------------------------
# Core logic (unchanged from the original tool - battle-tested)
# ----------------------------------------------------------------------

def _app_dir():
    """Folder the running script/exe lives in (works both as .py and as a
    PyInstaller --onefile .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_tool(name):
    """Prefer a copy of ffmpeg.exe/ffprobe.exe sitting next to this script
    or exe; fall back to the bare command name so PATH is used instead."""
    exe_name = f"{name}.exe" if os.name == "nt" else name
    local_path = os.path.join(_app_dir(), exe_name)
    if os.path.isfile(local_path):
        return local_path
    return name  # rely on PATH


FFMPEG_BIN = _resolve_tool("ffmpeg")
FFPROBE_BIN = _resolve_tool("ffprobe")

# Default install location used by setup_po_token_server.bat.
DEFAULT_POT_SERVER_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BgUtilPOTServer"
)
POT_SERVER_PING_URL = "http://127.0.0.1:4416/ping"

_pot_server_state = {"process": None}


def _is_pot_server_running(timeout=1.5):
    try:
        with urllib.request.urlopen(POT_SERVER_PING_URL, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def ensure_pot_server(server_dir, log, wait_seconds=8):
    """Make sure the BgUtils PO Token Provider server is running in the
    background, starting it silently if it isn't. This is what unlocks
    high-quality formats on age-restricted/login-gated YouTube videos -
    without it, YouTube silently limits downloads to a low-quality
    fallback (often 360p) even with valid cookies.

    Safe to call every time before a download: if the server is already
    running (from a previous run or started manually), this does nothing
    but confirm it's reachable. Never raises - if it can't be started,
    downloads still proceed, just possibly at lower quality for gated
    videos."""
    if _is_pot_server_running():
        log("  PO Token server: already running.")
        return True

    if not server_dir or not os.path.isdir(server_dir):
        log("  PO Token server: not set up yet (skipping - restricted videos "
            "may download at lower quality). Run setup_po_token_server.bat once "
            "to enable this.")
        return False

    build_entry = os.path.join(server_dir, "build", "main.js")
    if not os.path.isfile(build_entry):
        log(f"  PO Token server: folder found but not compiled ({build_entry} "
            f"missing). Run setup_po_token_server.bat to finish setup.")
        return False

    node_path = None
    for candidate in ("node", "node.exe"):
        from shutil import which
        found = which(candidate)
        if found:
            node_path = found
            break

    if not node_path:
        log("  PO Token server: Node.js not found on PATH - can't auto-start "
            "the server. Install Node.js from https://nodejs.org.")
        return False

    log("  PO Token server: not running yet, starting it in the background...")
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [node_path, "main.js"],
            cwd=os.path.join(server_dir, "build"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        _pot_server_state["process"] = process
    except OSError as e:
        log(f"  PO Token server: failed to start ({e}). Continuing without it.")
        return False

    for _ in range(int(wait_seconds * 2)):
        time.sleep(0.5)
        if _is_pot_server_running():
            log("  PO Token server: started successfully.")
            return True

    log("  PO Token server: didn't respond in time. Continuing without it "
        "(restricted videos may download at lower quality).")
    return False


@atexit.register
def _cleanup_pot_server():
    process = _pot_server_state.get("process")
    if process and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass


def check_ffmpeg_available():
    for tool in (FFMPEG_BIN, FFPROBE_BIN):
        try:
            subprocess.run(
                [tool, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    return True


def parse_time(value):
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)

    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Unrecognized timestamp format: '{value}'")

    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)

    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_timestamp(total_seconds):
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def parse_timestamps_text(text):
    """Parse timestamp pairs from raw text (same format as the file version)."""
    segments = []
    for line_num, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        pieces = re.split(r"[,\s]+|-{1,2}", line)
        pieces = [p for p in pieces if p]

        if len(pieces) != 2:
            raise ValueError(f"Line {line_num} is invalid: '{raw_line.strip()}'. "
                              f"Expected format: START,END")

        start = parse_time(pieces[0])
        end = parse_time(pieces[1])

        if end <= start:
            raise ValueError(f"Line {line_num}: end time must be after start time "
                              f"({pieces[0]} -> {pieces[1]})")

        segments.append((start, end))

    if not segments:
        raise ValueError("No valid timestamp lines found.")

    return segments


def get_video_duration(input_path):
    result = subprocess.run(
        [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def cut_clip(input_path, start, end, out_path, reencode, on_progress=None):
    """Cuts one clip with ffmpeg, reporting live fractional progress
    (0.0-1.0) via on_progress as ffmpeg reports how far into the clip it
    is - instead of only knowing "started" vs "finished"."""
    duration = max(end - start, 0.001)
    if reencode:
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", seconds_to_timestamp(start),
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "slow", "-crf", "16",
            "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", seconds_to_timestamp(start),
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
    cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error", out_path]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    stderr_lines = []

    def _drain_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    for line in process.stdout:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "out_time_ms":
            try:
                seconds_done = int(value) / 1_000_000
                if on_progress:
                    on_progress(min(1.0, max(0.0, seconds_done / duration)))
            except ValueError:
                pass
        elif key == "out_time" and value not in ("N/A", ""):
            try:
                seconds_done = parse_time(value)
                if on_progress:
                    on_progress(min(1.0, max(0.0, seconds_done / duration)))
            except ValueError:
                pass

    process.wait()
    stderr_thread.join(timeout=2)

    if process.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed while cutting clip {start}-{end}.\n{''.join(stderr_lines)}"
        )
    if on_progress:
        on_progress(1.0)


def _make_progress_hook(log, label, on_progress=None):
    """Reports real download progress into the GUI log (throttled) and,
    via on_progress, a live fractional value (0.0-1.0) for the progress
    bar - computed from actual bytes downloaded, not just start/finish."""
    state = {"last_logged_pct": -100}

    def hook(d):
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct_str = (d.get("_percent_str") or "").strip()
            speed_str = (d.get("_speed_str") or "").strip()
            eta_str = (d.get("_eta_str") or "").strip()

            frac = None
            if total:
                frac = min(1.0, max(0.0, downloaded / total))
            else:
                try:
                    frac = float(re.sub(r"[^\d.]", "", pct_str)) / 100 if pct_str else None
                except ValueError:
                    frac = None

            if on_progress and frac is not None:
                on_progress(frac)

            pct_display = frac * 100 if frac is not None else None
            if pct_display is None or pct_display - state["last_logged_pct"] >= 5 or pct_display >= 99.5:
                bits = [f"{label}: {pct_str or '...'}"]
                if speed_str:
                    bits.append(f"at {speed_str}")
                if eta_str:
                    bits.append(f"ETA {eta_str}")
                log("  " + " ".join(bits))
                if pct_display is not None:
                    state["last_logged_pct"] = pct_display
        elif status == "finished":
            if on_progress:
                on_progress(1.0)
            log(f"  {label}: download finished, merging/processing...")
        elif status == "error":
            log(f"  {label}: an error occurred during download.")

    return hook


def _build_cookie_and_client_opts(cookies_browser, cookies_profile, cookies_file):
    """Shared cookie + extractor settings for both download_segment and check_url.

    Prefers a cookies.txt FILE over browser extraction when both are given,
    since modern Chrome/Edge "app-bound encryption" often blocks reading
    cookies straight from the browser, even when it's fully closed.

    Queries yt-dlp's own default set of YouTube clients (which includes
    "web_creator" - the client that actually carries high-resolution
    formats for age-restricted/gated videos) rather than overriding it.

    Also allows yt-dlp to auto-fetch its official JS challenge-solver script
    (yt-dlp-ejs) straight from its GitHub repo - the officially published
    solver from the yt-dlp project itself."""
    opts = {
        "remote_components": ["ejs:github"],
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        if cookies_profile:
            opts["cookiesfrombrowser"] = (cookies_browser.lower(), cookies_profile)
        else:
            opts["cookiesfrombrowser"] = (cookies_browser.lower(),)
    return opts


# Quality presets: label shown in the GUI -> yt-dlp format selector string.
QUALITY_PRESETS = {
    "Best available": "bestvideo*+bestaudio/best",
    "Up to 4K (2160p)": "bestvideo*[height<=2160]+bestaudio/best[height<=2160]",
    "Up to 1440p": "bestvideo*[height<=1440]+bestaudio/best[height<=1440]",
    "Up to 1080p": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]",
    "Up to 720p": "bestvideo*[height<=720]+bestaudio/best[height<=720]",
    "Up to 480p": "bestvideo*[height<=480]+bestaudio/best[height<=480]",
}


def download_segment(url, start, end, out_path, log, cookies_browser=None,
                      cookies_profile=None, cookies_file=None, label="Downloading",
                      quality="Best available", on_progress=None):
    """Download only one timestamped section of a URL directly at the
    source, using yt-dlp's download-sections feature, then mux it into a
    single mp4 with ffmpeg."""
    if not YT_DLP_AVAILABLE:
        raise RuntimeError(
            "yt-dlp is not installed. Install it with:\n    pip install yt-dlp"
        )

    from yt_dlp.utils import download_range_func

    out_dir = os.path.dirname(out_path)
    out_template = os.path.join(out_dir, os.path.splitext(os.path.basename(out_path))[0])
    format_str = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["Best available"])

    ydl_opts = {
        "format": format_str,
        "merge_output_format": "mp4",
        "outtmpl": out_template + ".%(ext)s",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": False,
        "progress_hooks": [_make_progress_hook(log, label, on_progress=on_progress)],
        "logger": _YtDlpLogger(log),
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    ydl_opts.update(_build_cookie_and_client_opts(cookies_browser, cookies_profile, cookies_file))

    ffmpeg_dir = os.path.dirname(FFMPEG_BIN) if os.path.isabs(FFMPEG_BIN) else None
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    log(f"  Connecting to source for {label.lower()}...")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in to confirm your age" in msg or "cookies" in msg.lower():
            raise RuntimeError(
                f"Failed to download section {start}-{end}: this video needs you "
                f"to be signed in (age-restricted, private, or members-only).\n"
                f"Fix: use the 'cookies.txt file' option, exported with a browser "
                f"extension while logged in.\n\nDetails: {e}"
            )
        if "could not find" in msg.lower() and "cookies" in msg.lower():
            raise RuntimeError(
                f"Couldn't read cookies from the selected browser. Common fixes:\n"
                f" - Fully close that browser (including background processes)\n"
                f" - Make sure 'pip install pycryptodomex' has been run\n"
                f" - If you use a non-default profile, set the profile name\n\n"
                f"Details: {e}"
            )
        raise RuntimeError(f"Failed to download section {start}-{end} from URL:\n{e}")

    produced = out_template + ".mp4"
    if not os.path.isfile(produced):
        for fname in os.listdir(out_dir):
            if fname.startswith(os.path.basename(out_template) + "."):
                produced = os.path.join(out_dir, fname)
                break

    if not os.path.isfile(produced):
        raise RuntimeError(f"yt-dlp did not produce an output file for segment {start}-{end}.")

    if produced != out_path:
        os.replace(produced, out_path)


def fetch_url_title(url, log, cookies_browser=None, cookies_profile=None, cookies_file=None):
    """Quick metadata-only fetch, used just to suggest a filename before
    downloading. Never raises - returns None on any failure so the caller
    can fall back to a generic name."""
    if not YT_DLP_AVAILABLE:
        return None
    ydl_opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "skip_download": True, "logger": _YtDlpLogger(lambda m: None),
        "socket_timeout": 10, "retries": 1,
    }
    ydl_opts.update(_build_cookie_and_client_opts(cookies_browser, cookies_profile, cookies_file))
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("title")
    except Exception:
        return None


def check_url(url, log, cookies_browser=None, cookies_profile=None, cookies_file=None):
    """Fetch metadata only (no download) to verify a URL is supported and
    accessible before the user commits to typing timestamps."""
    if not YT_DLP_AVAILABLE:
        raise RuntimeError(
            "yt-dlp is not installed. Install it with:\n    pip install yt-dlp"
        )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "logger": _YtDlpLogger(log),
        "socket_timeout": 20,
        "retries": 3,
    }
    ydl_opts.update(_build_cookie_and_client_opts(cookies_browser, cookies_profile, cookies_file))

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in to confirm your age" in msg or "cookies" in msg.lower():
            raise RuntimeError(
                f"This video needs you to be signed in (age-restricted, private, "
                f"or members-only). Try the 'cookies.txt file' option instead of "
                f"'cookies from browser'.\n\nDetails: {e}"
            )
        if "could not find" in msg.lower() and "cookies" in msg.lower():
            raise RuntimeError(
                f"Couldn't read cookies from the selected browser. Use the "
                f"'cookies.txt file' option instead.\n\nDetails: {e}"
            )
        raise RuntimeError(f"Could not read this URL - the site may not be "
                            f"supported, or the link is invalid.\n\nDetails: {e}")

    duration = info.get("duration")
    return {
        "title": info.get("title") or "(untitled)",
        "uploader": info.get("uploader") or "(unknown uploader)",
        "duration": seconds_to_timestamp(duration) if duration else "(unknown)",
        "extractor": info.get("extractor_key") or info.get("extractor") or "(unknown site)",
    }


class _YtDlpLogger:
    """Routes yt-dlp's internal log messages into our GUI log box."""
    def __init__(self, log_fn):
        self.log_fn = log_fn

    def debug(self, msg):
        if msg.startswith("[download]") or "Destination" in msg:
            self.log_fn(f"  {msg}")

    def info(self, msg):
        self.log_fn(f"  {msg}")

    def warning(self, msg):
        self.log_fn(f"  WARNING: {msg}")

    def error(self, msg):
        self.log_fn(f"  ERROR: {msg}")


def join_clips(clip_paths, output_path, concat_list_path):
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for clip_path in clip_paths:
            safe_path = clip_path.replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while joining clips.\n{result.stderr}")


def sanitize_filename(name, fallback="clip"):
    """Strip characters Windows won't allow in a filename."""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.strip(" .")
    if not name:
        name = fallback
    return name[:120]  # keep it reasonable


def _finish_join(clip_paths, output_path, temp_dir, log):
    if len(clip_paths) == 1:
        log("Only one segment - using it directly.")
        if os.path.dirname(clip_paths[0]) != os.path.dirname(output_path) or clip_paths[0] != output_path:
            shutil.copy2(clip_paths[0], output_path)
    else:
        log("Joining clips...")
        concat_list_path = os.path.join(temp_dir, "concat_list.txt")
        join_clips(clip_paths, output_path, concat_list_path)


# ----------------------------------------------------------------------
# Export options: aspect presets, audio normalization, crossfade,
# side-exports (WebM/MP3), thumbnail/GIF
# ----------------------------------------------------------------------

# Scale-then-crop is used instead of a plain crop so this works regardless
# of the source's original aspect ratio (portrait, landscape, or square).
ASPECT_PRESETS = {
    "None (keep original)": None,
    "TikTok / Shorts / Reels (9:16)": "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "YouTube (16:9)": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
    "Instagram Square (1:1)": "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
}

LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _run_ffmpeg_with_progress(cmd, total_duration, log, error_context, on_progress=None):
    """Runs an ffmpeg command that includes '-progress pipe:1', streaming
    live fractional progress (0.0-1.0) via on_progress. Shared by every
    re-encoding step (filters, crossfade, side-exports) so they all get
    the same smooth progress behavior as the main clip cutting."""
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    stderr_lines = []

    def _drain_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    for line in process.stdout:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key == "out_time_ms" and total_duration:
            try:
                seconds_done = int(value) / 1_000_000
                if on_progress:
                    on_progress(min(1.0, max(0.0, seconds_done / total_duration)))
            except ValueError:
                pass

    process.wait()
    stderr_thread.join(timeout=2)

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while {error_context}.\n{''.join(stderr_lines)}")
    if on_progress:
        on_progress(1.0)


def apply_post_filters(clip_path, vf, af, log, on_progress=None):
    """Re-encodes a single clip in place with the given video/audio
    filters (aspect preset crop/scale and/or loudness normalization).
    No-op if neither filter is requested."""
    if not vf and not af:
        return
    duration = get_video_duration(clip_path) or 0
    tmp_path = clip_path + ".filtered.mp4"
    cmd = [FFMPEG_BIN, "-y", "-i", clip_path]
    if vf:
        cmd += ["-vf", vf]
    if af:
        cmd += ["-af", af]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        tmp_path,
    ]
    _run_ffmpeg_with_progress(cmd, duration, log, "applying export filters", on_progress)
    os.replace(tmp_path, clip_path)


def crossfade_join(clip_paths, output_path, transition_duration, log, on_progress=None):
    """Joins clips with a video crossfade + audio crossfade between each
    consecutive pair, instead of a hard cut. Requires re-encoding (can't
    be done with stream copy). Falls back to a plain hard-cut join if
    there are too many clips for a reasonable filter chain."""
    if len(clip_paths) < 2:
        _finish_join(clip_paths, output_path, os.path.dirname(output_path), log)
        if on_progress:
            on_progress(1.0)
        return

    if len(clip_paths) > 8:
        log("WARNING: too many clips for a crossfade chain - using a plain join instead.")
        concat_list_path = output_path + ".concat.txt"
        join_clips(clip_paths, output_path, concat_list_path)
        os.remove(concat_list_path)
        if on_progress:
            on_progress(1.0)
        return

    durations = [get_video_duration(p) or 0.0 for p in clip_paths]
    d = transition_duration
    if any(dur <= d for dur in durations):
        log(f"WARNING: a clip is shorter than the {d}s crossfade duration - "
            f"using a plain join instead to avoid a broken transition.")
        concat_list_path = output_path + ".concat.txt"
        join_clips(clip_paths, output_path, concat_list_path)
        os.remove(concat_list_path)
        if on_progress:
            on_progress(1.0)
        return

    cmd = [FFMPEG_BIN, "-y"]
    for p in clip_paths:
        cmd += ["-i", p]

    filter_parts = []
    v_label = "0:v"
    a_label = "0:a"
    cumulative = durations[0]
    for i in range(1, len(clip_paths)):
        offset = cumulative - d
        next_v = f"v{i}"
        next_a = f"a{i}"
        filter_parts.append(
            f"[{v_label}][{i}:v]xfade=transition=fade:duration={d}:offset={offset:.3f}[{next_v}]"
        )
        filter_parts.append(
            f"[{a_label}][{i}:a]acrossfade=d={d}[{next_a}]"
        )
        v_label, a_label = next_v, next_a
        cumulative += durations[i] - d

    filter_complex = ";".join(filter_parts)
    total_out_duration = cumulative

    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{v_label}]", "-map", f"[{a_label}]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        output_path,
    ]
    log(f"Joining clips with {d}s crossfade transitions...")
    _run_ffmpeg_with_progress(cmd, total_out_duration, log, "crossfading clips", on_progress)


def export_webm(input_path, out_path, log, on_progress=None):
    duration = get_video_duration(input_path) or 0
    cmd = [
        FFMPEG_BIN, "-y", "-i", input_path,
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-c:a", "libopus", "-b:a", "128k",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        out_path,
    ]
    log("Exporting WebM version...")
    _run_ffmpeg_with_progress(cmd, duration, log, "exporting WebM", on_progress)


def export_mp3(input_path, out_path, log):
    cmd = [
        FFMPEG_BIN, "-y", "-i", input_path,
        "-vn", "-c:a", "libmp3lame", "-q:a", "2",
        "-loglevel", "error",
        out_path,
    ]
    log("Exporting audio-only MP3...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while exporting MP3.\n{result.stderr}")


def generate_thumbnail(input_path, timestamp_seconds, out_path, log):
    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", seconds_to_timestamp(timestamp_seconds),
        "-i", input_path,
        "-frames:v", "1", "-q:v", "2",
        "-loglevel", "error",
        out_path,
    ]
    log(f"Saving thumbnail at {seconds_to_timestamp(timestamp_seconds)}...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while saving thumbnail.\n{result.stderr}")


def generate_gif(input_path, start_seconds, gif_duration, out_path, log):
    cmd = [
        FFMPEG_BIN, "-y",
        "-ss", seconds_to_timestamp(start_seconds),
        "-t", f"{gif_duration:.3f}",
        "-i", input_path,
        "-vf", "fps=12,scale=480:-1:flags=lanczos",
        "-loop", "0",
        "-loglevel", "error",
        out_path,
    ]
    log(f"Saving GIF ({gif_duration:.1f}s from {seconds_to_timestamp(start_seconds)})...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while saving GIF.\n{result.stderr}")


def _apply_export_extras(final_path, work_dir, export_options, log, progress, stage_base, stage_span):
    """Handles the optional side-exports (WebM, MP3, thumbnail, GIF) that
    run against the finished joined video. Returns nothing - files are
    written alongside output.mp4 in work_dir, and the GUI's save step
    later renames/moves them together with the main output."""
    tasks = []
    if export_options.get("export_webm"):
        tasks.append("webm")
    if export_options.get("export_mp3"):
        tasks.append("mp3")
    if export_options.get("thumbnail_time") is not None:
        tasks.append("thumbnail")
    if export_options.get("gif_start") is not None:
        tasks.append("gif")

    if not tasks:
        progress(stage_base + stage_span)
        return

    per_task = stage_span / len(tasks)
    done = 0

    if "webm" in tasks:
        def on_p(frac):
            progress(stage_base + per_task * done + per_task * frac)
        export_webm(final_path, os.path.join(work_dir, "output.webm"), log, on_progress=on_p)
        done += 1
        progress(stage_base + per_task * done)

    if "mp3" in tasks:
        export_mp3(final_path, os.path.join(work_dir, "output.mp3"), log)
        done += 1
        progress(stage_base + per_task * done)

    if "thumbnail" in tasks:
        generate_thumbnail(final_path, export_options["thumbnail_time"],
                            os.path.join(work_dir, "output_thumb.jpg"), log)
        done += 1
        progress(stage_base + per_task * done)

    if "gif" in tasks:
        generate_gif(final_path, export_options["gif_start"],
                     export_options.get("gif_duration", 3.0),
                     os.path.join(work_dir, "output.gif"), log)
        done += 1
        progress(stage_base + per_task * done)


def run_pipeline_from_file(input_path, segments, reencode, log, progress, work_dir,
                            keep_clips=False, export_options=None):
    """Clips + joins a local file. Writes the final result to
    work_dir/output.mp4 and returns that path - the GUI decides where it
    ultimately gets saved (and under what name) afterwards.

    `progress` is called with a single float fraction (0.0-1.0), weighted
    by each segment's actual duration so a 2-minute clip advances the bar
    more than a 5-second one - and driven by ffmpeg's own live progress
    output, not just "segment started / segment finished".

    `export_options` (dict, all keys optional):
        aspect_preset_filter: ffmpeg -vf string or None
        normalize_audio: bool
        crossfade_duration: float seconds, or None/0 to disable
        export_webm / export_mp3: bool
        thumbnail_time / gif_start / gif_duration: float seconds or None

    If keep_clips is True, the individual clip files are preserved in
    work_dir/clips instead of being deleted, so the caller can move that
    folder alongside the final saved file."""
    export_options = export_options or {}
    duration = get_video_duration(input_path)
    if duration is not None:
        for start, end in segments:
            if start > duration or end > duration + 0.5:
                log(f"WARNING: segment {start:.2f}-{end:.2f}s extends beyond the "
                    f"video's duration ({duration:.2f}s).")

    final_path = os.path.join(work_dir, "output.mp4")
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    needs_filter_pass = bool(export_options.get("aspect_preset_filter") or export_options.get("normalize_audio"))
    crossfade_d = export_options.get("crossfade_duration") or 0
    use_crossfade = crossfade_d > 0 and len(segments) > 1

    segments_share = 0.55
    filter_share = 0.15 if needs_filter_pass else 0.0
    join_share = 0.20 if use_crossfade else 0.05
    extras_share = max(0.0, 1.0 - segments_share - filter_share - join_share)

    total_dur = sum(max(end - start, 0.001) for start, end in segments) or 1.0
    clip_paths = []
    log(f"Found {len(segments)} segment(s) to cut.")
    cum_weight = 0.0

    for idx, (start, end) in enumerate(segments, start=1):
        seg_dur = max(end - start, 0.001)
        seg_weight = seg_dur / total_dur
        base = cum_weight

        def on_seg_progress(frac, base=base, seg_weight=seg_weight):
            progress(segments_share * (base + seg_weight * frac))

        ext = os.path.splitext(input_path)[1] or ".mp4"
        clip_path = os.path.join(clips_dir, f"clip_{idx:03d}{ext}")
        log(f"[{idx}/{len(segments)}] Cutting {seconds_to_timestamp(start)} "
            f"-> {seconds_to_timestamp(end)} ...")
        cut_clip(input_path, start, end, clip_path, reencode, on_progress=on_seg_progress)
        clip_paths.append(clip_path)
        cum_weight += seg_weight
        progress(segments_share * cum_weight)

    if needs_filter_pass:
        log("Applying export filters (aspect crop/audio normalize)...")
        vf = export_options.get("aspect_preset_filter")
        af = LOUDNORM_FILTER if export_options.get("normalize_audio") else None
        per_clip_share = filter_share / len(clip_paths)
        for i, cp in enumerate(clip_paths):
            def on_p(frac, i=i):
                progress(segments_share + per_clip_share * i + per_clip_share * frac)
            apply_post_filters(cp, vf, af, log, on_progress=on_p)
        progress(segments_share + filter_share)

    join_base = segments_share + filter_share
    if use_crossfade:
        def on_join_p(frac):
            progress(join_base + join_share * frac)
        crossfade_join(clip_paths, final_path, crossfade_d, log, on_progress=on_join_p)
    else:
        if len(segments) > 1:
            log("Joining clips...")
        _finish_join(clip_paths, final_path, clips_dir, log)
        progress(join_base + join_share)

    _apply_export_extras(final_path, work_dir, export_options, log, progress,
                          join_base + join_share, extras_share)
    progress(1.0)

    if not keep_clips:
        shutil.rmtree(clips_dir, ignore_errors=True)

    log("Done!")
    return final_path


def run_pipeline_from_url(url, segments, log, progress, work_dir,
                           cookies_browser=None, cookies_profile=None, cookies_file=None,
                           quality="Best available", auto_pot_server=True,
                           pot_server_dir=None, keep_clips=False, export_options=None):
    """Downloads only the timestamped sections + joins them. Writes the
    final result to work_dir/output.mp4 and returns that path.

    `progress` is called with a single float fraction (0.0-1.0), weighted
    by each segment's actual duration and driven by yt-dlp's real
    downloaded-bytes progress. See run_pipeline_from_file for the shape
    of `export_options`.

    If keep_clips is True, the individual downloaded clips are preserved
    in work_dir/clips instead of being deleted."""
    export_options = export_options or {}
    if auto_pot_server:
        progress(0.01)
        ensure_pot_server(pot_server_dir, log)

    final_path = os.path.join(work_dir, "output.mp4")
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    needs_filter_pass = bool(export_options.get("aspect_preset_filter") or export_options.get("normalize_audio"))
    crossfade_d = export_options.get("crossfade_duration") or 0
    use_crossfade = crossfade_d > 0 and len(segments) > 1

    segments_share = 0.55
    filter_share = 0.15 if needs_filter_pass else 0.0
    join_share = 0.20 if use_crossfade else 0.05
    extras_share = max(0.0, 1.0 - segments_share - filter_share - join_share)

    total_dur = sum(max(end - start, 0.001) for start, end in segments) or 1.0
    clip_paths = []
    log(f"Found {len(segments)} segment(s) to download directly from the URL.")
    cum_weight = 0.0

    for idx, (start, end) in enumerate(segments, start=1):
        seg_dur = max(end - start, 0.001)
        seg_weight = seg_dur / total_dur
        base = cum_weight

        def on_seg_progress(frac, base=base, seg_weight=seg_weight):
            progress(segments_share * (base + seg_weight * frac))

        clip_path = os.path.join(clips_dir, f"clip_{idx:03d}.mp4")
        label = f"[{idx}/{len(segments)}] {seconds_to_timestamp(start)} -> {seconds_to_timestamp(end)}"
        log(f"{label} ...")
        download_segment(
            url, start, end, clip_path, log,
            cookies_browser=cookies_browser, cookies_profile=cookies_profile,
            cookies_file=cookies_file, label=label, quality=quality,
            on_progress=on_seg_progress,
        )
        clip_paths.append(clip_path)
        cum_weight += seg_weight
        progress(segments_share * cum_weight)

    if needs_filter_pass:
        log("Applying export filters (aspect crop/audio normalize)...")
        vf = export_options.get("aspect_preset_filter")
        af = LOUDNORM_FILTER if export_options.get("normalize_audio") else None
        per_clip_share = filter_share / len(clip_paths)
        for i, cp in enumerate(clip_paths):
            def on_p(frac, i=i):
                progress(segments_share + per_clip_share * i + per_clip_share * frac)
            apply_post_filters(cp, vf, af, log, on_progress=on_p)
        progress(segments_share + filter_share)

    join_base = segments_share + filter_share
    if use_crossfade:
        def on_join_p(frac):
            progress(join_base + join_share * frac)
        crossfade_join(clip_paths, final_path, crossfade_d, log, on_progress=on_join_p)
    else:
        if len(segments) > 1:
            log("Joining clips...")
        _finish_join(clip_paths, final_path, clips_dir, log)
        progress(join_base + join_share)

    _apply_export_extras(final_path, work_dir, export_options, log, progress,
                          join_base + join_share, extras_share)
    progress(1.0)

    if not keep_clips:
        shutil.rmtree(clips_dir, ignore_errors=True)

    log("Done!")
    return final_path



# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

ACCENT = "#2f6fed"
ACCENT_DARK = "#2555bd"
BG = "#f5f6f8"
CARD_BG = "#ffffff"
BORDER = "#d8dce3"
TEXT_MUTED = "#6b7280"
DROP_ZONE_IDLE_BG = "#fafbfc"
DROP_ZONE_HOVER_BG = "#eef4ff"

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv")

FUN_STATUS_WORDS = [
    "Computing...",
    "Crunching frames...",
    "Thinking...",
    "Stitching clips...",
    "Doing video math...",
    "Working the ffmpeg magic...",
    "Buffering brilliance...",
    "Encoding excellence...",
    "Assembling your video...",
    "Polishing pixels...",
    "Untangling timestamps...",
    "Warming up the encoder...",
    "Almost there...",
]


def _make_root():
    if DND_AVAILABLE:
        return TkinterDnD.Tk()
    return tk.Tk()


class VideoClipperApp:
    def __init__(self, root):
        self.root = root
        root.title("ClipStitch")
        root.geometry("760x780")
        root.minsize(640, 560)
        root.configure(bg=BG)

        self._setup_styles()

        self.source_mode = tk.StringVar(value="file")
        self.input_path = tk.StringVar()
        self.url_value = tk.StringVar()
        self.reencode_var = tk.BooleanVar(value=False)
        self.keep_temp_var = tk.BooleanVar(value=False)
        self.cookies_browser = tk.StringVar(value="None")
        self.cookies_profile = tk.StringVar(value="")
        self.cookies_file = tk.StringVar(value="")
        self.quality_var = tk.StringVar(value="Best available")
        self.auto_pot_var = tk.BooleanVar(value=True)
        self.pot_server_dir = tk.StringVar(value=DEFAULT_POT_SERVER_DIR)
        self.advanced_visible = tk.BooleanVar(value=False)

        self.is_running = False
        self._pending_final_path = None
        self._pending_work_dir = None
        self._pending_suggested_name = "clip"

        self._build_layout()
        self._update_source_mode()

        if not check_ffmpeg_available():
            self.log(
                "WARNING: ffmpeg/ffprobe not found. Place ffmpeg.exe/ffprobe.exe next to "
                "this program, or install from https://ffmpeg.org/download.html."
            )
        if not YT_DLP_AVAILABLE:
            self.log("NOTE: yt-dlp is not installed, so URL mode won't work yet. "
                      "Install it with: pip install yt-dlp")
        if not DND_AVAILABLE:
            self.log("NOTE: drag & drop is unavailable (tkinterdnd2 not installed) - "
                      "click the drop zone to browse instead. Install with: "
                      "pip install tkinterdnd2")

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------
    def _setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("TLabel", background=BG, foreground="#1f2430", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD_BG, foreground="#1f2430", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=TEXT_MUTED, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=CARD_BG, foreground=TEXT_MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=BG, foreground="#12151c",
                         font=("Segoe UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=TEXT_MUTED,
                         font=("Segoe UI", 10))
        style.configure("SectionHeader.TLabel", background=BG, foreground="#12151c",
                         font=("Segoe UI", 11, "bold"))

        style.configure("Segmented.TRadiobutton", font=("Segoe UI", 10), padding=(16, 8))
        style.map("Segmented.TRadiobutton",
                  background=[("selected", ACCENT), ("!selected", CARD_BG)],
                  foreground=[("selected", "#ffffff"), ("!selected", "#1f2430")])

        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"),
                         padding=(18, 10), foreground="#ffffff", background=ACCENT)
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#a9bdf0")])

        style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(10, 5))

        style.configure("Advanced.TButton", font=("Segoe UI", 9), padding=(4, 4),
                         background=BG, borderwidth=0)

        style.configure("Card.TCheckbutton", background=CARD_BG, font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=BG, font=("Segoe UI", 9))

        style.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb",
                         background=ACCENT, thickness=10)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, bg=BG)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        main = ttk.Frame(canvas, style="TFrame", padding=(24, 20))
        main_window = canvas.create_window((0, 0), window=main, anchor="nw")

        def _on_main_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(main_window, width=event.width)

        main.bind("<Configure>", _on_main_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # --- Header ---
        header = ttk.Frame(main, style="TFrame")
        header.pack(fill="x", pady=(0, 18))
        ttk.Label(header, text="ClipStitch", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Clip. Stitch. Done.", style="Subtitle.TLabel").pack(anchor="w")

        # --- Source toggle ---
        toggle_row = ttk.Frame(main, style="TFrame")
        toggle_row.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(
            toggle_row, text="\U0001F4C1  Local file", variable=self.source_mode, value="file",
            style="Segmented.TRadiobutton", command=self._update_source_mode,
        ).pack(side="left")
        ttk.Radiobutton(
            toggle_row, text="\U0001F310  From URL", variable=self.source_mode, value="url",
            style="Segmented.TRadiobutton", command=self._update_source_mode,
        ).pack(side="left", padx=(6, 0))

        # --- Drop zone (local file mode) ---
        self.drop_zone = tk.Frame(main, bg=DROP_ZONE_IDLE_BG, highlightbackground=BORDER,
                                   highlightthickness=2, bd=0, cursor="hand2")
        self.drop_zone_label = tk.Label(
            self.drop_zone, text="\U0001F4E5  Drag & drop a video here\nor click to browse",
            bg=DROP_ZONE_IDLE_BG, fg=TEXT_MUTED, font=("Segoe UI", 11), justify="center",
        )
        self.drop_zone_label.pack(expand=True, fill="both", pady=28)
        self.drop_zone.pack(fill="x", pady=(0, 6))
        for widget in (self.drop_zone, self.drop_zone_label):
            widget.bind("<Button-1>", lambda e: self.browse_input())
        if DND_AVAILABLE:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop_file)
            self.drop_zone.dnd_bind("<<DropEnter>>", lambda e: self._set_drop_zone_hover(True))
            self.drop_zone.dnd_bind("<<DropLeave>>", lambda e: self._set_drop_zone_hover(False))

        self.selected_file_label = ttk.Label(main, text="", style="Muted.TLabel")
        self.selected_file_label.pack(fill="x", pady=(0, 10))

        # --- URL input (url mode) ---
        self.url_frame = ttk.Frame(main, style="TFrame")
        url_input_row = ttk.Frame(self.url_frame, style="TFrame")
        url_input_row.pack(fill="x")
        self.url_entry = ttk.Entry(url_input_row, textvariable=self.url_value, font=("Segoe UI", 10))
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))
        ttk.Button(url_input_row, text="Check", style="Secondary.TButton",
                   command=self.on_check_url).pack(side="left")
        ttk.Label(self.url_frame, text="YouTube and 1000+ other sites are supported.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 10))

        # --- Timestamps ---
        ttk.Label(main, text="Timestamps", style="SectionHeader.TLabel").pack(anchor="w", pady=(4, 2))
        ttk.Label(main, text="One clip per line: START,END  (e.g. 00:00:10,00:00:45). "
                              "You can also drag a .txt file onto the drop zone above.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

        ts_frame = tk.Frame(main, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        ts_frame.pack(fill="x", pady=(0, 4))
        self.timestamps_box = tk.Text(ts_frame, height=6, wrap="none", bd=0, padx=10, pady=8,
                                       font=("Consolas", 10), fg="#1f2430", bg=CARD_BG,
                                       insertbackground="#1f2430")
        self.timestamps_box.pack(fill="both", expand=True)
        self._placeholder_text = "00:00:10,00:00:45\n00:02:00,00:03:15"
        self._show_placeholder()
        self.timestamps_box.bind("<FocusIn>", self._clear_placeholder)

        ts_actions = ttk.Frame(main, style="TFrame")
        ts_actions.pack(fill="x", pady=(4, 16))
        ttk.Button(ts_actions, text="Load from .txt file...", style="Secondary.TButton",
                   command=self.load_timestamps_file).pack(side="right")

        # --- Advanced (collapsible) ---
        adv_toggle_row = ttk.Frame(main, style="TFrame")
        adv_toggle_row.pack(fill="x")
        self.advanced_toggle_btn = ttk.Button(
            adv_toggle_row, text="\u25B8  Advanced options", style="Advanced.TButton",
            command=self._toggle_advanced,
        )
        self.advanced_toggle_btn.pack(anchor="w")

        self.advanced_frame = tk.Frame(main, bg=CARD_BG, highlightbackground=BORDER,
                                        highlightthickness=1)
        self._build_advanced_contents(self.advanced_frame)
        # not packed yet - toggled on demand

        # --- Run button + progress ---
        run_row = ttk.Frame(main, style="TFrame")
        run_row.pack(fill="x", pady=(18, 2))
        self.run_button = ttk.Button(run_row, text="\u2702  Clip It", style="Accent.TButton",
                                      command=self.on_run)
        self.run_button.pack(side="left")
        self.progress_bar = ttk.Progressbar(run_row, mode="determinate", maximum=100,
                                             style="Horizontal.TProgressbar")
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(14, 0), ipady=2)

        self.progress_label = ttk.Label(main, text="", style="Muted.TLabel")
        self.progress_label.pack(anchor="w", pady=(4, 6))

        # --- Log (collapsible-ish, visible by default but compact) ---
        ttk.Label(main, text="Activity log", style="Muted.TLabel").pack(anchor="w", pady=(14, 2))
        log_frame = tk.Frame(main, bg="#12151c")
        log_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=9, state="disabled", bd=0, bg="#12151c", fg="#d7dae0",
            insertbackground="#d7dae0", font=("Consolas", 9), padx=10, pady=8,
        )
        self.log_box.pack(fill="both", expand=True)

    def _build_advanced_contents(self, parent):
        pad = {"padx": 14, "pady": 6}

        ttk.Label(parent, text="These only matter for URL downloads - safe to ignore for local files.",
                  style="CardMuted.TLabel").pack(anchor="w", padx=14, pady=(12, 6))

        # Quality
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Quality", style="Card.TLabel", width=16).pack(side="left")
        ttk.Combobox(row, textvariable=self.quality_var, state="readonly", width=18,
                     values=list(QUALITY_PRESETS.keys())).pack(side="left")

        # Cookies from browser
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Cookies from browser", style="Card.TLabel", width=16).pack(side="left")
        ttk.Combobox(row, textvariable=self.cookies_browser, state="readonly", width=12,
                     values=["None", "Chrome", "Firefox", "Edge", "Brave", "Opera", "Vivaldi"]
                     ).pack(side="left")
        ttk.Label(row, text="Profile:", style="Card.TLabel").pack(side="left", padx=(12, 4))
        ttk.Entry(row, textvariable=self.cookies_profile, width=12).pack(side="left")

        # Cookies file
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", **pad)
        ttk.Label(row, text="cookies.txt (recommended)", style="Card.TLabel", width=22).pack(side="left")
        ttk.Entry(row, textvariable=self.cookies_file).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row, text="Browse", style="Secondary.TButton",
                   command=self.browse_cookies_file).pack(side="left")

        # PO token server
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="Auto-start PO Token server (best quality on restricted videos)",
                         variable=self.auto_pot_var, style="Card.TCheckbutton").pack(side="left")

        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=14, pady=(0, 6))
        ttk.Label(row, text="Server folder:", style="Card.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.pot_server_dir).pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(row, text="Browse", style="Secondary.TButton",
                   command=self.browse_pot_server_dir).pack(side="left")

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=14, pady=8)

        # Local-file specific
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", **pad)
        ttk.Checkbutton(row, text="Frame-accurate cuts (re-encode, slower, exact timing)",
                         variable=self.reencode_var, style="Card.TCheckbutton").pack(side="left")

        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", padx=14, pady=(0, 14))
        ttk.Checkbutton(row, text="Keep individual clips (not just the joined result)",
                         variable=self.keep_temp_var, style="Card.TCheckbutton").pack(side="left")

        note = ttk.Label(
            parent,
            text="Only clip videos you own or have rights/permission to use.",
            style="CardMuted.TLabel", wraplength=640, justify="left",
        )
        note.pack(anchor="w", padx=14, pady=(0, 12))

    def _toggle_advanced(self):
        visible = not self.advanced_visible.get()
        self.advanced_visible.set(visible)
        if visible:
            self.advanced_toggle_btn.configure(text="\u25BE  Advanced options")
            self.advanced_frame.pack(fill="x", pady=(6, 0), before=self._run_row_anchor())
        else:
            self.advanced_toggle_btn.configure(text="\u25B8  Advanced options")
            self.advanced_frame.pack_forget()

    def _run_row_anchor(self):
        # the run_button's parent frame is packed right after advanced - find it
        return self.run_button.master

    # ------------------------------------------------------------------
    # Source mode / drop zone
    # ------------------------------------------------------------------
    def _update_source_mode(self):
        mode = self.source_mode.get()
        if mode == "file":
            self.url_frame.pack_forget()
            self.drop_zone.pack(fill="x", pady=(0, 6), before=self.selected_file_label)
            self.reencode_var_state = "normal"
        else:
            self.drop_zone.pack_forget()
            self.url_frame.pack(fill="x", pady=(0, 6), before=self.selected_file_label)

    def _set_drop_zone_hover(self, hovering):
        bg = DROP_ZONE_HOVER_BG if hovering else DROP_ZONE_IDLE_BG
        self.drop_zone.configure(bg=bg)
        self.drop_zone_label.configure(bg=bg)

    def _on_drop_file(self, event):
        self._set_drop_zone_hover(False)
        raw = event.data
        paths = self.root.tk.splitlist(raw)
        if not paths:
            return
        path = paths[0]
        ext = os.path.splitext(path)[1].lower()
        if ext == ".txt":
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.timestamps_box.delete("1.0", "end")
                self.timestamps_box.configure(fg="#1f2430")
                self.timestamps_box.insert("1.0", content)
            except OSError as e:
                messagebox.showerror("Error", f"Couldn't read that file:\n{e}")
        elif ext in VIDEO_EXTENSIONS:
            self.source_mode.set("file")
            self._update_source_mode()
            self._set_input_file(path)
        else:
            messagebox.showwarning(
                "Unrecognized file",
                f"'{os.path.basename(path)}' doesn't look like a video or .txt "
                f"timestamps file. Drop a video file, or a .txt with timestamps.",
            )

    def _set_input_file(self, path):
        self.input_path.set(path)
        name = os.path.basename(path)
        self.selected_file_label.configure(text=f"\u2705  Selected: {name}")
        self.drop_zone_label.configure(text=f"\U0001F4E5  {name}\n(drop another to replace)")

    # ------------------------------------------------------------------
    # Placeholder handling for timestamps box
    # ------------------------------------------------------------------
    def _show_placeholder(self):
        self.timestamps_box.delete("1.0", "end")
        self.timestamps_box.insert("1.0", self._placeholder_text)
        self.timestamps_box.configure(fg=TEXT_MUTED)
        self._placeholder_active = True

    def _clear_placeholder(self, event=None):
        if getattr(self, "_placeholder_active", False):
            self.timestamps_box.delete("1.0", "end")
            self.timestamps_box.configure(fg="#1f2430")
            self._placeholder_active = False

    def _get_timestamps_text(self):
        if getattr(self, "_placeholder_active", False):
            return ""
        return self.timestamps_box.get("1.0", "end")

    # ------------------------------------------------------------------
    # File pickers
    # ------------------------------------------------------------------
    def browse_input(self):
        path = filedialog.askopenfilename(
            title="Select input video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._set_input_file(path)

    def browse_cookies_file(self):
        path = filedialog.askopenfilename(
            title="Select cookies.txt file",
            filetypes=[("Text/cookies files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.cookies_file.set(path)

    def browse_pot_server_dir(self):
        path = filedialog.askdirectory(title="Select PO Token server folder")
        if path:
            self.pot_server_dir.set(path)

    def load_timestamps_file(self):
        path = filedialog.askopenfilename(
            title="Select timestamps file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.timestamps_box.delete("1.0", "end")
            self.timestamps_box.configure(fg="#1f2430")
            self.timestamps_box.insert("1.0", content)
            self._placeholder_active = False

    # ------------------------------------------------------------------
    # Logging / progress
    # ------------------------------------------------------------------
    def log(self, message):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _append)

    def set_progress(self, frac):
        """frac is 0.0-1.0. Called frequently (many times a second) by
        ffmpeg/yt-dlp's own live progress output, so the bar advances
        smoothly instead of jumping only when a whole segment finishes."""
        frac = max(0.0, min(1.0, frac))

        def _update():
            self._progress_frac = frac
            self.progress_bar["value"] = frac * 100
            self._refresh_progress_label()
        self.root.after(0, _update)

    def _start_progress_tracking(self):
        self._progress_frac = 0.0
        self._progress_start_time = time.time()
        self._progress_eta_smoothed = None
        self._fun_word_index = 0
        self._fun_word_tick = 0
        self._progress_tick_job = None
        self._tick_progress()

    def _stop_progress_tracking(self):
        if self._progress_tick_job is not None:
            self.root.after_cancel(self._progress_tick_job)
            self._progress_tick_job = None

    def _tick_progress(self):
        if not self.is_running:
            self._progress_tick_job = None
            return
        self._fun_word_tick += 1
        if self._fun_word_tick % 3 == 0:  # roughly every ~1.5s
            self._fun_word_index = (self._fun_word_index + 1) % len(FUN_STATUS_WORDS)
        self._refresh_progress_label()
        self._progress_tick_job = self.root.after(500, self._tick_progress)

    def _refresh_progress_label(self):
        frac = getattr(self, "_progress_frac", 0.0)
        pct = int(frac * 100)
        word = FUN_STATUS_WORDS[getattr(self, "_fun_word_index", 0)]

        if not self.is_running:
            self.progress_label.configure(text="")
            return

        eta_text = "estimating time..."
        if frac > 0.03:
            elapsed = time.time() - getattr(self, "_progress_start_time", time.time())
            raw_eta = elapsed * (1 - frac) / frac
            smoothed = getattr(self, "_progress_eta_smoothed", None)
            eta = raw_eta if smoothed is None else (0.7 * smoothed + 0.3 * raw_eta)
            self._progress_eta_smoothed = eta
            eta_text = self._format_eta(eta)

        self.progress_label.configure(text=f"{pct}%  \u2022  {eta_text}  \u2022  {word}")

    @staticmethod
    def _format_eta(seconds):
        seconds = max(0, seconds)
        if seconds < 2:
            return "almost done"
        if seconds < 60:
            return f"~{int(seconds)}s remaining"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"~{minutes}m {secs}s remaining"

    def set_running(self, running):
        self.is_running = running
        self.run_button.configure(state="disabled" if running else "normal")
        if running:
            self._start_progress_tracking()
        else:
            self._stop_progress_tracking()
            self.progress_label.configure(text="")

    # ------------------------------------------------------------------
    # Check URL
    # ------------------------------------------------------------------
    def on_check_url(self):
        if self.is_running:
            return
        url = self.url_value.get().strip()
        if not url:
            messagebox.showerror("Error", "Paste a video URL first.")
            return
        if not YT_DLP_AVAILABLE:
            messagebox.showerror("yt-dlp not installed",
                                  "Install it with:\n    pip install yt-dlp")
            return

        cookies_browser = None if self.cookies_browser.get() == "None" else self.cookies_browser.get()
        cookies_profile = self.cookies_profile.get().strip() or None
        cookies_file = self.cookies_file.get().strip() or None

        self.log(f"Checking URL: {url} ...")

        def worker():
            try:
                info = check_url(url, log=self.log, cookies_browser=cookies_browser,
                                  cookies_profile=cookies_profile, cookies_file=cookies_file)
                self.log(f"OK - '{info['title']}' by {info['uploader']} "
                          f"({info['duration']}) via {info['extractor']}")
                self.root.after(0, lambda: messagebox.showinfo(
                    "URL looks good",
                    f"Title: {info['title']}\nUploader: {info['uploader']}\n"
                    f"Duration: {info['duration']}\nSite: {info['extractor']}",
                ))
            except Exception as e:
                self.log(f"CHECK FAILED: {e}")
                self.root.after(0, lambda: messagebox.showerror("URL check failed", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def on_run(self):
        if self.is_running:
            return

        mode = self.source_mode.get()
        timestamps_text = self._get_timestamps_text()

        if mode == "file":
            input_path = self.input_path.get().strip()
            if not input_path or not os.path.isfile(input_path):
                messagebox.showerror("Error", "Drop a video file or click the drop zone to browse.")
                return
        else:
            url = self.url_value.get().strip()
            if not url:
                messagebox.showerror("Error", "Paste a video URL first.")
                return
            if not YT_DLP_AVAILABLE:
                messagebox.showerror("yt-dlp not installed",
                                      "Install it with:\n    pip install yt-dlp")
                return

        if not check_ffmpeg_available():
            messagebox.showerror(
                "ffmpeg not found",
                "Place ffmpeg.exe and ffprobe.exe next to this program, or install "
                "ffmpeg from https://ffmpeg.org/download.html and add it to PATH.",
            )
            return

        try:
            segments = parse_timestamps_text(timestamps_text)
        except ValueError as e:
            messagebox.showerror("Invalid timestamps", str(e))
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress_bar["value"] = 0
        self.set_running(True)

        work_dir = tempfile.mkdtemp(prefix="clipstitch_")

        def worker():
            try:
                keep_clips = self.keep_temp_var.get()
                if mode == "file":
                    input_path = self.input_path.get().strip()
                    reencode = self.reencode_var.get()
                    final_path = run_pipeline_from_file(
                        input_path, segments, reencode, log=self.log,
                        progress=self.set_progress, work_dir=work_dir,
                        keep_clips=keep_clips,
                    )
                    suggested_name = sanitize_filename(
                        os.path.splitext(os.path.basename(input_path))[0] + "_clipped"
                    )
                else:
                    url = self.url_value.get().strip()
                    cookies_browser = None if self.cookies_browser.get() == "None" else self.cookies_browser.get()
                    cookies_profile = self.cookies_profile.get().strip() or None
                    cookies_file = self.cookies_file.get().strip() or None
                    quality = self.quality_var.get()
                    auto_pot_server = self.auto_pot_var.get()
                    pot_server_dir = self.pot_server_dir.get().strip() or None

                    title = fetch_url_title(url, self.log, cookies_browser, cookies_profile, cookies_file)
                    suggested_name = sanitize_filename(title) if title else "clip"

                    final_path = run_pipeline_from_url(
                        url, segments, log=self.log, progress=self.set_progress, work_dir=work_dir,
                        cookies_browser=cookies_browser, cookies_profile=cookies_profile,
                        cookies_file=cookies_file, quality=quality,
                        auto_pot_server=auto_pot_server, pot_server_dir=pot_server_dir,
                        keep_clips=keep_clips,
                    )

                self._pending_final_path = final_path
                self._pending_work_dir = work_dir
                self._pending_suggested_name = suggested_name
                self._pending_keep_clips = keep_clips
                self.root.after(0, self._show_save_dialog)
            except Exception as e:
                self.log(f"ERROR: {e}")
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                shutil.rmtree(work_dir, ignore_errors=True)
            finally:
                self.root.after(0, lambda: self.set_running(False))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Save dialog (generate first, ask where to save after)
    # ------------------------------------------------------------------
    def _show_save_dialog(self):
        final_path = self._pending_final_path
        if not final_path or not os.path.isfile(final_path):
            messagebox.showerror("Error", "Something went wrong - no output file was produced.")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Save your video")
        dialog.configure(bg=CARD_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad = {"padx": 20, "pady": 8}

        tk.Label(dialog, text="\u2728  Your clip is ready!", bg=CARD_BG,
                 font=("Segoe UI", 13, "bold"), fg="#12151c").pack(anchor="w", padx=20, pady=(20, 4))
        tk.Label(dialog, text="Choose where to save it and what to name it.",
                 bg=CARD_BG, fg=TEXT_MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(0, 14))

        default_folder = self._default_output_folder()
        folder_var = tk.StringVar(value=default_folder)
        name_var = tk.StringVar(value=self._pending_suggested_name)

        row1 = ttk.Frame(dialog, style="Card.TFrame")
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="Folder:", style="Card.TLabel", width=8).pack(side="left")
        folder_entry = ttk.Entry(row1, textvariable=folder_var, width=42)
        folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def browse_folder():
            path = filedialog.askdirectory(title="Choose save folder", initialdir=folder_var.get())
            if path:
                folder_var.set(path)

        ttk.Button(row1, text="Browse", style="Secondary.TButton", command=browse_folder).pack(side="left")

        row2 = ttk.Frame(dialog, style="Card.TFrame")
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="File name:", style="Card.TLabel", width=8).pack(side="left")
        name_entry = ttk.Entry(row2, textvariable=name_var, width=42)
        name_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Label(row2, text=".mp4", style="Card.TLabel").pack(side="left")
        name_entry.icursor("end")
        name_entry.focus_set()

        btn_row = ttk.Frame(dialog, style="Card.TFrame")
        btn_row.pack(fill="x", padx=20, pady=(16, 20))

        def _move_clips_folder(dest_video_path):
            clips_src = os.path.join(self._pending_work_dir, "clips")
            if self._pending_keep_clips and os.path.isdir(clips_src):
                clips_dest = os.path.splitext(dest_video_path)[0] + "_clips"
                clips_dest = self._avoid_overwrite(clips_dest) if os.path.exists(clips_dest) else clips_dest
                try:
                    shutil.move(clips_src, clips_dest)
                    self.log(f"Individual clips saved to: {clips_dest}")
                except OSError as e:
                    self.log(f"WARNING: couldn't move individual clips folder: {e}")

        def do_save():
            folder = folder_var.get().strip()
            name = sanitize_filename(name_var.get().strip(), fallback="clip")
            if not folder:
                messagebox.showerror("Error", "Choose a folder.", parent=dialog)
                return
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Error", f"Couldn't create that folder:\n{e}", parent=dialog)
                return

            dest = os.path.join(folder, name + ".mp4")
            dest = self._avoid_overwrite(dest)
            try:
                shutil.move(final_path, dest)
            except OSError as e:
                messagebox.showerror("Error", f"Couldn't save the file:\n{e}", parent=dialog)
                return

            _move_clips_folder(dest)
            shutil.rmtree(self._pending_work_dir, ignore_errors=True)
            dialog.destroy()
            self._show_success(dest)

        def do_cancel():
            # Still save it somewhere sensible rather than losing the work.
            default_dest = os.path.join(default_folder, self._pending_suggested_name + ".mp4")
            default_dest = self._avoid_overwrite(default_dest)
            try:
                os.makedirs(default_folder, exist_ok=True)
                shutil.move(final_path, default_dest)
                _move_clips_folder(default_dest)
                shutil.rmtree(self._pending_work_dir, ignore_errors=True)
                dialog.destroy()
                self._show_success(default_dest)
            except OSError as e:
                dialog.destroy()
                messagebox.showerror("Error", f"Couldn't save the file:\n{e}")

        ttk.Button(btn_row, text="Cancel (save to default folder)",
                   style="Secondary.TButton", command=do_cancel).pack(side="left")
        ttk.Button(btn_row, text="Save", style="Accent.TButton", command=do_save).pack(side="right")

        dialog.bind("<Return>", lambda e: do_save())

    def _default_output_folder(self):
        home = os.path.expanduser("~")
        videos = os.path.join(home, "Videos", "ClipStitch")
        return videos

    def _avoid_overwrite(self, path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{base} ({n}){ext}"):
            n += 1
        return f"{base} ({n}){ext}"

    def _show_success(self, dest_path):
        dialog = tk.Toplevel(self.root)
        dialog.title("Done")
        dialog.configure(bg=CARD_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        tk.Label(dialog, text="\u2705  Saved!", bg=CARD_BG, font=("Segoe UI", 13, "bold"),
                 fg="#12151c").pack(anchor="w", padx=20, pady=(20, 4))
        tk.Label(dialog, text=dest_path, bg=CARD_BG, fg=TEXT_MUTED,
                 font=("Segoe UI", 9), wraplength=380, justify="left").pack(
            anchor="w", padx=20, pady=(0, 16))

        btn_row = ttk.Frame(dialog, style="Card.TFrame")
        btn_row.pack(fill="x", padx=20, pady=(0, 20))

        def open_folder():
            folder = os.path.dirname(dest_path)
            try:
                if os.name == "nt":
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except OSError:
                pass

        ttk.Button(btn_row, text="Open Folder", style="Secondary.TButton",
                   command=open_folder).pack(side="left")
        ttk.Button(btn_row, text="Close", style="Accent.TButton",
                   command=dialog.destroy).pack(side="right")

        self.log(f"Saved to: {dest_path}")


def main():
    root = _make_root()
    app = VideoClipperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
