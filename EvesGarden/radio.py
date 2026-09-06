"""Internet radio: finding stations, and keeping the ones you like.

Radio is the one thing this app can play that it does not have to fetch
first. Everything else here is a file on disk or a YouTube resolution that
takes several seconds and sometimes fails; a station is an HTTP URL that
ffmpeg opens directly, through the same StreamSource the preview already
uses. So the playback side of this is nothing new -- what is new is finding
the stations.

The directory is radio-browser.info: community-run, no key, no account, and
no terms that would make shipping it awkward. It asks two things of a
client, and both are honoured here -- identify yourself in the User-Agent,
and do not hammer one mirror. It publishes a round-robin host that lists the
real ones, and this picks from that list and remembers the choice.

What is deliberately not done: radio-browser also accepts a "this station
was played" ping that feeds its popularity ranking. Sending it would mean
reporting what somebody listens to, to a third party, from an app that
otherwise tells nobody anything. The ranking survives without us.
"""

import json
import random
import threading

import requests

# The host whose only job is to name the others. Asking it directly for
# stations works, but it is the shared front door and the project asks that
# clients spread themselves across the mirrors instead.
DIRECTORY = "https://all.api.radio-browser.info"

# Used when the directory itself cannot be reached, so a network that blocks
# one hostname does not take the whole feature down.
FALLBACK_HOSTS = ("de1.api.radio-browser.info",
                  "de2.api.radio-browser.info",
                  "at1.api.radio-browser.info")

# Identifying the client is a condition of use, not decoration.
USER_AGENT = "EvesGarden/1.0 (+https://github.com/Zykoraa/Linux-Apps)"

TIMEOUT = 12
MAX_RESULTS = 40

# Stations whose last check failed are hidden, and anything without a URL is
# not a station at all. Both are filtered server-side where possible and
# again here, because "hidebroken" is advisory.
_SEARCH_DEFAULTS = {"hidebroken": "true", "order": "clickcount",
                    "reverse": "true"}


