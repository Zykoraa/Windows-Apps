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

import requests
from urllib3.util.retry import Retry

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

# How many times a refused call is tried again, and the longest it may wait
# between attempts.
RETRIES = 3
RETRY_AFTER_CAP = 8.0

# "Too many requests". Spotify's own code for it, and the one status this
# will not sit and wait on.
RATE_LIMIT = 429


class _CappedRetry(Retry):
    """urllib3's retry policy, with a bound on how long it will sleep.

    Spotify answers a rate limit with Retry-After, and it does not ask for
    seconds. A real one seen from this app was 41549 -- eleven and a half
    hours. urllib3 obeys that literally, inside the request, with a bare
    time.sleep(), so the call neither returns nor raises and requests_timeout
    does not apply: it bounds the socket, not the waiting. Every `except
    Exception` downstream is useless against it, because there is no
    exception -- the thread that went to read an album simply never comes
    back, and the UI sits on "Reading ..." until the app is killed.

    The cap is kept for anything else that carries the header, but a rate
    limit is not retried at all -- see api_session.
    """

    def is_retry(self, method, status_code, has_retry_after=False):
        if status_code == RATE_LIMIT:
            # Leaving 429 out of status_forcelist is not enough on its own:
            # urllib3 retries any Retry-After-bearing response whose status
            # is in RETRY_AFTER_STATUS_CODES regardless of the list, and 429
            # is one of those. This is the only place the decision can
            # actually be made.
            return False
        return super().is_retry(method, status_code, has_retry_after)

    def get_retry_after(self, response):
        # Still capped for 413 and 503, which are retried and can carry the
        # header too.
        wait = super().get_retry_after(response)
        if wait is None:
            return None
        return min(wait, RETRY_AFTER_CAP)


def api_session():
    """A requests session for Spotify, which will not sleep for hours.

    spotipy builds its own session with an uncapped retry policy unless it
    is handed one, so both clients are built on this instead.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=_CappedRetry(
        total=RETRIES,
        connect=None,
        read=False,
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        status=RETRIES,
        backoff_factor=0.3,
        # 429 is deliberately not in this list, though spotipy puts it
        # there. Being rate-limited is not a transient blip to sit out:
        # Spotify has asked for eleven hours before now, and three attempts
        # cannot outlast that -- they only turn an instant answer into a
        # long pause and then fail anyway. Letting it raise immediately
        # gives the caller a 429 it can explain and fall back from, rather
        # than the "Max Retries reached" that retrying produces, which
        # names no cause at all.
        status_forcelist=(500, 502, 503, 504)))
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


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
        # Without this Spotify silently re-approves whoever the browser is
        # already logged in as, so signing out and back in hands back the
        # same account and there is no way to change it from inside the app.
        # Only sign_in() opens a browser, and only that call needs asking --
        # a token refresh never reaches the authorize URL at all.
        show_dialog=open_browser,
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
            requests_session=api_session(),
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
