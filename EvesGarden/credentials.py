"""Where credentials live, and how they get there.

Two locations, checked in order:

  portable  -- an `EvesGarden.env` file sitting next to gui.exe. This is what
               makes the zip self-contained: unzip and run, nothing to copy
               into AppData.
  user      -- %LOCALAPPDATA%\\EvesGarden\\.env, the fallback for when the app
               is installed somewhere read-only (Program Files, a network
               share) and cannot write beside itself.

Nothing secret is ever compiled into the binary. A Spotify client secret
baked into an executable is recoverable in plaintext by anyone who has the
file, so each person brings their own free credentials -- entered once, in
the app's setup screen.

The Discord application id is different in kind: it is a public identifier,
sent in the clear on every RPC handshake and visible to anyone who looks at a
presence. Shipping it as a default is safe and means Rich Presence works with
no setup at all.
"""

import os
import sys

# Public identifier, not a secret. See the module docstring.
DEFAULT_DISCORD_CLIENT_ID = "1542845450619457586"

PORTABLE_NAME = "EvesGarden.env"


def app_dir():
    """The folder the app itself lives in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def user_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "EvesGarden")


def portable_path():
    return os.path.join(app_dir(), PORTABLE_NAME)


def user_path():
    return os.path.join(user_dir(), ".env")


def env_paths():
    """Every file we read credentials from, most specific first."""
    return [
        portable_path(),
        os.path.join(app_dir(), ".env"),          # dev checkouts
        user_path(),
    ]


def load():
    """Populate os.environ from whichever files exist. Earlier files win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return []
    loaded = []
    for path in env_paths():
        if os.path.exists(path):
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded


def have_spotify():
    return bool(os.getenv("SPOTIPY_CLIENT_ID", "").strip()
                and os.getenv("SPOTIPY_CLIENT_SECRET", "").strip())


def discord_client_id():
    return (os.getenv("DISCORD_CLIENT_ID", "").strip()
            or DEFAULT_DISCORD_CLIENT_ID)


def _writable(directory):
    probe = os.path.join(directory, ".write-test")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(probe, "w") as fh:
            fh.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


def save(client_id, client_secret, discord_id=None, redirect_uri=None):
    """Write credentials beside the app, falling back to AppData.

    Returns the path written to, so the UI can tell the user where their
    keys ended up.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()

    lines = [
        "# Eve's Garden credentials.",
        "# Keep this file next to gui.exe to stay portable.",
        f"SPOTIPY_CLIENT_ID={client_id}",
        f"SPOTIPY_CLIENT_SECRET={client_secret}",
    ]
    if redirect_uri and redirect_uri.strip():
        lines.append(f"SPOTIPY_REDIRECT_URI={redirect_uri.strip()}")
    if discord_id:
        lines.append(f"DISCORD_CLIENT_ID={discord_id.strip()}")
    body = "\n".join(lines) + "\n"

    target = portable_path() if _writable(app_dir()) else user_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)

    # Make the new values live immediately, without a restart.
    os.environ["SPOTIPY_CLIENT_ID"] = client_id
    os.environ["SPOTIPY_CLIENT_SECRET"] = client_secret
    if discord_id:
        os.environ["DISCORD_CLIENT_ID"] = discord_id.strip()
    if redirect_uri and redirect_uri.strip():
        os.environ["SPOTIPY_REDIRECT_URI"] = redirect_uri.strip()
    return target


def verify(client_id, client_secret):
    """Check a pair against Spotify. Returns (ok, message)."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        return False, "Both the Client ID and the Client Secret are required."
    if len(client_id) < 20 or len(client_secret) < 20:
        return False, "Those look too short -- copy the full values from the dashboard."

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret,
                cache_handler=spotipy.cache_handler.MemoryCacheHandler(),
            ),
            requests_timeout=15,
        )
        sp.search(q="a", limit=1, type="track")
        return True, "Credentials accepted."
    except Exception as e:
        detail = str(e)
        if "invalid_client" in detail or "400" in detail or "401" in detail:
            return False, "Spotify rejected those credentials. Check for stray spaces."
        return False, f"Could not reach Spotify: {type(e).__name__}"
