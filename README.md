# ClipStitch

A free, no-nonsense desktop tool for cutting a video down to just the parts
you want and stitching them into one file — from a local video file, or
straight from a URL (YouTube and 1000+ other sites via `yt-dlp`), without
downloading anything you don't need.

Clip. Stitch. Done.

---

## Features

### Core clipping
- Cut one or many timestamped segments from a **local video file** or a
  **URL**, and join them into a single output.
- For URLs, only the requested sections are downloaded — not the whole
  video.
- Drag-and-drop a video file onto the app, or a `.txt` file of timestamps.
- Batch queue: line up several clipping jobs and run them unattended.

### Preview & mark clips (local files)
- Scrub through the actual video frame-by-frame using a slider, or
  Play/Pause to step through it in real time.
- **Set In** / **Set Out** buttons mark a range from wherever you're
  parked, and **+ Add clip to Timestamps** drops a `START,END` line
  straight into the Timestamps box.
- (URL mode shows an explanatory placeholder instead — clipping from a
  URL is only meant to download the sections you ask for, so there's
  nothing local to scrub through until you do.)

### Live URL preview
- Paste or type a URL and, after a short pause, a card appears showing
  the video's thumbnail, title, uploader, and duration — so you can
  confirm you've got the right video before setting up timestamps.

### Export options
- **Aspect/platform presets** — crop and scale for TikTok/Shorts,
  YouTube, or Instagram.
- **Loudness normalization** — consistent volume across clips.
- **Crossfade** — blend between clips instead of a hard cut.
- **Remove silent/dead-air sections** — automatically detects and cuts
  out quiet stretches (configurable threshold, minimum length, and
  padding so words don't get clipped).
- **Burned-in captions** — from an existing `.srt` file, or a quick
  manual script using the same `START,END,TEXT` format as the
  Timestamps box. Configurable font size, color, and position.
- **Side exports** — WebM, MP3 (audio-only), a JPG thumbnail, and/or a
  GIF, all taken from the finished clip.

### Advanced (URL mode)
- Quality selection, cookies from a browser or a `cookies.txt` file
  (for private/members-only/age-restricted videos).
- Automatic PO Token server support for gated YouTube videos.
- Optional frame-exact ("precise") cut points — slower, since it forces
  a re-encode after downloading.

### Advanced (local file mode)
- Frame-accurate cuts (re-encode for exact timing) vs. fast stream-copy
  cuts.
- Option to keep the individual clips, not just the joined result.

---

## Requirements

- **Python 3.9+**
- **ffmpeg** and **ffprobe** — either on your `PATH`, or placed next to
  the script/executable.
- Python packages:
  - `yt-dlp` — URL downloading
  - `customtkinter` — UI
  - `Pillow` — image handling (thumbnails, previews); installed
    automatically as a dependency of `customtkinter`, but listed
    explicitly in `build_exe.bat` since PyInstaller doesn't always
    detect it as a transitive dependency
  - `tkinterdnd2` *(optional)* — drag-and-drop support; the app still
    works without it, just without drag-and-drop
  - `pycryptodomex` — needed by `yt-dlp` for some gated content

Install everything with:

```
pip install yt-dlp customtkinter Pillow tkinterdnd2 pycryptodomex
```

## Running from source

```
python video_clipper_gui.py
```

## Building a standalone Windows .exe

```
build_exe.bat
```

This installs/upgrades the required packages and runs PyInstaller with
the flags needed to bundle `yt-dlp`, `tkinterdnd2`, `customtkinter`, and
`Pillow` correctly (several of these ship extra data files that
PyInstaller won't pick up automatically without `--collect-all`). Place
`ffmpeg.exe` and `ffprobe.exe` next to the built executable, or make
sure they're on `PATH`.

---

## Usage

1. **Pick a source** — drag a local video file onto the drop zone, or
   switch to "From URL" and paste a link.
2. **Find your clips** — for local files, use the Preview & mark clips
   player to scrub around and mark In/Out points; either way, you can
   type timestamps directly into the Timestamps box (`START,END`, one
   per line).
3. **(Optional) Set export options** — aspect preset, loudness
   normalization, crossfade, silence auto-trim, captions, side exports.
4. **Click "Clip It"** (or queue up multiple jobs and click "Run
   Queue").
5. Choose where to save, and you're done.

### Timestamp format

One clip per line:

```
00:00:10,00:00:45
00:02:00,00:03:15
```

Times can be `H:MM:SS`, `M:SS`, or plain seconds.

### Manual captions script format

Same idea, with a third field for the caption text:

```
00:00:01,00:00:04,Your first caption
00:00:05,00:00:08,Your second caption
```

---

## Notes & limitations

- URL section downloads use `yt-dlp`'s keyframe-based seeking, so very
  short clips carry roughly the same fixed setup/metadata overhead as a
  full download — this is inherent to how section downloads work, not
  a bug.
- Enabling "Precise cut points" (URL mode) or "Frame-accurate cuts"
  (local file mode) forces a full re-encode, which is meaningfully
  slower than the default fast path.
- Silence auto-trim runs **before** captions are burned in, since
  trimming shifts the timeline. If you're using both, base your caption
  timestamps on the trimmed result, not the original.
- Only clip and caption videos you own or have permission to use.

## Roadmap

Planned next (Tier 2): scene-change detection, savable project files.

Further out (Tier 3, bigger lift): local Whisper-based auto-captions,
automatic highlight/moment detection.
