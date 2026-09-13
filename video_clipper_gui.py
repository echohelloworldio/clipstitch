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
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except ImportError:
    CTK_AVAILABLE = False

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


def extract_preview_frame_bytes(input_path, timestamp_seconds, max_width=480, timeout=10):
    """Grabs a single JPEG-encoded frame near timestamp_seconds using a fast,
    keyframe-based ffmpeg seek (-ss before -i). Returns raw JPEG bytes.
    Raises RuntimeError on failure (bad path, ffmpeg missing/failed, or a
    timeout) so callers can show a friendly message instead of crashing.
    Used for the scrub-preview player, not for the actual clipping pipeline."""
    timestamp_seconds = max(0.0, timestamp_seconds)
    cmd = [
        FFMPEG_BIN, "-ss", f"{timestamp_seconds:.3f}", "-i", input_path,
        "-frames:v", "1", "-an", "-f", "image2pipe", "-vcodec", "mjpeg",
        "-vf", f"scale={max_width}:-2",
        "-loglevel", "error", "-",
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timed out grabbing a preview frame.")
    except OSError as e:
        raise RuntimeError(f"Couldn't run ffmpeg: {e}")
    if not result.stdout:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or "ffmpeg produced no frame data at that position.")
    return result.stdout


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
    bar.

    Byte totals (downloaded_bytes/total_bytes) are the primary source,
    but yt-dlp very often can't report a total size at all when
    downloading just a SECTION of a video (our download_ranges usage) -
    fragmented/DASH sources in particular. fragment_index/fragment_count
    is reported for those same fragmented downloads and gives a reliable
    fraction even when the byte total is unknown.

    IMPORTANT: when the format selector pulls video and audio as
    separate streams (the common "bestvideo+bestaudio" case), yt-dlp
    calls this SAME hook once per stream, each restarting from 0 bytes.
    Naively forwarding raw per-stream fractions makes the reported
    progress jump forward for the video stream, then regress back down
    when the audio stream starts - which is exactly what made the bar
    look broken/frozen. This tracks how many streams ('phases') make up
    the overall download (via info_dict's requested_formats, when
    present) and reports a combined, monotonically non-decreasing
    fraction across all of them instead."""
    state = {"last_logged_pct": -100, "completed_phases": 0, "total_phases": 1}

    def hook(d):
        status = d.get("status")
        requested = (d.get("info_dict") or {}).get("requested_formats")
        if requested:
            state["total_phases"] = max(state["total_phases"], len(requested))

        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            frag_index = d.get("fragment_index")
            frag_count = d.get("fragment_count")
            pct_str = (d.get("_percent_str") or "").strip()
            speed_str = (d.get("_speed_str") or "").strip()
            eta_str = (d.get("_eta_str") or "").strip()

            if total:
                phase_frac = min(1.0, max(0.0, downloaded / total))
            elif frag_count:
                # No byte total available (common for section/fragmented
                # downloads) - fall back to fragments completed, which
                # yt-dlp does report even then.
                phase_frac = min(1.0, max(0.0, (frag_index or 0) / frag_count))
            else:
                phase_frac = None

            if on_progress and phase_frac is not None:
                overall = (state["completed_phases"] + phase_frac) / state["total_phases"]
                on_progress(min(1.0, max(0.0, overall)))

            pct_display = phase_frac * 100 if phase_frac is not None else None
            if pct_display is None or pct_display - state["last_logged_pct"] >= 5 or pct_display >= 99.5:
                stream_note = (f" (part {state['completed_phases'] + 1}/{state['total_phases']})"
                                if state["total_phases"] > 1 else "")
                if pct_str.strip().upper().startswith("N/A") and frag_count:
                    bits = [f"{label}{stream_note}: fragment {frag_index or 0}/{frag_count}"]
                else:
                    bits = [f"{label}{stream_note}: {pct_str or '...'}"]
                if speed_str and not speed_str.strip().upper().startswith("UNKNOWN"):
                    bits.append(f"at {speed_str}")
                if eta_str and eta_str.strip() != "Unknown":
                    bits.append(f"ETA {eta_str}")
                log("  " + " ".join(bits))
                if pct_display is not None:
                    state["last_logged_pct"] = pct_display
        elif status == "finished":
            state["completed_phases"] = min(state["total_phases"], state["completed_phases"] + 1)
            state["last_logged_pct"] = -100
            if on_progress:
                on_progress(min(1.0, state["completed_phases"] / state["total_phases"]))
            if state["completed_phases"] >= state["total_phases"]:
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
                      quality="Best available", on_progress=None, precise_cuts=False):
    """Download only one timestamped section of a URL directly at the
    source, using yt-dlp's download-sections feature, then mux it into a
    single mp4 with ffmpeg.

    precise_cuts controls yt-dlp's force_keyframes_at_cuts: when True, it
    re-encodes the whole downloaded section with ffmpeg so the cut lands
    on the exact frame - this is what makes URL clipping noticeably
    slower than a plain yt-dlp download, since a full re-encode is CPU-
    bound and can easily take longer than the download itself. Default
    False snaps to the nearest keyframe instead (typically within a
    couple seconds), which is dramatically faster and matches the
    fast-by-default behavior of the local-file 'frame-accurate cuts'
    toggle elsewhere in this app."""
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
        "force_keyframes_at_cuts": precise_cuts,
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
        "thumbnail": info.get("thumbnail"),
    }


def fetch_url_preview(url, cookies_browser=None, cookies_profile=None, cookies_file=None):
    """Lightweight, non-raising metadata fetch used for the live link
    preview as the user types/pastes a URL. Returns a dict with
    title/uploader/duration/thumbnail (any of which may be None), or
    None entirely on failure. Deliberately quiet - this runs on a
    background thread on every URL change, so it must never pop up
    dialogs or spam the log."""
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
    except Exception:
        return None

    duration = info.get("duration")
    return {
        "title": info.get("title") or "(untitled)",
        "uploader": info.get("uploader") or "(unknown uploader)",
        "duration": seconds_to_timestamp(duration) if duration else None,
        "thumbnail": info.get("thumbnail"),
    }


class _YtDlpLogger:
    """Routes yt-dlp's internal log messages into our GUI log box."""
    # Postprocessing (merging video+audio, fixups, embedding, etc.) happens
    # AFTER the progress_hooks download phase finishes, with no progress
    # percentage available at all - it used to look like the app had
    # frozen right when it was actually busy muxing/converting. yt-dlp
    # reports these steps through logger.debug() the same as its
    # (filtered-out) per-fragment spam, so we specifically let these
    # through instead of silently dropping everything but "[download]".
    _POSTPROCESS_PREFIXES = (
        "[Merger]", "[ffmpeg]", "[Metadata]", "[VideoRemuxer]", "[VideoConvertor]",
        "[ExtractAudio]", "[EmbedThumbnail]", "[EmbedSubtitle]", "[Fixup", "[MoveFiles]",
    )

    def __init__(self, log_fn):
        self.log_fn = log_fn

    def debug(self, msg):
        if msg.startswith("[download]") or "Destination" in msg or msg.startswith(self._POSTPROCESS_PREFIXES):
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
                           pot_server_dir=None, keep_clips=False, export_options=None,
                           precise_cuts=False):
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
    if precise_cuts:
        log("  Precise cut points enabled: each section will be re-encoded after "
            "downloading for frame-exact start/end points - this is noticeably "
            "slower than a plain download.")
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
            on_progress=on_seg_progress, precise_cuts=precise_cuts,
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

# ----------------------------------------------------------------------
# GUI (CustomTkinter — dark, card-based, LocalSend/Spotify/Netflix-ish)
# ----------------------------------------------------------------------

