"""Reading whatever a lyrics provider hands back.

This used to live inline in the player as eight lines that split on "]" and
hoped for the best, and it threw away more than it kept:

  - syncedlyrics.search() races its providers and returns whichever answers
    first, so a song with a perfectly good timed transcript on one provider
    would come back as Genius's plain text instead -- and the player, having
    no timings, showed "no synced timings for this track" and none of the
    words it had just been handed. The same song did this only sometimes,
    which is what made it look like the lyrics were truncated at random.
  - One line may carry several timestamps: [00:12.00][01:45.30]Same chorus.
    Only the first was read, and the second was left sitting in the text, so
    a compressed file lost every repeat.
  - [mm:ss:xx], the legacy hundredths form, has three colon-separated parts
    and was dropped on the floor.
  - A timed line with no words marks an instrumental break. Dropping those
    left the last line sung before the break highlighted through all of it,
    and the closing one highlighted to the end of the song.

Nothing here talks to the network except fetch(), which takes the search
function as an argument, so all of it can be tested.
"""

import re

# [mm:ss], [mm:ss.xx], [mm:ss.xxx] and the legacy [mm:ss:xx]. Requires digits
# before the colon, which is what keeps [ar:...], [ti:...] and [length:03:24]
# out without having to list them.
TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# Page furniture Genius serves with the words.
_CONTRIBUTORS = re.compile(r"^\d+\s+Contributors?\b", re.IGNORECASE)
_EMBED = re.compile(r"\d*Embed$")
_ALSO_LIKE = "You might also like"
_HEADER_TAIL = re.compile(r"\bLyrics$")

# A bracketed tag with no digits at all -- [Chorus], [Verse 2], [Spoken].
# Kept, because in plain lyrics they are the only structure there is.
_SECTION = re.compile(r"^\[[^\]]+\]$")


def _seconds(match):
    value = int(match.group(1)) * 60 + int(match.group(2))
    frac = match.group(3)
    if frac:
        value += int(frac) / float(10 ** len(frac))
    return value


def parse(raw):
    """Turn a provider's answer into (lines, synced).

    `lines` is [(seconds, text), ...] when synced, and [(None, text), ...]
    when the provider only had words. An empty text is a real entry: it is
    where the singing stops.
    """
    if not raw:
        return [], False

    timed, plain = [], []
    for line in raw.splitlines():
        rest, stamps = line, []
        while True:
            match = TIMESTAMP.match(rest.lstrip())
            if not match:
                break
            rest = rest.lstrip()[match.end():]
            stamps.append(_seconds(match))
        text = rest.strip()
        if stamps:
            # One entry per timestamp: a compressed file gives the chorus
            # once and lists every time it comes round.
            timed.extend((at, text) for at in stamps)
        else:
            plain.append(line)

    if timed:
        timed.sort(key=lambda pair: pair[0])
        return _drop_repeats(timed), True
    return [(None, text) for text in clean_plain(plain)], False


def _drop_repeats(timed):
    """Collapse consecutive entries that share a timestamp and a text.

    Providers sometimes repeat a line rather than emit two timestamps, and a
    duplicate reads as the player having stuck.
    """
    out = []
    for at, text in timed:
        if out and out[-1][0] == at and out[-1][1] == text:
            continue
        out.append((at, text))
    return out


def clean_plain(lines):
    """Strip the page around the words.

    Genius answers with a contributor count, a "<Title> Lyrics" heading, its
    own recommendations and an "Embed" counter welded to the last line.
    """
    out, in_header = [], True
    for line in lines:
        text = line.strip()
        if in_header:
            if not text:
                continue
            if _CONTRIBUTORS.match(text) or (_HEADER_TAIL.search(text)
                                             and not _SECTION.match(text)):
                continue
            in_header = False
        if text == _ALSO_LIKE:
            continue
        text = _EMBED.sub("", text).strip()
        # One blank line between verses, never a run of them.
        if not text and (not out or not out[-1]):
            continue
        out.append(text)
    while out and not out[-1]:
        out.pop()
    return out


def fetch(query, search):
    """Ask for a timed transcript first, and settle for words second.

    search() on its own returns whatever any provider answers with, so plain
    text could beat a real LRC purely on which server was quicker. Asking for
    synced explicitly takes the race out of it, and only once that comes back
    empty is plain text worth having.
    """
    for kwargs in ({"synced_only": True}, {"plain_only": True}):
        try:
            found = search(query, **kwargs)
        except Exception:
            continue
        if found:
            lines, synced = parse(found)
            if lines:
                return lines, synced
    return [], False
