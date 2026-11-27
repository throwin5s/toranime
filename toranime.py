#!/usr/bin/env python3
"""
toranime.py - stream anime in mpv through TorBox, English dubs included.

Pipeline:
  1. search a Stremio catalog addon -> kitsu id + episode list (episode numbers and titles)
  2. ask a Stremio stream addon for /stream/series/<kitsu id>:<episode> -> releases
  3. keep the cached ones (POST /torrents/checkcached) and, for --dub, the dual-audio releases
  4. add the pick to TorBox, take its cheapest file, GET /torrents/requestdl -> a plain
     https link on TorBox's CDN that mpv streams with range requests

Nothing is downloaded to disk and nothing is torrented from your machine: TorBox fetches
the release, mpv streams it. Many releases carry both audio tracks, so --dub / --sub just
tell mpv which one to pick. Links last about three hours, so a binge resolves a couple of
episodes ahead instead of the whole range.

Defaults: metadata from Anime Kitsu, streams from Torrentio. Point either at anything else
(AIOStreams included) in ~/.config/toranime/sources.json. The TorBox key is read from
~/.config/toranime/torbox.key. A file left under the old name (~/.config/reanime) is
still read when toranime's folder has none, so an install from before the rename works.

Usage:
  python toranime.py                                    # INTERACTIVE: search, pick audio + episode
  python toranime.py "one piece"                        # skip the search prompt
  python toranime.py "one piece" 1151 --dub             # play 1151, then a next/replay/prev menu
  python toranime.py "one piece" 1151-1160 --dub        # one mpv playlist of the whole range
  python toranime.py "one piece" 1151- --dub            # 1151 through the newest episode
  python toranime.py kitsu:10740 1151 --dub             # an id instead of a search
  python toranime.py kitsu:10740 --list                 # print every episode then exit
  python toranime.py "one piece" 1151 --dub --print     # print the stream url, don't launch mpv
  python toranime.py --setup                            # find or install mpv, then exit
  python toranime.py --doctor                           # check everything, for pasting into an issue
  python toranime.py --torbox                           # save your TorBox API key, then exit

Players:
  desktop : mpv, installed on first run if missing (Windows: the official release
            from github.com/mpv-player/mpv, then scoop/choco; brew/port;
            apt/dnf/pacman/zypper/apk/xbps/nix, flatpak as a last resort)
  android : under Termux, episodes open in the mpv-android app (is.xyz.mpv)

Interactive controls:
  pickers : type to narrow the list, up/down arrows to move, Enter to select, Esc to cancel
  menus   : press the number, or just Enter for the default
"""

import argparse
import codecs
import email.utils
import glob
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

__version__ = "2.2.18"

# what this is called: the command, the module and the per-user folders. files left by
# the old name (LEGACY) are still read, so an install made before the rename keeps its
# key, sources.json and history.
APP = "toranime"
LEGACY = "reanime"

# addons: a catalog/meta addon for what exists, a stream addon for releases. both speak
# the Stremio addon protocol, so any manifest.json works -- point `stream` at an
# AIOStreams config URL and nothing else changes.
ADDONS = {
    "meta": "https://anime-kitsu.strem.fun/manifest.json",
    "stream": "https://torrentio.strem.fun/manifest.json",
}
# Anime Kitsu's searchable catalog. other meta addons name theirs differently.
META_SEARCH = "catalog/anime/kitsu-anime-list/search={}.json"
TORBOX_API = "https://api.torbox.app/v1/api"
# kitsu id -> anilist id, for tracking. free, no key, one call per show (then cached).
ANILIST_MAP_API = "https://aiostreams.viren070.me/api/v1/anime"
UA = {"User-Agent": "Mozilla/5.0 (toranime.py)"}

LANGS = ("sub", "dub")
LANG_LABEL = {"dub": "English Dub", "sub": "Japanese audio, English subs"}

KEY_RE = re.compile(r"^[a-z][a-z0-9]*:\d+$")      # kitsu:10740, tt4508902, anilist:21
# release names that carry the English dub. torrents are named, not flagged, so this is
# how --dub finds a file with an English audio track (dual-audio files count)
DUB_WORDS = ("dual audio", "dual-audio", "dualaudio", "multi audio", "multi-audio",
             "multiaudio", "eng dub", "english dub", "eng-dub", "e.dub", "engdub")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm")
TORBOX_WAIT = 180        # seconds to wait for TorBox to fetch an uncached release
MAX_TRIES = 4            # releases to try before giving up on an episode
# SeaDex: a community list of the best release of a show, at releases.moe. read through
# its REST API (PocketBase), matched by info hash, and remembered for a day.
SEADEX_API = "https://releases.moe/api/collections/entries/records"
SEADEX_TTL = 24 * 3600   # a day: it changes when someone releases something better
SEADEX_FIELDS = ("alID,trs,expand.trs.infoHash,expand.trs.isBest,expand.trs.releaseGroup,"
                 "expand.trs.dualAudio,expand.trs.url,expand.trs.tags")
SEADEX = {}              # anilist id -> {"best": hashes, "alt": hashes} for this run
SEADEX_LOCK = threading.Lock()
NO_SEADEX = False        # --no-seadex
# a SeaDex pick TorBox has not cached is asked for anyway (so it is ready next time), but
# a play never waits on it for more than this: the best cached release plays meanwhile.
SEADEX_WAIT = 20

# ------------------------------------------------------------------ interactive
# single-key input: msvcrt on Windows, termios/tty on POSIX.
try:
    import msvcrt                       # Windows
except ImportError:
    msvcrt = None
try:
    import termios, tty, select         # POSIX
except ImportError:
    termios = None

CSI = "\x1b["
HL = CSI + "7m"       # reverse video (highlight)
DIM = CSI + "2m"
RST = CSI + "0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


PINK = CSI + "38;5;218m"    # sakura: SeaDex's picks wear this


def visible(text):
    """text with terminal colours stripped: what gets searched and counted."""
    return ANSI_RE.sub("", text)


def fit(text, width):
    """clip to `width` visible characters: colour escapes survive, and the row stays one
    screen line (the picker redraws by counting lines, so wrapping would break it)."""
    if not ANSI_RE.search(text):
        return text[:width]
    out, shown, i = [], 0, 0
    while i < len(text) and shown < width:
        m = ANSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(text[i])
        shown += 1
        i += 1
    return "".join(out) + RST


def interactive_ok():
    """true if we can drive a raw-key TUI on this terminal."""
    return (bool(msvcrt) or bool(termios)) and sys.stdin.isatty() and sys.stdout.isatty()


def _enable_vt():
    """turn on ANSI escape handling in the Windows console (no-op elsewhere)."""
    if msvcrt:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)


def _getkey():
    """block for one keypress. return ('char', c) or ('key', name)."""
    if msvcrt:
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):        # arrow / function-key prefix
            c2 = msvcrt.getwch()
            return "key", {"H": "up", "P": "down", "K": "left", "M": "right"}.get(c2, "")
        if c in ("\r", "\n"):
            return "key", "enter"
        if c == "\x08":
            return "key", "backspace"
        if c == "\x1b":
            return "key", "esc"
        if c == "\x03":
            raise KeyboardInterrupt
        return "char", c
    # POSIX: read one key in cbreak mode (OPOST kept so \n still maps to \r\n)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # not the default TCSAFLUSH: keep keys typed ahead
        b = _byte(fd)
        if b == 0x1b:                    # ESC alone, or the start of an arrow key
            b = _byte(fd, 0.05)
            if b not in (0x5b, 0x4f):    # not "[" or "O": ESC was pressed alone
                if b is not None:
                    _held.insert(0, b)
                return "key", "esc"
            while True:                  # parameters ("1;5" for Ctrl), then a final byte
                b = _byte(fd, 0.05)
                if b is None or 0x40 <= b <= 0x7e:
                    break
            return "key", {0x41: "up", 0x42: "down", 0x43: "right", 0x44: "left"}.get(b, "")
        if b in (0x0d, 0x0a):
            return "key", "enter"
        if b in (0x7f, 0x08):
            return "key", "backspace"
        if b == 0x03:
            raise KeyboardInterrupt
        decode = codecs.getincrementaldecoder(getattr(sys.stdin, "encoding", None) or "utf-8")("replace")
        c = decode.decode(bytes([b]))
        while not c:                     # the rest of a multi-byte character
            b = _byte(fd, 0.05)
            c = decode.decode(b"" if b is None else bytes([b]), final=b is None)
        return "char", c
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


_held = bytearray()                     # bytes read from the terminal, not handled yet


def _byte(fd, wait=None):
    """
    next byte typed, or None if nothing arrives within `wait` seconds. reads the
    file descriptor itself: sys.stdin's buffer would swallow the rest of an arrow
    key's escape sequence, and select() would then report ESC pressed alone.
    """
    if not _held:
        if wait is not None and not select.select([fd], [], [], wait)[0]:
            return None
        _held.extend(os.read(fd, 64))
        if not _held:
            raise EOFError
    return _held.pop(0)


def _sane_tty():
    """
    put the terminal back into plain line-input mode, so Enter ends a line again.
    a terminal that mpv (or any raw-mode program) was killed in never sees Enter:
    input() then just echoes ^M and waits forever.
    """
    if not termios:
        return
    try:
        fd = sys.stdin.fileno()
        mode = termios.tcgetattr(fd)
    except (OSError, ValueError, AttributeError, termios.error):    # not a terminal
        return
    mode[0] |= termios.ICRNL | termios.IXON | termios.BRKINT   # CR from Enter reads as \n
    mode[1] |= termios.OPOST
    mode[3] |= termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)
    except (OSError, ValueError, termios.error):
        pass


def ask(prompt):
    """input() with the terminal put back into line mode first."""
    _sane_tty()
    return input(prompt)


def _tty_state():
    """the terminal modes to put back after mpv has had the terminal."""
    if not termios:
        return None
    try:
        fd = sys.stdin.fileno()
        return fd, termios.tcgetattr(fd)
    except (OSError, ValueError, AttributeError, termios.error):
        return None


def _tty_restore(state):
    if state is None:
        return
    try:
        termios.tcsetattr(state[0], termios.TCSADRAIN, state[1])
    except (OSError, ValueError, termios.error):
        pass


def fuzzy_pick(items, label, header, noun="items", initial=0):
    """
    live-filter picker. type letters to narrow, up/down to move, Enter to select.
    `items` = list, `label(item)` -> str shown & searched. returns item or None.
    prefix matches rank above substring matches. `initial` = starting selection.
    """
    if not interactive_ok():
        return _fallback_pick(items, label, header)
    query, sel, drawn = "", initial, 0
    print(header)                       # printed once; only the lines below are redrawn

    def filtered():
        q = query.lower().replace(" ", "")
        if not q:
            return list(items)
        pre, sub = [], []
        for it in items:
            t = visible(label(it)).lower().replace(" ", "")
            if t.startswith(q):
                pre.append(it)
            elif q in t:
                sub.append(it)
        return pre + sub

    while True:
        # the redraw moves the cursor up one line per entry in `out`, so every entry
        # must stay exactly one screen line: clip to the width, and keep the block
        # shorter than the window so it never scrolls
        cols, lines = shutil.get_terminal_size()
        width = max(10, cols - 4)
        max_rows = max(3, min(15, lines - 5))

        view = filtered()
        sel = max(0, min(sel, len(view) - 1)) if view else 0
        # window of rows around selection
        start = max(0, min(sel - max_rows + 1, len(view) - max_rows)) if len(view) > max_rows else 0
        rows = view[start:start + max_rows]

        hint = "type to filter · ↑↓ move · Enter select · Esc cancel"[:cols - 1]
        out = [f"{DIM}{hint}{RST}{CSI}K", f"> {query[-width:]}{CSI}K"]
        for i, it in enumerate(rows):
            real = start + i
            mark = HL + " > " if real == sel else "   "
            out.append(f"{mark}{fit(label(it), width)}{RST}{CSI}K")
        if not view:
            out.append(f"{DIM}  (no match){RST}{CSI}K")
        out.append(f"{DIM}  {len(view)}/{len(items)} {noun}{RST}{CSI}K")

        if drawn:
            sys.stdout.write(f"{CSI}{drawn}A")   # move cursor up to redraw
        sys.stdout.write("\r" + "\n".join(out) + f"{CSI}J\n")
        sys.stdout.flush()
        drawn = len(out)

        kind, val = _getkey()
        if kind == "char":
            query += val
            sel = 0
        elif val == "backspace":
            query = query[:-1]
            sel = 0
        elif val == "up":
            sel -= 1
        elif val == "down":
            sel += 1
        elif val == "enter":
            return view[sel] if view else None
        elif val == "esc":
            return None


def menu_pick(options, default_idx, header):
    """numbered menu. press 1-9 to pick, Enter for default. returns index."""
    print(header)
    for i, (lbl, _) in enumerate(options):
        d = f" {DIM}(default){RST}" if i == default_idx else ""
        print(f"  {i + 1}. {lbl}{d}")
    if not interactive_ok():
        raw = ask(f"choice [{default_idx + 1}]: ").strip()
        return default_idx if not raw else max(0, min(int(raw) - 1, len(options) - 1))
    sys.stdout.write(f"{DIM}press number, or Enter for default: {RST}")
    sys.stdout.flush()
    while True:
        kind, val = _getkey()
        if kind == "key" and val == "enter":
            idx = default_idx
            break
        if kind == "char" and val.isdigit() and 1 <= int(val) <= len(options):
            idx = int(val) - 1
            break
    print(f"{options[idx][0]}")
    return idx


def _fallback_pick(items, label, header):
    """no-msvcrt fallback: type a substring, plain input()."""
    print(header)
    q = ask("filter (substring): ").strip().lower()
    hits = [it for it in items if q in visible(label(it)).lower()] or list(items)
    for i, it in enumerate(hits):
        print(f"  {i + 1}. {label(it)}")
    raw = ask("number: ").strip()
    return hits[int(raw) - 1] if raw else hits[0]


# ------------------------------------------------------------------ addons

RETRY_ATTEMPTS = 2       # extra tries for a transient failure: 429, 5xx or a timeout
RETRY_DELAY = 0.5        # seconds before the first retry; it doubles after that