import io
from PIL import Image

if not CTK_AVAILABLE:
    raise SystemExit(
        "ClipStitch needs the 'customtkinter' package for its interface.\n"
        "Install it with:\n    pip install customtkinter"
    )

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# --- Palette -----------------------------------------------------------
BG = "#0e0f13"              # app background (near-black, Spotify/Netflix-ish)
BG_ELEVATED = "#15171d"     # header / top-level surface
CARD_BG = "#1a1d24"         # card surfaces
CARD_BG_HOVER = "#20232c"
BORDER = "#2a2e38"
TEXT = "#f2f3f5"
TEXT_MUTED = "#9aa1ad"
TEXT_FAINT = "#6b7280"
ACCENT = "#4c7bfa"
ACCENT_HOVER = "#3d63d9"
ACCENT_SOFT = "#22283a"
DANGER = "#ef4444"
SUCCESS = "#3ecf8e"
DROP_ZONE_IDLE_BG = "#161920"
DROP_ZONE_HOVER_BG = "#1b2333"
LOG_BG = "#0a0b0e"

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"

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


def _f(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def _mono(size=11):
    return ctk.CTkFont(family=MONO_FAMILY, size=size)


# --- Root: CTk + drag-and-drop, gracefully degrading -------------------
if DND_AVAILABLE:
    class _DnDRoot(TkinterDnD.Tk, ctk.CTk):
        def __init__(self, *args, **kwargs):
            ctk.CTk.__init__(self, *args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)

    def _make_root():
        return _DnDRoot()
else:
    def _make_root():
        return ctk.CTk()


def _card(parent, **kwargs):
    """A standard elevated card container."""
    defaults = dict(fg_color=CARD_BG, corner_radius=12, border_width=1, border_color=BORDER)
    defaults.update(kwargs)
    return ctk.CTkFrame(parent, **defaults)


def _row(parent, **kwargs):
    defaults = dict(fg_color="transparent")
    defaults.update(kwargs)
    return ctk.CTkFrame(parent, **defaults)


def _section_label(parent, text, **kwargs):
    defaults = dict(font=_f(13, "bold"), text_color=TEXT, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, text=text, **defaults)


def _muted_label(parent, text, **kwargs):
    defaults = dict(font=_f(11), text_color=TEXT_MUTED, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, text=text, **defaults)


def _secondary_button(parent, text, command, **kwargs):
    defaults = dict(
        font=_f(11), fg_color="transparent", hover_color=CARD_BG_HOVER,
        text_color=TEXT_MUTED, border_width=1, border_color=BORDER,
        corner_radius=8, height=30,
    )
    defaults.update(kwargs)
    return ctk.CTkButton(parent, text=text, command=command, **defaults)


def _ghost_button(parent, text, command, **kwargs):
    """Borderless toggle-style button (Advanced/Export headers)."""
    defaults = dict(
        font=_f(11, "bold"), fg_color="transparent", hover_color=BG_ELEVATED,
        text_color=TEXT_MUTED, anchor="w", corner_radius=6, height=28,
    )
    defaults.update(kwargs)
    return ctk.CTkButton(parent, text=text, command=command, **defaults)


class VideoClipperApp:
    def __init__(self, root):
        self.root = root
        root.title("ClipStitch")
        root.geometry("820x860")
        root.minsize(680, 600)
        root.configure(fg_color=BG)

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
        self.precise_url_cuts_var = tk.BooleanVar(value=False)
        self.pot_server_dir = tk.StringVar(value=DEFAULT_POT_SERVER_DIR)
        self.advanced_visible = tk.BooleanVar(value=False)

        # Export options
        self.aspect_preset_var = tk.StringVar(value="None (keep original)")
        self.normalize_audio_var = tk.BooleanVar(value=False)
        self.crossfade_var = tk.BooleanVar(value=False)
        self.crossfade_duration_var = tk.StringVar(value="0.5")
        self.export_webm_var = tk.BooleanVar(value=False)
        self.export_mp3_var = tk.BooleanVar(value=False)
        self.thumbnail_var = tk.BooleanVar(value=False)
        self.thumbnail_time_var = tk.StringVar(value="0:00")
        self.gif_var = tk.BooleanVar(value=False)
        self.gif_start_var = tk.StringVar(value="0:00")
        self.gif_duration_var = tk.StringVar(value="3")

        self.is_running = False
        self._pending_final_path = None
        self._pending_work_dir = None
        self._pending_suggested_name = "clip"

        self.queue = []  # list of job dicts
        self._queue_running = False
        self._queue_rows = {}  # job id -> row widgets

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
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        scroll = ctk.CTkScrollableFrame(self.root, fg_color=BG,
                                         scrollbar_button_color=BORDER,
                                         scrollbar_button_hover_color=TEXT_FAINT)
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        main = scroll  # alias: everything below packs into this scrollable frame

        # --- Header ---
        header = _row(main)
        header.pack(fill="x", padx=28, pady=(24, 18))
        ctk.CTkLabel(header, text="ClipStitch", font=_f(26, "bold"),
                     text_color=TEXT, anchor="w").pack(anchor="w")
        _muted_label(header, "Clip. Stitch. Done.", font=_f(12)).pack(anchor="w", pady=(2, 0))

        body = _row(main)
        body.pack(fill="x", padx=28, pady=(0, 24))

        # --- Source toggle ---
        self.source_toggle = ctk.CTkSegmentedButton(
            body, values=["\U0001F4C1  Local file", "\U0001F310  From URL"],
            font=_f(12), selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=CARD_BG, unselected_hover_color=CARD_BG_HOVER,
            fg_color=CARD_BG, text_color=TEXT, height=36, corner_radius=9,
            command=self._on_source_toggle,
        )
        self.source_toggle.set("\U0001F4C1  Local file")
        self.source_toggle.pack(fill="x", pady=(0, 12))

        # --- Drop zone (local file mode) ---
        self.drop_zone = ctk.CTkFrame(body, fg_color=DROP_ZONE_IDLE_BG, corner_radius=12,
                                       border_width=2, border_color=BORDER, cursor="hand2")
        self.drop_zone_label = ctk.CTkLabel(
            self.drop_zone, text="\U0001F4E5  Drag & drop a video here\nor click to browse",
            font=_f(13), text_color=TEXT_MUTED, justify="center",
        )
        self.drop_zone_label.pack(expand=True, fill="both", pady=36)
        self.drop_zone.pack(fill="x", pady=(0, 8))
        for widget in (self.drop_zone, self.drop_zone_label):
            widget.bind("<Button-1>", lambda e: self.browse_input())
        if DND_AVAILABLE:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop_file)
            self.drop_zone.dnd_bind("<<DropEnter>>", lambda e: self._set_drop_zone_hover(True))
            self.drop_zone.dnd_bind("<<DropLeave>>", lambda e: self._set_drop_zone_hover(False))

        self.selected_file_label = _muted_label(body, "")
        self.selected_file_label.pack(fill="x", pady=(0, 10))

        # --- URL input (url mode) ---
        self.url_frame = _row(body)
        url_input_row = _row(self.url_frame)
        url_input_row.pack(fill="x")
        self.url_entry = ctk.CTkEntry(
            url_input_row, textvariable=self.url_value, font=_f(12), height=36,
            fg_color=CARD_BG, border_color=BORDER, text_color=TEXT,
            placeholder_text="Paste a video URL...",
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        _secondary_button(url_input_row, "Check", self.on_check_url, height=36).pack(side="left")
        _muted_label(self.url_frame, "YouTube and 1000+ other sites are supported."
                     ).pack(anchor="w", pady=(6, 10))

        # --- Live link preview (thumbnail + title, fetched as you paste) ---
        self.url_preview_card = _card(self.url_frame, fg_color=BG_ELEVATED)
        self._url_preview_thumb_image = None  # keep a ref so CTkImage isn't GC'd
        preview_inner = _row(self.url_preview_card, fg_color="transparent")
        preview_inner.pack(fill="x", padx=10, pady=10)
        self.url_preview_thumb_label = ctk.CTkLabel(
            preview_inner, text="", width=120, height=68, fg_color=CARD_BG, corner_radius=8,
        )
        self.url_preview_thumb_label.pack(side="left", padx=(0, 12))
        preview_text_col = _row(preview_inner, fg_color="transparent")
        preview_text_col.pack(side="left", fill="both", expand=True)
        self.url_preview_title_label = ctk.CTkLabel(
            preview_text_col, text="", font=_f(12, "bold"), text_color=TEXT,
            anchor="w", justify="left", wraplength=440,
        )
        self.url_preview_title_label.pack(anchor="w", fill="x")
        self.url_preview_meta_label = _muted_label(preview_text_col, "")
        self.url_preview_meta_label.pack(anchor="w", fill="x", pady=(4, 0))
        # not packed yet - only shown once there's something to preview

        self._url_preview_job = None
        self._url_preview_cache = {}
        self._url_preview_last_fetched = None
        self.url_value.trace_add("write", self._on_url_value_changed)

        # --- Preview & mark clips (local files only) ---
        _section_label(body, "Preview & mark clips").pack(anchor="w", pady=(6, 2))
        self.preview_card = _card(body)
        self.preview_card.pack(fill="x", pady=(0, 4))

        self.preview_placeholder_label = _muted_label(
            self.preview_card, "Select a local video above to scrub through it and mark clips here.",
        )
        self.preview_placeholder_label.pack(anchor="w", padx=16, pady=16)

        self.preview_content = _row(self.preview_card)
        # not packed yet - shown once a local file is selected

        preview_pad = {"padx": 16}
        img_row = _row(self.preview_content)
        img_row.pack(fill="x", padx=16, pady=(14, 8))
        self.preview_image_label = ctk.CTkLabel(
            img_row, text="", width=480, height=270, fg_color=BG_ELEVATED, corner_radius=8,
        )
        self.preview_image_label.pack()
        self._preview_ctk_image = None  # keep a reference so it isn't GC'd

        slider_row = _row(self.preview_content)
        slider_row.pack(fill="x", **preview_pad, pady=(0, 2))
        self.preview_slider = ctk.CTkSlider(
            slider_row, from_=0, to=1, number_of_steps=1000, height=16,
            fg_color=BORDER, progress_color=ACCENT, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, command=self._on_preview_slider_moved,
        )
        self.preview_slider.set(0)
        self.preview_slider.pack(fill="x")

        time_row = _row(self.preview_content)
        time_row.pack(fill="x", **preview_pad, pady=(2, 10))
        self.preview_time_label = _muted_label(time_row, "0:00 / 0:00")
        self.preview_time_label.pack(side="left")
        self.preview_marks_label = _muted_label(time_row, "In: --   Out: --")
        self.preview_marks_label.pack(side="right")

        controls_row = _row(self.preview_content)
        controls_row.pack(fill="x", **preview_pad, pady=(0, 8))
        _secondary_button(controls_row, "\u23EA 5s", lambda: self._preview_seek_relative(-5),
                           width=56).pack(side="left")
        _secondary_button(controls_row, "\u25C0 1s", lambda: self._preview_seek_relative(-1),
                           width=56).pack(side="left", padx=(4, 0))
        self.preview_play_btn = ctk.CTkButton(
            controls_row, text="\u25B6  Play", font=_f(12, "bold"), width=90, height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
            corner_radius=8, command=self._toggle_preview_play,
        )
        self.preview_play_btn.pack(side="left", padx=(10, 10))
        _secondary_button(controls_row, "1s \u25B6", lambda: self._preview_seek_relative(1),
                           width=56).pack(side="left")
        _secondary_button(controls_row, "5s \u23E9", lambda: self._preview_seek_relative(5),
                           width=56).pack(side="left", padx=(4, 0))

        marks_row = _row(self.preview_content)
        marks_row.pack(fill="x", padx=16, pady=(0, 16))
        _secondary_button(marks_row, "Set In", self._preview_set_in).pack(side="left")
        _secondary_button(marks_row, "Set Out", self._preview_set_out
                           ).pack(side="left", padx=(6, 0))
        _secondary_button(marks_row, "Clear marks", self._preview_clear_marks
                           ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            marks_row, text="+ Add clip to Timestamps", font=_f(12, "bold"), height=30,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
            corner_radius=8, command=self._preview_add_clip,
        ).pack(side="right")

        # Preview player state
        self.preview_duration = 0.0
        self.preview_current_time = 0.0
        self.preview_in_point = None
        self.preview_out_point = None
        self.preview_playing = False
        self._preview_loaded_path = None
        self._preview_frame_inflight = False
        self._preview_pending_seek = None
        self._preview_play_job = None
        self._preview_last_tick = None

        # --- Timestamps ---
        _section_label(body, "Timestamps").pack(anchor="w", pady=(6, 2))
        _muted_label(body, "One clip per line: START,END  (e.g. 00:00:10,00:00:45). "
                            "You can also drag a .txt file onto the drop zone above."
                     ).pack(anchor="w", pady=(0, 8))

        ts_card = _card(body)
        ts_card.pack(fill="x", pady=(0, 4))
        self.timestamps_box = ctk.CTkTextbox(
            ts_card, height=130, wrap="none", fg_color=CARD_BG, text_color=TEXT_MUTED,
            font=_mono(12), corner_radius=12, border_width=0,
        )
        self.timestamps_box.pack(fill="both", expand=True, padx=2, pady=2)
        self._placeholder_text = "00:00:10,00:00:45\n00:02:00,00:03:15"
        self._show_placeholder()
        self.timestamps_box.bind("<FocusIn>", self._clear_placeholder)

        ts_actions = _row(body)
        ts_actions.pack(fill="x", pady=(8, 18))
        _secondary_button(ts_actions, "Load from .txt file...",
                           self.load_timestamps_file).pack(side="right")

        # --- Advanced (collapsible) ---
        self.advanced_toggle_btn = _ghost_button(
            body, "\u25B8  Advanced options", self._toggle_advanced,
        )
        self.advanced_toggle_btn.pack(anchor="w")

        self.advanced_frame = _card(body)
        self._build_advanced_contents(self.advanced_frame)
        # not packed yet - toggled on demand

        # --- Export options (collapsible) ---
        self.export_visible = tk.BooleanVar(value=False)
        self.export_toggle_btn = _ghost_button(
            body, "\u25B8  Export options (presets, crossfade, extras)",
            self._toggle_export_options,
        )
        self.export_toggle_btn.pack(anchor="w", pady=(8, 0))

        self.export_frame = _card(body)
        self._build_export_contents(self.export_frame)
        # not packed yet - toggled on demand

        # --- Batch queue ---
        queue_header_row = _row(body)
        queue_header_row.pack(fill="x", pady=(22, 8))
        _section_label(queue_header_row, "Batch queue").pack(side="left")
        _muted_label(queue_header_row, "  (optional - queue up several jobs to run unattended)"
                     ).pack(side="left")

        self.queue_card = _card(body)
        self.queue_card.pack(fill="x", pady=(0, 8))
        queue_head = _row(self.queue_card)
        queue_head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(queue_head, text="Source", font=_f(11, "bold"), text_color=TEXT_MUTED,
                     anchor="w").pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(queue_head, text="Status", font=_f(11, "bold"), text_color=TEXT_MUTED,
                     anchor="w", width=110).pack(side="right")
        self.queue_list_frame = _row(self.queue_card)
        self.queue_list_frame.pack(fill="x", padx=14, pady=(0, 12))
        self._queue_empty_label = _muted_label(self.queue_list_frame, "No jobs queued yet.")
        self._queue_empty_label.pack(anchor="w", pady=(2, 4))

        queue_btn_row = _row(body)
        queue_btn_row.pack(fill="x", pady=(0, 4))
        _secondary_button(queue_btn_row, "+ Add current setup to queue",
                           self.on_add_to_queue).pack(side="left")
        _secondary_button(queue_btn_row, "Remove selected",
                           self.on_remove_from_queue).pack(side="left", padx=(6, 0))
        _secondary_button(queue_btn_row, "Clear queue",
                           self.on_clear_queue).pack(side="left", padx=(6, 0))
        self.run_queue_button = _secondary_button(
            queue_btn_row, "\u25B6  Run Queue", self.on_run_queue,
            text_color=TEXT, border_color=ACCENT,
        )
        self.run_queue_button.pack(side="right")

        # --- Run button + progress ---
        run_row = _row(body)
        run_row.pack(fill="x", pady=(24, 4))
        self.run_button = ctk.CTkButton(
            run_row, text="\u2702  Clip It", font=_f(14, "bold"), height=44,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
            corner_radius=10, command=self.on_run,
        )
        self.run_button.pack(side="left")
        self.progress_bar = ctk.CTkProgressBar(
            run_row, height=10, corner_radius=6, fg_color=BORDER,
            progress_color=ACCENT,
        )
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(16, 0))

        self.progress_label = _muted_label(body, "")
        self.progress_label.pack(anchor="w", pady=(8, 4))

        # --- Log ---
        _muted_label(body, "Activity log").pack(anchor="w", pady=(16, 4))
        log_card = ctk.CTkFrame(body, fg_color=LOG_BG, corner_radius=12,
                                 border_width=1, border_color=BORDER)
        log_card.pack(fill="both", expand=True, pady=(0, 24))
        self.log_box = ctk.CTkTextbox(
            log_card, height=170, fg_color=LOG_BG, text_color="#c9cdd6",
            font=_mono(11), corner_radius=12, border_width=0, state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, padx=2, pady=2)

    def _build_advanced_contents(self, parent):
        pad = {"padx": 16, "pady": 6}

        _muted_label(parent, "These only matter for URL downloads - safe to ignore for local files."
                     ).pack(anchor="w", padx=16, pady=(14, 8))

        # Quality
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkLabel(row, text="Quality", font=_f(12), text_color=TEXT,
                     anchor="w", width=160).pack(side="left")
        ctk.CTkComboBox(row, variable=self.quality_var, state="readonly", width=200,
                         fg_color=BG_ELEVATED, border_color=BORDER, button_color=BORDER,
                         button_hover_color=ACCENT, text_color=TEXT, dropdown_fg_color=CARD_BG,
                         values=list(QUALITY_PRESETS.keys())).pack(side="left")

        # Precise cut points (speed vs. exactness tradeoff for URL downloads)
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Precise cut points (re-encodes each section - much slower)",
                         variable=self.precise_url_cuts_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")
        _muted_label(
            parent, "Off (default) snaps to the nearest keyframe, usually within a "
                    "couple seconds - this is why URL clips download much faster than "
                    "a full re-encode. Turn this on only if you need frame-exact starts.",
            wraplength=680, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 4))

        # Cookies from browser
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkLabel(row, text="Cookies from browser", font=_f(12), text_color=TEXT,
                     anchor="w", width=160).pack(side="left")
        ctk.CTkComboBox(row, variable=self.cookies_browser, state="readonly", width=130,
                         fg_color=BG_ELEVATED, border_color=BORDER, button_color=BORDER,
                         button_hover_color=ACCENT, text_color=TEXT, dropdown_fg_color=CARD_BG,
                         values=["None", "Chrome", "Firefox", "Edge", "Brave", "Opera", "Vivaldi"]
                         ).pack(side="left")
        ctk.CTkLabel(row, text="Profile:", font=_f(12), text_color=TEXT_MUTED
                     ).pack(side="left", padx=(14, 6))
        ctk.CTkEntry(row, textvariable=self.cookies_profile, width=120, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left")

        # Cookies file
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkLabel(row, text="cookies.txt (recommended)", font=_f(12), text_color=TEXT,
                     anchor="w", width=200).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.cookies_file, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        _secondary_button(row, "Browse", self.browse_cookies_file, height=28).pack(side="left")

        # PO token server
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Auto-start PO Token server (best quality on restricted videos)",
                         variable=self.auto_pot_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")

        row = _row(parent)
        row.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(row, text="Server folder:", font=_f(12), text_color=TEXT_MUTED
                     ).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.pot_server_dir, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", fill="x", expand=True, padx=(8, 8))
        _secondary_button(row, "Browse", self.browse_pot_server_dir, height=28).pack(side="left")

        sep = ctk.CTkFrame(parent, fg_color=BORDER, height=1)
        sep.pack(fill="x", padx=16, pady=10)

        # Local-file specific
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Frame-accurate cuts (re-encode, slower, exact timing)",
                         variable=self.reencode_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")

        row = _row(parent)
        row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkCheckBox(row, text="Keep individual clips (not just the joined result)",
                         variable=self.keep_temp_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")

        _muted_label(
            parent, "Only clip videos you own or have rights/permission to use.",
            wraplength=680, justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 14))

    def _toggle_advanced(self):
        visible = not self.advanced_visible.get()
        self.advanced_visible.set(visible)
        if visible:
            self.advanced_toggle_btn.configure(text="\u25BE  Advanced options")
            self.advanced_frame.pack(fill="x", pady=(8, 0), after=self.advanced_toggle_btn)
        else:
            self.advanced_toggle_btn.configure(text="\u25B8  Advanced options")
            self.advanced_frame.pack_forget()

    def _toggle_export_options(self):
        visible = not self.export_visible.get()
        self.export_visible.set(visible)
        if visible:
            self.export_toggle_btn.configure(text="\u25BE  Export options (presets, crossfade, extras)")
            self.export_frame.pack(fill="x", pady=(8, 0), after=self.export_toggle_btn)
        else:
            self.export_toggle_btn.configure(text="\u25B8  Export options (presets, crossfade, extras)")
            self.export_frame.pack_forget()

    def _build_export_contents(self, parent):
        pad = {"padx": 16, "pady": 6}

        # Aspect preset
        row = _row(parent)
        row.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(row, text="Aspect / preset", font=_f(12), text_color=TEXT,
                     anchor="w", width=160).pack(side="left")
        ctk.CTkComboBox(row, variable=self.aspect_preset_var, state="readonly", width=280,
                         fg_color=BG_ELEVATED, border_color=BORDER, button_color=BORDER,
                         button_hover_color=ACCENT, text_color=TEXT, dropdown_fg_color=CARD_BG,
                         values=list(ASPECT_PRESETS.keys())).pack(side="left")
        _muted_label(parent, "Crops+scales for TikTok/Shorts, YouTube, or Instagram. "
                             "Forces a re-encode.").pack(anchor="w", padx=16, pady=(0, 10))

        # Normalize audio
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Normalize audio loudness (consistent volume across clips)",
                         variable=self.normalize_audio_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")

        # Crossfade
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Crossfade between clips instead of a hard cut, duration (s):",
                         variable=self.crossfade_var, font=_f(12), text_color=TEXT,
                         fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.crossfade_duration_var, width=56, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", padx=(8, 0))
        _muted_label(parent, "Only applies when there are 2+ clips; forces a re-encode."
                     ).pack(anchor="w", padx=16, pady=(0, 10))

        sep = ctk.CTkFrame(parent, fg_color=BORDER, height=1)
        sep.pack(fill="x", padx=16, pady=6)

        # Side exports
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Also export a WebM version", variable=self.export_webm_var,
                         font=_f(12), text_color=TEXT, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, border_color=BORDER).pack(side="left")
        ctk.CTkCheckBox(row, text="Also export audio-only (MP3)", variable=self.export_mp3_var,
                         font=_f(12), text_color=TEXT, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, border_color=BORDER
                         ).pack(side="left", padx=(24, 0))

        # Thumbnail
        row = _row(parent)
        row.pack(fill="x", **pad)
        ctk.CTkCheckBox(row, text="Save a thumbnail at", variable=self.thumbnail_var,
                         font=_f(12), text_color=TEXT, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, border_color=BORDER).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.thumbnail_time_var, width=70, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", padx=(8, 6))
        _muted_label(row, "(time within the FINAL clip, e.g. 0:02)").pack(side="left")

        # GIF
        row = _row(parent)
        row.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkCheckBox(row, text="Save a GIF starting at", variable=self.gif_var,
                         font=_f(12), text_color=TEXT, fg_color=ACCENT,
                         hover_color=ACCENT_HOVER, border_color=BORDER).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.gif_start_var, width=70, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", padx=(8, 10))
        ctk.CTkLabel(row, text="for", font=_f(12), text_color=TEXT).pack(side="left")
        ctk.CTkEntry(row, textvariable=self.gif_duration_var, width=50, height=28,
                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT
                     ).pack(side="left", padx=(6, 6))
        _muted_label(row, "seconds (within the FINAL clip)").pack(side="left")

    def _gather_export_options(self):
        """Reads the Export options panel into the dict the pipeline
        functions expect. Raises ValueError with a friendly message if
        any of the free-typed fields are invalid."""
        opts = {}
        preset_label = self.aspect_preset_var.get()
        opts["aspect_preset_filter"] = ASPECT_PRESETS.get(preset_label)
        opts["normalize_audio"] = self.normalize_audio_var.get()

        if self.crossfade_var.get():
            try:
                opts["crossfade_duration"] = float(self.crossfade_duration_var.get())
            except ValueError:
                raise ValueError("Crossfade duration must be a number (seconds).")
        else:
            opts["crossfade_duration"] = None

        opts["export_webm"] = self.export_webm_var.get()
        opts["export_mp3"] = self.export_mp3_var.get()

        if self.thumbnail_var.get():
            try:
                opts["thumbnail_time"] = parse_time(self.thumbnail_time_var.get())
            except ValueError:
                raise ValueError("Thumbnail time isn't a valid timestamp (e.g. 0:02).")
        else:
            opts["thumbnail_time"] = None

        if self.gif_var.get():
            try:
                opts["gif_start"] = parse_time(self.gif_start_var.get())
                opts["gif_duration"] = float(self.gif_duration_var.get())
            except ValueError:
                raise ValueError("GIF start/duration isn't valid (e.g. start 0:01, duration 3).")
        else:
            opts["gif_start"] = None
            opts["gif_duration"] = 3.0

        return opts

    # ------------------------------------------------------------------
    # Source mode / drop zone
    # ------------------------------------------------------------------
    def _on_source_toggle(self, value):
        self.source_mode.set("file" if value.endswith("Local file") else "url")
        self._update_source_mode()

    def _update_source_mode(self):
        mode = self.source_mode.get()
        if mode == "file":
            self.url_frame.pack_forget()
            self.drop_zone.pack(fill="x", pady=(0, 8), before=self.selected_file_label)
        else:
            self.drop_zone.pack_forget()
            self.url_frame.pack(fill="x", pady=(0, 8), before=self.selected_file_label)
        self._refresh_preview_panel_visibility()

    # ------------------------------------------------------------------
    # Scrub preview / mark-clips player (local files only)
    # ------------------------------------------------------------------
    def _refresh_preview_panel_visibility(self):
        mode = self.source_mode.get()
        if mode == "url":
            self._stop_preview_playback()
            self.preview_content.pack_forget()
            self.preview_placeholder_label.configure(
                text="Preview is available for local files only - URL clips only "
                     "download the sections you request, so there's nothing to "
                     "scrub through yet."
            )
            self.preview_placeholder_label.pack(anchor="w", padx=16, pady=16)
        elif not self._preview_loaded_path:
            self.preview_content.pack_forget()
            self.preview_placeholder_label.configure(
                text="Select a local video above to scrub through it and mark clips here."
            )
            self.preview_placeholder_label.pack(anchor="w", padx=16, pady=16)
        else:
            self.preview_placeholder_label.pack_forget()
            self.preview_content.pack(fill="x")

    def _load_preview_for_file(self, path):
        self._stop_preview_playback()
        self._preview_loaded_path = path
        self.preview_in_point = None
        self.preview_out_point = None
        self.preview_current_time = 0.0
        self._preview_pending_seek = None
        self.preview_slider.set(0)
        self._update_preview_marks_label()
        self._refresh_preview_panel_visibility()

        if not check_ffmpeg_available():
            self.preview_time_label.configure(text="ffmpeg not found - preview unavailable")
            return

        self.preview_time_label.configure(text="Loading preview...")

        def worker():
            duration = get_video_duration(path)

            def apply():
                if path != self._preview_loaded_path:
                    return  # a different file was selected meanwhile
                self.preview_duration = duration or 0.0
                self.preview_slider.configure(to=max(self.preview_duration, 0.1))
                self._update_preview_time_label()
                self._preview_request_frame(0.0)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _update_preview_time_label(self):
        cur = seconds_to_timestamp(self.preview_current_time)
        total = seconds_to_timestamp(self.preview_duration)
        self.preview_time_label.configure(text=f"{cur} / {total}")

    def _update_preview_marks_label(self):
        in_text = seconds_to_timestamp(self.preview_in_point) if self.preview_in_point is not None else "--"
        out_text = seconds_to_timestamp(self.preview_out_point) if self.preview_out_point is not None else "--"
        self.preview_marks_label.configure(text=f"In: {in_text}   Out: {out_text}")

    def _on_preview_slider_moved(self, value):
        self._preview_seek_to(float(value))

    def _preview_seek_relative(self, delta_seconds):
        if not self._preview_loaded_path:
            return
        self._preview_seek_to(self.preview_current_time + delta_seconds)

    def _preview_seek_to(self, t):
        t = max(0.0, min(self.preview_duration, t))
        self.preview_current_time = t
        self.preview_slider.set(t)
        self._update_preview_time_label()
        self._preview_request_frame(t)

    def _preview_request_frame(self, t):
        if not self._preview_loaded_path:
            return
        if self._preview_frame_inflight:
            self._preview_pending_seek = t
            return

        self._preview_frame_inflight = True
        path = self._preview_loaded_path

        def worker():
            image = None
            error = None
            try:
                jpeg_bytes = extract_preview_frame_bytes(path, t, max_width=480)
                image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
            except Exception as e:
                error = str(e)

            def apply():
                self._preview_frame_inflight = False
                if path != self._preview_loaded_path:
                    pass  # stale - a new file was loaded, just drop this frame
                elif image is not None:
                    ctk_image = ctk.CTkImage(light_image=image, dark_image=image, size=image.size)
                    self._preview_ctk_image = ctk_image
                    self.preview_image_label.configure(image=ctk_image, text="")
                elif error:
                    self.preview_image_label.configure(image=None, text="\u26A0  Couldn't load frame")

                pending = self._preview_pending_seek
                self._preview_pending_seek = None
                if pending is not None and path == self._preview_loaded_path:
                    self._preview_request_frame(pending)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_preview_play(self):
        if not self._preview_loaded_path:
            return
        if self.preview_playing:
            self._stop_preview_playback()
        else:
            self.preview_playing = True
            self.preview_play_btn.configure(text="\u23F8  Pause")
            self._preview_last_tick = time.time()
            self._preview_play_tick()

    def _stop_preview_playback(self):
        self.preview_playing = False
        self.preview_play_btn.configure(text="\u25B6  Play")
        if self._preview_play_job is not None:
            self.root.after_cancel(self._preview_play_job)
            self._preview_play_job = None

    def _preview_play_tick(self):
        if not self.preview_playing:
            return
        now = time.time()
        elapsed = now - self._preview_last_tick
        self._preview_last_tick = now
        new_time = self.preview_current_time + elapsed
        if new_time >= self.preview_duration:
            self._preview_seek_to(self.preview_duration)
            self._stop_preview_playback()
            return
        self.preview_current_time = new_time
        self.preview_slider.set(new_time)
        self._update_preview_time_label()
        self._preview_request_frame(new_time)
        self._preview_play_job = self.root.after(150, self._preview_play_tick)

    def _preview_set_in(self):
        if not self._preview_loaded_path:
            return
        self.preview_in_point = self.preview_current_time
        self._update_preview_marks_label()

    def _preview_set_out(self):
        if not self._preview_loaded_path:
            return
        self.preview_out_point = self.preview_current_time
        self._update_preview_marks_label()

    def _preview_clear_marks(self):
        self.preview_in_point = None
        self.preview_out_point = None
        self._update_preview_marks_label()

    def _preview_add_clip(self):
        if self.preview_in_point is None or self.preview_out_point is None:
            messagebox.showerror("Error", "Set both an In and an Out point first.")
            return
        start, end = self.preview_in_point, self.preview_out_point
        if start >= end:
            messagebox.showerror("Error", "The In point must come before the Out point.")
            return

        line = f"{seconds_to_timestamp(start)},{seconds_to_timestamp(end)}"
        self._clear_placeholder()
        current = self.timestamps_box.get("1.0", "end").rstrip("\n")
        new_text = (current + "\n" + line) if current else line
        self.timestamps_box.delete("1.0", "end")
        self.timestamps_box.insert("1.0", new_text)
        self.log(f"Added clip to timestamps: {line}")
        self._preview_clear_marks()

    # ------------------------------------------------------------------
    # Live URL link preview (thumbnail + title, debounced background fetch)
    # ------------------------------------------------------------------
    def _on_url_value_changed(self, *_args):
        if self._url_preview_job is not None:
            self.root.after_cancel(self._url_preview_job)
            self._url_preview_job = None

        url = self.url_value.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            self._hide_url_preview()
            return

        # Wait for a pause in typing/pasting before hitting the network.
        self._url_preview_job = self.root.after(700, lambda: self._maybe_fetch_url_preview(url))

    def _maybe_fetch_url_preview(self, url):
        self._url_preview_job = None
        if url != self.url_value.get().strip():
            return  # stale - text changed again since this was scheduled

        cached = self._url_preview_cache.get(url)
        if cached is not None:
            self._render_url_preview(url, cached)
            return

        if not YT_DLP_AVAILABLE:
            return

        self._url_preview_last_fetched = url
        self._show_url_preview_loading()

        def worker():
            info = fetch_url_preview(
                url,
                cookies_browser=None if self.cookies_browser.get() == "None" else self.cookies_browser.get(),
                cookies_profile=self.cookies_profile.get().strip() or None,
                cookies_file=self.cookies_file.get().strip() or None,
            )
            thumb_image = None
            if info and info.get("thumbnail"):
                thumb_image = self._download_thumbnail(info["thumbnail"])

            def apply():
                # Only apply if this is still the URL currently in the box.
                if url != self.url_value.get().strip():
                    return
                if info is None:
                    self._hide_url_preview()
                    return
                info_with_image = dict(info)
                # Build the CTkImage once, here on the main thread, and cache
                # that (not the raw PIL image) - re-wrapping the same PIL
                # image in a fresh CTkImage on every render can hand Tk a
                # reference to an already-destroyed PhotoImage.
                if thumb_image is not None:
                    info_with_image["_thumb_ctk_image"] = ctk.CTkImage(
                        light_image=thumb_image, dark_image=thumb_image, size=thumb_image.size,
                    )
                else:
                    info_with_image["_thumb_ctk_image"] = None
                self._url_preview_cache[url] = info_with_image
                self._render_url_preview(url, info_with_image)

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _download_thumbnail(thumb_url, max_size=(120, 68)):
        """Fetches a thumbnail image and returns a PIL Image, or None on
        any failure (missing Pillow support is impossible here since
        customtkinter itself requires Pillow, but network/format issues
        are common and must never crash the preview)."""
        try:
            with urllib.request.urlopen(thumb_url, timeout=8) as resp:
                data = resp.read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail(max_size)
            return img
        except Exception:
            return None

    def _show_url_preview_loading(self):
        self.url_preview_thumb_label.configure(image=None, text="")
        self.url_preview_title_label.configure(text="Loading preview...")
        self.url_preview_meta_label.configure(text="")
        if not self.url_preview_card.winfo_manager():
            self.url_preview_card.pack(fill="x", pady=(0, 10))

    def _render_url_preview(self, url, info):
        if url != self.url_value.get().strip():
            return  # stale

        ctk_image = info.get("_thumb_ctk_image")
        if ctk_image is not None:
            self._url_preview_thumb_image = ctk_image  # keep a reference
            self.url_preview_thumb_label.configure(image=ctk_image, text="")
        else:
            self._url_preview_thumb_image = None
            self.url_preview_thumb_label.configure(image=None, text="\U0001F3AC")

        self.url_preview_title_label.configure(text=info.get("title") or "(untitled)")
        meta_bits = [info.get("uploader") or "(unknown uploader)"]
        if info.get("duration"):
            meta_bits.append(info["duration"])
        self.url_preview_meta_label.configure(text="  \u2022  ".join(meta_bits))

        if not self.url_preview_card.winfo_manager():
            self.url_preview_card.pack(fill="x", pady=(0, 10))

    def _hide_url_preview(self):
        if self.url_preview_card.winfo_manager():
            self.url_preview_card.pack_forget()
        self._url_preview_thumb_image = None

    def _set_drop_zone_hover(self, hovering):
        bg = DROP_ZONE_HOVER_BG if hovering else DROP_ZONE_IDLE_BG
        self.drop_zone.configure(fg_color=bg)
        self.drop_zone_label.configure(fg_color=bg)

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
                self.timestamps_box.configure(text_color=TEXT)
                self.timestamps_box.insert("1.0", content)
                self._placeholder_active = False
            except OSError as e:
                messagebox.showerror("Error", f"Couldn't read that file:\n{e}")
        elif ext in VIDEO_EXTENSIONS:
            self.source_mode.set("file")
            self.source_toggle.set("\U0001F4C1  Local file")
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
        self._load_preview_for_file(path)

    # ------------------------------------------------------------------
    # Placeholder handling for timestamps box
    # ------------------------------------------------------------------
    def _show_placeholder(self):
        self.timestamps_box.delete("1.0", "end")
        self.timestamps_box.insert("1.0", self._placeholder_text)
        self.timestamps_box.configure(text_color=TEXT_FAINT)
        self._placeholder_active = True

    def _clear_placeholder(self, event=None):
        if getattr(self, "_placeholder_active", False):
            self.timestamps_box.delete("1.0", "end")
            self.timestamps_box.configure(text_color=TEXT)
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
            self.timestamps_box.configure(text_color=TEXT)
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
            # Never let the bar visibly move backward. Even with the
            # combined-phase tracking in _make_progress_hook, other
            # sources (retries, a new segment starting) can still hand
            # us a smaller fraction than we already showed - clamp to
            # the high-water mark instead of letting the bar regress.
            prev_max = getattr(self, "_progress_max_frac", 0.0)
            display_frac = max(frac, prev_max)
            self._progress_max_frac = display_frac
            if display_frac > getattr(self, "_progress_last_frac_for_advance", 0.0):
                self._progress_last_advance_time = time.time()
                self._progress_last_frac_for_advance = display_frac
            self._progress_frac = display_frac
            self.progress_bar.set(display_frac)
            self._refresh_progress_label()
        self.root.after(0, _update)

    def set_running(self, running):
        self.is_running = running
        self.run_button.configure(state="disabled" if running else "normal")
        if running:
            self._start_progress_tracking()
        else:
            self._stop_progress_tracking()
            self.progress_label.configure(text="")

    def _start_progress_tracking(self):
        self._progress_frac = 0.0
        self._progress_max_frac = 0.0
        self._progress_start_time = time.time()
        self._progress_last_advance_time = time.time()
        self._progress_last_frac_for_advance = 0.0
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

        stalled_for = time.time() - getattr(self, "_progress_last_advance_time", time.time())

        if frac > 0.03 and stalled_for < 4:
            elapsed = time.time() - getattr(self, "_progress_start_time", time.time())
            raw_eta = elapsed * (1 - frac) / frac
            smoothed = getattr(self, "_progress_eta_smoothed", None)
            eta = raw_eta if smoothed is None else (0.7 * smoothed + 0.3 * raw_eta)
            self._progress_eta_smoothed = eta
            eta_text = self._format_eta(eta)
        else:
            # No reliable ETA yet, or progress hasn't advanced in a few
            # seconds (e.g. yt-dlp merging/remuxing after download, which
            # reports no percentage at all). A frozen or wildly-wrong ETA
            # here is worse than just being honest that it's still going.
            eta_text = f"still working ({int(stalled_for)}s, no % available)" if stalled_for >= 4 else "estimating time..."

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

        try:
            export_options = self._gather_export_options()
        except ValueError as e:
            messagebox.showerror("Invalid export options", str(e))
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.progress_bar.set(0)
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
                        keep_clips=keep_clips, export_options=export_options,
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
                        keep_clips=keep_clips, export_options=export_options,
                        precise_cuts=self.precise_url_cuts_var.get(),
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
    def _dialog_shell(self, title, min_width=460):
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(title)
        dialog.configure(fg_color=CARD_BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.after(50, dialog.grab_set)
        dialog.minsize(min_width, 0)
        return dialog

    def _show_save_dialog(self):
        final_path = self._pending_final_path
        if not final_path or not os.path.isfile(final_path):
            messagebox.showerror("Error", "Something went wrong - no output file was produced.")
            return

        dialog = self._dialog_shell("Save your video")

        pad = {"padx": 22, "pady": 8}

        ctk.CTkLabel(dialog, text="\u2728  Your clip is ready!", font=_f(15, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=22, pady=(22, 4))
        _muted_label(dialog, "Choose where to save it and what to name it."
                     ).pack(anchor="w", padx=22, pady=(0, 16))

        default_folder = self._default_output_folder()
        folder_var = tk.StringVar(value=default_folder)
        name_var = tk.StringVar(value=self._pending_suggested_name)

        row1 = _row(dialog)
        row1.pack(fill="x", **pad)
        ctk.CTkLabel(row1, text="Folder:", font=_f(12), text_color=TEXT_MUTED,
                     width=64, anchor="w").pack(side="left")
        folder_entry = ctk.CTkEntry(row1, textvariable=folder_var, height=32,
                                     fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT)
        folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse_folder():
            path = filedialog.askdirectory(title="Choose save folder", initialdir=folder_var.get())
            if path:
                folder_var.set(path)

        _secondary_button(row1, "Browse", browse_folder, height=32).pack(side="left")

        row2 = _row(dialog)
        row2.pack(fill="x", **pad)
        ctk.CTkLabel(row2, text="File name:", font=_f(12), text_color=TEXT_MUTED,
                     width=64, anchor="w").pack(side="left")
        name_entry = ctk.CTkEntry(row2, textvariable=name_var, height=32,
                                   fg_color=BG_ELEVATED, border_color=BORDER, text_color=TEXT)
        name_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(row2, text=".mp4", font=_f(12), text_color=TEXT_MUTED).pack(side="left")
        name_entry.focus_set()

        btn_row = _row(dialog)
        btn_row.pack(fill="x", padx=22, pady=(18, 22))

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

        def _move_extra_exports(dest_video_path):
            base = os.path.splitext(dest_video_path)[0]
            extras = [
                ("output.webm", base + ".webm"),
                ("output.mp3", base + ".mp3"),
                ("output_thumb.jpg", base + "_thumb.jpg"),
                ("output.gif", base + ".gif"),
            ]
            for src_name, dest_path in extras:
                src_path = os.path.join(self._pending_work_dir, src_name)
                if os.path.isfile(src_path):
                    dest_path = self._avoid_overwrite(dest_path)
                    try:
                        shutil.move(src_path, dest_path)
                        self.log(f"Saved: {dest_path}")
                    except OSError as e:
                        self.log(f"WARNING: couldn't move {src_name}: {e}")

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
            _move_extra_exports(dest)
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
                _move_extra_exports(default_dest)
                shutil.rmtree(self._pending_work_dir, ignore_errors=True)
                dialog.destroy()
                self._show_success(default_dest)
            except OSError as e:
                dialog.destroy()
                messagebox.showerror("Error", f"Couldn't save the file:\n{e}")

        _secondary_button(btn_row, "Cancel (save to default folder)", do_cancel
                           ).pack(side="left")
        ctk.CTkButton(btn_row, text="Save", font=_f(12, "bold"), height=34,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      corner_radius=8, command=do_save).pack(side="right")

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
        dialog = self._dialog_shell("Done", min_width=420)

        ctk.CTkLabel(dialog, text="\u2705  Saved!", font=_f(15, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=22, pady=(22, 4))
        ctk.CTkLabel(dialog, text=dest_path, font=_f(11), text_color=TEXT_MUTED,
                     wraplength=380, justify="left", anchor="w"
                     ).pack(anchor="w", padx=22, pady=(0, 18))

        btn_row = _row(dialog)
        btn_row.pack(fill="x", padx=22, pady=(0, 22))

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

        _secondary_button(btn_row, "Open Folder", open_folder).pack(side="left")
        ctk.CTkButton(btn_row, text="Close", font=_f(12, "bold"), height=34,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#ffffff",
                      corner_radius=8, command=dialog.destroy).pack(side="right")

        self.log(f"Saved to: {dest_path}")

    # ------------------------------------------------------------------
    # Batch queue
    # ------------------------------------------------------------------
    def _render_queue_row(self, job):
        if self._queue_empty_label.winfo_manager():
            self._queue_empty_label.pack_forget()

        row = _row(self.queue_list_frame, fg_color=BG_ELEVATED)
        row.pack(fill="x", pady=(0, 4))
        row._selected = False

        source_label = ctk.CTkLabel(row, text=job["source_display"], font=_f(11),
                                     text_color=TEXT, anchor="w")
        source_label.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=6)
        status_label = ctk.CTkLabel(row, text=job["status"], font=_f(11),
                                     text_color=TEXT_MUTED, width=110, anchor="w")
        status_label.pack(side="right", padx=(6, 10), pady=6)

        def toggle_select(event=None):
            row._selected = not row._selected
            row.configure(fg_color=ACCENT_SOFT if row._selected else BG_ELEVATED)

        for w in (row, source_label, status_label):
            w.bind("<Button-1>", toggle_select)

        self._queue_rows[job["_id"]] = {"row": row, "status": status_label}

    def _set_queue_row_status(self, job_id, text):
        widgets = self._queue_rows.get(job_id)
        if widgets:
            widgets["status"].configure(text=text)

    def on_add_to_queue(self):
        mode = self.source_mode.get()
        timestamps_text = self._get_timestamps_text()

        if mode == "file":
            input_path = self.input_path.get().strip()
            if not input_path or not os.path.isfile(input_path):
                messagebox.showerror("Error", "Drop a video file or click the drop zone to browse first.")
                return
            source_display = os.path.basename(input_path)
        else:
            url = self.url_value.get().strip()
            if not url:
                messagebox.showerror("Error", "Paste a video URL first.")
                return
            source_display = url

        try:
            segments = parse_timestamps_text(timestamps_text)
        except ValueError as e:
            messagebox.showerror("Invalid timestamps", str(e))
            return

        try:
            export_options = self._gather_export_options()
        except ValueError as e:
            messagebox.showerror("Invalid export options", str(e))
            return

        job_id = len(self.queue) + int(time.time() * 1000) % 100000
        job = {
            "_id": job_id,
            "mode": mode,
            "input_path": self.input_path.get().strip() if mode == "file" else None,
            "url": self.url_value.get().strip() if mode == "url" else None,
            "segments": segments,
            "reencode": self.reencode_var.get(),
            "keep_clips": self.keep_temp_var.get(),
            "cookies_browser": None if self.cookies_browser.get() == "None" else self.cookies_browser.get(),
            "cookies_profile": self.cookies_profile.get().strip() or None,
            "cookies_file": self.cookies_file.get().strip() or None,
            "quality": self.quality_var.get(),
            "auto_pot_server": self.auto_pot_var.get(),
            "pot_server_dir": self.pot_server_dir.get().strip() or None,
            "precise_cuts": self.precise_url_cuts_var.get(),
            "export_options": export_options,
            "source_display": source_display,
            "status": "Queued",
        }
        self.queue.append(job)
        self._render_queue_row(job)
        self.log(f"Added to queue: {source_display}")

    def on_remove_from_queue(self):
        if self._queue_running:
            messagebox.showwarning("Queue running", "Wait for the queue to finish before editing it.")
            return
        remaining = []
        for job in self.queue:
            widgets = self._queue_rows.get(job["_id"])
            if widgets and widgets["row"]._selected:
                widgets["row"].destroy()
                del self._queue_rows[job["_id"]]
            else:
                remaining.append(job)
        self.queue = remaining
        if not self.queue:
            self._queue_empty_label.pack(anchor="w", pady=(2, 4))

    def on_clear_queue(self):
        if self._queue_running:
            messagebox.showwarning("Queue running", "Wait for the queue to finish before clearing it.")
            return
        for widgets in self._queue_rows.values():
            widgets["row"].destroy()
        self._queue_rows.clear()
        self.queue.clear()
        self._queue_empty_label.pack(anchor="w", pady=(2, 4))

    def on_run_queue(self):
        if self.is_running or self._queue_running:
            return
        if not self.queue:
            messagebox.showinfo("Queue is empty", "Add at least one job to the queue first.")
            return
        if not check_ffmpeg_available():
            messagebox.showerror(
                "ffmpeg not found",
                "Place ffmpeg.exe and ffprobe.exe next to this program, or install "
                "ffmpeg from https://ffmpeg.org/download.html and add it to PATH.",
            )
            return

        self._queue_running = True
        self.run_button.configure(state="disabled")
        self.run_queue_button.configure(state="disabled")
        self.set_running(True)

        def worker():
            saved_paths = []
            for job in list(self.queue):
                job_id = job["_id"]
                self.root.after(0, lambda i=job_id: self._set_queue_row_status(i, "Running..."))
                self.log(f"--- Starting queued job: {job['source_display']} ---")
                self.progress_bar.set(0)
                work_dir = tempfile.mkdtemp(prefix="clipstitch_")
                try:
                    if job["mode"] == "file":
                        final_path = run_pipeline_from_file(
                            job["input_path"], job["segments"], job["reencode"], log=self.log,
                            progress=self.set_progress, work_dir=work_dir,
                            keep_clips=job["keep_clips"], export_options=job["export_options"],
                        )
                        suggested_name = sanitize_filename(
                            os.path.splitext(os.path.basename(job["input_path"]))[0] + "_clipped"
                        )
                    else:
                        title = fetch_url_title(job["url"], self.log, job["cookies_browser"],
                                                 job["cookies_profile"], job["cookies_file"])
                        suggested_name = sanitize_filename(title) if title else "clip"
                        final_path = run_pipeline_from_url(
                            job["url"], job["segments"], log=self.log, progress=self.set_progress,
                            work_dir=work_dir, cookies_browser=job["cookies_browser"],
                            cookies_profile=job["cookies_profile"], cookies_file=job["cookies_file"],
                            quality=job["quality"], auto_pot_server=job["auto_pot_server"],
                            pot_server_dir=job["pot_server_dir"], keep_clips=job["keep_clips"],
                            export_options=job["export_options"],
                            precise_cuts=job.get("precise_cuts", False),
                        )

                    # Queued jobs auto-save (no per-job dialog) so the whole
                    # queue can run unattended.
                    default_folder = self._default_output_folder()
                    os.makedirs(default_folder, exist_ok=True)
                    dest = self._avoid_overwrite(os.path.join(default_folder, suggested_name + ".mp4"))
                    shutil.move(final_path, dest)

                    if job["keep_clips"]:
                        clips_src = os.path.join(work_dir, "clips")
                        if os.path.isdir(clips_src):
                            clips_dest = self._avoid_overwrite(os.path.splitext(dest)[0] + "_clips")
                            shutil.move(clips_src, clips_dest)

                    for src_name, suffix in [("output.webm", ".webm"), ("output.mp3", ".mp3"),
                                              ("output_thumb.jpg", "_thumb.jpg"), ("output.gif", ".gif")]:
                        src_path = os.path.join(work_dir, src_name)
                        if os.path.isfile(src_path):
                            extra_dest = self._avoid_overwrite(os.path.splitext(dest)[0] + suffix)
                            shutil.move(src_path, extra_dest)

                    saved_paths.append(dest)
                    self.log(f"Saved: {dest}")
                    self.root.after(0, lambda i=job_id: self._set_queue_row_status(i, "\u2705 Done"))
                except Exception as e:
                    self.log(f"ERROR in queued job '{job['source_display']}': {e}")
                    self.root.after(0, lambda i=job_id: self._set_queue_row_status(i, "\u274c Failed"))
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

            self.queue.clear()
            self._queue_running = False
            self.root.after(0, lambda: self.run_button.configure(state="normal"))
            self.root.after(0, lambda: self.run_queue_button.configure(state="normal"))
            self.root.after(0, lambda: self.set_running(False))
            if saved_paths:
                self.root.after(0, lambda: self._show_success(os.path.dirname(saved_paths[0])))

        threading.Thread(target=worker, daemon=True).start()


def main():
    root = _make_root()
    app = VideoClipperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
