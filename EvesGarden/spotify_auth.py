"""Signing in to Spotify as a user, for reading playlists.

App-only credentials (the client-credentials flow) used to be enough to read
public playlists. Spotify has since tightened that: `/playlists/{id}/items`
now answers `401 Valid user authentication required` for *every* playlist,
public ones included. Reading a playlist therefore needs a real sign-in.

This runs the Authorization Code flow: the browser opens, you approve once,
and the refresh token is cached so it never asks again.

The Spotify app must list whichever redirect URI is in use. It defaults to
http://127.0.0.1:8888/callback, and SPOTIPY_REDIRECT_URI overrides that so an
existing redirect can be reused instead. Spotify requires the loopback IP
here, not the word "localhost".
"""

import os
import threading

# Spotify has to send the browser back somewhere after you approve, and the
# address must match one listed in your app's settings exactly. Set
# SPOTIPY_REDIRECT_URI in your .env to reuse a redirect you already have --
# any loopback address and path works, the port just has to be free.
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def redirect_uri():
    return (os.getenv("SPOTIPY_REDIRECT_URI", "").strip()
            or DEFAULT_REDIRECT_URI)


# Kept for callers that only want something to display.
REDIRECT_URI = DEFAULT_REDIRECT_URI

# Only what is needed to list playlists and read their tracks. No write
# scopes, nothing touching playback or the user's profile.
# user-library-read is what makes "Liked Songs" readable; it lives behind
# /me/tracks rather than being a real playlist.
SCOPES = ("playlist-read-private playlist-read-collaborative "
          "user-library-read")

_lock = threading.Lock()


def cache_path(config_dir):
    return os.path.join(config_dir, ".spotify-user-token")


def _auth_manager(client_id, client_secret, config_dir, open_browser):
    from spotipy.oauth2 import SpotifyOAuth
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri(),
        scope=SCOPES,
        cache_path=cache_path(config_dir),
        open_browser=open_browser,
    )


def is_signed_in(client_id, client_secret, config_dir):
    """True when a cached token exists and can be refreshed without asking."""
    if not client_id or not client_secret:
        return False
    if not os.path.exists(cache_path(config_dir)):
        return False
    try:
        auth = _auth_manager(client_id, client_secret, config_dir, False)
        token = auth.cache_handler.get_cached_token()
        return bool(auth.validate_token(token))
    except Exception:
        return False


def get_client(client_id, client_secret, config_dir):
    """A user-authenticated Spotify client, or None if not signed in yet.

    Never opens a browser -- call sign_in() for that.
    """
    if not is_signed_in(client_id, client_secret, config_dir):
        return None
    try:
        import spotipy
        return spotipy.Spotify(
            auth_manager=_auth_manager(client_id, client_secret, config_dir, False),
            requests_timeout=15,
            retries=3,
        )
    except Exception:
        return None


def sign_in(client_id, client_secret, config_dir):
    """Open the browser and complete the flow. Blocking; returns (ok, message).

    Call this from a worker thread -- it waits on the browser round-trip.
    """
    if not client_id or not client_secret:
        return False, "Set up your Spotify credentials first."

    with _lock:
        try:
            import spotipy
            auth = _auth_manager(client_id, client_secret, config_dir, True)
            # get_access_token drives the whole dance: it opens the browser,
            # runs a one-shot local server on the redirect port to catch it,
            # and exchanges the code for a token.
            auth.get_access_token(check_cache=True)
            client = spotipy.Spotify(auth_manager=auth, requests_timeout=15)
            who = client.current_user()
            name = who.get("display_name") or who.get("id") or "your account"
            return True, f"Signed in as {name}."
        except Exception as e:
            return False, _explain(e)


def sign_out(config_dir):
    path = cache_path(config_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


def _explain(error):
    """Turn Spotify's terser failures into something actionable."""
    text = str(error)
    if "INVALID_CLIENT" in text.upper() or "redirect" in text.lower():
        return ("Spotify rejected the redirect address. Open your app at\n"
                "https://developer.spotify.com/dashboard -> Settings, and add\n"
                f"this exact Redirect URI:\n    {redirect_uri()}")
    if "address already in use" in text.lower() or "10048" in text:
        port = redirect_uri().rsplit(":", 1)[-1].split("/")[0]
        return (f"Port {port} is already in use by another program. Close it,\n"
                " or point SPOTIPY_REDIRECT_URI at a different port.")
    if "access_denied" in text.lower():
        return "Sign-in was cancelled."
    return f"Sign-in failed: {type(error).__name__}: {text[:160]}"