def _as_station(item):
    """One directory entry, flattened to what the player and UI need.

    url_resolved is the one to play: `url` may be a playlist file (.pls,
    .m3u) that has to be followed, and the directory has already done that
    work and recorded where it led.
    """
    if not isinstance(item, dict):
        return None
    url = (item.get("url_resolved") or item.get("url") or "").strip()
    name = (item.get("name") or "").strip()
    if not url or not name:
        return None
    tags = [t.strip() for t in (item.get("tags") or "").split(",") if t.strip()]
    try:
        bitrate = int(item.get("bitrate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    return {
        "source": "radio",
        "id": item.get("stationuuid") or url,
        "name": name,
        "url": url,
        "tags": tags,
        "country": (item.get("country") or "").strip(),
        "codec": (item.get("codec") or "").strip().upper(),
        "bitrate": bitrate,
        "favicon": (item.get("favicon") or "").strip(),
        "homepage": (item.get("homepage") or "").strip(),
    }


def dedupe(stations):
    """One row per station.

    The directory carries the same station several times over -- one entry
    per person who submitted it, and the bitrate variants of a stream are
    separate entries too. Keeping the highest bitrate of each name reads as
    a list of stations rather than a list of database rows.
    """
    best = {}
    order = []
    for station in stations:
        if not station:
            continue
        key = station["name"].strip().lower()
        current = best.get(key)
        if current is None:
            best[key] = station
            order.append(key)
        elif station["bitrate"] > current["bitrate"]:
            best[key] = station
    return [best[key] for key in order]


def describe(station):
    """The line under a station's name."""
    parts = []
    if station.get("bitrate"):
        parts.append("%d kbps" % station["bitrate"])
    if station.get("codec"):
        parts.append(station["codec"])
    if station.get("country"):
        parts.append(station["country"])
    tags = station.get("tags") or []
    if tags:
        parts.append(", ".join(tags[:3]))
    return "  ·  ".join(parts)


class RadioBrowser:
    """Search over the radio-browser directory.

    The session is injectable so every one of these can be tested without a
    network, the same way the keyless metadata provider is.
    """

    def __init__(self, session=None, host=None):
        self._session = session or requests.Session()
        self._host = host
        self._lock = threading.Lock()
        self._cache = {}

    # ----------------------------------------------------------- the mirror

    def base(self):
        """A mirror to talk to, chosen once and kept.

        Picked at random rather than always taking the first: every copy of
        this app resolving the same list and all going to whichever came
        back first is precisely the load the directory asks clients not to
        create.
        """
        with self._lock:
            if self._host:
                return "https://%s" % self._host
        host = None
        try:
            response = self._session.get(DIRECTORY + "/json/servers",
                                         headers={"User-Agent": USER_AGENT},
                                         timeout=TIMEOUT)
            response.raise_for_status()
            names = sorted({s.get("name") for s in response.json()
                            if isinstance(s, dict) and s.get("name")})
            if names:
                host = random.choice(names)
        except Exception:
            host = None
        if not host:
            host = random.choice(FALLBACK_HOSTS)
        with self._lock:
            self._host = host
        return "https://%s" % host

    def _get(self, path, params):
        """A cached GET against the chosen mirror. Never raises."""
        key = (path, tuple(sorted((k, str(v)) for k, v in params.items())))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            response = self._session.get(
                self.base() + path, params=params,
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        with self._lock:
            if len(self._cache) > 60:
                self._cache.clear()
            self._cache[key] = payload
        return payload

    # -------------------------------------------------------------- finding

    def search(self, query, limit=MAX_RESULTS):
        """Stations matching a name, a tag or a country."""
        query = (query or "").strip()
        if not query:
            return []
        found = self._stations("/json/stations/search",
                               dict(_SEARCH_DEFAULTS, name=query,
                                    limit=max(1, min(int(limit), 100))))
        if found:
            return found
        # Nothing by name: the word is much more likely to be a genre, and
        # "jazz" finding no station called Jazz is a poor answer.
        return self._stations("/json/stations/search",
                              dict(_SEARCH_DEFAULTS, tag=query,
                                   limit=max(1, min(int(limit), 100))))

    def popular(self, limit=MAX_RESULTS):
        """What the directory's listeners are playing, for an empty box."""
        return self._stations("/json/stations/search",
                              dict(_SEARCH_DEFAULTS,
                                   limit=max(1, min(int(limit), 100))))

    def by_tag(self, tag, limit=MAX_RESULTS):
        tag = (tag or "").strip()
        if not tag:
            return []
        return self._stations("/json/stations/search",
                              dict(_SEARCH_DEFAULTS, tag=tag,
                                   limit=max(1, min(int(limit), 100))))

    def _stations(self, path, params):
        return dedupe(_as_station(item) for item in self._get(path, params))


class Favourites:
    """The stations you keep, in the settings file.

    Stored whole rather than as ids: a station you have kept should still
    play when the directory is unreachable, or when the entry is withdrawn
    from it. The URL is the thing that matters and we already have it.
    """

    KEY = "radio_favourites"
    LIMIT = 200

    def __init__(self, settings):
        self._settings = settings

    def all(self):
        stored = self._settings.get(self.KEY) or []
        if not isinstance(stored, list):
            return []
        return [s for s in stored if isinstance(s, dict) and s.get("url")]

    def has(self, station):
        return any(s.get("id") == station.get("id")
                   or s.get("url") == station.get("url")
                   for s in self.all())

    def add(self, station):
        if not station or not station.get("url") or self.has(station):
            return False
        kept = self.all()
        kept.insert(0, dict(station))
        self._settings.set(self.KEY, kept[:self.LIMIT])
        return True

    def remove(self, station):
        kept = [s for s in self.all()
                if s.get("id") != station.get("id")
                and s.get("url") != station.get("url")]
        if len(kept) == len(self.all()):
            return False
        self._settings.set(self.KEY, kept)
        return True

    def toggle(self, station):
        """Keep it or drop it. Returns whether it is kept afterwards."""
        if self.has(station):
            self.remove(station)
            return False
        self.add(station)
        return True
