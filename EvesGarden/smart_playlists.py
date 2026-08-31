"""Playlists that are a question rather than a list.

The index already records when a track arrived, how many times it has been
played, when it was last played, when it was liked and how long it runs. All
of that sat there only powering sort orders, when the more useful thing to ask
is "what have I not heard in a while" -- a question whose answer changes on its
own, which a hand-built playlist can never do.

A rule is data, not code: a name, a WHERE clause and an ORDER BY. The clauses
are interpolated into SQL rather than bound, because they are structure rather
than values -- which is safe precisely because they all live in this file and
nothing user-supplied ever becomes one.
"""

import time

DAY = 86400.0


class SmartPlaylist:
    """One saved question about the library."""

    __slots__ = ("key", "name", "hint", "clause", "order", "_params")

    def __init__(self, key, name, hint, clause, order, params=None):
        self.key = key
        self.name = name
        self.hint = hint
        self.clause = clause
        self.order = order
        self._params = params

    def params(self):
        # Called per query, so "the last 30 days" means the last 30 days now
        # and not 30 days from whenever the app started.
        return tuple(self._params()) if self._params else ()

    def __repr__(self):
        return "<SmartPlaylist %s>" % self.key


def _ago(days):
    return time.time() - days * DAY


RULES = [
    SmartPlaylist(
        "recent-adds", "Recently added",
        "Everything from the last 30 days",
        "COALESCE(added, 0) >= ?", "added DESC",
        lambda: (_ago(30),)),
    SmartPlaylist(
        "never-played", "Never played",
        "In the library, but you have not listened to it yet",
        "COALESCE(play_count, 0) = 0", "COALESCE(added, 0) DESC"),
    SmartPlaylist(
        "forgotten", "Forgotten favourites",
        "Liked, but not played in a month",
        "liked = 1 AND COALESCE(last_played, 0) < ?",
        "COALESCE(last_played, 0) ASC",
        lambda: (_ago(30),)),
    SmartPlaylist(
        "on-repeat", "On repeat",
        "The ones you keep coming back to",
        "COALESCE(play_count, 0) >= 3", "play_count DESC"),
    SmartPlaylist(
        "this-week", "Heard this week",
        "Played in the last seven days",
        "COALESCE(last_played, 0) >= ?", "COALESCE(last_played, 0) DESC",
        lambda: (_ago(7),)),
    SmartPlaylist(
        "long-players", "Long players",
        "Anything over six minutes",
        "COALESCE(duration, 0) >= ?", "duration DESC",
        lambda: (360.0,)),
]


def by_key(key):
    return next((rule for rule in RULES if rule.key == key), None)
