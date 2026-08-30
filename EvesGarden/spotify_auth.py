"""Signing in to Spotify as a user, for reading playlists.

App-only credentials (the client-credentials flow) used to be enough to read
public playlists. Spotify has since tightened that: `/playlists/{id}/items`
now answers `401 Valid user authentication required` for *every* playlist,
public ones included. Reading a playlist therefore needs a real sign-in.

This runs the Authorization Code flow: the browser opens, you approve once,
and the refresh token is cached so it never asks again.

The Spotify app must list this exact redirect URI (Spotify requires the
loopback IP, not the word "localhost"):

    http://127.0.0.1:8888/callback
"""

import os
import threading

REDIRECT_URI = "http://127.0.0.1:8888/callback"

# Only what is needed to list playlists and read their tracks. No write
# scopes, nothing touching playback or the user's profile.
SCOPES = "playlist-read-private playlist-read-collaborative"

_lock = threading.Lock()


def cache_path(config_dir):
    return os.path.join(config_dir, ".spotify-user-token")


def _auth_manager(client_id, client_secret, config_dir, open_browser):
    from spotipy.oauth2 import SpotifyOAuth
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=REDIRECT_URI,
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
            # runs a one-shot local server on port 8888 to catch the redirect,
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
                f"this exact Redirect URI:\n    {REDIRECT_URI}")
    if "address already in use" in text.lower() or "10048" in text:
        return ("Port 8888 is already in use by another program. Close it and\n"
                "try again.")
    if "access_denied" in text.lower():
        return "Sign-in was cancelled."
    return f"Sign-in failed: {type(error).__name__}: {text[:160]}"
