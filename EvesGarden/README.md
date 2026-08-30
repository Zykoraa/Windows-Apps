# Eve's Garden

A Windows music player and downloader. It reads track, album and artwork
details from Spotify, pulls the audio from YouTube, tags the result properly,
and plays it back with a 10-band EQ, synced lyrics, 32 visualisers and Discord
Rich Presence.

![icon](assets/icon.png)

## Download

**[Download the latest release](https://github.com/Zykoraa/Windows-Apps/releases/latest)**
-- a single `.zip`, no installer.

1. Unzip it anywhere
2. Run `gui.exe`

Nothing else to install. Python, ffmpeg and every library are already inside
the folder. Just keep `gui.exe` and `_internal/` together.

Windows will likely show a blue "Windows protected your PC" box the first
time, because the executable is not code-signed. Click **More info** ->
**Run anyway**.

On first launch the app asks for Spotify credentials -- see [Setup](#setup)
below. You can skip that and still play music you already have; it is only
needed for downloading.

## What it does

**Library** — a SQLite index over the ID3 tags of your files, so you can
browse by song, album or artist, sort by year or play count, and search across
title, artist and album rather than just filenames.

**Downloading** — paste a Spotify track, album or playlist link, or just
search. Candidate YouTube sources are scored against the Spotify track's
duration and penalised for `live`, `karaoke`, `remix`, `full album` and the
like, so you get the right version rather than the first hit. Downloads run
three at a time with per-track state, cancel, and retry-failed.

**Playback** — a serial cascade of RBJ peaking biquads for the EQ (unity at
0 dB, so "flat" really is flat), soft-clipping to round off peaks, volume,
seek, shuffle, repeat, and resume where you left off.

**Presentation** — 18 themes, 32 visualiser modes with 13 colour palettes,
synced lyrics, and album-art-derived accent colours with a contrast check.

## Setup

You need your own free Spotify API credentials. The app asks for them on
first run and writes them itself:

1. Create an app at <https://developer.spotify.com/dashboard> — any name, no
   card required
2. In that app's **Settings**, add this exact Redirect URI:
   `http://127.0.0.1:8888/callback`
   (already have a redirect you would rather reuse? Put it in
   `SPOTIPY_REDIRECT_URI` instead — any loopback address and path works)
3. Copy the Client ID and Client Secret into the app's "Connect Spotify"
   screen

### Playlists need a sign-in

Spotify no longer lets an app read playlists — even public ones — without a
signed-in user. Press **Sign in to Spotify** in the downloader, approve it in
the browser once, and playlist links start working. The token is cached, so
it only asks the first time.

Only two scopes are requested, both read-only:
`playlist-read-private` and `playlist-read-collaborative`.

They are saved to `EvesGarden.env` next to the executable, so a copied folder
stays working. If the app lives somewhere read-only it falls back to
`%LOCALAPPDATA%\EvesGarden\`. See `.env.example` to write the file by hand
instead.

Discord Rich Presence needs no setup.

## Running from source

```bash
pip install -r requirements.txt
python gui.py
```

`ffmpeg.exe` and `ffprobe.exe` must be in `bin/`. They are not committed to
the repository. Grab the `essentials` build from
<https://www.gyan.dev/ffmpeg/builds/> — it has everything this app needs
(MP3 encoding, WebM/Opus and M4A/AAC decoding) at less than half the size of
the full build.

Releases are built automatically: pushing a tag like `v1.0.2` runs
[the workflow](../.github/workflows/release-evesgarden.yml), which fetches
ffmpeg, builds the `.exe`, zips it and attaches it to a new GitHub release.

## Building the .exe

```bash
pip install pyinstaller
python -m PyInstaller gui.spec --noconfirm
```

Output lands in `dist/gui/`. Keep `gui.exe` and `_internal/` together.

To change the app icon, edit `DESIGN` in `make_icon.py` (`Monstera`, `Leaf`,
`Bloom` or `Sprout`), run it, and rebuild — it regenerates `assets/icon.ico`
and the base64 copy embedded in `app_icon.py`.

## Layout

| File | Purpose |
| --- | --- |
| `gui.py` | The app window, playback controls and overlays |
| `library_index.py` | SQLite index over the ID3 tags |
| `library_view.py` | Songs / Albums / Artists browser |
| `downloader.py` | Spotify metadata, YouTube sourcing, tagging |
| `download_manager.py` | Download queue with per-track state |
| `player_engine.py` | Decoding, EQ, playback |
| `visualizers.py` | 32 visualiser modes and 13 palettes |
| `discord_presence.py` | Rich Presence |
| `credentials.py` | Where credentials are read from and written to |
| `settings.py` | Persisted UI state |
| `media_keys.py` | Media keys via `RegisterHotKey` |
| `make_icon.py` | Generates the app icon |

## Notes

Media keys are registered with `RegisterHotKey`, which asks Windows for those
specific keys only — not a system-wide keyboard hook.

Credentials are never compiled into the binary. A client secret inside an
executable can be read straight back out of the PyInstaller archive, so each
person supplies their own. If you share a build you made, delete
`EvesGarden.env` from the folder first.

For personal use. You are responsible for respecting the terms of the
services it talks to.