def _retry_after(e):
    """a Retry-After header on an HTTP error -> seconds to wait, or None."""
    raw = (getattr(e, "headers", None) or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:                                        # the other allowed form: an HTTP date
        when = email.utils.parsedate_tz(str(raw))
        return max(0.0, email.utils.mktime_tz(when) - time.time()) if when else None
    except (TypeError, ValueError, OverflowError):
        return None


def _transient(e):
    """is this HTTP failure worth another try? 429 and 5xx are; a 4xx is not."""
    code = getattr(e, "code", None) or 0
    return code == 429 or 500 <= code < 600


def fetch(url, headers=None, data=None, timeout=30, retries=0):
    """GET (or POST when `data` is given) -> the response body as text.

    `retries` covers transient failures: 429, any 5xx and a timeout. a GET is also
    retried when the connection drops; a POST only when the server answered with a
    status, since a timed-out write may already have been applied. A Retry-After
    header wins over the backoff. The last failure is the one raised."""
    req = urllib.request.Request(url, data, {**UA, **(headers or {})})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if attempt == retries or not _transient(e):
                raise
            wait = _retry_after(e)
            e.close()
            time.sleep(RETRY_DELAY * 2 ** attempt if wait is None else wait)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            if attempt == retries or data is not None:
                raise
            time.sleep(RETRY_DELAY * 2 ** attempt)


def fetch_json(url, headers=None, data=None, timeout=30, retries=0):
    return json.loads(fetch(url, headers, data, timeout, retries))


def config_dir():
    """where the TorBox key and the addon list live."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def config_file(name):
    """a config file: this install's, else the one the old name left behind. only that
    first read falls back; nothing is copied, and a new install writes only here."""
    path = os.path.join(config_dir(), name)
    old = os.path.join(os.path.dirname(config_dir()), LEGACY, name)
    return path if os.path.exists(path) or not os.path.exists(old) else old


def sources():
    """{"meta": manifest url, "stream": manifest url}, overridden by sources.json."""
    cfg = dict(ADDONS)
    try:
        with open(config_file("sources.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for key in cfg:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                cfg[key] = value.strip()
    return cfg


PICK = {}                   # --pick: {"prefer_groups": [...]} for the rest of this run


def prefs():
    """release preferences: ~/.config/toranime/prefs.json plus this run's --pick.
    {"prefer_groups": ["Sokudo"], "avoid_groups": ["Judas"], "min_height": 1080,
    "max_size": 8}. max_size is GB per release. missing, broken or empty -> no prefs."""
    data = {}
    try:
        with open(config_file("prefs.json"), encoding="utf-8") as fh:
            loaded = json.load(fh)
        data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    out = {}
    for key in ("prefer_groups", "avoid_groups"):
        names = data.get(key) if isinstance(data.get(key), list) else []
        out[key] = [str(n).strip() for n in names if str(n).strip()]
    for key in ("min_height", "max_size"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            out[key] = value
    if PICK.get("prefer_groups"):             # --pick wins for this run
        out["prefer_groups"] = list(PICK["prefer_groups"]) + out["prefer_groups"]
    return out


# your own subtitle looks: subs.json names a look, --style (or prefs.json) applies it.
# the fields are ASS style fields, so anything mpv understands can be set here too.
STYLE_FIELDS = {
    "font": "FontName", "size": "FontSize", "bold": "Bold", "italic": "Italic",
    "outline": "Outline", "shadow": "Shadow", "margin_v": "MarginV", "border": "BorderStyle",
    "primary": "PrimaryColour", "outline_colour": "OutlineColour", "back_colour": "BackColour",
}
NATIVE_STYLE = ("", "native", "off", "none", "release")


def styles_file():
    return config_file("subs.json")


def load_styles():
    """the looks in subs.json -> {"roboto": {"font": "Roboto Medium", ...}}.
    a missing, empty or broken file is not an error: it just means no looks yet."""
    try:
        with open(styles_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    table = data.get("presets") if isinstance(data.get("presets"), dict) else {
        name: spec for name, spec in data.items() if name != "presets"}
    return {str(name): spec for name, spec in table.items()
            if isinstance(spec, dict) and any(spec.get(k) not in (None, "") for k in STYLE_FIELDS)}


def pref_style():
    """the style name saved in prefs.json as "subtitle_style", or "" for the default."""
    try:
        with open(config_file("prefs.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return str(data.get("subtitle_style") or "").strip() if isinstance(data, dict) else ""


def default_style():
    """the look kept loaded on every run: the one prefs names, else the only/first one."""
    name = pref_style()
    if name:
        return name
    looks = load_styles()
    return next(iter(looks), "")


STYLE_INDEX = {name: i for i, name in enumerate((
    "Name", "FontName", "FontSize", "PrimaryColour", "SecondaryColour", "OutlineColour",
    "BackColour", "Bold", "Italic", "Underline", "StrikeOut", "ScaleX", "ScaleY",
    "Spacing", "Angle", "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL",
    "MarginR", "MarginV", "Encoding"))}
BURN = {}           # the look to bake into the subtitle files this run ({} = mpv's flags)
LOOK_SELECTED = False   # start on the restyled copy, or leave the release's subs selected
SUBS_TIMEOUT = 300  # seconds for pulling a subtitle track out of a stream
SIGNS_RATE = 2.0    # cues per minute: dialogue runs ~15, a signs track a couple at most
SIGNS_SHARE = 0.15  # share of the sample spent showing subtitles
SIGNS_SAMPLE = 300  # seconds of the episode read to decide


def style_spec(name):
    """a named look from subs.json -> its fields; {} for the release's own styling."""
    name = str(name or "").strip()
    if name.lower() in NATIVE_STYLE:
        return {}
    styles = load_styles()
    return dict(styles.get(name) or next((v for k, v in styles.items()
                                         if k.lower() == name.lower()), {}))


def styled_ass(text, spec):
    """a subtitle file's text with `spec` applied to every style, borders scaled.
    returns "" when that text is not an ASS script. rewriting the file is the only way
    to make a border scale the way the release's own styles do: mpv's style overrides
    do not scale it, so the same border number renders differently per file."""
    if "[Script Info]" not in text or "[V4+ Styles]" not in text:
        return ""
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("Style:"):
            head, _, rest = line.partition("Style: ")
            parts = rest.rstrip("\n").split(",")
            if len(parts) >= len(STYLE_INDEX):
                for key, value in spec.items():
                    field = STYLE_FIELDS.get(key)
                    if field in STYLE_INDEX and value not in (None, ""):
                        parts[STYLE_INDEX[field]] = str(value)
                line = head + "Style: " + ",".join(parts) + "\n"
        out.append(line)
    if not any(line.startswith("ScaledBorderAndShadow") for line in out):
        for i, line in enumerate(out):
            if line.startswith("PlayResY"):
                out.insert(i + 1, "ScaledBorderAndShadow: yes\n")
                break
    return "".join(out)


def subs_tag(video, url, spec):
    """a stable name for one episode's baked copy: the file it comes from, not its url.
    stream urls carry a rotating token, so keying on the url re-baked every launch."""
    ident = str(video.get("infoHash") or url.split("?")[0])
    return hashlib.sha1((ident + "|" + json.dumps(spec, sort_keys=True)).encode()).hexdigest()[:16]


def subs_path(video, url, spec):
    return os.path.join(cache_dir(), "subs", subs_tag(video, url, spec) + ".ass")


def restyle_subs(url, spec, video=None, quick=False):
    """the episode's own subtitles with the look baked in -> a file path, or None.
    needs ffmpeg to pull the track out of the stream; without it, or for a track that
    isn't ASS, playback falls back to mpv's style overrides. a copy already baked for
    this file is reused, and `quick` refuses to do the slow work at all."""
    if not spec or not shutil.which("ffmpeg"):
        return None
    video = video or {}
    path = subs_path(video, url, spec)
    if os.path.exists(path):
        return path
    if quick:
        return None
    track = "0:s:m:language:eng"      # the English track, whatever order it sits in
    try:
        done = subprocess.run(["ffmpeg", "-v", "error", "-i", url, "-map", track,
                               "-c:s", "copy", "-f", "ass", "-"],
                              capture_output=True, timeout=SUBS_TIMEOUT)
        if not done.stdout:            # no stream tagged eng: take the first one
            done = subprocess.run(["ffmpeg", "-v", "error", "-i", url, "-map", "0:s:0",
                                   "-c:s", "copy", "-f", "ass", "-"],
                                  capture_output=True, timeout=SUBS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    text = styled_ass(done.stdout.decode("utf-8", "replace"), spec)
    if not text:
        return None
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None
    return path


def ass_seconds(stamp):
    """an ASS timestamp (0:01:02.34) -> seconds."""
    try:
        hours, minutes, seconds = stamp.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0


def subtitle_spans(text, seconds):
    """how long each cue of an ASS file is on screen, clipped to the sampled seconds."""
    spans = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start, end = ass_seconds(parts[1]), ass_seconds(parts[2])
        if end > start:
            spans.append(max(0.0, min(end, seconds) - start))
    return spans


def looks_like_signs(text, seconds):
    """a sparse, brief subtitle file: signs, songs and title cards rather than speech."""
    spans = subtitle_spans(text, seconds)
    if not spans:
        return False
    return (len(spans) / (seconds / 60) < SIGNS_RATE
            and sum(spans) / seconds < SIGNS_SHARE)


def sub_streams(url):
    """a file's subtitle streams and its runtime -> ([{index, lang, title, forced}], secs)."""
    if not shutil.which("ffprobe"):
        return [], 0.0
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
             "stream=index:stream_tags=language,title:stream_disposition=forced"
             ":format=duration", "-of", "json", url],
            capture_output=True, timeout=SUBS_TIMEOUT)
        data = json.loads(done.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], 0.0
    found = []
    for stream in data.get("streams") or []:
        tags, flags = stream.get("tags") or {}, stream.get("disposition") or {}
        found.append({"index": stream.get("index"), "lang": tags.get("language", ""),
                      "title": tags.get("title", ""), "forced": bool(flags.get("forced"))})
    try:
        runtime = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        runtime = 0.0
    return found, runtime


def extract_subs(url, index, seconds):
    """the first `seconds` of one subtitle stream as ASS text, or ""."""
    if not shutil.which("ffmpeg"):
        return ""
    try:
        done = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{seconds:g}", "-i", url,
                               "-map", f"0:{index}", "-c:s", "copy", "-f", "ass", "-"],
                              capture_output=True, timeout=SUBS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace")


