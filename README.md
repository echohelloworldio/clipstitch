# ClipStitch

A simple, free desktop tool to clip a video at timestamps you choose, and stitch the clips into a single video — from a local file **or** directly from a URL (YouTube and 1000+ other sites via `yt-dlp`), downloading only the sections you actually need.

No timeline to fight with. No re-encoding unless you want it. Paste your timestamps, hit Run.

## Features

- ✂️ Clip a local video file at multiple timestamp ranges and join them into one output
- 🌐 Clip directly from a URL — only the timestamped sections are downloaded, not the whole video
- ⚡ Fast, lossless stream-copy cutting by default; optional frame-accurate re-encode mode
- 🎚️ Quality selector for URL downloads (up to 4K)
- 🔑 Cookie support (cookies.txt) for age-restricted/private videos
- 🚀 Optional PO Token server integration for full-quality downloads on gated videos
- 🖥️ Plain Tkinter GUI — no browser, no account, no telemetry
- 📦 One-click build into a standalone Windows `.exe`

## Requirements

- [Python 3.9+](https://www.python.org/downloads/)
- [ffmpeg](https://ffmpeg.org/download.html) (the build script can fetch this for you automatically)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — for URL mode (`pip install yt-dlp`)

## Quick start (running from source)

```bash
pip install yt-dlp pycryptodomex
python video_clipper_gui.py
```

Make sure `ffmpeg` and `ffprobe` are either on your system PATH, or copied into the same folder as the script.

## Building a standalone .exe (Windows)

Run `build_exe.bat`. It will:
- Install PyInstaller, yt-dlp, and pycryptodomex
- Build `VideoClipper.exe`
- Automatically download and bundle `ffmpeg.exe` / `ffprobe.exe` next to it

Everything you need ends up in the `dist/` folder — copy it anywhere and run.

## Timestamps format

One clip per line, `START,END`:

```
00:00:10,00:00:45
00:02:00,00:03:15
```

Times can be plain seconds (`90`), `MM:SS`, or `HH:MM:SS`. Lines starting with `#` are treated as comments.

## Downloading age-restricted / login-gated videos at full quality

By default, YouTube limits gated videos (age-restricted, private, members-only) to a low-quality fallback unless a valid PO Token is supplied. To unlock full quality:

1. Install [Node.js](https://nodejs.org)
2. Run `setup_po_token_server.bat` once — it installs the yt-dlp plugin and compiles a small local token-provider server ([BgUtils](https://github.com/Brainicism/bgutil-ytdlp-pot-provider))
3. That's it — the app auto-starts the server in the background whenever it's needed. No manual steps after that.

You'll also want a `cookies.txt` file exported from your browser (e.g. via the "Get cookies.txt LOCALLY" extension) for any video that requires being logged in.

## A note on responsible use

Only download or clip video content you own, have explicit permission to use, or that is licensed for reuse (e.g. Creative Commons). This tool does not bypass DRM and does not grant any rights beyond what the source platform's terms already allow for your account.

## Credits

This tool is a thin, friendly wrapper around excellent existing open-source projects:

- [ffmpeg](https://ffmpeg.org/) — video processing
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — universal video downloading
- [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) — PO Token generation
- [Deno](https://deno.com/) — JS challenge solving for yt-dlp

## License

MIT — see [LICENSE](LICENSE).