def signs_cache():
    try:
        with open(os.path.join(cache_dir(), "signs.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_signs(key, path):
    data = signs_cache()
    data[key] = path
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        with open(os.path.join(cache_dir(), "signs.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def is_english_lang(lang):
    lang = str(lang or "").lower()
    return lang in ("eng", "en") or lang.startswith("en-")


SIGNS_WORDS = ("sign", "song", "forced", "karaoke")   # how releases name these tracks


def signs_title(title):
    title = str(title or "").lower()
    return any(word in title for word in SIGNS_WORDS)


def signs_file(url, video):
    """a dub's signs/songs track, pulled out of a release that never labelled one.
    most files name or force-flag those tracks, and pick-signs.lua takes them from there;
    this only runs when a file has several unlabelled English tracks to choose from, and
    it decides by measuring them: speech is dense, signs are a handful of short cues.
    the verdict is remembered per file, so an episode is sampled once."""
    key = str(video.get("infoHash") or url)
    cached = signs_cache()
    if key in cached:
        path = cached[key]
        return path if path and os.path.exists(path) else None
    streams, runtime = sub_streams(url)
    english = [s for s in streams if is_english_lang(s.get("lang"))]
    if not english:                      # a file that never tagged its tracks
        english = [s for s in streams if str(s.get("lang") or "") in ("", "und")][:3]
    labelled = any(s.get("forced") or signs_title(s.get("title")) for s in english)
    if labelled or len(english) < 2:      # the release said which track is which
        remember_signs(key, "")
        return None
    seconds = max(1.0, min(runtime or SIGNS_SAMPLE, SIGNS_SAMPLE))
    sparsest = None
    for stream in english:
        text = extract_subs(url, stream["index"], seconds)
        spans = subtitle_spans(text, seconds)
        if spans and (sparsest is None or sum(spans) < sparsest[0]):
            sparsest = (sum(spans), text, len(spans))
    if not sparsest:
        remember_signs(key, "")
        return None
    covered, text, cues = sparsest
    if cues / (seconds / 60) >= SIGNS_RATE or covered / seconds >= SIGNS_SHARE:
        remember_signs(key, "")      # the sparsest track is still dialogue: no signs here
        return None
    folder = os.path.join(cache_dir(), "subs")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "signs-" + hashlib.sha1(key.encode()).hexdigest()[:16] + ".ass")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None
    remember_signs(key, path)
    return path


def extra_subs(url, video, lang, quick=False):
    """the subtitle file to add for this episode: the look's copy, or a dub's signs track.
    `quick` only takes a copy that is already baked, so playback never waits for ffmpeg."""
    if lang == "dub":
        return signs_file(url, video)
    return restyle_subs(url, BURN, video, quick) if BURN else None


def style_args(name):
    """mpv flags that apply one of your subtitle looks; [] keeps the release's own.
    fonts are read from the config folder's fonts/ too, so a look can bring its own."""
    name = str(name or "").strip()
    spec = style_spec(name)
    if not spec:
        if name.lower() not in NATIVE_STYLE:
            known = ", ".join(sorted(load_styles())) or "none defined"
            sys.stderr.write(f"no subtitle style {name!r} in {styles_file()} ({known}) - "
                             "keeping the release's own styling\n")
        return []
    fields = [f"{STYLE_FIELDS[k]}={spec[k]}" for k in STYLE_FIELDS if spec.get(k) not in (None, "")]
    args = []
    if spec.get("font"):
        args.append(f"--sub-font={spec['font']}")          # plain subs (srt) have no ASS style
    args.append("--sub-ass-override=yes")
    args.append("--sub-ass-style-overrides=" + ",".join(fields))
    fonts = os.path.join(config_dir(), "fonts")
    if os.path.isdir(fonts):
        args.append(f"--sub-fonts-dir={fonts}")
    return args


def look_load_args(name):
    """mpv flags that make a look's copy usable without restyling the release's own subs."""
    if not style_spec(name):
        return []
    fonts = os.path.join(config_dir(), "fonts")
    return [f"--sub-fonts-dir={fonts}"] if os.path.isdir(fonts) else []


def addon_url(manifest, path):
    """a manifest url + a resource path -> that resource's url."""
    base = manifest.split("?")[0]
    if base.endswith("/manifest.json"):
        base = base[:-len("/manifest.json")]
    return base.rstrip("/") + "/" + path.lstrip("/")


def addon_get(manifest, path, timeout=30):
    return fetch_json(addon_url(manifest, path), timeout=timeout,
                      retries=RETRY_ATTEMPTS)


def title_of(t):
    if isinstance(t, str):
        return t
    t = t or {}
    return t.get("english") or t.get("user_preferred") or t.get("romaji") or t.get("native") or "?"


def titles(t):
    """every title variant (english, romaji, native, ...) as a list."""
    return [t] if isinstance(t, str) else [v for v in (t or {}).values() if isinstance(v, str)]


def epnum(ep):
    n = ep.get("number", ep.get("episode_number", 0))
    return str(int(n)) if float(n).is_integer() else str(n)


def search(query):
    """query -> catalog entries from the meta addon (kitsu ids)."""
    data = addon_get(sources()["meta"], META_SEARCH.format(urllib.parse.quote(query)))
    return [m for m in (data.get("metas") or []) if isinstance(m, dict) and m.get("id")]


def search_label(r):
    extra = [str(x) for x in (r.get("animeType"), r.get("releaseInfo")) if x]
    return (r.get("name") or "?") + (f"  ({', '.join(extra)})" if extra else "")


def episode_of(v):
    """a meta addon video -> the episode dict the picker, ranges and mpv titles use.
    "episode" stays the within-season number when the addon also gives an absolute one:
    a release counts within its season even when the show is numbered from episode 1."""
    n = v.get("absoluteEpisode") or v.get("episode") or 0
    return {"number": n, "episode": v.get("episode") or 0, "title": v.get("title") or "",
            "season": v.get("season") or 1,
            "filler": bool(v.get("filler") or v.get("isFiller")), "imdb_id": v.get("imdb_id"),
            "imdbSeason": v.get("imdbSeason"), "imdbEpisode": v.get("imdbEpisode"),
            "_id": v.get("id")}


def load_anime(key):
    """kitsu:10740 -> (anime, episodes) from the meta addon; None when it has no such show."""
    key = key if ":" in key else f"kitsu:{key}"
    try:
        data = addon_get(sources()["meta"], f"meta/series/{urllib.parse.quote(key)}.json")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None
        raise
    meta = data.get("meta")
    if not isinstance(meta, dict) or not meta.get("name"):
        return None
    anime = {"_key": key, "title": meta.get("name"), "year": meta.get("year"),
             "kitsu_id": str(meta.get("kitsu_id") or key.split(":", 1)[1]),
             "imdb_id": meta.get("imdb_id"), "anilist_id": meta.get("anilist_id")}
    eps = [episode_of(v) for v in (meta.get("videos") or []) if isinstance(v, dict)]
    if not eps and isinstance(meta.get("episodes"), int):
        # some meta addons give a count instead of a list (a show still airing, say)
        eps = [{"number": n, "title": "", "season": 1, "_id": None}
               for n in range(1, meta["episodes"] + 1)]
    return anime, eps


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def find_anime(query, auto):
    """query -> (anime, episodes). `auto` takes the best search hit instead of asking."""
    if query and (KEY_RE.match(query) or query.isdigit()):
        got = load_anime(query)          # kitsu:10740, or a bare kitsu id; else search
        if got:
            return got
    while True:
        if not query:
            query = ask("search anime: ").strip()
            if not query:
                sys.exit("cancelled")
        results = search(query)
        if not results:
            if auto or not interactive_ok():
                sys.exit(f"no anime matches '{query}'")
            print(f"no results for '{query}'")
            query = ""
            continue
        if auto or len(results) == 1 or not interactive_ok():
            exact = [r for r in results
                     if norm(query) in {norm(n) for n in
                                        [r.get("name") or ""] + list(r.get("aliases") or [])}]
            pick = (exact or results)[0]
            sys.stderr.write(f"using {pick.get('name')}  ({pick['id']})\n")
        else:
            pick = fuzzy_pick(results, search_label, "select an anime:", noun="results")
            if pick is None:
                sys.exit("cancelled")
        got = load_anime(pick["id"])
        if not got:
            sys.exit(f"the meta addon has no episodes for {pick['id']}")
        return got


def stream_text(s):
    """everything an addon says about a stream: for ranking and for dub detection."""
    hints = s.get("behaviorHints") or {}
    text = " ".join(str(x) for x in (s.get("name"), s.get("title"), s.get("description"),
                                     hints.get("filename")) if x)
    return " ".join(text.split())          # addons put line breaks in their labels


def stream_ids(anime, ep):
    """the ids to ask the stream addon for, best first: the meta addon's own, then imdb."""
    ids = []
    if ep.get("_id"):
        ids.append(ep["_id"])
    if anime.get("kitsu_id"):
        ids.append(f"kitsu:{anime['kitsu_id']}:{epnum(ep)}")
    imdb = ep.get("imdb_id") or anime.get("imdb_id")
    if imdb:
        ids.append(f"{imdb}:{ep.get('imdbSeason') or ep.get('season') or 1}:"
                   f"{ep.get('imdbEpisode') or epnum(ep)}")
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def streams(anime, ep):
    """every stream the addon returns for this episode, each tagged with the id used."""
    addon = sources()["stream"]
    for sid in stream_ids(anime, ep):
        path = f"stream/series/{urllib.parse.quote(sid)}.json"
        try:
            data = addon_get(addon, path, timeout=45)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                continue
            raise
        found = [dict(s, _id=sid) for s in (data.get("streams") or [])
                 if isinstance(s, dict) and (s.get("infoHash") or s.get("url"))]
        if found:
            return found
    return []


def is_dub(stream):
    """does this release advertise an English audio track? (torrents are named, not tagged)"""
    text = stream_text(stream).lower()
    return any(word in text for word in DUB_WORDS)


# release names put the group in the first [bracket]. these are the tags that are not one.
QUALITY_TAGS = {
    "bd", "bdrip", "bluray", "blu-ray", "bdremux", "web", "webdl", "web-dl", "webrip",
    "hdtv", "dvd", "tv", "special", "ova", "movie", "complete", "batch", "unofficial",
    "hevc", "x265", "x264", "h264", "h265", "av1", "10bit", "8bit", "12bit", "hi10p",
    "dual", "dual-audio", "multi", "multi-audio", "multi-subs", "multi-sub", "dual-audio+multi-subs",
    "multiple", "subtitle", "subtitles", "aac", "ac3", "eac3", "flac", "opus", "dts",
    "hdr", "dv", "sdr", "sub", "subs", "dub", "uncensored", "remux", "repack",
    # languages and regions, which releases also put in brackets
    "eng", "english", "jpn", "japanese", "jap", "rus", "ru", "en", "es", "fr", "de",
    "it", "pt", "br", "lat", "mx", "us", "uk", "cn", "tw", "kr", "kor", "chi", "ext",
}


def tag_is_not_a_group(tag):
    """is this bracket a quality, source or language tag rather than the group's name?
    [TV], [BDRip+WEB], [MX-EN-PT], [10 + 2 из 10 + 2] and [1080p] are all not groups."""
    low = re.sub(r"\([^)]*\)", " ", tag).strip().lower().strip(".")
    if re.fullmatch(r"\d{3,4}p", low) or low in QUALITY_TAGS:
        return True
    if re.fullmatch(r"[0-9A-Fa-f]{6,}", tag.strip()):
        return True                # a file id or hash fragment, not a group
    # every separator a tag uses: [TV+Special], [MX-EN-PT], [BD 1080p]. a real group
    # survives it: [-ZR-] -> ["-zr-"], [F-R] -> ["f", "r"], [Erai-raws] -> two words
    parts = [p for p in re.split(r"[+_,;/\s.-]+", low) if p]
    if not parts:
        return True
    def known(part):
        return part in QUALITY_TAGS or bool(re.fullmatch(r"\d{3,4}p", part))

    return any(p.isdigit() for p in parts) or all(known(p) for p in parts)


def release_group(stream):
    """the fansub group in a release name: its first [tag] that is not a quality tag.
    None when the addon names no group, so preferences simply do not match it."""
    text = str(stream.get("title") or stream.get("name") or "")
    for tag in re.findall(r"\[([^\[\]]{1,24})\]", text):
        tag = tag.strip()
        if not tag or " " in tag:
            continue
        if tag_is_not_a_group(tag):
            continue
        return tag
    return None


def release_source(stream):
    """the tracker Torrentio names in its label (⚙️ NyaaSi). for a release that names no
    group there is still something to show, and it is at least true."""
    m = re.search("\u2699\ufe0f?\\s*([A-Za-z0-9][A-Za-z0-9.!+_-]{0,23})", stream_text(stream))
    return m.group(1) if m else ""


def group_name(stream):
    """the first column of a release row: its group, else where it came from."""
    group = release_group(stream)
    return f"[{group}]" if group else release_source(stream)


def size_bytes(text):
    """the size a release label prints (💾 1.31 GB) -> bytes, or None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|GiB|MB|MiB)\b", text, re.I)
    if not m:
        return None
    return int(float(m.group(1)) * {"tb": 1024 ** 4, "gb": 1024 ** 3, "gib": 1024 ** 3,
                                    "mb": 1024 ** 2, "mib": 1024 ** 2}[m.group(2).lower()])


def stream_rank(stream, cached, want=None, seadex=None):
    """a release's sort key: the groups you asked for first, then SeaDex's picks, then
    cached (instant), then resolution. min_height and max_size are penalties, not filters:
    a release you capped out is still used when nothing better exists."""
    want = want or {}
    text = stream_text(stream)
    low = text.lower()
    height = 2160 if re.search(r"\b(2160p|4k|uhd)\b", low) else 1080 if "1080p" in low \
        else 720 if "720p" in low else 0
    group = (release_group(stream) or "").lower()
    prefer = [str(g).lower() for g in want.get("prefer_groups") or ()]
    avoid = [str(g).lower() for g in want.get("avoid_groups") or ()]
    floor, cap = want.get("min_height") or 0, want.get("max_size") or 0
    size = size_bytes(text)
    h = str(stream.get("infoHash") or "").lower()
    picked = (seadex or {}).get("best") or ()
    tier = 0 if height >= 2160 else 1 if height >= 1080 else 2 if height >= 720 else 3
    return (0 if group and group in prefer else 1,
            0 if h and h in picked else 1,
            0 if h and h in cached else 1,
            1 if group and group in avoid else 0,
            1 if floor and height and height < floor else 0,
            1 if cap and size and size > cap * 1024 ** 3 else 0,
            tier)


class TorboxError(RuntimeError):
    """TorBox refused: `code` is its error (DUPLICATE_ITEM, MONTHLY_LIMIT, ...)."""

    def __init__(self, code, detail):
        super().__init__(detail or code or "TorBox request failed")
        self.code = code


def refused(code, detail, hint=True):
    """a TorboxError, with a pointer at the key file when the key is the problem.
    `hint` is off while a pasted key is being checked: it isn't in that file yet."""
    detail = detail or "TorBox request failed"
    if hint and ("token" in detail.lower() or str(code or "").lower() in ("bad_token", "auth_error")):
        detail += (f" (check the key in {os.path.join(config_dir(), 'torbox.key')}"
                   " — torbox.app > Settings > API)")
    return TorboxError(code, detail)


def saved_key():
    """the TorBox API key on disk, or "" when there is none."""
    try:
        with open(config_file("torbox.key"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def key_help():
    """how to save the key by hand, in the command this shell has."""
    new = os.path.join(config_dir(), "torbox.key")
    # Windows has neither printf nor chmod
    return (f"  New-Item -ItemType Directory -Force '{config_dir()}' | Out-Null; "
            f"Set-Content -NoNewline '{new}' 'KEY'" if os.name == "nt" else
            f"  mkdir -p {config_dir()} && printf '%s' 'KEY' > {new} && chmod 600 {new}")


def torbox_token():
    """the API key from ~/.config/toranime/torbox.key (or the old name's folder)."""
    token = saved_key()
    if not token:
        sys.exit("no TorBox API key. create one at torbox.app > Settings > API, then save it "
                 "with:\n  toranime --torbox\nor by hand:\n" + key_help())
    return token


def torbox(path, query=None, body=None, raw=None, content_type=None, timeout=30, token=None):
    """one TorBox API call -> its `data`. raises TorboxError when TorBox refuses.
    `token` checks a key before it is saved; by default the saved one is used."""
    saved = token is None
    token = token or torbox_token()
    url = TORBOX_API + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    try:
        reply = json.loads(fetch(url, headers, data, timeout, retries=RETRY_ATTEMPTS))
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read().decode("utf-8", "replace"))
        except (ValueError, OSError):
            said = {}
        raise refused(said.get("error"), said.get("detail") or f"TorBox HTTP {e.code}",
                      hint=saved) from None
    if not reply.get("success", True):
        raise refused(reply.get("error"), reply.get("detail"), hint=saved)
    return reply.get("data")


TORBOX_PLANS = {0: "free", 1: "essential", 2: "pro", 3: "standard"}


def torbox_who(token):
    """the account `token` belongs to -> {"email": ..., "plan": ...}. raises
    TorboxError when TorBox refuses the key."""
    data = torbox("/user/me", token=token, timeout=15) or {}
    plan = data.get("plan")
    try:
        plan = TORBOX_PLANS.get(int(plan), "")
    except (TypeError, ValueError):
        plan = ""
    return {"email": str(data.get("email") or ""), "plan": plan}


def who_label(who):
    """what a checked key shows: the account's email and plan, when TorBox said."""
    email, plan = (who or {}).get("email", ""), (who or {}).get("plan", "")
    if email and plan:
        return f"{email} (plan: {plan})"
    return email or "your account"


def torbox_login():
    """save the TorBox API key: paste it once, and it is checked before it is written."""
    import getpass
    existing = saved_key()
    who = None
    if existing:
        try:
            who = torbox_who(existing)
            print(f"already logged in to TorBox as {who_label(who)}.")
        except TorboxError:
            print("the key saved now is no longer accepted by TorBox.")
        except (OSError, ValueError) as e:
            print(f"couldn't check the key saved now ({e}).")
        print("paste a new key to replace it, or press Enter to keep what is there.")
    key = ""
    for _ in range(3):           # a cut-short paste should not mean starting over
        try:
            raw = getpass.getpass("TorBox API key (torbox.app > Settings > API): ")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nno key given: nothing changed")
        if not raw.strip():
            if existing:
                print(f"keeping the key already saved ({who_label(who) if who else 'as it is'})")
                return
            sys.exit("no key given: nothing changed")
        key = clean_token(raw)
        if not key:
            sys.exit("no key given: nothing changed")
        try:
            who = torbox_who(key)
        except TorboxError as e:
            print(f"TorBox rejected that key ({e}): copy the whole key from "
                  "torbox.app > Settings > API and paste it again.")
            key = ""
            continue
        except (OSError, ValueError) as e:
            sys.exit(f"TorBox didn't answer: {e}\nnothing was saved")
        break
    if not key or not who:
        sys.exit("not saved. you can also write the key yourself:\n" + key_help()
                 if not existing else "not saved: the key already there is unchanged")
    os.makedirs(config_dir(), exist_ok=True)
    try:
        os.chmod(config_dir(), 0o700)
    except OSError:
        pass                      # Windows has no such bits: %APPDATA% is already yours
    path = os.path.join(config_dir(), "torbox.key")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    try:
        os.chmod(path, 0o600)     # a file made earlier by hand may be more open than that
    except OSError:
        pass
    print(f"logged in to TorBox as {who_label(who)}. toranime can use it now")


def torbox_logout():
    """remove the TorBox API key, here and from the folder the old name left."""
    removed = False
    for path in (os.path.join(config_dir(), "torbox.key"),
                 os.path.join(os.path.dirname(config_dir()), LEGACY, "torbox.key")):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    if not removed:
        print("no TorBox key saved")
        return
    print("removed the TorBox key. to revoke it as well: torbox.app > Settings > API")


def multipart(fields):
    """the multipart/form-data body createtorrent wants -> (bytes, content type)."""
    boundary = "----toranime" + secrets.token_hex(8)
    out = b""
    for name, value in fields.items():
        if value is None:
            continue
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def magnet_of(stream):
    """the release's infoHash and trackers -> a magnet link."""
    link = f"magnet:?xt=urn:btih:{stream['infoHash']}"
    for src in stream.get("sources") or []:
        if isinstance(src, str) and src.startswith("tracker:"):
            link += "&tr=" + urllib.parse.quote(src[len("tracker:"):], safe="")
    return link


def torbox_cached(hashes):
    """which of `hashes` TorBox already holds, and so can play at once."""
    hashes = [h for h in {str(h).lower() for h in hashes if h}
              if re.fullmatch(r"[0-9a-f]{40}", h)]
    cached = set()
    for i in range(0, len(hashes), 100):        # checkcached takes about 100 at a time
        data = torbox("/torrents/checkcached",
                      {"hash": ",".join(hashes[i:i + 100]), "format": "list"})
        for item in data if isinstance(data, list) else []:
            if isinstance(item, str):
                cached.add(item.lower())
            elif isinstance(item, dict) and item.get("hash") and item.get("cached", True):
                cached.add(str(item["hash"]).lower())
    return cached


def torbox_lookup(info_hash):
    """the torrent already in the account with this hash, or None."""
    data = torbox("/torrents/mylist", {"bypass_cache": "true", "limit": 1000}) or []
    for t in data if isinstance(data, list) else []:
        if str(t.get("hash") or "").lower() == info_hash.lower():
            return t
    return None


def torbox_add(stream, allow_fetch=False):
    """add a release to the account (or reuse it) -> its torrent id. `allow_fetch` lets
    TorBox start downloading one it has not cached: that is how a release becomes
    playable next time, at the cost of it sitting in the account until it arrives."""
    info_hash = str(stream["infoHash"]).lower()
    known = torbox_lookup(info_hash)
    if known and known.get("id"):
        return int(known["id"])
    raw, ctype = multipart({"magnet": magnet_of(stream), "allow_zip": "false",
                            "add_only_if_cached": "false" if allow_fetch else "true"})
    try:
        data = torbox("/torrents/createtorrent", raw=raw, content_type=ctype, timeout=60) or {}
    except TorboxError as e:
        if e.code != "DUPLICATE_ITEM":
            raise
        again = torbox_lookup(info_hash)
        if not again or not again.get("id"):
            raise
        return int(again["id"])
    tid = data.get("torrent_id") or data.get("id")
    if not tid:
        raise TorboxError(None, "TorBox did not return a torrent id")
    return int(tid)


def torbox_files(torrent):
    """a torrent record -> its video files (the whole pack, for a season torrent)."""
    files = [f for f in (torrent.get("files") or [])
             if str(f.get("name") or "").lower().endswith(VIDEO_EXT)]
    return files or [f for f in (torrent.get("files") or []) if f.get("id") is not None]


def torbox_state(torrent_id):
    """(torrent record, its files). raises LookupError when TorBox has lost it."""
    data = torbox("/torrents/mylist", {"id": torrent_id, "bypass_cache": "true"}) or {}
    torrent = (data[0] if data else {}) if isinstance(data, list) else data
    if not isinstance(torrent, dict) or not torrent:
        raise LookupError("TorBox does not have that torrent any more")
    return torrent, torbox_files(torrent)


def file_name_of(name):
    """just the file's own name: a torrent's folder can say "Seasons 1-2" and must not
    be mistaken for an episode number."""
    return os.path.basename(str(name or "").replace("\\", "/"))


def file_season(name):
    """the season a file name states, or None."""
    m = re.search(r"(?i)\bS(\d{1,2})[\s._-]*E(?:P)?\d", file_name_of(name))
    return int(m.group(1)) if m else None


def episode_in(name, number):
    """does this file name carry episode `number`? (a season pack holds the whole run)"""
    base = file_name_of(name)
    stated = re.search(r"(?i)\bS\d{1,2}[\s._-]*E(?:P)?(\d{1,4})(?:v\d)?\b", base)
    if stated:                       # the file says which episode it is: trust it
        return int(stated.group(1)) == int(number)
    stated = re.search(r"(?i)\bE(?:P)?(\d{1,4})(?:v\d)?\b", base)
    if stated:
        return int(stated.group(1)) == int(number)
    return bool(re.search(rf"(?i)(?:^|[^0-9])0*{re.escape(str(number))}(?:v\d)?(?:[^0-9]|$)",
                          base))


PACK_EPISODE_RE = re.compile(r"(?i)\bE(?:P)?\d{1,4}(?:v\d)?\b")


def pick_file(files, number, season=None, episode=None):
    """the file for this episode: one named for it, that season first, else the only
    video (a single-file release states no episode).

    the meta addon may number episodes absolutely (1151) while a release counts within
    its season (S02E05), so `episode` is that season-relative number when the addon has
    one. a multi-file release that numbers its files but holds none for this episode is
    refused: its biggest file would be some other episode."""
    if not files:
        return None

    def name(f):
        return str(f.get("name") or "")

    named = [f for f in files if episode_in(name(f), number)]
    if not named and season is not None and episode and str(episode) != str(number):
        named = [f for f in files if file_season(name(f)) in (None, season)
                 and episode_in(name(f), episode)]
    if not named:
        if len(files) > 1 and any(file_season(name(f)) is not None
                                  or PACK_EPISODE_RE.search(name(f)) for f in files):
            raise LookupError(f"that release has no file for episode {number}")
        return max(files, key=lambda f: f.get("size") or 0)
    if season is not None:
        same = [f for f in named if file_season(name(f)) == season]
        loose = [f for f in named if file_season(name(f)) is None]
        named = same or loose or named
    return max(named, key=lambda f: f.get("size") or 0)


def human_size(size):
    size = float(size or 0)
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.0f} MB"
    return f"{size / 1024:.0f} KB"


def torbox_link(torrent_id, file_id=None):
    """a direct https link for one file. signed, short-lived, and never printed."""
    query = {"token": torbox_token(), "torrent_id": torrent_id}
    if file_id is not None:
        query["file_id"] = file_id
    data = torbox("/torrents/requestdl", query)
    url = data if isinstance(data, str) else (data or {}).get("url")
    if not isinstance(url, str) or not url.startswith("http"):
        raise TorboxError(None, "TorBox did not return a download link")
    return url


def downloaded(torrent):
    """has TorBox finished with this torrent, so a link can be asked for?"""
    state = str((torrent or {}).get("download_state") or (torrent or {}).get("state") or "").lower()
    return bool((torrent or {}).get("download_finished") or (torrent or {}).get("download_present")
                or state in ("cached", "completed", "downloaded", "uploading"))


def wait_download(torrent_id, verbose=True):
    """wait for TorBox to finish a release it had not cached yet -> (record, files)."""
    deadline = time.monotonic() + TORBOX_WAIT
    shown = False
    while True:
        torrent, files = torbox_state(torrent_id)
        state = str(torrent.get("download_state") or torrent.get("state") or "").lower()
        if downloaded(torrent):
            if shown:
                sys.stderr.write("\n")
            return torrent, files
        if state in ("error", "failed", "missingfiles", "download_error"):
            raise LookupError(f"TorBox could not fetch this release ({state})")
        if time.monotonic() > deadline:
            raise LookupError(f"TorBox is still fetching this release after {TORBOX_WAIT}s")
        if verbose:
            shown = True
            pct = float(torrent.get("progress") or 0) * 100
            sys.stderr.write(f"\r  TorBox is fetching the release: {state or 'queued'} "
                             f"{pct:.0f}%{CSI}K")
            sys.stderr.flush()
        time.sleep(3)


def warm_pick(stream, budget=0):
    """ask TorBox to fetch a SeaDex pick even when it is not cached, and give it `budget`
    seconds. True when it can play now; either way TorBox keeps fetching it, so the pick
    is cached the next time it is wanted. never raises."""
    try:
        tid = torbox_add(stream, allow_fetch=True)
    except (LookupError, RuntimeError, OSError, ValueError):
        return False
    deadline = time.monotonic() + max(0, budget)
    while True:
        try:
            torrent, _files = torbox_state(tid)
        except (LookupError, RuntimeError, OSError, ValueError):
            return False
        if downloaded(torrent):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def resolve_torrent(stream, number, season=None, verbose=True, episode=None):
    """one release -> (url, info) through TorBox."""
    tid = torbox_add(stream)
    torrent, files = torbox_state(tid)
    if not (torrent.get("download_finished") or torrent.get("download_present")):
        torrent, files = wait_download(tid, verbose)
    f = pick_file(files, number, season, episode)
    if not f:
        raise LookupError("no video file in that release")
    return torbox_link(tid, f.get("id")), {
        "sizeHuman": human_size(f.get("size")), "filename": f.get("name"),
        "infoHash": str(stream["infoHash"]).lower()}


def resolve(anime, ep, lang, verbose=True):
    """episode -> (stream url, file info). raises LookupError/RuntimeError/OSError."""
    number = epnum(ep)
    found = streams(anime, ep)
    if not found:
        raise LookupError(f"no releases for episode {number}")
    if lang == "dub":
        found = [s for s in found if is_dub(s)]
        if not found:
            raise LookupError(f"no dub release for episode {number} (sub releases exist)")
    torrents = [s for s in found if s.get("infoHash")]
    direct = [s for s in found if not s.get("infoHash") and s.get("url")]
    cached = torbox_cached([s["infoHash"] for s in torrents]) if torrents else set()
    want = prefs()
    picked = seadex_for(anime)
    torrents.sort(key=lambda s: stream_rank(s, cached, want, picked))
    season = ep.get("imdbSeason") or ep.get("season")
    within = (ep.get("imdbEpisode") if ep.get("imdbSeason") else None) or ep.get("episode")
    # SeaDex first, but never at the price of three silent minutes: an uncached pick is
    # asked for in the background (it leads from the next play on) while a cached release
    # plays now. only when nothing is cached is the pick waited on, since it is all there is.
    pick = next((s for s in torrents
                 if str(s.get("infoHash") or "").lower() in picked["best"]), None)
    if pick and str(pick.get("infoHash") or "").lower() not in cached:
        rest = [s for s in torrents if s is not pick]
        if any(str(s.get("infoHash") or "").lower() in cached for s in rest):
            # something can play right now: ask for the pick without standing in its way
            in_background(warm_pick, pick, 0)
            torrents = rest
            if verbose:
                sys.stderr.write(f"  SeaDex's pick {group_name(pick)} is not cached yet: "
                                 f"playing the best cached release now, it leads once it "
                                 f"has arrived\n")
        else:
            warm_pick(pick, SEADEX_WAIT)     # nothing else can play: waiting is the only way
    errors = []
    for stream in torrents[:MAX_TRIES]:
        try:
            return resolve_torrent(stream, number, season, verbose, within)
        except (LookupError, RuntimeError, OSError, ValueError) as e:
            errors.append(f"{stream_text(stream)[:60]}: {e}")
    for stream in direct:               # an addon that proxies (AIOStreams) needs no TorBox
        url = stream.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url, {"sizeHuman": "?", "filename":
                         (stream.get("behaviorHints") or {}).get("filename")}
    detail = " | ".join(errors[:2]) if errors else "nothing cached on TorBox"
    raise LookupError(f"no playable release for episode {number}: {detail}")


def group_candidates(anime, ep, lang):
    """the best release of each group, best first, and the set TorBox has cached."""
    found = streams(anime, ep)
    if lang == "dub":
        found = [s for s in found if is_dub(s)]
    if not found:
        return [], set()
    hashes = [s["infoHash"] for s in found if s.get("infoHash")]
    cached = torbox_cached(hashes) if hashes else set()
    picked = seadex_for(anime)
    found.sort(key=lambda s: stream_rank(s, cached, prefs(), picked))
    best, seen = [], set()
    for s in found:
        group = release_group(s)
        # releases with no group are not one group: each stays its own row
        key = group.lower() if group else "~" + stream_text(s)[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        best.append((group, s))
    return best, cached


GROUP_MENU = 8        # numbered rows for release groups, before typing to narrow


def group_label(group, s, cached, seadex=None):
    """one line per release group: its best release's cache state, size and name."""
    text = stream_text(s)
    size = size_bytes(text)
    state = "cached" if str(s.get("infoHash") or "").lower() in cached else "not cached"
    tag = seadex_tag(s, seadex)
    tag = tag + " " if tag else ""
    room = 54 - (10 if tag else 0)          # the pink tag must not push the row wider
    name = f"[{group}]" if group else release_source(s)
    return (f"{name:<14} {state:<10} {human_size(size) if size else '?':>9}  "
            f"{tag}{text[:max(12, room)]}")


def choose_group(anime, ep, lang):
    """after the episode is picked: one numbered line per release group, Enter for the
    best cached one. the chosen group is preferred for the rest of the run. past
    GROUP_MENU groups the numbered keys run out, so the groups become a picker you
    narrow by typing instead."""
    if not interactive_ok():
        return
    try:
        groups, cached = group_candidates(anime, ep, lang)
    except (LookupError, RuntimeError, OSError, ValueError):
        return                      # the menu is a convenience: never block playback
    if len(groups) < 2:
        return
    picked = seadex_for(anime)
    if len(groups) > GROUP_MENU:
        chosen = fuzzy_pick(groups, lambda g: group_label(g[0], g[1], cached, picked),
                            f"\nreleases for {ep_label(ep)}:", noun="release groups")
        group = chosen[0] if chosen else None
    else:
        options = [(group_label(g, s, cached, picked), g) for g, s in groups]
        now = f" (now [{groups[0][0]}])" if groups[0][0] else ""
        options.append((f"top pick{now} - press Enter", None))
        group = options[menu_pick(options, len(options) - 1,
                                  f"\nreleases for {ep_label(ep)}:")][1]
    if group:
        PICK["prefer_groups"] = [group]
        sys.stderr.write(f"preferring [{group}] for the rest of this run\n")


def pick_release(anime, ep, lang):
    """--pick: show this episode's releases, and prefer the chosen one's group from here on."""
    found = streams(anime, ep)
    if lang == "dub":
        found = [s for s in found if is_dub(s)]
    if not found:
        sys.stderr.write(f"no {lang} releases for episode {epnum(ep)} to pick from\n")
        return
    hashes = [s["infoHash"] for s in found if s.get("infoHash")]
    cached = torbox_cached(hashes) if hashes else set()
    picked = seadex_for(anime)
    found.sort(key=lambda s: stream_rank(s, cached, prefs(), picked))

    def label(s):
        text = stream_text(s)
        size = size_bytes(text)
        state = "cached" if str(s.get("infoHash") or "").lower() in cached else "not cached"
        tag = seadex_tag(s, picked)
        tag = tag + " " if tag else ""
        room = 54 - (10 if tag else 0)
        return (f"{group_name(s):<14} {human_size(size) if size else '?':>9} "
                f"{state:<10} {tag}{text[:max(12, room)]}")

    chosen = fuzzy_pick(found[:25], label, f"\n{ep_label(ep)}: releases (best first):",
                        noun="releases")
    if not chosen:
        return
    group = release_group(chosen)
    if group:
        PICK["prefer_groups"] = [group]
    sys.stderr.write(f"release: {stream_text(chosen)[:100]}\n")
    if group:
        sys.stderr.write(f"preferring [{group}] for the rest of this run\n")


def resolve_many(anime, eps, lang, quiet=False):
    """resolve a range in parallel, keeping order. failed episodes are reported and skipped."""
    if not quiet:
        sys.stderr.write(f"resolving {len(eps)} episode{'s' if len(eps) != 1 else ''}...\n")
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(resolve, anime, ep, lang, len(eps) == 1 and not quiet) for ep in eps]
        out = []
        for ep, fut in zip(eps, futs):
            try:
                out.append((ep, *fut.result()))
            except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
                sys.stderr.write(f"  skipping episode {epnum(ep)}: {e}\n")
    return out


# ------------------------------------------------------------------ playback

# some releases flag the episode title card or a signs-only track as the default
# English subtitles, so --slang alone shows next to nothing. this mpv script picks
# the English track by its title instead, before mpv selects tracks. if it can't
# load (no Lua, unreadable file), mpv simply picks the way it always did.
SUB_PICKER = """\
-- written by toranime (sub mode): pick full English subtitles over partial tracks, and
-- keep a restyled copy of them loaded as a second track to flip to with S
local partial = {"episode name", "episode title", "sign", "song", "forced", "karaoke", "dubtitle"}

local function is_partial(t)
    if t.forced then return true end
    local title = (t.title or ""):lower()
    for _, word in ipairs(partial) do
        if title:find(word, 1, true) then return true end
    end
    return false
end

local function is_english(t)
    local lang = (t.lang or ""):lower()
    return lang == "eng" or lang == "en" or lang:sub(1, 3) == "en-"
end

-- runs after a file opens and before mpv selects its tracks. file-local, so each
-- episode in a playlist gets its own pick
local full, signs, look   -- this file's English tracks, and our restyled copy of them
local want_look = false   -- S pressed before the copy was there to switch to

mp.add_hook("on_preloaded", 50, function()
    full, signs, look, want_look = nil, nil, nil, false
    if mp.get_property("options/sid") ~= "auto" then return end
    for _, t in ipairs(mp.get_property_native("track-list") or {}) do
        if t.type == "sub" then
            if t.external then
                look = t                        -- the copy opened alongside this file
            elseif is_english(t) then
                if is_partial(t) then
                    if not signs then signs = t end
                elseif not full or (t.default and not full.default) then
                    full = t
                end
            end
        end
    end
    local want_look = look and mp.get_opt("toranime-look") == "select"
    if look and (want_look or not full) then
        mp.set_property("file-local-options/sid", tostring(look.id))
    elseif full then
        mp.set_property("file-local-options/sid", tostring(full.id))
    elseif signs then
        mp.set_property("file-local-options/sid", tostring(signs.id))
    end
end)

-- S flips between the release's own subtitles and the restyled copy, mid-episode.
-- looked up live: the copy can be added after the file started (baked in the background)
local function find_copy()
    if look then return look end       -- the copy this file opened with
    local newest                       -- else one that arrived while it played
    for _, t in ipairs(mp.get_property_native("track-list") or {}) do
        if t.type == "sub" and t.external and (not newest or t.id > newest.id) then newest = t end
    end
    return newest
end

-- forced: mpv's own S is a screenshot (and a config can bind S too), which would
-- otherwise win and leave the subtitles alone
mp.add_forced_key_binding("S", "toranime-look", function()
    local copy = find_copy()
    if not copy then
        if not mp.get_opt("toranime-look") then
            mp.osd_message("toranime: no subtitle look is loaded (--style NAME)", 4)
            return
        end
        want_look = true
        mp.osd_message("toranime: your look is still being made - switching when it arrives", 4)
        return
    end
    want_look = false
    if tostring(mp.get_property("sid")) == tostring(copy.id) then
        local back = full or signs
        mp.set_property("file-local-options/sid", back and tostring(back.id) or "no")
        mp.osd_message("subtitles: the release's own")
    else
        mp.set_property("file-local-options/sid", tostring(copy.id))
        mp.osd_message("subtitles: your look")
    end
end)

-- the copy is usually baked while the episode plays: switch to it as soon as it lands
mp.observe_property("track-list", "native", function()
    if not want_look then return end
    local copy = find_copy()
    if not copy then return end
    want_look = false
    mp.set_property("file-local-options/sid", tostring(copy.id))
    mp.osd_message("subtitles: your look", 2)
end)
"""

# over a dub the dialogue subtitles should stay off, but signs and songs still need
# showing. same idea as SUB_PICKER, inverted: pick a partial track, or nothing at all.
SIGNS_PICKER = """\
-- written by toranime (dub mode): show signs and songs, keep dialogue subtitles off
local partial = {"sign", "song", "forced", "karaoke"}
local title_card = {"episode name", "episode title", "dubtitle"}

local function mentions(t, words)
    local title = (t.title or ""):lower()
    for _, word in ipairs(words) do
        if title:find(word, 1, true) then return true end
    end
    return false
end

local function is_partial(t)
    return t.forced or mentions(t, partial)
end

local function is_english(t)
    local lang = (t.lang or ""):lower()
    return lang == "eng" or lang == "en" or lang:sub(1, 3) == "en-"
end

-- runs after a file opens and before mpv selects its tracks. file-local, so each
-- episode in a playlist gets its own pick
local full, signs    -- this file's dialogue subtitles, and the signs/songs track to show

mp.add_hook("on_preloaded", 50, function()
    full, signs = nil, nil
    local best, other
    for _, t in ipairs(mp.get_property_native("track-list") or {}) do
        if t.type == "sub" then
            if t.external then
                signs = t              -- a signs track pulled out of this file (see signs_file)
            elseif is_english(t) then
                if is_partial(t) then
                    if not best or (t.forced and not best.forced) then best = t end
                    if not other or (t.forced and not other.forced) then other = t end
                elseif not mentions(t, title_card) then
                    -- a title card ("Episode Name") is not dialogue: S skips it
                    if not full or (t.default and not full.default) then full = t end
                end
            end
        end
    end
    signs = signs or best or other
    if mp.get_property("options/sid") ~= "auto" then return end
    -- no signs track in this file: no subtitles, rather than the full dialogue ones
    mp.set_property("file-local-options/sid", signs and tostring(signs.id) or "no")
end)

-- S over a dub: the full English subtitles on, and back to the signs and songs after.
-- forced, so mpv's screenshot binding (or a config's) cannot take the key
mp.add_forced_key_binding("S", "toranime-look", function()
    if not full then
        mp.osd_message("toranime: no English dialogue subtitles in this file", 3)
        return
    end
    if tostring(mp.get_property("sid")) == tostring(full.id) then
        mp.set_property("file-local-options/sid", signs and tostring(signs.id) or "no")
        mp.osd_message(signs and "subtitles: signs and songs only" or "subtitles: off", 2)
    else
        mp.set_property("file-local-options/sid", tostring(full.id))
        mp.osd_message("subtitles: full English", 2)
    end
end)
"""

# many files mark the opening and the ending as chapters, under all sorts of names.
# this mpv script jumps past the opening, and past the ending and preview: to an extra
# after the ending if there is one, else to the next episode. files without chapters
# just play through. Tab skips by hand.
CHAPTER_SKIPPER = """\
-- written by toranime: skip openings, and endings and previews (to an extra after the
-- ending if there is one, else to the next episode).
-- Tab skips by hand: to the next chapter, or 85s (an opening) in files without them.
-- titles seen on toranime's files: "Opening Song", "Intro", "To Be Continued & Ending
-- Song", "Credits", "On The Next episode". whole words only, so "Introduction" stays.
-- "Intro" is an opening only in files that name no chapter "Opening" or "OP": some
-- (Jujutsu Kaisen) call the story before the opening "Intro". openings are matched
-- before endings, so "Opening Credits" is an opening
local OPENING = {"opening", "op%d*"}
local INTRO = {"intro"}
local ENDING = {"ending", "credits", "outro", "ed%d*", "preview", "next episode", "next time"}
local KEY, BY_HAND, NEAR = "TAB", 85, 150

local function matches(title, words)
    title = (title or ""):lower()
    for _, w in ipairs(words) do
        if title:find("%f[%w]" .. w .. "%f[%W]") then return true end
    end
    return false
end

local kinds, kinds_of = {}, nil     -- chapter number -> "opening" / "ending", for a file

local function classify(path)
    if kinds_of == path then return kinds end
    local list, named = mp.get_property_native("chapter-list") or {}, false
    kinds, kinds_of = {}, path
    for i, c in ipairs(list) do
        if matches(c.title, OPENING) then
            kinds[i - 1], named = "opening", true
        elseif matches(c.title, ENDING) then
            kinds[i - 1] = "ending"
        end
    end
    for i, c in ipairs(list) do
        if not named and not kinds[i - 1] and matches(c.title, INTRO) then
            kinds[i - 1] = "opening"
        end
    end
    return kinds
end

local at, opened        -- the chapter playing now, and the file it started in
local waiting = false   -- reached an ending before the next episode was queued
local seeking, settled = false, 0

local function next_episode()
    local pos = mp.get_property_number("playlist-pos", 0)
    waiting = pos + 1 >= mp.get_property_number("playlist-count", 0)
    if not waiting then
        mp.msg.info("ending: next episode")
        mp.osd_message("skipped the ending  (< to go back)", 3)
        mp.command("playlist-next")
    end
end

-- past chapter ch to the next one that isn't an opening, ending or preview (after an
-- ending that keeps extras like Jujutsu Kaisen's "Juju Stroll"), else to the next
-- episode once only endings and previews are left
local function skip_from(path, ch, hint)
    local list, kinds = mp.get_property_native("chapter-list") or {}, classify(path)
    local skipped = list[ch + 1] and list[ch + 1].title or "to the next chapter"
    for j = ch + 1, #list - 1 do
        if not kinds[j] then
            mp.osd_message("skipped " .. skipped .. hint, 3)
            return mp.commandv("seek", list[j + 1].time, "absolute+exact")
        end
    end
    for j = math.max(ch, 0), #list - 1 do
        if kinds[j] == "ending" then return next_episode() end
    end
end

local function check()
    local path, ch = mp.get_property("path"), mp.get_property_number("chapter")
    local key = path and ch and (path .. "#" .. ch)
    if key == at then return end
    at, waiting = key, false
    if not key then return end
    local list = mp.get_property_native("chapter-list") or {}
    local c, what, first = list[ch + 1], classify(path)[ch], opened ~= path
    opened = path
    if not (c and what) then return end
    -- skip a chapter playback runs into, or the one a file opens in (a resume). never
    -- one the viewer seeks or jumps to (PgDn back to an opening plays it), but rewind
    -- to before it and it is skipped again
    local pos = mp.get_property_number("time-pos")
    local by_seek = seeking or mp.get_time() - settled < 0.5
    if not first and (by_seek or (pos and pos > c.time + 2)) then return end
    mp.msg.info("skipped " .. c.title)
    skip_from(path, ch, what == "opening" and "  (PgDn to watch it)" or "")
end

-- by hand: past the chapter playing (and any opening, ending or preview right after
-- it), unless the next chapter is far off or there are none: then an opening's worth
mp.add_key_binding(KEY, "skip", function()
    local path, ch = mp.get_property("path"), mp.get_property_number("chapter", -1)
    local pos = mp.get_property_number("time-pos")
    if not (path and pos) then return end
    local list = mp.get_property_native("chapter-list") or {}
    local c, after = list[ch + 1], list[ch + 2]
    if classify(path)[ch] or (after and after.time - pos <= NEAR) then
        mp.msg.info("skip key: " .. (c and c.title or "start"))
        skip_from(path, ch, "")
    else
        mp.msg.info("skip key: " .. BY_HAND .. "s")
        mp.osd_message("skipped " .. BY_HAND .. "s", 2)
        mp.commandv("seek", BY_HAND, "relative+exact")
    end
end)

mp.register_event("seek", function() seeking = true end)
mp.register_event("playback-restart", function() seeking, settled = false, mp.get_time() end)
-- where the ending starts, for toranime to count quitting during it as watched
mp.register_event("file-loaded", function()
    local path, start = mp.get_property("path"), 0
    for i, c in ipairs(mp.get_property_native("chapter-list") or {}) do
        if classify(path)[i - 1] == "ending" then start = c.time break end
    end
    mp.set_property_native("user-data/toranime/ending", start)
    check()
end)
mp.observe_property("chapter", "number", check)
mp.observe_property("playlist-count", "number", function()
    if waiting then next_episode() end
end)
"""


def cache_dir():
    """per-user cache folder. not the temp folder: other users can write there, and a
    flatpak mpv only sees its own private /tmp."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "toranime")


def mpv_script(name, source):
    """write an mpv script into the cache folder (only when it changed).
    its path, or None if it can't be written."""
    path = os.path.join(cache_dir(), name)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == source:
                return path
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(source)
    except OSError:
        return None
    return path


def sub_picker():
    return mpv_script("pick-subs.lua", SUB_PICKER)


def signs_picker():
    return mpv_script("pick-signs.lua", SIGNS_PICKER)


def skip_args():
    """mpv options that load CHAPTER_SKIPPER, or none if it can't be written."""
    path = mpv_script("skip-chapters.lua", CHAPTER_SKIPPER)
    return [f"--script={path}"] if path else []


def mpv_args(lang):
    if lang == "dub":
        # dual-audio files flag the full English subs as default; a dub wants the signs
        # and songs only, so the picker takes a partial track or turns subtitles off
        picker = signs_picker()
        return ["--alang=eng,en"] + ([f"--script={picker}"] if picker else ["--sid=no"])
    picker = sub_picker()
    args = ["--alang=jpn,ja", "--slang=eng,en"] + ([f"--script={picker}"] if picker else [])
    if picker and BURN:               # a look is set up: its copy arrives now or later
        args.append("--script-opts=toranime-look=" + ("select" if LOOK_SELECTED else "auto"))
    return args


def ep_title(name, ep):
    t = ep.get("title") or ""
    return f"{name} - {epnum(ep)}" + (f" - {t}" if t else "")


def ep_label(ep):
    tag = "  [filler]" if ep.get("filler") or ep.get("is_filler") else ""
    return f"{epnum(ep):>5}  {ep.get('title') or ''}{tag}".rstrip()


def write_m3u(path, entries, title):
    """write a standard EXTM3U playlist. entries = [(name, url)]."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for name, url in entries:
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(url)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def launch(cmd, url, title, lang):
    """play one episode. cmd = mpv argv prefix, or None on Android (mpv-android app)."""
    if cmd is None:
        android_open(url)
    else:
        subprocess.run([*cmd, f"--force-media-title={title}", *mpv_args(lang), url])


# ------------------------------------------------------------------ history
# where each show was left off, and the last volume, so plain `toranime` can offer
# "continue ONE PIECE 1155 at 12:34" and mpv reopens as loud as it was.

def state_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, APP)


def state_file(name):
    """a state file: this install's, else the one the old name left behind. history is
    read from there and then saved here, so the first save is the migration."""
    path = os.path.join(state_dir(), name)
    old = os.path.join(os.path.dirname(state_dir()), LEGACY, name)
    return path if os.path.exists(path) or not os.path.exists(old) else old


def load_history():
    try:
        with open(state_file("history.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def show_tweaks(history, anime, lang):
    """the TWEAKS saved for anime in lang: {mpv property: value}."""
    saved = (history.get("tweaks") or {}).get(f"{anime.get('_key')}/{lang}")
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items()
            if k in TWEAKS and isinstance(v, (int, float)) and not isinstance(v, bool)}


def remember(anime, ep, lang, pos=None, volume=None, tweaks=None):
    """note where anime was left off: pos seconds into ep, or None once ep was watched,
    and the TWEAKS changed for it in lang. history is a convenience, so a failed write
    never stops playback."""
    data = load_history()
    key = anime.get("_key")
    if key:
        shows = data.get("shows") if isinstance(data.get("shows"), dict) else {}
        shows[key] = {"title": title_of(anime.get("title")), "lang": lang,
                      "episode": str(epnum(ep)),
                      "position": None if pos is None else round(pos, 1),
                      "time": int(time.time())}
        data["shows"], data["last"] = shows, key
    if key and tweaks:
        everything = data.get("tweaks") if isinstance(data.get("tweaks"), dict) else {}
        everything[f"{key}/{lang}"] = {**show_tweaks(data, anime, lang), **tweaks}
        data["tweaks"] = everything
    if isinstance(volume, (int, float)):
        data["volume"] = round(volume)
    path = os.path.join(state_dir(), "history.json")
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def resume_point(anime, eps, lang, history):
    """(index into eps, start seconds) to carry on with anime in lang, or None.
    start is None when the saved episode was the newest and already watched."""
    h = (history.get("shows") or {}).get(anime.get("_key"))
    if not isinstance(h, dict) or h.get("lang") != lang:
        return None
    for i, ep in enumerate(eps):
        if str(epnum(ep)) == h.get("episode"):
            pos = h.get("position")
            if isinstance(pos, (int, float)):
                return i, float(pos)
            return (i + 1, 0.0) if i + 1 < len(eps) else (i, None)
    return None


def clock(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ------------------------------------------------------------------ AniList
# opt-in progress tracking. `toranime --anilist` logs in once; from then on every
# episode you finish moves your AniList progress up to it. the meta addon works in
# kitsu ids, so its id is mapped to the AniList one once per show (and cached).
# progress only goes up: an episode at or below it, or anything you completed (a
# rewatch), is left alone.
ANILIST_API = "https://graphql.anilist.co"
# toranime's AniList API client (anilist.co/settings/developer, redirect url
# https://anilist.co/api/v2/oauth/pin so AniList shows the token to paste back). a
# fork can use its own with TORANIME_ANILIST_CLIENT
ANILIST_CLIENT = os.environ.get("TORANIME_ANILIST_CLIENT", "56373")
ANILIST_LOGIN = "https://anilist.co/api/v2/oauth/authorize?client_id={}&response_type=token"
ANILIST_MEDIA = """query ($id: Int) {
  Media(id: $id) { episodes mediaListEntry { status progress } } }"""
ANILIST_SAVE = """mutation ($id: Int, $progress: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $id, progress: $progress, status: $status) { progress } }"""
_anilist_lock = threading.Lock()
ANILIST_TOKEN_MIN = 100      # an AniList token is a long jwt; a short paste is a code


class AniListAuth(RuntimeError):
    """AniList refused the token: invalid, expired or revoked."""


def clean_token(text):
    """what was pasted -> the token itself: no quotes, no `Bearer `, no whitespace."""
    token = (text or "").strip().strip("\"'").strip()
    return re.sub(r"(?i)^bearer[\s:]+", "", token).strip()


def anilist_file():
    return state_file("anilist.json")


def anilist_token():
    """the saved AniList access token, or None when not logged in."""
    try:
        with open(anilist_file(), encoding="utf-8") as fh:
            token = json.load(fh).get("token")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def anilist(token, query, **variables):
    """one GraphQL request to AniList as the logged-in viewer -> its data."""
    req = urllib.request.Request(
        ANILIST_API, json.dumps({"query": query, "variables": variables}).encode(),
        {**UA, "Content-Type": "application/json", "Accept": "application/json",
         "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read().decode("utf-8"))
        except ValueError:
            reply = {}
        errors = reply.get("errors") if isinstance(reply, dict) else None
        msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") or ""
        if e.code in (400, 401) or "token" in msg.lower():
            raise AniListAuth("your login expired or was revoked, "
                              "run toranime --anilist again") from None
        raise RuntimeError(msg or f"HTTP {e.code}") from None
    errors = reply.get("errors")
    if errors:
        raise RuntimeError(errors[0].get("message") or "request failed")
    return reply.get("data") or {}


def anilist_map_file():
    return state_file("anilist-map.json")


def anilist_id_of(anime):
    """anime -> its AniList id, mapped once from its kitsu id and remembered."""
    if anime.get("anilist_id"):
        return anime["anilist_id"]
    kitsu = anime.get("kitsu_id")
    if not kitsu:
        return None
    path = anilist_map_file()
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}
    if isinstance(cached.get(str(kitsu)), int):
        anime["anilist_id"] = cached[str(kitsu)]
        return anime["anilist_id"]
    try:
        data = fetch_json(f"{ANILIST_MAP_API}?idType=kitsuId&idValue={kitsu}", timeout=15)
        found = ((data.get("data") or {}).get("mappings") or {}).get("anilistId")
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return None                     # tracking is a convenience: never stop playback
    if not isinstance(found, int):
        return None
    cached[str(kitsu)] = found
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cached, fh, indent=1)
    except OSError:
        pass
    anime["anilist_id"] = found
    return found


# ------------------------------------------------------------------ seadex
# releases.moe keeps one entry per show: which torrent is the best version of it, and
# which are the alternatives. matching is by info hash, which is exactly what the stream
# addon gives us, so a SeaDex pick is the same torrent, not a lookalike.

def seadex_file():
    return state_file("seadex.json")


def seadex_sets(entry):
    """one SeaDex entry's expanded torrents -> {"best": hashes, "alt": hashes}.
    private-tracker entries carry no hash ("<redacted>"), so they cannot be matched."""
    out = {"best": set(), "alt": set()}
    for tr in ((entry or {}).get("expand") or {}).get("trs") or []:
        if not isinstance(tr, dict):
            continue
        h = str(tr.get("infoHash") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", h):
            continue
        out["best" if tr.get("isBest") else "alt"].add(h)
    return out


def seadex_for(anime):
    """SeaDex's picks for a show -> {"best": hashes, "alt": hashes}. empty when the show
    has no entry, releases.moe is unreachable, or --no-seadex was given: the release order
    is then simply what it always was. one request per show per day, kept in state_dir()."""
    empty = {"best": set(), "alt": set()}
    if NO_SEADEX:
        return empty
    media = anilist_id_of(anime)
    if not media:
        return empty
    key = str(media)
    if key in SEADEX:
        return SEADEX[key]
    path = seadex_file()
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = {}
    cached = cached if isinstance(cached, dict) else {}
    got = cached.get(key) if isinstance(cached.get(key), dict) else {}
    if not got or time.time() - float(got.get("at") or 0) >= SEADEX_TTL:
        query = {"filter": f"(alID={int(media)})", "expand": "trs", "perPage": "1",
                 "fields": SEADEX_FIELDS}
        with SEADEX_LOCK:                  # a binge asks from several threads at once
            if key not in SEADEX:
                try:
                    data = fetch_json(SEADEX_API + "?" + urllib.parse.urlencode(query),
                                      timeout=15)
                    items = data.get("items") or []
                    found = seadex_sets(items[0] if items else {})
                    got = {"at": time.time(), "best": sorted(found["best"]),
                           "alt": sorted(found["alt"])}
                    cached[key] = got
                    try:
                        os.makedirs(state_dir(), exist_ok=True)
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump(cached, fh)
                    except OSError:
                        pass
                except (OSError, ValueError, AttributeError, urllib.error.URLError):
                    pass                   # no SeaDex today: nothing to mark, nothing breaks
    out = {"best": {str(h).lower() for h in (got or {}).get("best") or ()},
           "alt": {str(h).lower() for h in (got or {}).get("alt") or ()}}
    SEADEX[key] = out
    return out


def seadex_tag(stream, seadex):
    """`♥ seadex` in sakura pink for a SeaDex pick, a quiet note for an alternative."""
    h = str(stream.get("infoHash") or "").lower()
    if not h or not seadex:
        return ""
    if h in (seadex.get("best") or ()):
        return f"{PINK}♥ seadex{RST}"
    if h in (seadex.get("alt") or ()):
        return f"{DIM}seadex alt{RST}"
    return ""


WATCHED = {}        # anime key -> episodes AniList has watched, for this run


def anilist_watched(anime):
    """how many episodes AniList says are watched for this show, or None.
    fails quietly: no login, no map, no network just means no marks in the picker."""
    token = anilist_token()
    if not token:
        return None
    key = str(anime.get("id") or anime.get("kitsu_id") or "")
    if key in WATCHED:
        return WATCHED[key]
    progress = None
    media_id = anilist_id_of(anime)
    if media_id:
        try:
            media = anilist(token, ANILIST_MEDIA, id=media_id).get("Media") or {}
            value = (media.get("mediaListEntry") or {}).get("progress")
            if isinstance(value, int):
                progress = value
        except (OSError, RuntimeError, ValueError):
            progress = None
    WATCHED[key] = progress
    return progress


def episode_label(anime, watched=None):
    """ep_label with AniList marks: ✓ for what you've watched, ▶ for the next one."""
    watched = anilist_watched(anime) if watched is None else watched
    if not watched:
        return ep_label
    following = watched + 1

    def label(ep):
        number = epnum(ep)
        text = ep_label(ep)
        if not number.isdigit():
            return text
        n = int(number)
        if n <= watched:
            return f"{DIM}✓ {text}{RST}"
        if n == following:
            return f"▶ {text}"          # same colour as the rest, just the marker
        return f"  {text}"
    return label


def episode_picker(anime, eps, header, noun="episodes", initial=0):
    """the episode picker, marked up with what AniList already has when it can."""
    watched = anilist_watched(anime)
    if watched:
        header += f"   {DIM}✓ watched · ▶ next{RST}"
    return fuzzy_pick(eps, episode_label(anime, watched), header, noun=noun, initial=initial)


def anilist_progress(token, anime, ep):
    """move AniList progress for anime up to ep -> what changed, or None if nothing did."""
    n = epnum(ep)
    if not n.isdigit() or int(n) < 1:
        return None                     # recaps and specials numbered 12.5 aren't progress
    n, media_id = int(n), anilist_id_of(anime)
    if not media_id:
        return None
    with _anilist_lock:                 # one read-then-save at a time: progress never drops
        media = anilist(token, ANILIST_MEDIA, id=media_id).get("Media") or {}
        entry = media.get("mediaListEntry") or {}
        if entry.get("status") == "COMPLETED" or (entry.get("progress") or 0) >= n:
            return None
        total = media.get("episodes")
        status = ("COMPLETED" if total and n >= total
                  else "REPEATING" if entry.get("status") == "REPEATING" else "CURRENT")
        anilist(token, ANILIST_SAVE, id=media_id, progress=n, status=status)
    done = "  (completed)" if status == "COMPLETED" else ""
    return f"{title_of(anime.get('title'))} {n}{done}"


def track(anime, ep):
    """when logged in, move AniList progress up to ep in the background. -> a job for
    tracked(), or None."""
    token = anilist_token()
    if not token:
        return None
    return in_background(anilist_progress, token, anime, ep)


def tracked(jobs, say, wait=0.0):
    """report each job that finished (waiting up to `wait` seconds) -> the ones still running."""
    left = []
    for th, box in jobs:
        th.join(wait)
        if th.is_alive():
            left.append((th, box))
        elif "err" in box:
            say(f"AniList: {box['err']}")
        elif box.get("ok"):
            say(f"AniList: progress {box['ok']}")
    return left


def anilist_login():
    """log in to AniList: approve toranime in the browser, paste the token AniList shows."""
    import getpass
    import webbrowser
    if not ANILIST_CLIENT:
        sys.exit("AniList login isn't set up in this copy of toranime")
    url = ANILIST_LOGIN.format(ANILIST_CLIENT)
    print(f"1. open this link and approve toranime (it may have opened already):\n   {url}")
    print("2. copy the token the page shows (a long line of letters), and paste it here:")
    print("   it won't show as you paste, and Enter sends it")
    try:
        webbrowser.open(url)
    except Exception:
        pass                            # no browser here: the link above is enough
    name = None
    for _ in range(3):                  # a cut-short paste should not mean starting over
        try:
            raw = getpass.getpass("token: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nno token, not logged in")
        token = clean_token(raw)
        if not token:
            sys.exit("no token, not logged in")
        if token.lower().startswith("http"):
            print("that is the link, not the token: open it, approve toranime, then copy the "
                  "long token the page shows.")
            continue
        try:
            name = (anilist(token, "query { Viewer { name } }").get("Viewer") or {}).get("name")
        except AniListAuth:
            if len(token) < ANILIST_TOKEN_MIN:
                print(f"that is only {len(token)} characters, so it is probably the code and "
                      "not the token: copy the long value the page shows.")
            else:
                print("AniList rejected that token: it may have been cut short when copied, or "
                      "it expired. copy the whole thing and paste it again.")
            continue
        except (OSError, ValueError) as e:
            sys.exit(f"AniList didn't answer: {e}")
        break
    if not name:
        path = os.path.join(state_dir(), "anilist.json")
        sys.exit("not logged in. you can also write a token yourself:\n"
                 f"  mkdir -p {state_dir()} && printf '%s' '{{\"token\":\"YOUR_TOKEN\"}}' > "
                 f"{path} && chmod 600 {path}")
    os.makedirs(state_dir(), exist_ok=True)
    fd = os.open(os.path.join(state_dir(), "anilist.json"),
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "user": name}, fh)
    print(f"logged in to AniList as {name}. episodes you finish now update your progress")


def anilist_logout():
    removed = False
    for path in {anilist_file(), os.path.join(state_dir(), "anilist.json")}:
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    if not removed:
        print("not logged in to AniList")
        return
    print("logged out of AniList. to also revoke toranime's access: "
          "https://anilist.co/settings/apps")


# ------------------------------------------------------------------ binge sessions
# on desktop one mpv window plays a whole binge. toranime talks to it over mpv's JSON
# IPC and appends the next episode while the current one plays, so episodes advance
# on their own, Enter or > (mpv's playlist-next) skips ahead, < goes back, and volume
# and fullscreen carry over. links are fetched one episode ahead, so none is older
# than about one episode when mpv opens it (they expire after six hours).

# what you change in mpv for a show sticks to it, for dub and sub separately: audio
# delay (Ctrl + and Ctrl -), subtitle size (F, G) and position (r, R). some releases
# need their dub shifted, or have huge subtitles
TWEAKS = ("audio-delay", "sub-scale", "sub-pos")
POLL = 1.0       # seconds between looks at mpv's playlist position
DONE_AT = 120    # quitting this close to the end, or once the ending starts (when the
                 # file marks it as a chapter), counts as watched


def finished(pos, dur, ending):
    """watched: within DONE_AT of the end, or into the ending the chapter skipper found."""
    return pos is not None and bool((dur and dur - pos < DONE_AT) or (ending and pos >= ending))


class MpvError(Exception):
    pass


class MpvIpc:
    """mpv's JSON IPC, one request at a time. nothing reads in the background: a
    Windows pipe handle serializes reads and writes, so a pending read would block
    every command."""

    def __init__(self, conn):
        self.conn, self.buf, self.rid = conn, b"", 0
        if isinstance(conn, socket.socket):
            self.send, self.recv = conn.sendall, lambda: conn.recv(65536)
        else:
            self.send, self.recv = conn.write, lambda: conn.read(65536)

    def command(self, *args, **named):
        """run an mpv command, positional (["seek", 10]) or named (name=..., url=...)."""
        self.rid += 1
        cmd = dict(name=args[0], **named) if named else list(args)
        self.send((json.dumps({"command": cmd, "request_id": self.rid}) + "\n").encode())
        while True:
            while b"\n" not in self.buf:
                chunk = self.recv()
                if not chunk:
                    raise EOFError("mpv closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg.get("request_id") == self.rid:
                if msg.get("error") != "success":
                    raise MpvError(msg.get("error"))
                return msg.get("data")

    def get(self, prop):
        try:
            return self.command("get_property", prop)
        except MpvError:                # "property unavailable" while a file loads
            return None

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


def ipc_path():
    tag = f"toranime-{os.getpid()}-{secrets.token_hex(4)}"
    if os.name == "nt":
        return "\\\\.\\pipe\\" + tag
    # a private folder: whoever can open the socket can make mpv run commands
    os.makedirs(cache_dir(), mode=0o700, exist_ok=True)
    return os.path.join(cache_dir(), tag + ".sock")


def mpv_connect(path, proc, timeout=15):
    """connect to the ipc server of the mpv that proc runs, or None if it never opens
    one (an mpv without ipc, or a flatpak sandbox that can't reach the path)."""
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        conn = None
        try:
            if os.name == "nt":
                conn = open(path, "r+b", buffering=0)
            else:
                conn = socket.socket(socket.AF_UNIX)
                conn.settimeout(10)
                conn.connect(path)
            ipc = MpvIpc(conn)
            ipc.command("disable_event", "all")     # replies only, no event chatter
            return ipc
        except (OSError, EOFError, MpvError, ValueError):
            if conn is not None:
                conn.close()
            time.sleep(0.1)
    return None


def in_background(fn, *args):
    """run fn on a daemon thread (never holds up exit). -> (thread, box with ok/err)."""
    box = {}

    def run():
        try:
            box["ok"] = fn(*args)
        except Exception as e:
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box


def prepare(anime, ep, lang, verbose=True):
    """resolve an episode, plus the subtitle file to add for it (look or signs)."""
    url, video = resolve(anime, ep, lang, verbose)
    return url, video, extra_subs(url, video, lang)


def title_option(title):
    # mpv's %bytes% quoting keeps commas and quotes in the title intact
    return f"force-media-title=%{len(title.encode())}%{title}"


def play_session(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    """play eps[idx] from start seconds in one mpv window, appending every following
    episode (through eps[stop], else the newest) while the one before it plays.

    returns (index, seconds, status) for where the viewer left off. status is "done"
    (watched to the end, or mpv couldn't be followed), "stopped" (quit mid-episode at
    seconds), "failed" (eps[idx] couldn't be resolved) or "quit" (Ctrl+C)."""
    name = title_of(anime.get("title"))
    last = len(eps) - 1 if stop is None else stop
    try:
        url, video = resolve(anime, eps[idx], lang)
    except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"episode {epnum(eps[idx])}: {e}\n")
        return idx, None, "failed"
    burned = extra_subs(url, video, lang, quick=True)
    look_job = None
    if burned is None and lang == "sub" and BURN:      # bake it while the episode plays
        look_job = in_background(restyle_subs, url, BURN, video)
    title = ep_title(name, eps[idx])
    sys.stderr.write(f"{title}  ({lang}, {video.get('sizeHuman', '?')})\n")

    path = ipc_path()
    # --idle=no: mpv 0.42's mpv.exe started without a console (pseudo-gui) idles after
    # the last episode instead of closing, and the session only ends when mpv does
    argv = [*cmd, *mpv_args(lang), "--idle=no", f"--input-ipc-server={path}"]
    history = load_history()
    volume = history.get("volume")
    if isinstance(volume, (int, float)):
        argv.append(f"--volume={volume:g}")
    tweaks = show_tweaks(history, anime, lang)
    if tweaks:
        argv += [f"--{k}={v:g}" for k, v in tweaks.items()]
        sys.stderr.write(f"your settings for {name} ({lang}): "
                         + "  ".join(f"{k} {v:g}" for k, v in tweaks.items()) + "\n")
    first = [f"--force-media-title={title}"] + ([f"--start={start:.1f}"] if start else [])
    if burned:
        first.append(f"--sub-file={burned}")      # the look is baked into this copy
    tty_before = _tty_state()          # mpv drives the terminal itself: put it back after
    proc = subprocess.Popen([*argv, "--{", *first, url, "--}"])   # per-file options

    queue = [idx]                     # mpv playlist entry -> index into eps
    cur, pos, dur, vol, status = idx, (start or None), None, None, "done"
    ending = None                     # where the ending starts, from CHAPTER_SKIPPER
    began, now = {}, {}               # TWEAKS as mpv opened, and as they are
    nxt, pending, misses = idx + 1, None, 0
    jobs = []                         # AniList updates on their way
    ipc = mpv_connect(path, proc)

    def say(msg):                     # mpv prints it to the terminal around its status line
        try:
            ipc.command("print-text", f"toranime: {msg}")
        except (EOFError, OSError, ValueError, MpvError):
            pass
    try:
        while ipc and proc.poll() is None:
            try:
                entry = ipc.get("playlist-pos")
                t, d, v = ipc.get("time-pos"), ipc.get("duration"), ipc.get("volume")
                e = ipc.get("user-data/toranime/ending")
                for k in TWEAKS:
                    x = ipc.get(k)
                    if isinstance(x, (int, float)):
                        began.setdefault(k, x)
                        now[k] = x
            except (EOFError, OSError, ValueError):
                break                           # mpv is shutting down
            if isinstance(entry, int) and 0 <= entry < len(queue):
                if queue[entry] != cur:
                    job = finished(pos, dur, ending) and track(anime, eps[cur])
                    if job:                     # it played to its end, not skipped with >
                        jobs.append(job)
                    cur, pos, dur, ending = queue[entry], None, None, None
                if t is not None:
                    pos, dur = t, d
                    ending = e if isinstance(e, (int, float)) and e > 0 else None
            if v is not None:
                vol = v
            # keep one episode queued past the one playing
            if pending is None and entry == len(queue) - 1 and nxt <= last and misses < 3:
                pending = in_background(prepare, anime, eps[nxt], lang, False)
            if pending is not None and not pending[0].is_alive():
                nxt_title = ep_title(name, eps[nxt])
                try:
                    if "err" in pending[1]:
                        raise pending[1]["err"]
                    nxt_url, _, nxt_subs = pending[1]["ok"]
                    options = title_option(nxt_title)
                    if nxt_subs:
                        options += f",sub-file=%{len(nxt_subs.encode())}%{nxt_subs}"
                    ipc.command("loadfile", url=nxt_url, flags="append", options=options)
                    ipc.command("print-text", f"toranime: up next  {nxt_title}")
                    queue.append(nxt)
                    misses = 0
                except (LookupError, RuntimeError, OSError, ValueError, KeyError, MpvError) as e:
                    misses += 1
                    say(f"skipping episode {epnum(eps[nxt])}: {e}")
                nxt, pending = nxt + 1, None
            if look_job is not None and not look_job[0].is_alive():
                copy = look_job[1].get("ok")
                look_job = None
                if copy:                    # the look's copy is ready: add it as a track
                    try:
                        track_id = ipc.command("sub-add", copy, "auto")
                        if LOOK_SELECTED and track_id is not None:
                            ipc.command("set", "sid", track_id)
                        say("your subtitles are ready: press S to switch")
                    except (EOFError, OSError, ValueError, MpvError):
                        pass
            jobs = tracked(jobs, say)
            time.sleep(POLL)
    except KeyboardInterrupt:
        status = "quit"
    finally:
        if ipc:
            ipc.close()
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            try:
                proc.wait(timeout=5)   # reap it, so its own tty restore cannot land after ours
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                pass
            status = "quit"
        _tty_restore(tty_before)       # a killed mpv never puts the terminal back
        if os.name != "nt" and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    watched = finished(pos, dur, ending)
    if ipc and status == "done" and pos is not None and not watched:
        status = "stopped"
    seconds = pos if status in ("stopped", "quit") else None
    changed = {k: round(x, 3) for k, x in now.items() if x != began[k]}
    remember(anime, eps[cur], lang, seconds, vol, changed)
    job = watched and track(anime, eps[cur])
    tracked(jobs + [job] if job else jobs, lambda m: sys.stderr.write(m + "\n"), wait=10)
    return cur, seconds, status


def binge(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    """desktop playback: one mpv window per session, and a menu once it closes, with
    resume as the default after quitting mid-episode and next after finishing one."""
    name = title_of(anime.get("title"))
    while True:
        idx, pos, status = play_session(cmd, anime, eps, idx, lang, start, stop)
        if status == "quit":
            return
        ep, start = eps[idx], 0.0
        last = len(eps) - 1 if stop is None else stop
        if status == "done" and idx >= last and stop is not None:
            sys.stderr.write(f"\n{name} {epnum(eps[stop])}: end of the range\n")
            return
        opts = []
        if status == "stopped":
            opts.append((f"resume    {epnum(ep)} at {clock(pos)}", "resume"))
        if idx + 1 < len(eps):
            opts.append((f"next      {ep_label(eps[idx + 1]).strip()}", "next"))
        if status != "failed":
            opts.append(("replay" if status == "done" else f"restart   {epnum(ep)}", "replay"))
        opts += [("pick episode", "pick"), ("quit", "quit")]
        default = 0 if opts[0][1] in ("resume", "next") else len(opts) - 1
        head = {"stopped": f"stopped at {clock(pos)}", "failed": "unavailable"}.get(status, "done")
        try:
            choice = opts[menu_pick(opts, default, f"\n{name} {epnum(ep)} {head}:")][1]
        except EOFError:
            return
        if choice == "quit":
            return
        if choice == "resume":
            start = pos
        elif choice == "next":
            idx += 1
        elif choice == "pick":
            ep = episode_picker(anime, eps, f"{name} ({lang}):", initial=idx)
            if ep is None:
                return
            idx, stop = eps.index(ep), None


def play_loop(cmd, anime, eps, idx, lang):
    """Android (mpv-android takes one url at a time and can't be followed): open
    eps[idx], then offer next/replay/previous. the next episode is resolved in the
    background while the current one plays, so Enter starts it right away."""
    name = title_of(anime.get("title"))
    cache, pre = {}, None

    def prefetch(i):
        try:
            cache[i] = resolve(anime, eps[i], lang, verbose=False)
        except Exception:
            pass                        # resolved again, with errors shown, when it is needed

    while True:
        ep = eps[idx]
        if pre:
            pre.join()
        got = cache.pop(idx, None)
        cache.clear()                   # links expire; never hold one past the next episode
        if not got:
            try:
                got = resolve(anime, ep, lang)
            except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
                sys.stderr.write(f"episode {epnum(ep)}: {e}\n")
        pre = None
        if got:
            if idx + 1 < len(eps):
                pre = threading.Thread(target=prefetch, args=(idx + 1,), daemon=True)
                pre.start()
            url, video = got
            title = ep_title(name, ep)
            sys.stderr.write(f"{title}  ({lang}, {video.get('sizeHuman', '?')})\n")
            launch(cmd, url, title, lang)
            remember(anime, ep, lang)       # the app reports nothing back: count it watched

        opts = []
        if idx + 1 < len(eps):
            opts.append((f"next      {ep_label(eps[idx + 1]).strip()}", "next"))
        if got:
            opts.append(("replay", "replay"))
        if idx > 0:
            opts.append((f"previous  {epnum(eps[idx - 1])}", "prev"))
        opts += [("pick episode", "pick"), ("quit", "quit")]
        default = 0 if opts[0][1] == "next" else len(opts) - 1
        try:
            state = "done" if cmd else "opened in mpv-android"
            choice = opts[menu_pick(opts, default, f"\n{name} {epnum(ep)} {state}:")][1]
        except EOFError:
            return
        if choice == "quit":
            return
        if choice == "next":
            job = got and track(anime, ep)      # moving on: count this one watched
            if job:
                tracked([job], lambda m: sys.stderr.write(m + "\n"), wait=10)
            idx += 1
        elif choice == "prev":
            idx -= 1
        elif choice == "pick":
            ep = episode_picker(anime, eps, f"{name} ({lang}):", initial=idx)
            if ep is None:
                return
            idx = eps.index(ep)


# Termux on Android has no desktop for mpv to open a window on, so episodes go to
# the mpv-android app through an intent instead (the same route ani-cli takes).
ANDROID = bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")
MPV_ANDROID = "is.xyz.mpv/.MPVActivity"
MPV_ANDROID_HELP = ("install the mpv-android app (is.xyz.mpv) from Google Play or F-Droid: "
                    "https://github.com/mpv-android/mpv-android")

# os key -> [(package manager, [commands])]. every manager on PATH is offered in
# order; if one fails the next is tried. on Windows the official mpv release above is
# tried before any of these, so a bare Windows needs no package manager at all. scoop's
# extras/mpv installs that same zip with a pinned hash, and mpv.io lists chocolatey's
# mpvio; both are fine. "mpv.net" is a different player (mpvnet.exe), and chocolatey's
# "mpv" is deprecated in favour of "mpvio".
# winget's shinchiro.mpv is deliberately absent: it is an unrelated project's repack of
# mpv in its own unsigned installer, not mpv's release.
MPV_INSTALL = {
    "nt": [("scoop", [["scoop", "bucket", "add", "extras"], ["scoop", "install", "extras/mpv"]]),
           ("choco", [["choco", "install", "mpvio", "-y"]])],
    "darwin": [("brew", [["brew", "install", "mpv"]]),
               ("port", [["sudo", "port", "install", "mpv"]])],
    "linux": [("apt", [["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "mpv"]]),
              ("dnf", [["sudo", "dnf", "install", "-y", "mpv"]]),
              ("pacman", [["sudo", "pacman", "-S", "--needed", "--noconfirm", "mpv"]]),
              ("zypper", [["sudo", "zypper", "install", "-y", "mpv"]]),
              ("apk", [["sudo", "apk", "add", "mpv"]]),
              ("xbps-install", [["sudo", "xbps-install", "-y", "mpv"]]),
              ("pkg", [["sudo", "pkg", "install", "-y", "mpv"]]),           # FreeBSD
              ("nix", [["nix", "profile", "install", "nixpkgs#mpv"]]),
              # immutable distros (Steam Deck, Silverblue): no root needed
              ("flatpak", [["flatpak", "remote-add", "--user", "--if-not-exists", "flathub",
                            "https://dl.flathub.org/repo/flathub.flatpakrepo"],
                           ["flatpak", "install", "--user", "-y", "flathub", "io.mpv.Mpv"]])],
}
# where those installs leave mpv. a fresh install is usually not on this process's
# PATH yet (a scoop shim is, only in a new shell), so look here directly.
MPV_DIRS = {
    "nt": [r"%ProgramFiles%\MPV Player", r"%ProgramFiles%\mpv", r"%LOCALAPPDATA%\Programs\mpv",
           r"%LOCALAPPDATA%\Microsoft\WinGet\Links", r"%USERPROFILE%\scoop\shims",
           r"%ProgramData%\chocolatey\bin"],
    "darwin": ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
               "/Applications/mpv.app/Contents/MacOS"],
    "linux": ["/usr/bin", "/usr/local/bin", "~/.nix-profile/bin", "/run/current-system/sw/bin",
              "/snap/bin"],
}


def _os_key():
    return "nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")


def find_mpv(mpv):
    """argv prefix that runs mpv, or None. checks PATH, the usual install dirs, then flatpak."""
    hit = shutil.which(mpv) or (mpv if os.path.isfile(mpv) else None)
    if hit:
        return [hit]
    if mpv != "mpv":
        return None                     # an explicit --mpv path that isn't there
    exe = "mpv.exe" if os.name == "nt" else "mpv"
    for d in MPV_DIRS[_os_key()]:
        path = os.path.join(os.path.expanduser(os.path.expandvars(d)), exe)
        if os.path.isfile(path):
            return [path]
    if os.name == "nt":
        for path in sorted(glob.glob(os.path.join(cache_dir(), "mpv", "*", "mpv.exe")),
                           reverse=True):
            return [path]               # the copy toranime downloaded from mpv's own releases
        pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        for path in glob.glob(os.path.join(pkgs, "*mpv*", "**", "mpv.exe"), recursive=True):
            return [path]
    if shutil.which("flatpak") and subprocess.run(
            ["flatpak", "info", "io.mpv.Mpv"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return ["flatpak", "run", "io.mpv.Mpv"]
    return None


# mpv's own releases (github.com/mpv-player/mpv) are built by the project's CI, and
# every asset carries a SHA-256 in the API plus a detached .asc. the zip is the same one
# Scoop installs, hash for hash: no repack, no third-party installer, nothing to trust
# beyond mpv's own repository.
MPV_RELEASE_API = "https://api.github.com/repos/mpv-player/mpv/releases/latest"


def mpv_arch():
    """"x86_64" or "aarch64", as mpv names its Windows builds."""
    return ("aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64")


def official_mpv_release(arch=None):
    """the official Windows build for this machine, or None.
    -> {"version", "url", "sha256", "size"}; the hash is the digest GitHub publishes
    for that asset, not something this program ships and could get stale."""
    arch = arch or mpv_arch()
    try:
        data = fetch_json(MPV_RELEASE_API, {"Accept": "application/vnd.github+json"},
                          timeout=15)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("tag_name") or "").strip().lstrip("vV")
    want = f"mpv-v{version}-{arch}-w64-mingw32-full.zip"
    for asset in data.get("assets") or []:
        digest = str(asset.get("digest") or "")
        if asset.get("name") == want and digest.startswith("sha256:"):
            return {"version": version, "url": asset.get("browser_download_url"),
                    "sha256": digest[len("sha256:"):].lower(), "size": asset.get("size") or 0}
    return None


def download_mpv(release):
    """fetch, verify and unpack an official build into the cache folder -> argv prefix,
    or None. nothing is unpacked before the SHA-256 matches, so a tampered or truncated
    download leaves nothing behind."""
    version = release["version"]
    folder = os.path.join(cache_dir(), "mpv", version)
    exe = os.path.join(folder, "mpv.exe")
    if os.path.isfile(exe):
        return [exe]                    # already here from an earlier run
    size = release.get("size") or 0
    tmp = os.path.join(cache_dir(), "mpv", f".mpv-{version}.zip")
    try:
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        sys.stderr.write(f"downloading mpv {version}"
                         + (f" ({human_size(size)})" if size else "")
                         + " from mpv's own releases...\n")
        digest = hashlib.sha256()
        with urllib.request.urlopen(urllib.request.Request(release["url"], headers=UA),
                                    timeout=60) as r, open(tmp, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest().lower() != release["sha256"]:
            raise ValueError("the download does not match the published SHA-256")
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
                    raise ValueError("the mpv zip has an unsafe path")
            z.extractall(folder)
        if not os.path.isfile(exe):
            raise ValueError("the mpv zip has no mpv.exe")
        sys.stderr.write(f"mpv {version} verified and unpacked: {exe}\n")
        return [exe]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        sys.stderr.write(f"mpv download failed: {e}\n")
        shutil.rmtree(folder, ignore_errors=True)
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _sudo(cmd):
    """drop sudo when already root or when there is none (containers, minimal installs)."""
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    return cmd[1:] if cmd[0] == "sudo" and (root or not shutil.which("sudo")) else cmd


def ensure_player(mpv, assume_yes=False):
    """mpv argv prefix, installing mpv on first run when it is missing (without asking
    when `assume_yes`). returns None on Android, where the mpv-android app plays
    instead. exits if there is no player."""
    if ANDROID:
        return None
    cmd = find_mpv(mpv)
    if cmd:
        return cmd
    if mpv != "mpv":
        sys.exit(f"mpv not found at {mpv}")

    sys.stderr.write("mpv (the video player toranime streams into) is not installed.\n")
    if _os_key() == "nt":
        # mpv's own build first: one zip, no repack, no installer, no admin rights
        release = official_mpv_release()
        where = f"mpv {release['version']}" if release else "mpv"
        size = f" ({human_size(release['size'])} zip)" if release and release.get("size") else ""
        if (assume_yes or interactive_ok()) and (assume_yes or ask(
                f"install {where} from mpv's own releases?{size}  "
                f"SHA-256 checked, no installer [Y/n] ").strip().lower() not in ("n", "no")):
            cmd = download_mpv(release) if release else None
            if cmd:
                sys.stderr.write(f"mpv ready: {' '.join(cmd)}\n")
                return cmd
            sys.stderr.write("the official download didn't work; trying a package manager.\n")
    candidates = [(mgr, [_sudo(c) for c in steps])
                  for mgr, steps in MPV_INSTALL[_os_key()] if shutil.which(mgr)]
    if not candidates:
        sys.exit("no supported package manager found. install mpv from "
                 "https://mpv.io/installation/ and retry.")
    if not (assume_yes or interactive_ok()):
        sys.exit("install it with: " + " && ".join(" ".join(c) for c in candidates[0][1])
                 + "\nor let toranime do it: toranime --setup --yes")

    for mgr, steps in candidates:
        shown = " && ".join(" ".join(c) for c in steps)
        if assume_yes:
            sys.stderr.write(f"installing mpv via {mgr}: {shown}\n")
        elif ask(f"install mpv now via {mgr}?  ({shown}) [Y/n] ").strip().lower() in ("n", "no"):
            sys.exit("mpv required. aborting.")
        for c in steps:
            subprocess.run(c)           # judged by the result below, not exit codes:
        cmd = find_mpv(mpv)             # "bucket already added" exits non-zero, harmlessly
        if cmd:
            sys.stderr.write(f"mpv ready: {' '.join(cmd)}\n")
            return cmd
        sys.stderr.write(f"{mgr} did not leave a runnable mpv.\n")
    sys.exit("mpv still not available. install it from https://mpv.io/installation/, "
             "open a new terminal, and retry.")


def android_open(url):
    """hand a stream to the mpv-android app. returns at once; the app plays on its own."""
    try:
        r = subprocess.run(["am", "start", "--user", "0", "-a", "android.intent.action.VIEW",
                            "-d", url, "-n", MPV_ANDROID], capture_output=True, text=True)
    except OSError:
        sys.exit("Termux's `am` command is missing: pkg install termux-am")
    if r.returncode or "Error" in r.stdout + r.stderr:
        sys.exit(f"could not open mpv-android. {MPV_ANDROID_HELP}")


def select_eps(eps, spec, lang):
    """'12' / '12-24' / '12-' -> (indices into eps, is_range)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:(-)\s*(\d+(?:\.\d+)?)?)?\s*", spec)
    if not m:
        sys.exit(f"bad episode '{spec}'. use 12, 12-24, or 12- (through the newest)")
    lo = float(m.group(1))
    hi = float(m.group(3)) if m.group(3) else (float("inf") if m.group(2) else lo)
    picked = [i for i, ep in enumerate(eps) if lo <= float(epnum(ep)) <= hi]
    if not picked:
        span = f"{epnum(eps[0])}-{epnum(eps[-1])}" if eps else "none"
        sys.exit(f"episode {spec} has no {lang} release. {lang} episodes available: {span}")
    return picked, bool(m.group(2))


def available(eps, lang):
    """the episodes worth offering: a torrent is named for its language, not flagged,
    so a dub release is discovered when the episode is resolved (failures are skipped)."""
    return [ep for ep in eps if ep.get("number")]


RECENT_SHOWS = 8         # how many of the shows watched recently the first prompt offers


def recent_shows(history, limit=RECENT_SHOWS):
    """history -> [(key, entry)] newest first: the shows worth offering to continue."""
    shows = history.get("shows") if isinstance(history.get("shows"), dict) else {}
    rows = [(key, h) for key, h in shows.items()
            if isinstance(h, dict) and h.get("lang") in LANGS]
    rows.sort(key=lambda row: row[1].get("time") or 0, reverse=True)
    return rows[:limit]


def recent_label(h):
    """one line for the continue list: title, episode, language, position and when."""
    line = f"{h.get('title') or '?'} {h.get('episode') or '?'} ({h.get('lang')})"
    pos = h.get("position")
    if isinstance(pos, (int, float)) and pos > 0:
        line += f" at {clock(pos)}"
    at = h.get("time")
    if isinstance(at, (int, float)) and at > 0:
        line += f"  ·  {_doc_age(time.time() - at)}"
    return line


def continue_prompt():
    """offer the shows watched recently, newest first; type to filter.
    -> (anime, episodes, lang, index, start seconds), or None to search."""
    history = load_history()
    rows = recent_shows(history)
    if not rows or not interactive_ok():
        return None
    items = [dict(h, _key=key) for key, h in rows] + [{"_search": True}]

    def label(item):
        return "search for something else" if item.get("_search") else recent_label(item)

    while True:
        pick = fuzzy_pick(items, label, "\ncontinue watching:", noun="shows")
        if not pick or pick.get("_search"):
            return None
        lang = pick["lang"]
        got = load_anime(pick["_key"])
        if not got:
            sys.stderr.write(f"{pick.get('title')}: the meta addon has no episodes for it\n")
            continue
        anime, eps = got[0], available(got[1], lang)
        point = resume_point(anime, eps, lang, history)
        if not point:
            sys.stderr.write(f"{pick.get('title')}: where you left off isn't in the "
                             f"episode list\n")
            continue
        i, start = point
        if start is None:               # caught up: let them pick an earlier episode
            name = title_of(anime.get("title"))
            ep = episode_picker(anime, eps, f"\n{name} ({lang}):", initial=i)
            if ep is None:
                sys.exit("cancelled")
            i, start = eps.index(ep), 0.0
        return anime, eps, lang, i, start


def watch(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    if cmd is None:
        play_loop(cmd, anime, eps, idx, lang)       # mpv-android
    else:
        binge(cmd, anime, eps, idx, lang, start, stop)


# the update check: one line, once a day, and silent when it can't find out
RELEASES_API = "https://api.github.com/repos/throwin5s/toranime/releases/latest"
UPDATE_EVERY = 24 * 3600        # seconds between automatic checks


def as_version(text):
    """'v2.2.6' -> (2, 2, 6). junk or missing -> (), which compares older than anything."""
    return tuple(int(n) for n in re.findall(r"\d+", str(text or ""))[:3])


def latest_release(timeout=2.5):
    """the newest release number on GitHub, or None. never raises: an unreachable or
    private repo is not worth interrupting playback for."""
    try:
        data = fetch_json(RELEASES_API, {"Accept": "application/vnd.github+json"},
                          timeout=timeout)
        return as_version(data.get("tag_name"))
    except (OSError, ValueError, KeyError, AttributeError):
        return None


def update_state():
    """what the last check found: {"checked": seconds, "latest": (2, 2, 6)}."""
    try:
        with open(os.path.join(cache_dir(), "update.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_check(latest, now=None):
    """the answer, so the next runs don't ask again (best effort: no cache, no problem)."""
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        with open(os.path.join(cache_dir(), "update.json"), "w", encoding="utf-8") as fh:
            json.dump({"checked": time.time() if now is None else now,
                       "latest": ".".join(str(n) for n in latest)}, fh)
    except OSError:
        pass


def update_notice(now=None):
    """say so, once a day, when GitHub has a newer release. -> the line, or None."""
    if os.environ.get("TORANIME_NO_UPDATE_CHECK"):
        return None
    now = time.time() if now is None else now
    state = update_state()
    checked = state.get("checked")
    if isinstance(checked, (int, float)) and 0 <= now - checked < UPDATE_EVERY:
        latest = as_version(state.get("latest"))       # answered earlier today
    else:
        latest = latest_release()
        remember_check(latest or (), now)              # a failed check waits a day too
    if latest and latest > as_version(__version__):
        line = (f"toranime {'.'.join(str(n) for n in latest)} is available "
                f"(you have {__version__}): run toranime --update")
        sys.stderr.write(line + "\n")
        return line
    return None


# what the one-line installers install, and how to run them again
PACKAGE = "toranime-mpv @ https://github.com/throwin5s/toranime/archive/refs/heads/main.zip"
INSTALLER = ("irm https://raw.githubusercontent.com/throwin5s/toranime/main/install.ps1 | iex"
             if os.name == "nt" else
             "curl -fsSL https://raw.githubusercontent.com/throwin5s/toranime/main/install.sh | sh")


def update():
    """reinstall toranime from main into the environment this copy runs from: uv when
    it's a venv (uv tool, pipx), else pip (Termux, like its installer). not the
    installers' `uv tool install --force`: that deletes the venv first, and Windows
    won't delete the python.exe running this. a git checkout or a lone toranime.py is
    left to you."""
    here = os.path.dirname(os.path.abspath(__file__))
    paths = sysconfig.get_paths()
    if os.path.normcase(here) not in {os.path.normcase(os.path.abspath(paths[k]))
                                      for k in ("purelib", "platlib")}:
        if os.path.isdir(os.path.join(here, ".git")):
            sys.exit(f"this toranime runs from a git checkout ({here}): update it with git pull")
        sys.exit(f"this toranime is a single file ({os.path.abspath(__file__)}), which --update "
                 f"can't replace. install it with:\n  {INSTALLER}")
    pkg = os.environ.get("TORANIME_PKG") or PACKAGE      # like the installers
    uv = shutil.which("uv")
    if uv and sys.prefix != sys.base_prefix:
        argv = [uv, "pip", "install", "--python", sys.executable, "--reinstall", pkg]
    else:
        argv = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", pkg]
    sys.stderr.write(f"updating toranime {__version__}: {' '.join(argv)}\n")
    if subprocess.run(argv).returncode:
        sys.exit(f"update failed. run the installer again:\n  {INSTALLER}")
    # -I: not a toranime.py in the current folder
    new = subprocess.run([sys.executable, "-I", "-c", "import toranime; print(toranime.__version__)"],
                         capture_output=True, text=True).stdout.strip()
    if not new:
        sys.exit(f"the update installed, but the new toranime doesn't start. run the installer "
                 f"again:\n  {INSTALLER}")
    print(f"toranime {new}: already the newest" if new == __version__
          else f"updated toranime {__version__} -> {new}")


# ------------------------------------------------------------------ doctor
# `toranime --doctor`: everything a run depends on, on one page. paths, hosts and
# answers only -- never the TorBox key, an AniList token or full username, an account
# email, or a sources.json url that carries a private config in its path. plain ASCII
# lines, so it survives any paste.

def mask_email(email):
    """an address -> only what tells you it is yours: first character and domain."""
    local, at, domain = str(email or "").rpartition("@")
    if not at or not domain:
        return "***" if email else ""
    return f"{local[:1] or '?'}***@{domain}"


def mask_name(name):
    """a username -> only its first character: u***, so a paste names nobody."""
    name = str(name or "")
    return f"{name[:1]}***" if name else ""


def doc_account(who):
    """--doctor's account line: a masked email and the plan, never the address itself."""
    email = mask_email((who or {}).get("email", ""))
    plan = (who or {}).get("plan", "")
    if plan:
        return f"{email} (plan: {plan})" if email else f"your account (plan: {plan})"
    return email or "your account"


def doc_path(path):
    """a path with the home folder shortened to `~`, so a paste carries less of who you are."""
    text = str(path)
    home = os.path.expanduser("~").rstrip(os.sep)
    if home and home != "~" and text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def doc_host(url):
    """an addon url -> just its host: a sources.json url can carry a private config."""
    try:
        return urllib.parse.urlsplit(str(url)).netloc or str(url).split("/")[0]
    except (AttributeError, ValueError):
        return "?"


def mpv_version(cmd):
    """the first line of `mpv --version`, or "" when it won't run."""
    try:
        done = subprocess.run([*cmd, "--version"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else ""


def mpv_where(cmd, wanted):
    """how find_mpv found mpv: flatpak, toranime's own download, --mpv, PATH, a folder."""
    if cmd[0] == "flatpak":
        return "flatpak (io.mpv.Mpv)"
    path = os.path.abspath(cmd[0])
    downloaded = os.path.abspath(os.path.join(cache_dir(), "mpv"))
    if path == downloaded or path.startswith(downloaded + os.sep):
        return f"downloaded by toranime ({cmd[0]})"
    if wanted != "mpv" and os.path.abspath(wanted) == path:
        return f"the path you gave ({cmd[0]})"
    on_path = shutil.which(wanted)
    if on_path and os.path.abspath(on_path) == path:
        return f"PATH ({cmd[0]})"
    return f"an install folder ({cmd[0]})"


def _doc_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _doc_entries(path):
    """a state json file -> (entries, newest `at` timestamp). a broken file is 0."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0, 0.0
    if not isinstance(data, dict):
        return 0, 0.0
    newest = 0.0
    for value in data.values():
        at = value.get("at") if isinstance(value, dict) else None
        if isinstance(at, (int, float)):
            newest = max(newest, float(at))
    return len(data), newest


def doctor(mpv="mpv"):
    """every check a run depends on -> an exit code: 0 when nothing needs attention."""
    out, bad = [], []

    def line(level, label, text):
        out.append(f"[{level:<2}] {label:<16} {text}")
        if level == "!!":
            bad.append(label)

    line("--", "toranime", f"{__version__} (checked {time.strftime('%Y-%m-%d %H:%M')})")
    line("--", "python", f"{platform.python_version()} ({sys.platform}, {platform.machine()})")
    line("--", "app", doc_path(os.path.abspath(__file__)))
    line("--", "config", doc_path(config_dir())
         + ("" if os.path.isdir(config_dir()) else " (missing)"))
    line("--", "state", doc_path(state_dir())
         + ("" if os.path.isdir(state_dir()) else " (missing)"))

    out.append("")
    out.append("TorBox")
    key = saved_key()
    key_path = config_file("torbox.key")
    if not key:
        line("!!", "key", f"no key saved - run toranime --torbox (writes {doc_path(key_path)})")
    else:
        detail = f"{doc_path(key_path)} ({len(key)} characters"
        if os.name == "nt":
            detail += ")"       # Windows has no readable permission bits
        else:
            try:
                detail += f", mode {os.stat(key_path).st_mode & 0o777:03o})"
            except OSError:
                detail += ")"
        line("ok", "key", detail)
        try:
            line("ok", "account", doc_account(torbox_who(key)))
        except TorboxError as e:
            line("!!", "account", f"TorBox rejected the key: {e}")
        except (OSError, ValueError) as e:
            line("!!", "account", f"TorBox did not answer: {e}")

    out.append("")
    out.append("mpv")
    if ANDROID:
        line("--", "player", "mpv-android app plays instead (Termux)")
    else:
        cmd = find_mpv(mpv)
        if not cmd:
            line("!!", "player", "not found - toranime --setup, or --mpv /path/to/mpv")
        else:
            line("ok", "player", mpv_where(cmd, mpv))
            version = mpv_version(cmd)
            line("ok" if version else "!!", "version",
                 version or "mpv did not answer --version")

    out.append("")
    out.append("addons")
    cfg = sources()
    where = "from sources.json" if os.path.exists(config_file("sources.json")) else "default"
    for kind in ("meta", "stream"):
        host = doc_host(cfg[kind])
        try:
            man = addon_get(cfg[kind], "manifest.json", timeout=15) or {}
            name = str(man.get("name") or man.get("id") or "?")
            version = str(man.get("version") or "").strip()
            line("ok", f"{kind} addon", f"{host} - {name}"
                 + (f" {version}" if version else "") + f" ({where})")
        except (OSError, ValueError, AttributeError) as e:
            line("!!", f"{kind} addon", f"{host} - {e} ({where})")

    probe = None
    try:
        hits = search("one piece")
        line("ok" if hits else "!!", "meta search",
             f"'one piece' -> {len(hits)} result(s)"
             + (f", first {hits[0]['id']}" if hits else " (the addon may have changed)"))
        if hits:
            got = load_anime(hits[0]["id"])
            if got and got[1]:
                probe = (got[0], got[1][0])
            else:
                line("!!", "meta episodes", f"{hits[0]['id']} has none")
    except (OSError, ValueError, AttributeError) as e:
        line("!!", "meta search", f"'one piece' -> {e}")
    if probe:
        anime, ep = probe
        what = f"{anime.get('_key')} episode {epnum(ep)}"
        try:
            found = streams(anime, ep)
            text = f"{what} -> {len(found)} release(s)"
            if found and key:
                hashes = [s["infoHash"] for s in found if s.get("infoHash")]
                text += f", {len(torbox_cached(hashes))} cached on TorBox"
            line("ok" if found else "!!", "stream probe", text)
        except (OSError, ValueError, AttributeError, RuntimeError) as e:
            line("!!", "stream probe", f"{what} -> {e}")

    out.append("")
    out.append("SeaDex")
    if NO_SEADEX:
        line("--", "lookup", "turned off with --no-seadex")
    else:
        host = doc_host(SEADEX_API)
        try:
            query = {"filter": "(alID=21)", "expand": "trs", "perPage": "1",
                     "fields": SEADEX_FIELDS}
            data = fetch_json(SEADEX_API + "?" + urllib.parse.urlencode(query), timeout=15)
            items = data.get("items") or []
            sets = seadex_sets(items[0] if items else {})
            line("ok" if items else "!!", "api",
                 f"{host} - " + (f"1 entry for alID 21: {len(sets['best'])} best, "
                                  f"{len(sets['alt'])} alternative(s)" if items else
                                  "no entry for alID 21 (the API may have changed)"))
        except (OSError, ValueError, AttributeError) as e:
            line("!!", "api", f"{host} - {e}")
    count, newest = _doc_entries(seadex_file())
    if count:
        when = f", newest {_doc_age(time.time() - newest)}" if newest else ""
        line("ok", "cache", f"{doc_path(seadex_file())} - {count} show(s){when}")
    else:
        line("--", "cache", f"{doc_path(seadex_file())} - empty (filled on the first run)")

    out.append("")
    out.append("AniList")
    host = doc_host(ANILIST_MAP_API)
    try:
        data = fetch_json(f"{ANILIST_MAP_API}?idType=kitsuId&idValue=10740", timeout=15)
        mapped = ((data.get("data") or {}).get("mappings") or {}).get("anilistId")
        line("ok" if isinstance(mapped, int) else "!!", "mapping",
             f"{host} - kitsu:10740 -> " + (f"anilist {mapped}" if isinstance(mapped, int)
                                            else "no mapping (SeaDex cannot match)"))
    except (OSError, ValueError, AttributeError) as e:
        line("!!", "mapping", f"{host} - {e} (SeaDex cannot match)")
    count, _newest = _doc_entries(anilist_map_file())
    line("ok" if count else "--", "map cache",
         f"{doc_path(anilist_map_file())} - {count} show(s)")
    token = anilist_token()
    if not token:
        line("--", "login", "not logged in (tracking off; toranime --anilist)")
    else:
        try:
            reply = json.loads(fetch(
                ANILIST_API, {"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json",
                              "Accept": "application/json"},
                json.dumps({"query": "query { Viewer { name } }"}).encode(), timeout=15))
            name = mask_name(((reply.get("data") or {}).get("Viewer") or {}).get("name"))
            line("ok", "login", f"{name} (tracking on)" if name else
                 "signed in (tracking on)")
        except urllib.error.HTTPError as e:
            line("!!", "login", f"AniList refused the saved login (HTTP {e.code}) - "
                                 "toranime --anilist")
        except (OSError, ValueError, AttributeError) as e:
            line("!!", "login", f"AniList did not answer: {e}")

    print("\n".join(out))
    print()
    print("all checks passed" if not bad
          else f"{len(bad)} check(s) need attention: {', '.join(bad)}")
    print("nothing here is a key, a token or a full account name: safe to paste into an issue.")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser(description="stream anime in mpv through TorBox")
    ap.add_argument("anime", nargs="?",
                    help="search text, or an id the meta addon knows (kitsu:10740)")
    ap.add_argument("episode", nargs="?",
                    help="episode or range: 1151, 1151-1160, 1151- (through the newest)")
    lang_flag = ap.add_mutually_exclusive_group()
    lang_flag.add_argument("--dub", action="store_true", help="English dub")
    lang_flag.add_argument("--sub", action="store_true",
                           help="Japanese audio with English subs (the default when an "
                                "episode is given without --dub)")
    ap.add_argument("--list", action="store_true", help="list every episode and exit")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print stream urls instead of launching mpv")
    ap.add_argument("--m3u", nargs="?", const="", metavar="PATH",
                    help="write an .m3u playlist instead of launching mpv. links expire after "
                         "a few hours and only work from your current network")
    ap.add_argument("--mpv", default="mpv", help="path to mpv binary")
    ap.add_argument("--pick", action="store_true",
                    help="choose the release yourself; that group is preferred for this run")
    ap.add_argument("--style", metavar="NAME",
                    help="use a subtitle look from subs.json instead of the release's own "
                         "styling (--style native keeps the release's styling)")
    ap.add_argument("--no-skip", action="store_true",
                    help="don't skip openings and endings (skipped when the file marks "
                         "them as chapters)")
    ap.add_argument("--no-seadex", action="store_true",
                    help="don't ask releases.moe which release SeaDex recommends")
    ap.add_argument("--anilist", nargs="?", const="login", choices=("login", "logout"),
                    help="log in to AniList, so episodes you finish update your progress "
                         "there (--anilist logout to stop)")
    ap.add_argument("--torbox", nargs="?", const="login", choices=("login", "logout"),
                    help="save your TorBox API key, checked with TorBox before it is saved "
                         "(--torbox logout to remove it)")
    ap.add_argument("--setup", action="store_true",
                    help="find or install mpv and exit (the installers run this)")
    ap.add_argument("--doctor", action="store_true",
                    help="check TorBox, mpv, the addons, SeaDex and AniList, print a "
                         "pasteable diagnosis and exit (no key or token is printed)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="install mpv without asking if it is missing (scripts, CI)")
    ap.add_argument("--update", action="store_true",
                    help="update toranime to the newest version and exit")
    ap.add_argument("--no-update-check", action="store_true",
                    help="don't ask GitHub for a newer release on startup")
    ap.add_argument("--version", action="version", version=f"toranime {__version__}")
    args = ap.parse_args()

    _enable_vt()
    if interactive_ok():
        _sane_tty()                    # a terminal a killed player left raw would eat Enter
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # titles in scripts a codepage can't show
        except AttributeError:
            pass

    global NO_SEADEX
    NO_SEADEX = bool(args.no_seadex)
    if args.doctor:
        sys.exit(doctor(args.mpv))
    if args.anilist:
        (anilist_login if args.anilist == "login" else anilist_logout)()
        return
    if args.torbox:
        (torbox_login if args.torbox == "login" else torbox_logout)()
        return
    if args.setup:
        cmd = ensure_player(args.mpv, args.yes)
        print(MPV_ANDROID_HELP if cmd is None else f"ready: {' '.join(cmd)}")
        return
    if args.update:
        update()
        return
    # check the player before any searching, so a first run sets everything up front
    playing = not (args.list or args.print_only or args.m3u is not None)
    if playing and interactive_ok() and not args.no_update_check:
        update_notice()             # one line a day, before anything asks for input
    # check the player before any searching, so a first run sets everything up front
    cmd = ensure_player(args.mpv, args.yes) if playing else None
    if cmd and not args.no_skip:
        cmd = [*cmd, *skip_args()]
    if cmd:
        global LOOK_SELECTED
        explicit = args.style
        chosen = explicit if explicit is not None else default_style()
        BURN.clear()
        LOOK_SELECTED = False
        if explicit is not None:
            cmd = [*cmd, *style_args(explicit)]    # --style NAME: apply it now
            LOOK_SELECTED = str(explicit).lower() not in NATIVE_STYLE
            if LOOK_SELECTED:
                BURN.update(style_spec(explicit))
        elif chosen:
            # no flag: keep the look loaded as a second track, release's own subs on top
            cmd = [*cmd, *look_load_args(chosen)]
            BURN.update(style_spec(chosen))

    if playing and not (args.anime or args.episode or args.dub or args.sub):
        cont = continue_prompt()
        if cont:
            anime, eps, lang, idx, start = cont
            if args.pick:
                pick_release(anime, eps[idx], lang)
            else:
                choose_group(anime, eps[idx], lang)
            watch(cmd, anime, eps, idx, lang, start)
            return

    spec = args.episode
    anime, eps = find_anime(args.anime, auto=bool(spec))
    name = title_of(anime.get("title"))

    if args.list:
        for ep in eps:
            print(ep_label(ep))
        return

    history = load_history() if playing else {}
    seen = (history.get("shows") or {}).get(anime.get("_key")) or {}
    lang = "dub" if args.dub else "sub"
    if not (args.dub or args.sub) and not spec and interactive_ok():
        opts = [(LANG_LABEL[lg], lg) for lg in ("sub", "dub")]
        last_lang = [i for i, (_, lg) in enumerate(opts) if lg == seen.get("lang")]
        print(f"\n{name}")
        lang = opts[menu_pick(opts, (last_lang or [0])[0], "\naudio:")][1]

    avail = available(eps, lang)
    if not avail:
        sys.exit(f"{name} has no episodes")
    start = 0.0
    if spec:
        picked, is_range = select_eps(avail, spec, lang)
    else:
        # open the picker on where this show was left off, else on the newest episode
        point = resume_point(anime, avail, lang, history)
        ep = episode_picker(anime, avail, f"\n{name} ({lang}):",
                            initial=point[0] if point else len(avail) - 1)
        if ep is None:
            sys.exit("cancelled")
        picked, is_range = [avail.index(ep)], False
        if point and point[0] == picked[0] and point[1]:
            start = point[1]
            sys.stderr.write(f"resuming at {clock(start)}\n")

    if args.pick:
        pick_release(anime, avail[picked[0]], lang)
    else:
        choose_group(anime, avail[picked[0]], lang)

    if args.print_only or args.m3u is not None:
        resolved = resolve_many(anime, [avail[i] for i in picked], lang)
        if not resolved:
            sys.exit("nothing to play")
        entries = [(ep_title(name, ep), url) for ep, url, _ in resolved]
        if args.print_only:
            print("\n".join(url for _, url in entries))
            return
        first, last = epnum(resolved[0][0]), epnum(resolved[-1][0])
        span = first if first == last else f"{first}-{last}"
        path = args.m3u or f"{anime.get('_key', 'toranime').replace(':', '-')}-{span}-{lang}.m3u"
        write_m3u(path, entries, f"{name} {span} ({lang})")
        print(os.path.abspath(path))
        return

    watch(cmd, anime, avail, picked[0], lang, start, picked[-1] if is_range else None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\ncancelled")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"network error: {e}")
    except TorboxError as e:
        sys.exit(f"TorBox: {e}")
    except ValueError as e:
        sys.exit(f"unexpected response from an addon or TorBox (service changed?): {e}")
