"""
offline tests for toranime.py: no network, stdlib only.

    python -m unittest discover -s tests -v

the addons and TorBox are replaced by fixtures shaped like the real responses (Anime
Kitsu's search and meta, Torrentio's stream list, TorBox's checkcached / mylist /
createtorrent / requestdl), so every step of the pipeline runs without touching any
of them. the tests for the mpv scripts and a whole binge session drive a real mpv when
one is installed, and are skipped otherwise.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import zipfile
from unittest import mock

# the update check talks to GitHub: tests never do (UpdateCheck turns it back on)
os.environ["TORANIME_NO_UPDATE_CHECK"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import toranime  # noqa: E402

# every test writes toranime's cache (the sub-mode mpv script, ipc sockets), its watch
# history and its TorBox key here, never into the real folders
CACHE = None


def setUpModule():
    global CACHE
    CACHE = tempfile.TemporaryDirectory()
    for name, sub in (("cache_dir", "toranime"), ("state_dir", "state"),
                      ("config_dir", "config")):
        patcher = mock.patch.object(toranime, name, lambda sub=sub: os.path.join(CACHE.name, sub))
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(CACHE.cleanup)
    os.makedirs(toranime.config_dir(), exist_ok=True)
    with open(os.path.join(toranime.config_dir(), "torbox.key"), "w", encoding="utf-8") as fh:
        fh.write("test-token\n")

    class NoSeaDex(dict):
        """every show looks already answered, so no test calls releases.moe."""
        EMPTY = {"best": set(), "alt": set()}

        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            return self.EMPTY

    patcher = mock.patch.object(toranime, "SEADEX", NoSeaDex())
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


# ------------------------------------------------------------------ fixtures


ANIME = {
    "_key": "kitsu:10740", "kitsu_id": "10740", "imdb_id": "tt4508902",
    "anilist_id": 21, "title": "ONE PIECE", "year": 1999,
}
EPISODES = [
    {"number": 1150, "title": "Egghead A", "season": 1, "_id": "kitsu:10740:1150"},
    {"number": 1151, "title": "Her and Her Father's Dream!", "season": 1,
     "_id": "kitsu:10740:1151"},
    {"number": 1152, "title": "Egghead C", "season": 1, "_id": "kitsu:10740:1152",
     "filler": True},
    {"number": 1180, "title": "", "season": 1, "_id": "kitsu:10740:1180"},
]
META = {"meta": {
    "id": "kitsu:10740", "kitsu_id": "10740", "imdb_id": "tt4508902",
    "name": "ONE PIECE", "year": 1999,
    "videos": [dict(e, id=e["_id"], episode=e["number"], absoluteEpisode=e["number"],
                    imdb_id="tt4508902", imdbSeason=1, imdbEpisode=e["number"])
               for e in EPISODES],
}}
CATALOG = {"metas": [
    {"id": "kitsu:9", "name": "One Piece Film: Red", "animeType": "Movie"},
    {"id": "kitsu:10740", "name": "ONE PIECE", "animeType": "TV", "releaseInfo": "1999",
     "aliases": ["ONE PIECE", "One Piece"]},
]}
FILE_URL = "https://cdn.torbox.app/abc/ep1151.mkv?token=signed"
SUB_STREAM = {"name": "Torrentio\n1080p", "infoHash": "a" * 40, "fileIdx": 0,
              "title": "[SubsPlease] ONE PIECE - 1151 (1080p) [HEVC]"}
DUB_STREAM = {"name": "Torrentio\n1080p", "infoHash": "b" * 40, "fileIdx": 0,
              "title": "[Judas] ONE PIECE - 1151 (1080p) [Dual Audio] (English Dub)",
              "sources": ["tracker:udp://tracker.example:1337/announce"]}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class FakeNet:
    """the addons and TorBox from fixtures: `cached` is what TorBox already holds,
    `files` is what its mylist reports, `mapping` the anilist id it resolves to."""

    def __init__(self, streams=None, cached=("b" * 40,), files=None, link=FILE_URL,
                 mapping=21087, torbox_error=None):
        self.streams = [DUB_STREAM] if streams is None else streams
        self.torbox_error = torbox_error      # (status, body): TorBox answers with HTTP
        self.cached = set(cached)
        self.files = files if files is not None else [
            {"id": 11, "name": "ONE PIECE - 1151 [1080p].mkv", "size": 1400000000}]
        self.link, self.mapping = link, mapping
        self.calls, self.torrents = [], {}

    def __call__(self, url, headers=None, data=None, timeout=None):
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        path = parts.path
        if url.startswith("https://anime-kitsu.strem.fun/"):
            return json.dumps(CATALOG if "/catalog/" in path else META)
        if url.startswith("https://torrentio.strem.fun/"):
            return json.dumps({"streams": self.streams})
        if url.startswith(toranime.ANILIST_MAP_API.split("/api/")[0]):
            found = {} if self.mapping is None else {"anilistId": self.mapping}
            return json.dumps({"success": True, "data": {"mappings": found}})
        if not url.startswith(toranime.TORBOX_API):
            raise AssertionError(f"unexpected url {url}")
        if self.torbox_error:
            status, body = self.torbox_error
            raise urllib.error.HTTPError(url, status, "refused", {},
                                         io.BytesIO(json.dumps(body).encode()))
        if path.endswith("/user/me"):
            if (headers or {}).get("Authorization") == "Bearer bad":
                return json.dumps({"success": False, "error": "BAD_TOKEN",
                                   "detail": "That key is invalid"})
            return json.dumps({"success": True,
                               "data": {"email": "viewer@example.com", "plan": 3}})
        if path.endswith("/torrents/checkcached"):
            hashes = [h for h in query.get("hash", "").split(",") if h]
            return json.dumps({"success": True, "data": [
                {"hash": h, "cached": h in self.cached} for h in hashes]})
        if path.endswith("/torrents/mylist"):
            if "id" in query:
                return json.dumps({"success": True,
                                   "data": self.torrents.get(int(query["id"]))})
            return json.dumps({"success": True, "data": list(self.torrents.values())})
        if path.endswith("/torrents/createtorrent"):
            body = (data or b"").decode("utf-8", "replace")
            found = re.search(r"btih:([0-9a-fA-F]{40})", body)
            h = (found.group(1) if found else "").lower()
            if h not in self.cached:        # add_only_if_cached: TorBox refuses these
                return json.dumps({"success": False, "error": "NOT_CACHED",
                                   "detail": "That torrent is not cached"})
            if any(t["hash"] == h for t in self.torrents.values()):
                return json.dumps({"success": False, "error": "DUPLICATE_ITEM",
                                   "detail": "Already in your account"})
            tid = 900 + len(self.torrents)
            self.torrents[tid] = {
                "id": tid, "hash": h, "download_finished": h in self.cached,
                "download_state": "cached" if h in self.cached else "downloading",
                "progress": 1.0 if h in self.cached else 0.0, "files": self.files}
            return json.dumps({"success": True, "data": {"torrent_id": tid}})
        if path.endswith("/torrents/requestdl"):
            return json.dumps({"success": True, "data": self.link})
        raise AssertionError(f"unexpected url {url}")


@contextlib.contextmanager
def net(**kw):
    """swap the network for fixtures. yields the fake, whose .calls lists the urls."""
    fake = FakeNet(**kw)
    with mock.patch.object(toranime, "fetch", fake):
        yield fake


def run_main(*argv):
    """run toranime.main() with argv; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["toranime", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            toranime.main()
        except SystemExit as e:
            code = e.code
        finally:      # globals a run leaves behind must not leak into the next test
            toranime.BURN.clear()
            toranime.LOOK_SELECTED = False
    return code, out.getvalue(), err.getvalue()


class Terminal:
    """just enough of a VT100 to replay the picker: \\r \\n, CSI nA / K / J / m, wrapping."""

    def __init__(self, cols):
        self.cols, self.rows, self.y, self.x = cols, [[]], 0, 0

    def feed(self, raw):
        i = 0
        while i < len(raw):
            m = re.match(r"\x1b\[(\d*)([A-Za-z])", raw[i:])
            if m:
                n, cmd = int(m.group(1) or 1), m.group(2)
                if cmd == "A":
                    self.y = max(0, self.y - n)
                elif cmd == "K":
                    del self.rows[self.y][self.x:]
                elif cmd == "J":
                    del self.rows[self.y][self.x:]
                    del self.rows[self.y + 1:]
                i += len(m.group(0))
                continue
            c = raw[i]
            if c == "\r":
                self.x = 0
            elif c == "\n":
                self._down()
            else:
                if self.x >= self.cols:
                    self._down()
                row = self.rows[self.y]
                row.extend(" " * (self.x - len(row)))
                if self.x < len(row):
                    row[self.x] = c
                else:
                    row.append(c)
                self.x += 1
            i += 1
        return ["".join(r).rstrip() for r in self.rows]

    def _down(self):
        self.y, self.x = self.y + 1, 0
        while len(self.rows) <= self.y:
            self.rows.append([])


# ------------------------------------------------------------------ parsing

class Parsing(unittest.TestCase):
    def test_addon_url_from_any_manifest(self):
        self.assertEqual(toranime.addon_url("https://host/manifest.json", "stream/series/x.json"),
                         "https://host/stream/series/x.json")
        self.assertEqual(toranime.addon_url("https://host/sub/manifest.json?x=1", "/meta/a.json"),
                         "https://host/sub/meta/a.json")

    def test_sources_default_and_override(self):
        self.assertEqual(toranime.sources(), toranime.ADDONS)
        path = os.path.join(toranime.config_dir(), "sources.json")
        os.makedirs(toranime.config_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stream": "https://aiostreams.example/config/manifest.json"}, fh)
        try:
            cfg = toranime.sources()
            self.assertEqual(cfg["stream"], "https://aiostreams.example/config/manifest.json")
            self.assertEqual(cfg["meta"], toranime.ADDONS["meta"])
        finally:
            os.remove(path)

    def test_search_returns_kitsu_ids(self):
        with net() as fake:
            found = toranime.search("one piece")
        self.assertEqual([m["id"] for m in found], ["kitsu:9", "kitsu:10740"])
        self.assertIn("kitsu-anime-list/search=one%20piece.json", fake.calls[0])

    def test_load_anime_reads_episodes_from_the_meta_addon(self):
        with net() as fake:
            anime, eps = toranime.load_anime("kitsu:10740")
        self.assertEqual(anime["_key"], "kitsu:10740")
        self.assertEqual((anime["title"], anime["kitsu_id"], anime["imdb_id"]),
                         ("ONE PIECE", "10740", "tt4508902"))
        self.assertEqual([toranime.epnum(e) for e in eps], ["1150", "1151", "1152", "1180"])
        self.assertEqual((eps[1]["_id"], eps[1]["imdbEpisode"]), ("kitsu:10740:1151", 1151))
        self.assertTrue(urllib.parse.unquote(fake.calls[0]).endswith("/meta/series/kitsu:10740.json"))

    def test_load_anime_falls_back_to_an_episode_count(self):
        meta = {"meta": {"id": "kitsu:5", "name": "X", "episodes": 3}}
        with mock.patch.object(toranime, "addon_get", lambda *a, **k: meta):
            _, eps = toranime.load_anime("kitsu:5")
        self.assertEqual([toranime.epnum(e) for e in eps], ["1", "2", "3"])

    def test_load_anime_is_none_when_the_addon_has_no_such_show(self):
        def missing(*a, **k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch.object(toranime, "addon_get", missing):
            self.assertIsNone(toranime.load_anime("kitsu:404"))

    def test_stream_ids_try_the_meta_id_then_kitsu_then_imdb(self):
        self.assertEqual(toranime.stream_ids(ANIME, EPISODES[1]),
                         ["kitsu:10740:1151", "tt4508902:1:1151"])
        self.assertEqual(toranime.stream_ids({}, {"number": 4}), [])

    def test_titles_and_numbers(self):
        self.assertEqual(toranime.title_of({"romaji": "Shingeki", "english": "Attack on Titan"}),
                         "Attack on Titan")
        self.assertEqual(toranime.title_of({}), "?")
        self.assertEqual(toranime.titles("plain"), ["plain"])
        self.assertEqual(toranime.epnum({"number": 1151}), "1151")
        self.assertEqual(toranime.epnum({"number": 6.5}), "6.5")
        self.assertEqual(toranime.epnum({"episode_number": 3.0}), "3")
        self.assertIn("[filler]", toranime.ep_label(EPISODES[2]))
        self.assertTrue(toranime.ep_label(EPISODES[1]).startswith(" 1151  Her"))

    def test_select_eps(self):
        eps = EPISODES[:3]
        self.assertEqual(toranime.select_eps(eps, "1151", "dub"), ([1], False))
        self.assertEqual(toranime.select_eps(eps, "1150-1151", "dub"), ([0, 1], True))
        self.assertEqual(toranime.select_eps(eps, "1151-", "dub"), ([1, 2], True))
        with self.assertRaises(SystemExit) as cm:
            toranime.select_eps(eps, "9999", "dub")
        self.assertIn("1150-1152", str(cm.exception))
        with self.assertRaises(SystemExit):
            toranime.select_eps(eps, "abc", "dub")


# ------------------------------------------------------------------ network steps

class Pipeline(unittest.TestCase):
    def test_streams_are_tagged_with_the_id_that_found_them(self):
        with net() as fake:
            found = toranime.streams(ANIME, EPISODES[1])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["_id"], "kitsu:10740:1151")
        self.assertTrue(urllib.parse.unquote(fake.calls[0]).endswith(
            "/stream/series/kitsu:10740:1151.json"))

    def test_streams_are_empty_when_the_addon_knows_nothing(self):
        with net(streams=[]):
            self.assertEqual(toranime.streams(ANIME, EPISODES[1]), [])

    def test_dub_detection(self):
        self.assertTrue(toranime.is_dub(DUB_STREAM))
        self.assertFalse(toranime.is_dub(SUB_STREAM))
        self.assertTrue(toranime.is_dub({"title": "[Group] Show - 01 [Multi-Audio]"}))

    def test_release_group_and_size_are_read_from_the_label(self):
        self.assertEqual(toranime.release_group({"title": "[Sokudo] Show S01 [1080p BD AV1]"}),
                         "Sokudo")
        self.assertEqual(toranime.release_group({"title": "Torrentio 1080p [sam] Show S1"}),
                         "sam")
        self.assertIsNone(toranime.release_group(
            {"title": "Show [S1 S2 OVA&ODA][Dual Audio][1080p][BD][HEVC]"}))
        self.assertIsNone(toranime.release_group({"title": "Show - S01 (2160p) (Bilibili)"}))
        self.assertEqual(toranime.size_bytes("💾 1.31 GB per file"), int(1.31 * 1024 ** 3))
        self.assertEqual(toranime.size_bytes("700 MB"), 700 * 1024 ** 2)
        self.assertIsNone(toranime.size_bytes("no size here"))

    def test_prefs_put_your_group_first_and_avoided_last(self):
        judas = {"title": "[Judas] Show - 01 [1080p]", "infoHash": "a" * 40}
        sokudo = {"title": "[Sokudo] Show - 01 [1080p]", "infoHash": "b" * 40}
        cached = {"a" * 40, "b" * 40}
        def rank(want):
            return sorted([judas, sokudo],
                          key=lambda s: toranime.stream_rank(s, cached, want))

        self.assertEqual(rank({"prefer_groups": ["Sokudo"]})[0], sokudo)
        self.assertEqual(rank({"avoid_groups": ["Judas"]})[0], sokudo)
        self.assertEqual(rank({})[0], judas)                    # no prefs: the addon's order
        small = {"title": "[Sokudo] Show - 01 [720p]", "infoHash": "c" * 40}
        huge = {"title": "[Other] Show - 01 [2160p]", "infoHash": "d" * 40}
        both = {"c" * 40, "d" * 40}
        self.assertEqual(sorted([small, huge],
                                key=lambda s: toranime.stream_rank(s, both,
                                                                  {"prefer_groups": ["Sokudo"]}
                                                                  ))[0], small)

    def test_min_height_and_max_size_are_penalties_not_filters(self):
        small = {"title": "[A] Show - 01 [720p] 💾 700 MB", "infoHash": "a" * 40}
        big = {"title": "[B] Show - 01 [2160p] 💾 20 GB", "infoHash": "b" * 40}
        cached = {"a" * 40, "b" * 40}
        def first(want):
            return sorted([small, big],
                          key=lambda s: toranime.stream_rank(s, cached, want))[0]

        self.assertEqual(first({}), big)
        self.assertEqual(first({"min_height": 1080}), big)
        self.assertEqual(first({"max_size": 8}), small)
        # it never drops a release: an only candidate still sorts
        self.assertIsInstance(toranime.stream_rank(small, cached, {"min_height": 2160}), tuple)

    def test_prefs_file_and_the_pick_group(self):
        path = os.path.join(toranime.config_dir(), "prefs.json")
        os.makedirs(toranime.config_dir(), exist_ok=True)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"prefer_groups": ["Sokudo"], "avoid_groups": ["Judas"],
                       "min_height": 1080, "max_size": 8}, fh)
        got = toranime.prefs()
        self.assertEqual(got["prefer_groups"], ["Sokudo"])
        self.assertEqual(got["avoid_groups"], ["Judas"])
        self.assertEqual((got["min_height"], got["max_size"]), (1080, 8))
        toranime.PICK["prefer_groups"] = ["Breeze"]
        self.addCleanup(toranime.PICK.clear)
        self.assertEqual(toranime.prefs()["prefer_groups"], ["Breeze", "Sokudo"])

    def test_release_menu_lists_each_group_once_and_enter_keeps_auto(self):
        judas = {"title": "[Judas] Show - 01 [1080p]", "infoHash": "a" * 40}
        judas720 = {"title": "[Judas] Show - 01 [720p]", "infoHash": "b" * 40}
        sokudo = {"title": "[Sokudo] Show - 01 [1080p]", "infoHash": "c" * 40}
        seen = {}

        def fake_menu(options, default, header):
            seen["options"], seen["default"] = options, default
            return default

        toranime.PICK.clear()
        self.addCleanup(toranime.PICK.clear)
        with mock.patch.object(toranime, "streams", lambda a, e: [judas720, judas, sokudo]), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "menu_pick", fake_menu), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                contextlib.redirect_stderr(io.StringIO()):
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        labels = [label for label, _ in seen["options"]]
        self.assertEqual([group for _, group in seen["options"]], ["Judas", "Sokudo", None])
        self.assertEqual(seen["default"], len(labels) - 1)        # Enter = the automatic pick
        self.assertIn("[Judas] cached", labels[0])
        self.assertIn("1080p", labels[0])                        # the group's best release
        self.assertTrue(labels[-1].startswith("top pick"))
        self.assertNotIn("prefer_groups", toranime.PICK)          # Enter changed nothing

    def test_release_menu_choice_becomes_the_preferred_group(self):
        streams = [{"title": f"[{g}] Show - 01 [1080p]", "infoHash": c * 40}
                   for g, c in (("Judas", "a"), ("Sokudo", "b"))]
        toranime.PICK.clear()
        self.addCleanup(toranime.PICK.clear)
        with mock.patch.object(toranime, "streams", lambda a, e: streams), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "menu_pick", lambda *a, **kw: 1), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        self.assertEqual(toranime.PICK["prefer_groups"], ["Sokudo"])
        self.assertIn("preferring [Sokudo]", err.getvalue())

    def test_too_many_groups_become_a_picker_you_can_type_narrow(self):
        # the numbered menu has nine lines, so past eight groups the rest had no key:
        # switch to the live-filter picker, with every group still reachable
        many = [{"title": f"[Group{i}] Show - 01 [1080p]", "infoHash": f"{i:x}" * 40}
                for i in range(12)]
        seen = {}

        def pick(items, label, header, noun="items", initial=0):
            seen.update(noun=noun, items=list(items), labels=[label(it) for it in items])
            return items[9]
        toranime.PICK.clear()
        self.addCleanup(toranime.PICK.clear)
        with mock.patch.object(toranime, "streams", lambda a, e: many), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "fuzzy_pick", pick), \
                mock.patch.object(toranime, "menu_pick") as menu, \
                contextlib.redirect_stderr(io.StringIO()):
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        menu.assert_not_called()
        self.assertEqual(seen["noun"], "release groups")
        self.assertEqual(len(seen["items"]), 12)                  # nothing dropped
        self.assertIn(f"[{seen['items'][9][0]}] cached", seen["labels"][9])
        self.assertEqual(toranime.PICK["prefer_groups"], [seen["items"][9][0]])
        with mock.patch.object(toranime, "streams", lambda a, e: many), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "fuzzy_pick", lambda *a, **kw: None), \
                contextlib.redirect_stderr(io.StringIO()):
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        self.assertEqual(toranime.PICK["prefer_groups"], [seen["items"][9][0]])   # Esc: kept

    def test_a_short_release_list_stays_numbered_and_a_single_group_is_skipped(self):
        eight = [{"title": f"[Group{i}] Show - 01 [1080p]", "infoHash": f"{i:x}" * 40}
                 for i in range(8)]
        seen = {}
        with mock.patch.object(toranime, "streams", lambda a, e: eight), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "menu_pick",
                                  lambda options, default, header: seen.update(
                                      n=len(options), default=default) or default), \
                mock.patch.object(toranime, "fuzzy_pick") as fuzzy:
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        fuzzy.assert_not_called()
        self.assertEqual(seen["n"], 9)                            # 8 groups + auto
        single = [{"title": "[Judas] Show - 01 [1080p]", "infoHash": "a" * 40}]
        with mock.patch.object(toranime, "streams", lambda a, e: single), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "menu_pick") as menu:
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        menu.assert_not_called()                                  # nothing to choose
        with mock.patch.object(toranime, "interactive_ok", lambda: False), \
                mock.patch.object(toranime, "menu_pick") as menu:
            toranime.choose_group(ANIME, EPISODES[1], "sub")
        menu.assert_not_called()                                  # scripts and pipes

    def test_pick_chooses_a_release_and_prefers_its_group(self):
        judas = {"title": "[Judas] Show - 01 [1080p]", "infoHash": "a" * 40}
        sokudo = {"title": "[Sokudo] Show - 01 [1080p]", "infoHash": "b" * 40}
        seen = {}

        def fake_pick(items, label, header, **kw):
            seen["labels"] = [label(i) for i in items]
            return sokudo

        self.addCleanup(toranime.PICK.clear)
        with mock.patch.object(toranime, "streams", lambda a, e: [judas, sokudo]), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: set(hashes)), \
                mock.patch.object(toranime, "fuzzy_pick", fake_pick), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            toranime.pick_release(ANIME, EPISODES[1], "sub")
        self.assertEqual(toranime.PICK["prefer_groups"], ["Sokudo"])
        self.assertIn("Sokudo", seen["labels"][1])
        self.assertIn("preferring [Sokudo]", err.getvalue())

    def test_ranking_puts_cached_releases_first(self):
        cached = {"c" * 40}
        small = {"infoHash": "c" * 40, "title": "720p"}
        big = {"infoHash": "d" * 40, "title": "2160p"}
        self.assertEqual(sorted([big, small], key=lambda s: toranime.stream_rank(s, cached)),
                         [small, big])

    def test_magnet_carries_the_trackers(self):
        magnet = toranime.magnet_of(DUB_STREAM)
        self.assertTrue(magnet.startswith(f"magnet:?xt=urn:btih:{'b' * 40}"))
        self.assertIn("&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce", magnet)

    def test_multipart_body(self):
        body, ctype = toranime.multipart({"magnet": "magnet:?xt=urn:btih:x", "allow_zip": "false"})
        self.assertIn("multipart/form-data; boundary=", ctype)
        self.assertIn(b'name="magnet"', body)
        self.assertTrue(body.endswith(b"--\r\n"))

    def test_cached_checks_a_hundred_hashes_at_a_time(self):
        hashes = [f"{i:040x}" for i in range(150)]
        with net(cached=hashes[:120]) as fake:
            self.assertEqual(toranime.torbox_cached(hashes), set(hashes[:120]))
        self.assertEqual(len(fake.calls), 2)

    def test_a_bad_key_says_where_the_key_lives(self):
        bad = toranime.refused("AUTH_ERROR", "An error occurred while verifying your token.")
        self.assertIn("torbox.key", str(bad))
        self.assertEqual(bad.code, "AUTH_ERROR")
        # every other refusal stays as it was
        self.assertEqual(str(toranime.refused("NOT_CACHED", "not cached")), "not cached")
        self.assertEqual(str(toranime.refused(None, None)), "TorBox request failed")

    def test_episode_numbers_in_file_names(self):
        self.assertTrue(toranime.episode_in("[G] Show - 05 [1080p].mkv", 5))
        self.assertTrue(toranime.episode_in("Show.S01E05.1080p.mkv", 5))
        self.assertTrue(toranime.episode_in("Show - 1151v2.mkv", 1151))
        self.assertFalse(toranime.episode_in("[G] Show - 15 [1080p].mkv", 5))
        # a batch's folder says "Seasons 1-2": that is not episode 1
        in_batch = "[Judas] Show (Seasons 1-2)/[Judas] Show - S02E12.mkv"
        self.assertFalse(toranime.episode_in(in_batch, 1))
        self.assertTrue(toranime.episode_in("[Judas] Show (Seasons 1-2)/Show - S01E01.mkv", 1))
        # S01E08 is episode 8, not episode 1
        self.assertFalse(toranime.episode_in("Show/Show - S01E08.mkv", 1))
        self.assertTrue(toranime.episode_in("Show/Show - S01E08.mkv", 8))

    def test_pick_file_prefers_the_named_episode_then_the_biggest(self):
        files = [{"id": 1, "name": "Show - 01.mkv", "size": 900},
                 {"id": 2, "name": "Show - 02.mkv", "size": 100},
                 {"id": 3, "name": "Show - 03.mkv", "size": 800}]
        self.assertEqual(toranime.pick_file(files, 2)["id"], 2)
        self.assertEqual(toranime.pick_file(files, 99)["id"], 1)
        self.assertIsNone(toranime.pick_file([], 1))

    def test_pick_file_uses_the_season_inside_a_multi_season_batch(self):
        pack = [{"id": 1, "name": "[J] Show (Seasons 1-2)/[J] Show - S02E12.mkv", "size": 900},
                {"id": 2, "name": "[J] Show (Seasons 1-2)/[J] Show - S01E01.mkv", "size": 100},
                {"id": 3, "name": "[J] Show (Seasons 1-2)/[J] Show - S01E02.mkv", "size": 800}]
        self.assertEqual(toranime.pick_file(pack, 1, season=1)["id"], 2)
        self.assertEqual(toranime.pick_file(pack, 12, season=2)["id"], 1)
        self.assertEqual(toranime.pick_file(pack, 1)["id"], 2)      # no season needed: only E01
        # a season-less name still wins over another season's file
        loose = [{"id": 4, "name": "Show/Show - S02E01.mkv", "size": 900},
                 {"id": 5, "name": "Show/Show - 01.mkv", "size": 100}]
        self.assertEqual(toranime.pick_file(loose, 1, season=1)["id"], 5)

    def test_human_size(self):
        self.assertEqual([toranime.human_size(n) for n in (1400000000, 900 * 1024 ** 2, 2048)],
                         ["1.30 GB", "900 MB", "2 KB"])

    def test_resolve_end_to_end_through_torbox(self):
        with net() as fake:
            url, info = toranime.resolve(ANIME, EPISODES[1], "dub")
        self.assertEqual(url, FILE_URL)
        self.assertEqual((info["sizeHuman"], info["infoHash"]), ("1.30 GB", "b" * 40))
        self.assertTrue(any("/torrents/checkcached" in c for c in fake.calls))
        self.assertNotIn("test-token", url)                  # mpv never sees the key
        keyed = [c for c in fake.calls if "test-token" in c]
        self.assertEqual(len(keyed), 1)                      # only requestdl, as TorBox requires
        self.assertIn("/torrents/requestdl", keyed[0])

    def test_resolve_reuses_a_torrent_already_in_the_account(self):
        with net() as fake:
            fake.torrents[7] = {"id": 7, "hash": "b" * 40, "download_finished": True,
                                "files": fake.files}
            self.assertEqual(toranime.resolve(ANIME, EPISODES[1], "dub")[0], FILE_URL)
        self.assertFalse([c for c in fake.calls if "createtorrent" in c])

    def test_resolve_survives_a_duplicate_torrent(self):
        with net() as fake:
            toranime.resolve(ANIME, EPISODES[1], "dub")
            self.assertEqual(toranime.resolve(ANIME, EPISODES[1], "dub")[0], FILE_URL)
        self.assertEqual(len(fake.torrents), 1)                  # reused, not added twice

    def test_resolve_without_a_release(self):
        with net(streams=[]):
            with self.assertRaisesRegex(LookupError, "no releases"):
                toranime.resolve(ANIME, EPISODES[1], "dub")

    def test_resolve_dub_ignores_sub_only_releases(self):
        with net(streams=[SUB_STREAM]):
            with self.assertRaisesRegex(LookupError, "no dub release"):
                toranime.resolve(ANIME, EPISODES[1], "dub")
        with net(streams=[SUB_STREAM], cached=("a" * 40,)):
            self.assertEqual(toranime.resolve(ANIME, EPISODES[1], "sub")[0], FILE_URL)

    def test_a_torbox_403_is_reported_and_points_at_the_key(self):
        refused = (403, {"error": "BAD_TOKEN",
                         "detail": "An error occurred while verifying your token."})
        with net(torbox_error=refused), self.assertRaises(toranime.TorboxError) as caught:
            toranime.resolve(ANIME, EPISODES[1], "dub")
        self.assertEqual(caught.exception.code, "BAD_TOKEN")
        self.assertIn("torbox.key", str(caught.exception))      # tells you what to check

    def test_a_torbox_error_that_is_not_about_the_key_stays_plain(self):
        with net(torbox_error=(500, {"error": "SERVER_ERROR", "detail": "try later"})), \
                self.assertRaises(toranime.TorboxError) as caught:
            toranime.resolve(ANIME, EPISODES[1], "dub")
        self.assertEqual(str(caught.exception), "try later")
        self.assertNotIn("torbox.key", str(caught.exception))

    def test_resolve_says_nothing_is_cached_when_torbox_has_none(self):
        with net(cached=()):
            with self.assertRaisesRegex(LookupError, "no playable release"):
                toranime.resolve(ANIME, EPISODES[1], "dub")

    def test_wait_download_polls_a_release_torbox_is_still_fetching(self):
        records = [{"download_state": "downloading", "progress": 0.5},
                   {"download_state": "completed", "download_finished": True,
                    "files": [{"id": 3, "name": "ep.mkv", "size": 10}]}]
        err = io.StringIO()
        with mock.patch.object(toranime, "torbox_state", side_effect=[(r, r.get("files") or [])
                                                                     for r in records]), \
                mock.patch.object(toranime.time, "sleep", lambda s: None), \
                contextlib.redirect_stderr(err):
            torrent, files = toranime.wait_download(9)
        self.assertTrue(torrent["download_finished"])
        self.assertEqual(files[0]["id"], 3)
        self.assertIn("TorBox is fetching the release: downloading 50%", err.getvalue())

    def test_wait_download_gives_up_when_torbox_fails(self):
        with mock.patch.object(toranime, "torbox_state",
                               return_value=({"download_state": "failed"}, [])), \
                self.assertRaisesRegex(LookupError, "could not fetch"):
            toranime.wait_download(9, verbose=False)

    def test_resolve_many_keeps_order_and_skips_failures(self):
        def flaky(anime, ep, lang, verbose=True):
            if toranime.epnum(ep) == "1151":
                raise LookupError("nope")
            return fake_resolve(anime, ep, lang)
        err = io.StringIO()
        with mock.patch.object(toranime, "resolve", flaky), contextlib.redirect_stderr(err):
            got = toranime.resolve_many(ANIME, EPISODES[:2], "dub")
        self.assertEqual([toranime.epnum(ep) for ep, _, _ in got], ["1150"])
        self.assertIn("skipping episode 1151", err.getvalue())

    def test_find_anime_prefers_the_exact_title_and_opens_ids_directly(self):
        err = io.StringIO()
        with net(), contextlib.redirect_stderr(err):
            anime, _ = toranime.find_anime("one piece", auto=True)
            self.assertEqual(anime["_key"], "kitsu:10740")
            direct, _ = toranime.find_anime("kitsu:10740", auto=True)       # an id, no search
            self.assertEqual(direct["_key"], "kitsu:10740")
        self.assertIn("using ONE PIECE", err.getvalue())


# ------------------------------------------------------------------ main()

class Rename(unittest.TestCase):
    """files left behind by the old name are still read; new writes go to the new one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.new = os.path.join(self.tmp.name, toranime.APP)
        self.old = os.path.join(self.tmp.name, toranime.LEGACY)
        os.makedirs(self.old)
        for name in ("config_dir", "state_dir"):
            patch = mock.patch.object(toranime, name, lambda: self.new)
            patch.start()
            self.addCleanup(patch.stop)

    def put(self, folder, name, text):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_the_key_and_sources_fall_back_to_the_old_folder(self):
        self.put(self.old, "torbox.key", "old-key\n")
        self.put(self.old, "sources.json", '{"stream": "https://old.example/manifest.json"}')
        self.assertEqual(toranime.torbox_token(), "old-key")
        self.assertEqual(toranime.sources()["stream"], "https://old.example/manifest.json")
        self.put(self.new, "torbox.key", "new-key\n")
        self.assertEqual(toranime.torbox_token(), "new-key")        # the new folder wins

    def test_history_is_read_there_and_then_saved_here(self):
        self.put(self.old, "history.json", json.dumps(
            {"shows": {"kitsu:1": {"lang": "sub", "episode": "3"}}, "last": "kitsu:1"}))
        self.assertEqual(toranime.load_history()["last"], "kitsu:1")
        toranime.remember({"_key": "kitsu:1", "title": "Show"}, {"number": 4}, "sub")
        with open(os.path.join(self.new, "history.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["shows"]["kitsu:1"]["episode"], "4")
        self.assertEqual(saved["last"], "kitsu:1")
        self.assertTrue(os.path.exists(os.path.join(self.old, "history.json")))   # untouched

    def test_a_fresh_install_writes_only_to_the_new_folder(self):
        self.assertEqual(toranime.config_file("torbox.key"),
                         os.path.join(self.new, "torbox.key"))
        self.assertEqual(toranime.state_file("history.json"),
                         os.path.join(self.new, "history.json"))


class Main(unittest.TestCase):
    def test_print_single_episode(self):
        with net():
            code, out, err = run_main("one piece", "1151", "--dub", "--print")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), FILE_URL)

    def test_print_range(self):
        with net():
            code, out, err = run_main("one piece", "1150-1152", "--dub", "--print")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), [FILE_URL] * 3)

    def test_list(self):
        with net():
            code, out, _ = run_main("kitsu:10740", "--list")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("1151  Her and Her Father's Dream!", lines[1])
        self.assertIn("[filler]", lines[2])

    def test_m3u(self):
        with net(), tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.m3u")
            code, out, err = run_main("one piece", "1150-1151", "--dub", "--m3u", path)
            self.assertEqual(code, 0, err)
            text = read(path)
        self.assertTrue(text.startswith("#EXTM3U\n#PLAYLIST:ONE PIECE 1150-1151 (dub)"))
        self.assertIn("#EXTINF:-1,ONE PIECE - 1151 - Her and Her Father's Dream!\n" + FILE_URL, text)

    def test_dub_episode_with_only_sub_releases(self):
        with net(streams=[SUB_STREAM]):
            code, _, err = run_main("one piece", "1151", "--dub", "--print")
        self.assertEqual(code, "nothing to play")
        self.assertIn("no dub release", err)

    def test_the_cli_reports_an_unreachable_addon_instead_of_tracing(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "toranime"))
            with open(os.path.join(home, "toranime", "sources.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"meta": "http://127.0.0.1:9/manifest.json"}, fh)   # nothing listens
            env = {**os.environ, "XDG_CONFIG_HOME": home, "APPDATA": home, "HOME": home,
                   "USERPROFILE": home, "NO_PROXY": "*", "no_proxy": "*"}
            proc = subprocess.run([sys.executable, os.path.join(ROOT, "toranime.py"),
                                   "one punch man", "--list"],
                                  capture_output=True, text=True, env=env, timeout=60, cwd=ROOT)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("network error", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_torbox_key_says_where_it_goes(self):
        key = os.path.join(toranime.config_dir(), "torbox.key")
        os.rename(key, key + ".bak")
        try:
            with net():
                code, _, _ = run_main("one piece", "1151", "--dub", "--print")
        finally:
            os.rename(key + ".bak", key)
        self.assertIn("no TorBox API key", str(code))

    def test_setup_reports_player(self):
        with mock.patch.object(toranime, "find_mpv", return_value=["/usr/bin/mpv"]):
            code, out, _ = run_main("--setup")
        self.assertEqual((code, out.strip()), (0, "ready: /usr/bin/mpv"))

    def test_chapter_skipper_loads_unless_no_skip(self):
        played = []
        with net(), mock.patch.object(toranime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(toranime, "watch", lambda cmd, *rest: played.append(cmd)):
            run_main("one piece", "1151", "--dub")
            run_main("one piece", "1151", "--dub", "--no-skip")
        skipper = os.path.join(CACHE.name, "toranime", "skip-chapters.lua")
        self.assertEqual(played, [["mpv", f"--script={skipper}"], ["mpv"]])
        self.assertEqual(read(skipper), toranime.CHAPTER_SKIPPER)

    def test_a_subtitle_style_from_subs_json_reaches_the_player(self):
        path = os.path.join(toranime.config_dir(), "subs.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"presets": {"roboto": {"font": "Roboto Medium"}}}, fh)
        self.addCleanup(os.remove, path)
        played = []
        with net(), mock.patch.object(toranime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(toranime, "watch", lambda cmd, *rest: played.append(cmd)):
            run_main("one piece", "1151", "--dub", "--no-skip", "--style", "roboto")
            run_main("one piece", "1151", "--dub", "--no-skip", "--style", "native")
            run_main("one piece", "1151", "--dub", "--no-skip")
        self.assertIn("--sub-ass-override=yes", played[0])
        self.assertIn("--sub-font=Roboto Medium", played[0])
        self.assertEqual(played[1:], [["mpv"], ["mpv"]])     # untouched unless asked

    def test_version_matches_pyproject_and_changelog(self):
        pyproject = read(os.path.join(ROOT, "pyproject.toml"))
        changelog = read(os.path.join(ROOT, "CHANGELOG.md"))
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
                         toranime.__version__)
        self.assertIn(f"## [{toranime.__version__}]", changelog)


# ------------------------------------------------------------------ terminal UI

class Picker(unittest.TestCase):
    def pick(self, keys, cols=120, lines=40, header="\nONE PIECE (sub):"):
        eps = [{"number": n, "title": f"Episode {n} " + "long title " * (8 if n % 7 == 0 else 0)}
               for n in range(1, 1181)]
        keys = iter(keys)
        out = io.StringIO()
        with mock.patch.object(toranime, "interactive_ok", return_value=True), \
                mock.patch.object(toranime, "_getkey", lambda: next(keys)), \
                mock.patch.object(toranime.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((cols, lines))), \
                contextlib.redirect_stdout(out):
            picked = toranime.fuzzy_pick(eps, toranime.ep_label, header, noun="episodes",
                                        initial=len(eps) - 1)
        return picked, Terminal(cols).feed(out.getvalue())

    def test_redraw_never_duplicates_header_or_hint(self):
        moves = [("key", "up")] * 30 + [("key", "down")] * 5 + [("key", "enter")]
        for cols, lines in ((120, 40), (60, 14), (30, 8)):
            picked, screen = self.pick(moves, cols, lines)
            self.assertEqual(picked["number"], 1155)
            self.assertEqual(sum("ONE PIECE (sub):" in s for s in screen), 1, (cols, screen))
            self.assertEqual(sum(s.startswith("type to filter") for s in screen), 1, (cols, screen))
            self.assertLessEqual(len(screen), lines + 2)   # header block + one redraw region

    def test_typing_filters_by_episode_prefix(self):
        keys = [("char", c) for c in "1151"] + [("key", "enter")]
        picked, screen = self.pick(keys)
        self.assertEqual(picked["number"], 1151)
        self.assertIn("1/1180 episodes", screen[-2])

    def test_escape_cancels(self):
        self.assertIsNone(self.pick([("key", "esc")])[0])


@unittest.skipUnless(hasattr(os, "openpty") and toranime.termios, "needs a POSIX terminal")
class Keys(unittest.TestCase):
    """keys typed into a real pseudo-terminal, sent the way Linux and macOS terminals send them."""

    def setUp(self):
        self.master, slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        toranime.tty.setcbreak(slave)            # keys written below are readable at once
        # what a terminal looks like to Python: a text wrapper over a buffered reader
        tty_in = io.TextIOWrapper(io.BufferedReader(io.FileIO(slave, "rb", closefd=False)),
                                  encoding="utf-8")
        for patch in (mock.patch.object(sys, "stdin", tty_in),
                      mock.patch.object(toranime, "_held", bytearray())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_escape_sequences_arrive_whole(self):
        cases = [
            (b"\x1b[A", [("key", "up")]),
            (b"\x1b[B", [("key", "down")]),
            (b"\x1bOA", [("key", "up")]),               # application cursor mode
            (b"\x1b[1;5B", [("key", "down")]),          # Ctrl+Down
            (b"\x1b[3~", [("key", "")]),                # Delete: ignored, not typed as "~"
            (b"\x1b", [("key", "esc")]),
            (b"\x1b[Ax", [("key", "up"), ("char", "x")]),
            (b"ab", [("char", "a"), ("char", "b")]),
            ("é".encode(), [("char", "é")]),
            (b"\r", [("key", "enter")]),
            (b"\x7f", [("key", "backspace")]),
        ]
        for typed, want in cases:
            with self.subTest(typed=typed):
                os.write(self.master, typed)
                self.assertEqual([toranime._getkey() for _ in want], want)

    def test_arrows_move_the_picker_instead_of_closing_it(self):
        os.write(self.master, b"\x1b[B\x1b[B\x1b[A\r")
        with mock.patch.object(toranime, "interactive_ok", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            picked = toranime.fuzzy_pick(["one", "two", "three"], str, "\npick:")
        self.assertEqual(picked, "two")


@unittest.skipUnless(hasattr(os, "openpty") and toranime.termios, "needs a POSIX terminal")
class SaneTerminal(unittest.TestCase):
    """prompts still read a line when a killed player left the terminal in raw mode."""

    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        tty_in = io.TextIOWrapper(io.BufferedReader(io.FileIO(self.slave, "rb", closefd=False)),
                                  encoding="utf-8")
        for patch in (mock.patch.object(sys, "stdin", tty_in),
                      mock.patch.object(sys, "stdout", io.StringIO())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_enter_still_ends_the_line_after_a_raw_terminal_was_left_behind(self):
        toranime.tty.setraw(self.slave)         # what a killed mpv leaves: no Enter
        later = threading.Timer(0.2, os.write, (self.master, b"one piece\r"))
        later.start()
        self.addCleanup(later.cancel)
        self.assertEqual(toranime.ask("search anime: "), "one piece")

    def test_the_terminal_goes_back_to_line_mode(self):
        termios = toranime.termios
        state = toranime._tty_state()
        toranime.tty.setraw(self.slave)         # no canonical mode, no echo, CR stays CR
        toranime._tty_restore(state)
        after = termios.tcgetattr(self.slave)
        self.assertTrue(after[3] & (termios.ICANON | termios.ECHO | termios.ISIG))
        self.assertTrue(after[0] & termios.ICRNL)


class UpdateCheck(unittest.TestCase):
    """the daily "a newer release exists" line: cheap, quiet, and never in the way."""

    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        for patch in (mock.patch.object(toranime, "cache_dir", return_value=self.cache.name),
                      mock.patch.dict(os.environ, {"TORANIME_NO_UPDATE_CHECK": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, tag=None, now=None, asked=None):
        """update_notice() with GitHub's answer replaced by `tag`."""
        def fake(timeout=2.5):
            if asked is not None:
                asked.append(tag)
            return toranime.as_version(tag) if tag else None
        err = io.StringIO()
        with mock.patch.object(toranime, "latest_release", side_effect=fake), \
                contextlib.redirect_stderr(err):
            line = toranime.update_notice(now=now)
        return line, err.getvalue()

    def test_a_newer_release_is_announced(self):
        line, err = self.check("v9.9.9")
        self.assertIn("toranime 9.9.9 is available", line)
        self.assertIn("toranime --update", line)
        self.assertIn(toranime.__version__, line)        # what you have
        self.assertEqual(err.strip(), line)              # exactly one line, on stderr

    def test_the_newest_version_is_silent(self):
        self.assertEqual(self.check(f"v{toranime.__version__}"), (None, ""))

    def test_github_is_asked_once_a_day(self):
        asked = []
        self.check("v9.9.9", now=1000, asked=asked)
        self.check("v9.9.9", now=1000 + 3600, asked=asked)          # same day: cached answer
        self.assertEqual(len(asked), 1)
        self.check("v9.9.9", now=1000 + toranime.UPDATE_EVERY + 1, asked=asked)
        self.assertEqual(len(asked), 2)                             # a day later: ask again

    def test_a_failed_check_is_quiet_and_waits_a_day(self):
        asked = []
        self.assertEqual(self.check(None, now=1000, asked=asked), (None, ""))
        self.assertEqual(self.check(None, now=2000, asked=asked), (None, ""))
        self.assertEqual(len(asked), 1)                             # offline twice, asked once

    def test_the_environment_switches_it_off(self):
        with mock.patch.object(toranime, "latest_release") as latest, \
                mock.patch.dict(os.environ, {"TORANIME_NO_UPDATE_CHECK": "1"}):
            self.assertIsNone(toranime.update_notice())
        latest.assert_not_called()

    def test_a_tag_becomes_numbers(self):
        for text, want in (("v2.2.6", (2, 2, 6)), ("2.3", (2, 3)), ("v2.2.6-rc1", (2, 2, 6)),
                           ("", ()), (None, ()), ("newest", ())):
            with self.subTest(text=text):
                self.assertEqual(toranime.as_version(text), want)

    def test_a_normal_run_checks_before_asking_anything(self):
        with mock.patch.object(toranime, "interactive_ok", return_value=True), \
                mock.patch.object(toranime, "ensure_player", return_value=["mpv"]), \
                mock.patch.object(toranime, "continue_prompt", return_value=None), \
                mock.patch.object(toranime, "find_anime", side_effect=SystemExit(0)), \
                mock.patch.object(toranime, "update_notice") as notice:
            run_main()
        notice.assert_called_once()

    def test_the_quiet_flags_never_ask(self):
        for argv in (["--list"], ["--print"], ["--m3u"], ["--no-update-check"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(toranime, "interactive_ok", return_value=True), \
                    mock.patch.object(toranime, "ensure_player", return_value=["mpv"]), \
                    mock.patch.object(toranime, "continue_prompt", return_value=None), \
                    mock.patch.object(toranime, "find_anime", side_effect=SystemExit(0)), \
                    mock.patch.object(toranime, "update_notice") as notice:
                run_main(*argv)
            notice.assert_not_called()

    def test_a_non_interactive_run_never_asks(self):
        with mock.patch.object(toranime, "ensure_player", return_value=["mpv"]), \
                mock.patch.object(toranime, "continue_prompt", return_value=None), \
                mock.patch.object(toranime, "find_anime", side_effect=SystemExit(0)), \
                mock.patch.object(toranime, "update_notice") as notice:
            run_main()
        notice.assert_not_called()


class OfficialMpv(unittest.TestCase):
    """Windows gets mpv from mpv's own GitHub release, and the hash is checked first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(toranime, "cache_dir", return_value=self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def payload():
        """a stand-in for mpv's zip: its bytes and their SHA-256."""
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as z:
            z.writestr("mpv.exe", b"MZ not really mpv")
            z.writestr("LICENSE.GPL", b"GPL")
        data = raw.getvalue()
        return data, hashlib.sha256(data).hexdigest()

    @staticmethod
    def release(sha, size=1234):
        return {"version": "9.9.9", "url": "https://example.invalid/mpv.zip",
                "sha256": sha, "size": size}

    def download(self, release, data):
        class Fake(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(toranime.urllib.request, "urlopen", return_value=Fake(data)), \
                contextlib.redirect_stderr(io.StringIO()):
            return toranime.download_mpv(release)

    def test_a_matching_hash_is_unpacked_and_then_reused(self):
        data, sha = self.payload()
        exe = os.path.join(self.tmp.name, "mpv", "9.9.9", "mpv.exe")
        self.assertEqual(self.download(self.release(sha), data), [exe])
        self.assertTrue(os.path.isfile(exe))
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "mpv")), ["9.9.9"])  # zip is gone
        with mock.patch.object(toranime.urllib.request, "urlopen") as fetch:
            self.assertEqual(toranime.download_mpv(self.release(sha)), [exe])
        fetch.assert_not_called()          # the unpacked copy is reused

    def test_a_wrong_hash_unpacks_nothing(self):
        data, _ = self.payload()
        self.assertIsNone(self.download(self.release("0" * 64), data))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "mpv", "9.9.9")))

    def test_the_asset_must_be_mpvs_own_windows_zip(self):
        asset = {"name": "mpv-v9.9.9-x86_64-w64-mingw32-full.zip",
                 "browser_download_url": "https://example.invalid/mpv.zip",
                 "digest": "sha256:" + "a" * 64, "size": 7}
        with mock.patch.object(toranime, "fetch_json",
                               return_value={"tag_name": "v9.9.9", "assets": [asset]}):
            self.assertEqual(toranime.official_mpv_release("x86_64"),
                             {"version": "9.9.9", "url": "https://example.invalid/mpv.zip",
                              "sha256": "a" * 64, "size": 7})
            self.assertIsNone(toranime.official_mpv_release("aarch64"))   # not published
        with mock.patch.object(toranime, "fetch_json", return_value={"assets": [asset]}):
            self.assertIsNone(toranime.official_mpv_release("x86_64"))    # no tag, no digest

    def test_winget_is_not_offered_anymore(self):
        self.assertNotIn("winget", [mgr for mgr, _ in toranime.MPV_INSTALL["nt"]])

    def test_windows_offers_the_official_release_before_any_manager(self):
        with mock.patch.object(toranime, "_os_key", return_value="nt"), \
                mock.patch.object(toranime, "find_mpv", return_value=None), \
                mock.patch.object(toranime, "official_mpv_release",
                                  return_value=self.release("a" * 64)), \
                mock.patch.object(toranime, "download_mpv",
                                  return_value=[r"C:\mpv\mpv.exe"]) as download, \
                mock.patch.object(toranime, "interactive_ok", return_value=True), \
                mock.patch.object(toranime.shutil, "which", return_value="/usr/bin/scoop"), \
                mock.patch("builtins.input", return_value=""), \
                mock.patch.object(toranime.subprocess, "run") as run, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(toranime.ensure_player("mpv"), [r"C:\mpv\mpv.exe"])
        download.assert_called_once()
        run.assert_not_called()            # no package manager had to run


# ------------------------------------------------------------------ playback

def fake_resolve(anime, ep, lang, verbose=True):
    return f"https://u/{toranime.epnum(ep)}", {"sizeHuman": "1 MB"}


class Playback(unittest.TestCase):
    eps = [{"number": n, "title": f"T{n}"} for n in (1, 2, 3)]

    def test_the_copy_starts_hidden_unless_the_flag_asked_for_it(self):
        self.assertNotIn("--script-opts=toranime-look=select", toranime.mpv_args("sub"))
        with mock.patch.object(toranime, "BURN", {"font": "Trebuchet MS"}), \
                mock.patch.object(toranime, "LOOK_SELECTED", True):
            self.assertIn("--script-opts=toranime-look=select", toranime.mpv_args("sub"))

    def test_mpv_args(self):
        signs = os.path.join(CACHE.name, "toranime", "pick-signs.lua")
        self.assertEqual(toranime.mpv_args("dub"), ["--alang=eng,en", f"--script={signs}"])
        with open(signs, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), toranime.SIGNS_PICKER)
        picker = os.path.join(CACHE.name, "toranime", "pick-subs.lua")
        self.assertEqual(toranime.mpv_args("sub"),
                         ["--alang=jpn,ja", "--slang=eng,en", f"--script={picker}"])
        with open(picker, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), toranime.SUB_PICKER)

    def run_loop(self, start, script):
        launched, menus = [], []

        def menu(opts, default, header):
            menus.append((opts, default, header))
            return [v for _, v in opts].index(script.pop(0))

        with mock.patch.object(toranime, "resolve", fake_resolve), \
                mock.patch.object(toranime, "launch", lambda c, u, t, lg: launched.append((u, t))), \
                mock.patch.object(toranime, "menu_pick", menu), \
                contextlib.redirect_stderr(io.StringIO()):
            toranime.play_loop(["mpv"], ANIME, self.eps, start, "dub")
        return launched, menus

    def test_loop_next_replay_previous(self):
        launched, menus = self.run_loop(0, ["next", "replay", "prev", "quit"])
        self.assertEqual([u for u, _ in launched], ["https://u/1", "https://u/2", "https://u/2",
                                                     "https://u/1"])
        self.assertEqual(launched[1][1], "ONE PIECE - 2 - T2")
        opts, default, _ = menus[0]
        self.assertEqual(opts[default][1], "next")

    def test_loop_defaults_to_quit_on_newest_episode(self):
        _, menus = self.run_loop(2, ["quit"])
        opts, default, _ = menus[0]
        self.assertEqual(opts[default][1], "quit")
        self.assertNotIn("next", [v for _, v in opts])

    def test_loop_counts_an_episode_watched_on_moving_to_the_next(self):
        fake = FakeAniList()
        with anilist(fake):
            self.run_loop(0, ["next", "replay", "quit"])        # mpv-android reports nothing
        self.assertEqual(fake.saved, [(21, 1, "CURRENT")])



class FakeMpv:
    """one mpv process plus its IPC connection. the playlist starts with the urls on
    the command line; every file lasts `length` polls of `duration` seconds, then the
    next one plays, or mpv exits after the last. `actions` maps a poll number to
    "next" (Enter in mpv) or "quit" (q). `ending` is where the chapter skipper says
    every file's ending starts (None: no chapters, or no skipper). `changes` maps a poll
    number to settings the viewer changes then ({"audio-delay": 0.9})."""

    def __init__(self, argv, length=3, actions=None, duration=1400.0, volume=55.0, ending=None,
                 changes=None):
        self.argv = argv
        self.playlist = [a for a in argv if a.startswith("https://")]
        self.titles = [a.split("=", 1)[1] for a in argv if a.startswith("--force-media-title=")]
        self.length, self.actions, self.duration, self.volume = length, actions or {}, duration, volume
        self.ending, self.changes = ending, changes or {}
        self.settings = {"audio-delay": 0.0, "sub-scale": 1.0, "sub-pos": 100}
        for a in argv:                          # --audio-delay=0.9 and the like
            k, _, v = a[2:].partition("=")
            if a.startswith("--") and k in self.settings:
                self.settings[k] = float(v)
        self.pos, self.tick, self.polls, self.exited, self.printed = 0, 0, 0, False, []

    # the process
    def poll(self):
        return 0 if self.exited else None

    def wait(self):
        self.exited = True
        return 0

    def kill(self):
        self.exited = True

    # the ipc connection
    def get(self, prop):
        if prop == "playlist-pos":
            self.polls += 1
            self.tick += 1
            self.settings.update(self.changes.get(self.polls, {}))
            action = self.actions.get(self.polls)
            if action == "next" or self.tick > self.length:
                if self.pos + 1 >= len(self.playlist):
                    self.exited = True
                    raise EOFError
                self.pos, self.tick = self.pos + 1, 1
            if action == "quit":
                self.exited = True
                raise EOFError
            return self.pos
        return {"time-pos": self.duration * self.tick / self.length, "duration": self.duration,
                "volume": self.volume, "user-data/toranime/ending": self.ending,
                **self.settings}[prop]

    def command(self, *args, **named):
        if named.get("name", args[0] if args else None) == "loadfile" or args[:1] == ("loadfile",):
            self.playlist.append(named["url"])
            self.titles.append(named["options"].split("%", 2)[2])
        elif args[:1] == ("print-text",):
            self.printed.append(args[1])

    def close(self):
        pass


class FakeAniList:
    """AniList's GraphQL API for one viewer. `entry` is their {status, progress} for the
    show (None: not on their list), `episodes` its length (None: still airing), and
    `token` the only one it accepts."""

    def __init__(self, episodes=None, entry=None, token="tok"):
        self.episodes, self.entry, self.token, self.saved = episodes, entry, token, []
        self.requests = 0

    def __call__(self, req, timeout=None):
        assert req.full_url == toranime.ANILIST_API, req.full_url
        self.requests += 1
        if req.get_header("Authorization") != f"Bearer {self.token}":
            body = json.dumps({"errors": [{"message": "Invalid token", "status": 400}]})
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(body.encode()))
        q = json.loads(req.data.decode())
        v = q["variables"]
        if "Viewer" in q["query"]:
            data = {"Viewer": {"name": "vash"}}
        elif "SaveMediaListEntry" in q["query"]:
            self.saved.append((v["id"], v["progress"], v["status"]))
            self.entry = {"status": v["status"], "progress": v["progress"]}
            data = {"SaveMediaListEntry": {"progress": v["progress"]}}
        else:
            data = {"Media": {"episodes": self.episodes, "mediaListEntry": self.entry}}
        return io.BytesIO(json.dumps({"data": data}).encode())


@contextlib.contextmanager
def anilist(fake, token="tok"):
    """talk to the fake AniList, logged in with `token` (None: logged out)."""
    path = os.path.join(toranime.state_dir(), "anilist.json")
    if token:
        os.makedirs(toranime.state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"token": token}, fh)
    try:
        with mock.patch.object(toranime.urllib.request, "urlopen", fake):
            yield fake
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


class Session(unittest.TestCase):
    """the desktop binge: one mpv window, next episode appended while one plays."""
    eps = [{"number": n, "title": f"T{n}"} for n in (1, 2, 3)]
    anime = dict(ANIME)

    def setUp(self):
        for name in ("history.json", "anilist.json"):        # each test starts with none
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(toranime.state_dir(), name))

    def run_session(self, idx=0, stop=None, start=0.0, resolve=fake_resolve, lang="dub", **fake):
        mpvs = []

        def popen(argv):
            mpvs.append(FakeMpv(argv, **fake))
            return mpvs[-1]
        with mock.patch.object(toranime, "resolve", resolve), \
                mock.patch.object(toranime.subprocess, "Popen", popen), \
                mock.patch.object(toranime, "signs_file", lambda url, video: None), \
                mock.patch.dict(toranime.BURN, {}, clear=True), \
                mock.patch.object(toranime, "mpv_connect", lambda path, proc: proc), \
                mock.patch.object(toranime, "POLL", 0.001), \
                contextlib.redirect_stderr(io.StringIO()):
            out = toranime.play_session(["mpv"], self.anime, self.eps, idx, lang, start, stop)
        self.assertEqual(len(mpvs), 1)          # one window for the whole session
        return out, mpvs[0]

    def test_plays_every_episode_in_one_window(self):
        (idx, pos, status), mpv = self.run_session()
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/2", "https://u/3"])
        self.assertEqual(mpv.titles, ["ONE PIECE - 1 - T1", "ONE PIECE - 2 - T2",
                                      "ONE PIECE - 3 - T3"])
        self.assertEqual((idx, pos, status), (2, None, "done"))
        self.assertIn("--{", mpv.argv)          # first file's title is file-local
        self.assertTrue(any(a.startswith("--input-ipc-server=") for a in mpv.argv))
        h = toranime.load_history()
        self.assertEqual((h["last"], h["shows"]["kitsu:10740"]["episode"], h["volume"]),
                         ("kitsu:10740", "3", 55))
        self.assertIsNone(h["shows"]["kitsu:10740"]["position"])

    def test_quit_mid_episode_is_remembered_with_position_and_volume(self):
        (idx, pos, status), _ = self.run_session(actions={5: "quit"}, volume=72.0)
        self.assertEqual((idx, status), (1, "stopped"))          # quit 1/3 into episode 2
        self.assertAlmostEqual(pos, 1400 / 3, places=1)
        h = toranime.load_history()
        self.assertEqual(h["shows"]["kitsu:10740"]["episode"], "2")
        self.assertEqual(h["volume"], 72)
        # the next run opens mpv at that volume, and a resume starts at the position
        (_, _, _), mpv = self.run_session(idx=1, start=466.7, actions={2: "quit"})
        self.assertIn("--volume=72", mpv.argv)
        self.assertIn("--start=466.7", mpv.argv)

    def test_settings_changed_in_mpv_stick_to_the_show_and_language(self):
        # dub: audio delay raised to +0.9s (Ctrl + nine times) during episode 1
        self.run_session(actions={4: "quit"}, changes={2: {"audio-delay": 0.1 * 9}})
        self.assertEqual(toranime.load_history()["tweaks"],
                         {"kitsu:10740/dub": {"audio-delay": 0.9}})
        # the next dub session opens with it, and changing the subtitles adds to it
        _, mpv = self.run_session(actions={3: "quit"}, changes={2: {"sub-scale": 0.5}})
        self.assertIn("--audio-delay=0.9", mpv.argv)
        self.assertEqual(toranime.load_history()["tweaks"],
                         {"kitsu:10740/dub": {"audio-delay": 0.9, "sub-scale": 0.5}})
        # sub keeps its own: no audio delay
        _, mpv = self.run_session(actions={2: "quit"}, lang="sub")
        self.assertFalse([a for a in mpv.argv if a.startswith(("--audio-delay", "--sub-scale"))])
        # setting it back to 0 is a change too
        self.run_session(actions={3: "quit"}, changes={2: {"audio-delay": 0.0}})
        self.assertEqual(toranime.load_history()["tweaks"]["kitsu:10740/dub"]["audio-delay"], 0)

    def test_quitting_during_the_ending_counts_as_watched(self):
        # quit 2/3 into episode 1 (933s of 1400): past an ending marked at 900s...
        (idx, pos, status), _ = self.run_session(actions={3: "quit"}, ending=900.0)
        self.assertEqual((idx, pos, status), (0, None, "done"))
        self.assertIsNone(toranime.load_history()["shows"]["kitsu:10740"]["position"])
        # ...but not before it
        (idx, pos, status), _ = self.run_session(actions={3: "quit"}, ending=1000.0)
        self.assertEqual((idx, status), (0, "stopped"))

    def test_episodes_played_to_the_end_move_anilist_progress(self):
        fake = FakeAniList()
        with anilist(fake):
            self.run_session()                                  # 1, 2 and 3 play through
        self.assertEqual(fake.entry, {"status": "CURRENT", "progress": 3})
        done = [p for _, p, _ in fake.saved]
        self.assertEqual(done, sorted(done))                    # never went down
        # > past episode 1, then quit a third into episode 2: neither was watched
        fake = FakeAniList()
        with anilist(fake):
            self.run_session(actions={2: "next", 4: "quit"})
        self.assertEqual(fake.saved, [])
        # logged out: AniList is never contacted
        with anilist(FakeAniList(), token=None) as fake:
            self.run_session()
        self.assertEqual(fake.requests, 0)

    def test_enter_skips_ahead_and_range_stops_at_its_end(self):
        (idx, _, status), mpv = self.run_session(stop=1, actions={3: "next"})
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/2"])     # nothing past 2
        self.assertEqual((idx, status), (1, "done"))

    def test_unresolvable_next_episode_is_skipped(self):
        def flaky(anime, ep, lang, verbose=True):
            if str(toranime.epnum(ep)) == "2":
                raise LookupError("no dub source for episode 2")
            return fake_resolve(anime, ep, lang)
        (idx, _, _), mpv = self.run_session(resolve=flaky)
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/3"])
        self.assertTrue(any("skipping episode 2" in p for p in mpv.printed))

    def test_without_ipc_it_still_plays_the_first_episode(self):
        with mock.patch.object(toranime, "mpv_connect", lambda path, proc: None), \
                mock.patch.object(toranime, "resolve", fake_resolve), \
                mock.patch.object(toranime, "signs_file", lambda url, video: None), \
                mock.patch.object(toranime.subprocess, "Popen", lambda argv: FakeMpv(argv)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(toranime.play_session(["mpv"], self.anime, self.eps, 0, "dub"),
                             (0, None, "done"))

    def test_menu_resumes_by_default_after_quitting_mid_episode(self):
        sessions, menus = [(1, 754.0, "stopped"), (1, None, "quit")], []
        calls = []

        def session(cmd, anime, eps, idx, lang, start, stop):
            calls.append((idx, start))
            return sessions.pop(0)

        def menu(opts, default, header):
            menus.append((opts, default, header))
            return default
        with mock.patch.object(toranime, "play_session", session), \
                mock.patch.object(toranime, "menu_pick", menu):
            toranime.binge(["mpv"], self.anime, self.eps, 0, "dub")
        opts, default, header = menus[0]
        self.assertEqual(opts[default][1], "resume")
        self.assertIn("stopped at 12:34", header)
        self.assertEqual(calls, [(0, 0.0), (1, 754.0)])

    def test_resume_point_and_continue_prompt(self):
        eps = [{"number": n, "dubbed": True} for n in (1154, 1155, 1156)]
        hist = {"last": "kitsu:10740", "shows": {"kitsu:10740": {
            "lang": "dub", "episode": "1155", "position": 754.0}}}
        self.assertEqual(toranime.resume_point(self.anime, eps, "dub", hist), (1, 754.0))
        self.assertIsNone(toranime.resume_point(self.anime, eps, "sub", hist))
        hist["shows"]["kitsu:10740"]["position"] = None            # watched: next one
        self.assertEqual(toranime.resume_point(self.anime, eps, "dub", hist), (2, 0.0))
        hist["shows"]["kitsu:10740"]["episode"] = "1156"           # newest: caught up
        self.assertEqual(toranime.resume_point(self.anime, eps, "dub", hist), (2, None))

        toranime.remember(self.anime, eps[1], "dub", 754.0)
        labels = []
        with mock.patch.object(toranime, "load_anime", lambda slug: (self.anime, eps)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "menu_pick",
                                  lambda opts, d, h: labels.append(opts[0][0]) or 0):
            anime, got_eps, lang, idx, start = toranime.continue_prompt()
        self.assertEqual((lang, idx, start), ("dub", 1, 754.0))
        self.assertEqual(labels, ["ONE PIECE 1155 (dub) at 12:34"])

    def test_plain_toranime_continues_where_it_left_off(self):
        # the whole path from `toranime` with no arguments to mpv: Enter on "continue"
        # plays the saved episode from the saved position
        eps = [{"number": n, "subbed": True} for n in (1, 2, 3)]
        toranime.remember(self.anime, eps[0], "sub", 1181.0)
        played = []
        with mock.patch.object(toranime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(toranime, "load_anime", lambda slug: (self.anime, eps)), \
                mock.patch.object(toranime, "interactive_ok", lambda: True), \
                mock.patch.object(toranime, "menu_pick", lambda opts, default, header: default), \
                mock.patch.object(toranime, "choose_group", lambda *a: None), \
                mock.patch.object(toranime, "play_session",
                                  lambda *a: played.append(a) or (0, None, "quit")):
            code, _, err = run_main("--no-skip")
        self.assertEqual(code, 0, err)
        self.assertEqual(played, [(["mpv"], self.anime, eps, 0, "sub", 1181.0, None)])

    def test_history_survives_a_corrupt_file(self):
        os.makedirs(toranime.state_dir(), exist_ok=True)
        with open(os.path.join(toranime.state_dir(), "history.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(toranime.load_history(), {})
        toranime.remember(self.anime, self.eps[0], "sub", 10.0)
        self.assertEqual(toranime.load_history()["shows"]["kitsu:10740"]["lang"], "sub")

    def test_clock(self):
        self.assertEqual([toranime.clock(s) for s in (0, 754, 3723)], ["0:00", "12:34", "1:02:03"])

    def test_real_mpv_plays_a_whole_session_in_one_window(self):
        # three generated 2s "episodes" through a real mpv and its IPC (pipe or socket).
        # TORANIME_TEST_MPV makes this mandatory (CI's install stage)
        forced = os.environ.get("TORANIME_TEST_MPV")
        cmd = [forced] if forced else toranime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        sent, settings = [], {}
        real_command = toranime.MpvIpc.command

        def spy(ipc, *args, **named):
            sent.append(named.get("name") or args[0])
            got = real_command(ipc, *args, **named)
            if args[:1] == ("get_property",) and args[1] in toranime.TWEAKS:
                settings[args[1]] = got
            return got

        def lavfi(anime, ep, lang, verbose=True):
            return f"av://lavfi:testsrc=duration=2:size=64x64:rate={10 + ep['number']}", {}
        # settings saved for this show: mpv has to accept them as options
        toranime.remember(self.anime, self.eps[0], "dub",
                         tweaks={"audio-delay": 0.9, "sub-scale": 0.5, "sub-pos": 102})
        with mock.patch.object(toranime, "resolve", lavfi), \
                mock.patch.object(toranime.MpvIpc, "command", spy), \
                mock.patch.object(toranime, "POLL", 0.1), \
                contextlib.redirect_stderr(io.StringIO()):
            # --idle=yes: what mpv 0.42's mpv.exe does with no console. toranime has to
            # override it, or mpv idles after the last episode and the session never ends
            got = toranime.play_session([*cmd, "--no-config", "--vo=null", "--ao=null",
                                        "--really-quiet", "--idle=yes"],
                                       self.anime, self.eps, 0, "dub")
        self.assertEqual(got, (2, None, "done"))
        self.assertEqual(sent.count("loadfile"), 2)                    # 2 and 3 appended
        self.assertEqual(settings, {"audio-delay": 0.9, "sub-scale": 0.5, "sub-pos": 102})


class AniList(unittest.TestCase):
    """opt-in progress tracking: --anilist logs in, finished episodes move progress up."""
    anime = dict(ANIME)

    def progress(self, fake, number, token="tok"):
        """track episode `number` -> what it reports, or None if it did nothing."""
        with anilist(fake, token):
            job = toranime.track(self.anime, {"number": number})
            if job is None:
                return None
            job[0].join(10)
        if "err" in job[1]:
            raise job[1]["err"]
        return job[1]["ok"]

    def test_progress_only_moves_up(self):
        fake = FakeAniList()                                    # One Piece: still airing
        self.assertEqual(self.progress(fake, 1150), "ONE PIECE 1150")   # added to the list
        self.assertEqual(fake.saved, [(21, 1150, "CURRENT")])
        self.assertIsNone(self.progress(fake, 1150))            # already there
        self.assertIsNone(self.progress(fake, 1100))            # rewatching an older one
        self.assertIsNone(self.progress(fake, 1150.5))          # a recap isn't progress
        self.assertEqual(self.progress(fake, 1153), "ONE PIECE 1153")   # skipping ahead counts
        self.assertEqual(len(fake.saved), 2)

    def test_last_episode_completes_and_completed_shows_are_left_alone(self):
        fake = FakeAniList(episodes=24, entry={"status": "CURRENT", "progress": 23})
        self.assertEqual(self.progress(fake, 24), "ONE PIECE 24  (completed)")
        self.assertEqual(fake.saved[-1], (21, 24, "COMPLETED"))
        self.assertIsNone(self.progress(fake, 3))               # a rewatch doesn't touch it
        fake.entry = {"status": "REPEATING", "progress": 2}     # unless marked as rewatching
        self.progress(fake, 3)
        self.assertEqual(fake.saved[-1], (21, 3, "REPEATING"))
        fake.entry = {"status": "PAUSED", "progress": 2}        # picking a show back up
        self.progress(fake, 3)
        self.assertEqual(fake.saved[-1], (21, 3, "CURRENT"))

    def test_logged_out_or_unmappable_sends_nothing(self):
        fake = FakeAniList()
        self.assertIsNone(self.progress(fake, 1150, token=None))
        self.assertEqual(fake.saved, [])
        self.assertIsNone(toranime.anilist_id_of({"_key": "unknown:id"}))

    def test_kitsu_ids_are_mapped_to_anilist_once(self):
        show = {"_key": "kitsu:77", "kitsu_id": "77"}
        with net(mapping=21087) as fake:
            self.assertEqual(toranime.anilist_id_of(show), 21087)
            self.assertEqual(toranime.anilist_id_of({"_key": "kitsu:77", "kitsu_id": "77"}),
                             21087)
        self.assertEqual(len([c for c in fake.calls if "/api/v1/anime" in c]), 1)   # cached

    def test_clean_token_accepts_the_shapes_people_paste(self):
        token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.long"
        for pasted in (token, f"  {token}\n", f'"{token}"', f"Bearer {token}",
                       f"bearer\t{token} "):
            self.assertEqual(toranime.clean_token(pasted), token)
        self.assertEqual(toranime.clean_token(""), "")
        self.assertEqual(toranime.clean_token(None), "")

    def test_login_reprompts_after_a_rejected_token(self):
        good = "eyJhbGciOi" + "x" * 200
        print_out = io.StringIO()
        with mock.patch("getpass.getpass", side_effect=["short-code", good]), \
                mock.patch("webbrowser.open", lambda url: False), \
                mock.patch.object(toranime, "anilist",
                                  side_effect=[toranime.AniListAuth("no"),
                                               {"Viewer": {"name": "viewer"}}]), \
                contextlib.redirect_stdout(print_out):
            toranime.anilist_login()
        self.assertIn("only 10 characters", print_out.getvalue())    # names the real problem
        self.assertIn("logged in to AniList as viewer", print_out.getvalue())
        self.assertEqual(toranime.anilist_token(), good)

    def test_login_gives_up_with_the_manual_path_after_three_tries(self):
        out = io.StringIO()
        with contextlib.suppress(FileNotFoundError):
            os.remove(toranime.anilist_file())
        with mock.patch("getpass.getpass", side_effect=["a", "b", "c"]), \
                mock.patch("webbrowser.open", lambda url: False), \
                mock.patch.object(toranime, "anilist",
                                  side_effect=toranime.AniListAuth("no")), \
                contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as stopped:
            toranime.anilist_login()
        self.assertIn("not logged in", str(stopped.exception.code))
        self.assertIn("anilist.json", str(stopped.exception.code))
        self.assertEqual(toranime.anilist_token(), None)

    def test_expired_token_says_to_log_in_again(self):
        with self.assertRaisesRegex(RuntimeError, r"run toranime --anilist again"):
            self.progress(FakeAniList(token="new"), 1150, token="old")

    def test_login_and_logout(self):
        with anilist(FakeAniList(), token=None), \
                mock.patch.object(toranime, "ANILIST_CLIENT", "123"), \
                mock.patch("webbrowser.open") as browser:
            with mock.patch("getpass.getpass", return_value="wrong"):
                code, out, _ = run_main("--anilist")
            self.assertIn("not logged in", code)          # three tries, then how to do it by hand
            self.assertIn("only 5 characters", out)       # says why it was refused
            self.assertIsNone(toranime.anilist_token())
            with mock.patch("getpass.getpass", return_value=" tok\n"):
                code, out, _ = run_main("--anilist")
            self.assertEqual(code, 0)
            self.assertIn("logged in to AniList as vash", out)
            self.assertIn("client_id=123&response_type=token", browser.call_args[0][0])
            self.assertEqual(toranime.anilist_token(), "tok")
            code, out, _ = run_main("--anilist", "logout")
            self.assertIn("logged out of AniList", out)
            self.assertIsNone(toranime.anilist_token())
        with mock.patch.object(toranime, "ANILIST_CLIENT", ""):
            self.assertIn("isn't set up", run_main("--anilist")[0])


class TorboxKey(unittest.TestCase):
    """--torbox checks the key with TorBox, then saves it; logout removes it."""

    def setUp(self):
        self.path = os.path.join(toranime.config_dir(), "torbox.key")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("test-token\n")

    def tearDown(self):
        for path in (self.path, os.path.join(os.path.dirname(toranime.config_dir()),
                                            toranime.LEGACY, "torbox.key")):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)

    def saved(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_login_saves_the_key_torbox_confirmed(self):
        with net() as fake, mock.patch("getpass.getpass", return_value="  new-key\n"):
            code, out, _ = run_main("--torbox")
        self.assertEqual(code, 0)
        self.assertIn("logged in to TorBox as viewer@example.com (plan: standard)", out)
        self.assertNotIn("new-key", out)              # the key itself is never printed
        self.assertEqual(self.saved(), "new-key\n")
        if os.name != "nt":        # Windows has no such bits: %APPDATA% is already per-user
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(toranime.saved_key(), "new-key")
        self.assertTrue(any(url.endswith("/user/me") for url in fake.calls))

    def test_a_bearer_prefix_or_quotes_are_stripped(self):
        with net(), mock.patch("getpass.getpass", return_value='"Bearer  new-key"'):
            code, out, _ = run_main("--torbox")
        self.assertEqual(code, 0)
        self.assertEqual(self.saved(), "new-key\n")

    def test_a_rejected_key_is_asked_again_and_says_why(self):
        with net(), mock.patch("getpass.getpass", side_effect=["bad", "good-key"]):
            code, out, _ = run_main("--torbox")
        self.assertEqual(code, 0)
        self.assertIn("TorBox rejected that key (That key is invalid)", out)
        self.assertEqual(self.saved(), "good-key\n")

    def test_three_rejections_leave_the_key_already_there_alone(self):
        with net(), mock.patch("getpass.getpass", return_value="bad"):
            code, out, _ = run_main("--torbox")
        self.assertNotEqual(code, 0)
        self.assertIn("not saved", str(code))
        self.assertEqual(self.saved(), "test-token\n")     # untouched

    def test_enter_keeps_the_key_already_saved(self):
        with net(), mock.patch("getpass.getpass", return_value=""):
            code, out, _ = run_main("--torbox")
        self.assertEqual(code, 0)
        self.assertIn("already logged in to TorBox as viewer@example.com", out)
        self.assertIn("keeping the key already saved", out)
        self.assertEqual(self.saved(), "test-token\n")

    def test_logout_removes_it_here_and_from_the_old_folder(self):
        legacy = os.path.join(os.path.dirname(toranime.config_dir()), toranime.LEGACY)
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "torbox.key"), "w", encoding="utf-8") as fh:
            fh.write("old-key\n")
        with net():
            code, out, _ = run_main("--torbox", "logout")
        self.assertEqual(code, 0)
        self.assertIn("removed the TorBox key", out)
        self.assertIn("torbox.app > Settings > API", out)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(os.path.join(legacy, "torbox.key")))
        with net():
            code, out, _ = run_main("--torbox", "logout")
        self.assertIn("no TorBox key saved", out)

    def test_who_asks_torbox_with_the_key_it_was_given(self):
        seen = {}

        def fake(url, headers=None, data=None, timeout=None):
            seen["url"], seen["auth"] = url, (headers or {}).get("Authorization")
            return json.dumps({"success": True, "data": {"email": "a@b", "plan": 1}})

        with mock.patch.object(toranime, "fetch", fake):
            who = toranime.torbox_who("candidate")
        self.assertTrue(seen["url"].endswith("/user/me"))
        self.assertEqual(seen["auth"], "Bearer candidate")
        self.assertEqual(who, {"email": "a@b", "plan": "essential"})

        def refused_call(url, headers=None, data=None, timeout=None):
            return json.dumps({"success": False, "error": "BAD_TOKEN", "detail": "nope"})

        with mock.patch.object(toranime, "fetch", refused_call), \
                self.assertRaises(toranime.TorboxError):
            toranime.torbox_who("nope")

    def test_a_missing_key_points_at_the_command(self):
        os.remove(self.path)
        with self.assertRaises(SystemExit) as stopped:
            toranime.torbox_token()
        said = str(stopped.exception.code)
        self.assertIn("toranime --torbox", said)
        # the by-hand way is still there, in the shell this platform has
        self.assertIn("Set-Content" if os.name == "nt" else "chmod 600", said)


SEADEX_ENTRY = {"items": [{"alID": 21, "expand": {"trs": [
    {"infoHash": "a" * 40, "isBest": True, "releaseGroup": "A&C"},
    {"infoHash": "b" * 40, "isBest": False, "releaseGroup": "F-R"},
    {"infoHash": "<redacted>", "isBest": True, "releaseGroup": "-ZR-"},     # private tracker
]}}]}


class SeaDex(unittest.TestCase):
    """SeaDex's picks are matched by info hash, put first, and marked in sakura pink."""

    def setUp(self):
        for patcher in (mock.patch.object(toranime, "SEADEX", {}),
                        mock.patch.object(toranime, "NO_SEADEX", False),
                        mock.patch.object(toranime, "SEADEX_TTL", 24 * 3600)):
            patcher.start()
            self.addCleanup(patcher.stop)
        with contextlib.suppress(FileNotFoundError):
            os.remove(toranime.seadex_file())

    @staticmethod
    def anime(anilist_id=21):
        return {"kitsu_id": "10740", "anilist_id": anilist_id}

    def age(self, seconds):
        """pretend the on-disk copy is `seconds` old."""
        with open(toranime.seadex_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data.values():
            entry["at"] = time.time() - seconds
        with open(toranime.seadex_file(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_picks_are_split_and_private_hashes_are_skipped(self):
        with mock.patch.object(toranime, "fetch_json", return_value=SEADEX_ENTRY) as asked:
            got = toranime.seadex_for(self.anime())
        self.assertEqual(got["best"], {"a" * 40})
        self.assertEqual(got["alt"], {"b" * 40})
        url = asked.call_args[0][0]
        self.assertTrue(url.startswith(toranime.SEADEX_API))
        self.assertIn("expand=trs", url)
        self.assertIn("21", url)

    def test_a_show_without_an_entry_is_simply_unmarked(self):
        with mock.patch.object(toranime, "fetch_json", return_value={"items": []}):
            self.assertEqual(toranime.seadex_for(self.anime()), {"best": set(), "alt": set()})

    def test_no_anilist_id_means_no_request(self):
        with mock.patch.object(toranime, "fetch_json", side_effect=AssertionError("asked")):
            self.assertEqual(toranime.seadex_for({}), {"best": set(), "alt": set()})

    def test_one_request_a_day_then_the_disk_copy_is_used(self):
        with mock.patch.object(toranime, "fetch_json", return_value=SEADEX_ENTRY) as asked:
            toranime.seadex_for(self.anime())
            toranime.SEADEX.clear()                 # a second run: only the disk copy
            toranime.seadex_for(self.anime())
        self.assertEqual(asked.call_count, 1)
        self.age(2 * 24 * 3600)                     # two days later it is asked again
        toranime.SEADEX.clear()
        with mock.patch.object(toranime, "fetch_json", return_value=SEADEX_ENTRY) as again:
            toranime.seadex_for(self.anime())
        self.assertEqual(again.call_count, 1)

    def test_a_failed_refresh_keeps_what_was_there(self):
        with mock.patch.object(toranime, "fetch_json", return_value=SEADEX_ENTRY):
            toranime.seadex_for(self.anime())
        self.age(2 * 24 * 3600)
        toranime.SEADEX.clear()
        with mock.patch.object(toranime, "fetch_json", side_effect=OSError("down")):
            self.assertEqual(toranime.seadex_for(self.anime())["best"], {"a" * 40})

    def test_a_seadex_pick_beats_even_a_cached_4k_release(self):
        sd = {"best": {"a" * 40}, "alt": set()}
        mine = {"infoHash": "a" * 40, "title": "[A&C] ONE PIECE 1151 [1080p]"}
        other = {"infoHash": "c" * 40, "title": "[X] ONE PIECE 1151 [2160p]"}
        self.assertLess(toranime.stream_rank(mine, set(), None, sd),
                        toranime.stream_rank(other, {"c" * 40}, None, sd))

    def test_resolve_asks_torbox_for_the_seadex_pick(self):
        pick = {"infoHash": "a" * 40, "title": "[A&C] ONE PIECE 1151 [1080p]"}
        better = {"infoHash": "c" * 40, "title": "[X] ONE PIECE 1151 [2160p]"}
        seen = {}

        def fake_resolve(stream, number, season=None, verbose=True):
            seen["stream"] = stream
            return "https://example.invalid/link", {}

        with mock.patch.object(toranime, "streams", lambda a, e: [better, pick]), \
                mock.patch.object(toranime, "torbox_cached", lambda hashes: {"c" * 40}), \
                mock.patch.object(toranime, "seadex_for",
                                  lambda a: {"best": {"a" * 40}, "alt": set()}), \
                mock.patch.object(toranime, "resolve_torrent", fake_resolve), \
                mock.patch.object(toranime, "prefs", lambda: {}):
            toranime.resolve(ANIME, EPISODES[1], "sub")
        self.assertEqual(seen["stream"]["infoHash"], "a" * 40)   # SeaDex over cached 4K

    def test_a_group_you_asked_for_still_wins(self):
        sd = {"best": {"a" * 40}, "alt": set()}
        mine = {"infoHash": "a" * 40, "title": "[A&C] ONE PIECE 1151 [2160p]"}
        other = {"infoHash": "c" * 40, "title": "[X] ONE PIECE 1151 [720p]"}
        want = {"prefer_groups": ["X"]}
        self.assertLess(toranime.stream_rank(other, set(), want, sd),
                        toranime.stream_rank(mine, set(), want, sd))

    def test_an_alternative_is_not_moved_up(self):
        sd = {"best": set(), "alt": {"b" * 40}}
        alt = {"infoHash": "b" * 40, "title": "[F-R] ONE PIECE 1151 [1080p]"}
        other = {"infoHash": "d" * 40, "title": "[S] ONE PIECE 1151 [1080p]"}
        self.assertEqual(toranime.stream_rank(alt, set(), None, sd),
                         toranime.stream_rank(other, set(), None, sd))

    def test_the_tag_is_pink_for_a_pick_and_quiet_for_an_alternative(self):
        sd = {"best": {"a" * 40}, "alt": {"b" * 40}}
        self.assertEqual(toranime.seadex_tag({"infoHash": "a" * 40}, sd),
                         f"{toranime.PINK}♥ seadex{toranime.RST}")
        self.assertIn("38;5;218", toranime.PINK)            # sakura, not generic magenta
        self.assertIn("seadex alt", toranime.seadex_tag({"infoHash": "b" * 40}, sd))
        self.assertEqual(toranime.seadex_tag({"infoHash": "c" * 40}, sd), "")
        self.assertEqual(toranime.seadex_tag({"infoHash": "c" * 40}, {}), "")

    def test_the_group_line_shows_the_tag_without_growing(self):
        s = {"infoHash": "a" * 40, "title": "[A&C] ONE PIECE 1151 [1080p] - 1.31 GB"}
        picked = toranime.group_label("A&C", s, {"a" * 40}, {"best": {"a" * 40}, "alt": set()})
        plain = toranime.group_label("A&C", s, {"a" * 40}, None)
        self.assertIn("♥ seadex", picked)
        self.assertLessEqual(len(toranime.visible(picked)), len(toranime.visible(plain)) + 9)

    def test_fit_clips_visible_text_but_keeps_the_colour(self):
        text = toranime.PINK + "♥ seadex" + toranime.RST + " " + "x" * 40
        out = toranime.fit(text, 10)
        self.assertLessEqual(len(toranime.visible(out)), 10)
        self.assertTrue(out.endswith(toranime.RST))
        self.assertIn("38;5;218", out)
        self.assertEqual(toranime.fit("plain text", 5), "plain")

    def test_no_seadex_asks_nothing(self):
        with mock.patch.object(toranime, "NO_SEADEX", True), \
                mock.patch.object(toranime, "fetch_json", side_effect=AssertionError("asked")):
            self.assertEqual(toranime.seadex_for(self.anime()), {"best": set(), "alt": set()})

    def test_the_flag_reaches_the_global(self):
        with mock.patch.object(toranime, "ensure_player", return_value=["mpv"]), \
                mock.patch.object(toranime, "find_anime", side_effect=SystemExit(0)):
            run_main("--no-seadex", "one piece")
        self.assertTrue(toranime.NO_SEADEX)


# ------------------------------------------------------------------ player setup

class EpisodeMarks(unittest.TestCase):
    """the episode picker shows what AniList already has: ✓ watched, ▶ next."""

    anime = dict(ANIME)

    def setUp(self):
        toranime.WATCHED.clear()
        self.addCleanup(toranime.WATCHED.clear)

    def test_colour_codes_stay_out_of_the_search_text(self):
        self.assertEqual(toranime.visible(f"{toranime.DIM}✓  12  Title{toranime.RST}"),
                         "✓  12  Title")

    def test_without_a_login_there_are_no_marks(self):
        with mock.patch.object(toranime, "anilist_token", lambda: None), \
                mock.patch.object(toranime, "anilist", lambda *a, **k: self.fail("asked AniList")):
            self.assertIsNone(toranime.anilist_watched(self.anime))
        self.assertIs(toranime.episode_label(self.anime, 0), toranime.ep_label)

    def test_episodes_up_to_progress_are_ticked_and_the_next_is_pointed_at(self):
        label = toranime.episode_label(self.anime, 8)
        text = {n: toranime.visible(label({"number": n, "title": f"T{n}"})) for n in (7, 8, 9, 10)}
        self.assertTrue(text[7].startswith("✓"))
        self.assertTrue(text[8].startswith("✓"))
        self.assertTrue(text[9].startswith("▶"))
        self.assertFalse(text[10].startswith(("✓", "▶")))

    def test_the_header_says_what_the_marks_mean(self):
        seen = {}

        def pick(eps, label, header, noun="items", initial=0):
            seen.update(header=header, first=toranime.visible(label(eps[0])))
            return eps[0]
        with mock.patch.object(toranime, "anilist_watched", lambda anime: 8), \
                mock.patch.object(toranime, "fuzzy_pick", pick):
            got = toranime.episode_picker(self.anime, [{"number": 7, "title": "x"}], "pick:")
        self.assertEqual(got, {"number": 7, "title": "x"})
        self.assertIn("✓ watched · ▶ next", seen["header"])
        self.assertTrue(seen["first"].startswith("✓"))


class SubPicker(unittest.TestCase):
    """sub mode's mpv script that skips title-card / signs tracks flagged as default."""

    def test_written_once_and_repaired_when_changed(self):
        path = toranime.sub_picker()
        with mock.patch.object(toranime.os, "makedirs", side_effect=AssertionError("rewrote")):
            self.assertEqual(toranime.sub_picker(), path)        # up to date: left alone
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("-- stale")
        toranime.sub_picker()
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), toranime.SUB_PICKER)

    def test_unwritable_cache_falls_back_to_plain_slang(self):
        blocker = os.path.join(CACHE.name, "not-a-dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        with mock.patch.object(toranime, "cache_dir", lambda: os.path.join(blocker, "toranime")):
            self.assertIsNone(toranime.sub_picker())
            self.assertEqual(toranime.mpv_args("sub"), ["--alang=jpn,ja", "--slang=eng,en"])
            self.assertEqual(toranime.mpv_args("dub"), ["--alang=eng,en", "--sid=no"])

    def test_partial_words_match_real_partial_titles_only(self):
        words = re.findall(r'"([^"]+)"', re.search(r"local partial = \{(.*)\}", toranime.SUB_PICKER)[1])

        def partial(title):
            return any(w in title.lower() for w in words)
        # track titles seen on toranime's files
        for title in ("Episode Name (Restyled By Simmsy)", "Episode Name (Orignal)", "Forced",
                      "Signs & Songs", "Dubtitle(AI)"):
            self.assertTrue(partial(title), title)
        for title in ("Full Subtitles (Restyled By Simmsy)", "Full Subtitles (Orignal)",
                      "Track 4 (ENG)", "English", "Dialogue"):
            self.assertFalse(partial(title), title)

    def test_real_mpv_skips_the_title_card_track(self):
        # tests/fixtures/partial-subs.mkv: English "Episode Name" [default], "Signs & Songs",
        # "Full Subtitles". TORANIME_TEST_MPV makes this mandatory (CI's install stage)
        forced = os.environ.get("TORANIME_TEST_MPV")
        cmd = [forced] if forced else toranime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")

        def picked(args):
            log = os.path.join(CACHE.name, "mpv.log")
            subprocess.run([*cmd, "--no-config", "--vo=null", "--ao=null", "--frames=1", *args,
                            "--term-playing-msg=subs=${current-tracks/sub/title:none}",
                            f"--log-file={log}", fixture],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            with open(log, encoding="utf-8", errors="replace") as fh:
                return re.findall(r"\] subs=(.*)", fh.read())

        # control: plain --slang lands on the title card, the bug the picker fixes
        self.assertEqual(picked(["--slang=eng,en"]), ["Episode Name"])
        self.assertEqual(picked(toranime.mpv_args("sub")), ["Full Subtitles"])
        # a dub keeps the signs and songs track instead of the full dialogue one
        self.assertEqual(picked(toranime.mpv_args("dub")), ["Signs & Songs"])

    def live(self, script, *args):
        """the fixture in a real mpv with one of the pickers loaded: its IPC connection."""
        forced = os.environ.get("TORANIME_TEST_MPV")
        cmd = [forced] if forced else toranime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")
        sock = os.path.join(CACHE.name, f"live-{time.monotonic_ns()}.sock")
        proc = subprocess.Popen([*cmd, "--no-config", "--vo=null", "--ao=null", "--idle=no",
                                 f"--input-ipc-server={sock}", f"--script={script}", *args,
                                 fixture],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        ipc = toranime.mpv_connect(sock, proc)
        self.assertIsNotNone(ipc, "mpv never opened its ipc socket")
        self.addCleanup(ipc.close)
        deadline = time.monotonic() + 10
        while ipc.get("sid") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return ipc

    def test_real_mpv_switches_as_soon_as_the_copy_arrives(self):
        # the copy is baked while the episode plays, so S often comes first: the press is
        # remembered, and the copy is selected the moment it is added, without a second one
        ipc = self.live(toranime.sub_picker(), "--script-opts=toranime-look=auto")
        tracks = {t["title"]: t["id"] for t in ipc.get("track-list") if t.get("type") == "sub"}
        copy = os.path.join(CACHE.name, "late copy.ass")
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
                     "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
                     "Effect, Text\nDialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,late\n")
        ipc.command("keypress", "S")
        time.sleep(0.3)
        self.assertEqual(ipc.get("sid"), tracks["Full Subtitles"])   # nothing to switch to yet
        ipc.command("sub-add", copy, "auto")
        deadline = time.monotonic() + 5
        while ipc.get("sid") in tracks.values() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotIn(ipc.get("sid"), list(tracks.values()) + ["no"])  # the copy, unasked

    def test_real_mpv_s_wins_over_a_config_that_binds_it_to_a_screenshot(self):
        # mpv's own S takes a screenshot, and a config's S wins over a plain script
        # binding: the picker has to claim the key, or the subtitles never switch
        conf = os.path.join(CACHE.name, "input.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("S screenshot\n")
        copy = os.path.join(CACHE.name, "look.ass")
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
                     "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
                     "Effect, Text\nDialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,look\n")
        ipc = self.live(toranime.sub_picker(), f"--input-conf={conf}", f"--sub-file={copy}")
        subs = [t for t in ipc.get("track-list") if t.get("type") == "sub"]
        full = next(t["id"] for t in subs if t.get("title") == "Full Subtitles")
        copy_id = next(t["id"] for t in subs if t.get("external"))
        self.assertEqual(ipc.get("sid"), full)
        ipc.command("keypress", "S")
        time.sleep(0.4)
        self.assertEqual(ipc.get("sid"), copy_id)          # switched, not screenshotted

    def test_real_mpv_dub_keeps_the_dialogue_subtitles_one_key_away(self):
        ipc = self.live(toranime.signs_picker())
        tracks = {t["title"]: t["id"] for t in ipc.get("track-list") if t.get("type") == "sub"}
        self.assertEqual(ipc.get("sid"), tracks["Signs & Songs"])
        ipc.command("keypress", "S")
        time.sleep(0.3)
        self.assertEqual(ipc.get("sid"), tracks["Full Subtitles"])
        ipc.command("keypress", "S")
        time.sleep(0.3)
        self.assertEqual(ipc.get("sid"), tracks["Signs & Songs"])

    def test_a_look_is_announced_to_the_picker_so_a_late_press_can_wait(self):
        for selected, expected in ((False, "auto"), (True, "select")):
            with mock.patch.object(toranime, "BURN", {"font": "Trebuchet MS"}), \
                    mock.patch.object(toranime, "LOOK_SELECTED", selected):
                self.assertIn(f"--script-opts=toranime-look={expected}", toranime.mpv_args("sub"))


class SignsDetect(unittest.TestCase):
    """a dub's signs track when the release never labelled one: decided by measuring it."""

    def setUp(self):
        def clean():
            path = os.path.join(toranime.cache_dir(), "signs.json")
            if os.path.exists(path):
                os.remove(path)
        clean()
        self.addCleanup(clean)

    @staticmethod
    def ass(starts, dur=2.0):
        def stamp(t):
            h, rest = divmod(int(t), 3600)
            m, s = divmod(rest, 60)
            return f"{h}:{m:02d}:{s:02d}.00"
        head = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n")
        return head + "".join(f"Dialogue: 0,{stamp(t)},{stamp(t + dur)},Default,,0,0,0,,line\n"
                              for t in starts)

    def test_dense_speech_is_not_signs(self):
        self.assertFalse(toranime.looks_like_signs(self.ass(range(0, 300, 5)), 300))
        self.assertFalse(toranime.looks_like_signs("no dialogue here", 300))

    def test_a_handful_of_short_cues_is_signs(self):
        self.assertTrue(toranime.looks_like_signs(self.ass([10, 60, 130]), 300))

    def test_the_sparse_track_wins_and_the_verdict_is_cached(self):
        streams = [{"index": 2, "lang": "eng", "title": "", "forced": False},
                   {"index": 3, "lang": "eng", "title": "", "forced": False}]
        dense, sparse, calls = self.ass(range(0, 300, 5)), self.ass([10, 60]), []

        def extract(url, index, seconds):
            calls.append(index)
            return dense if index == 2 else sparse
        with mock.patch.object(toranime, "sub_streams", lambda url: (streams, 1420.0)), \
                mock.patch.object(toranime, "extract_subs", extract):
            path = toranime.signs_file("https://u/1", {"infoHash": "aaa"})
            again = toranime.signs_file("https://u/1", {"infoHash": "aaa"})
        self.assertTrue(path and os.path.exists(path))
        self.assertEqual(path, again)
        self.assertEqual(calls, [2, 3])                  # measured once, then remembered

    def test_a_labelled_track_is_left_to_the_mpv_script(self):
        streams = [{"index": 2, "lang": "eng", "title": "Signs & Songs", "forced": False},
                   {"index": 3, "lang": "eng", "title": "English", "forced": False}]
        with mock.patch.object(toranime, "sub_streams", lambda url: (streams, 1420.0)), \
                mock.patch.object(toranime, "extract_subs", lambda *a: self.fail("extracted")):
            self.assertIsNone(toranime.signs_file("https://u/2", {"infoHash": "bbb"}))

    def test_two_full_tracks_mean_there_are_no_signs(self):
        streams = [{"index": 2, "lang": "eng", "title": "", "forced": False},
                   {"index": 3, "lang": "eng", "title": "", "forced": False}]
        dense = self.ass(range(0, 300, 5))
        with mock.patch.object(toranime, "sub_streams", lambda url: (streams, 1420.0)), \
                mock.patch.object(toranime, "extract_subs", lambda *a: dense):
            self.assertIsNone(toranime.signs_file("https://u/3", {"infoHash": "ccc"}))

    def test_extra_subs_hands_a_dub_its_signs_track(self):
        with mock.patch.object(toranime, "signs_file", lambda url, video: "/cache/signs.ass"):
            self.assertEqual(toranime.extra_subs("https://u/1", {}, "dub"), "/cache/signs.ass")
        toranime.BURN.clear()
        self.assertIsNone(toranime.extra_subs("https://u/1", {}, "sub"))


class Styles(unittest.TestCase):
    """subs.json looks: opt-in only, so the release keeps its own styling by default."""

    def setUp(self):
        self.path = os.path.join(toranime.config_dir(), "subs.json")
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.addCleanup(toranime.BURN.clear)
        self.addCleanup(setattr, toranime, "LOOK_SELECTED", False)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_no_subs_json_keeps_the_release_look(self):
        self.assertEqual(toranime.load_styles(), {})
        for native in (None, "", "native", "none", "off", "release"):
            self.assertEqual(toranime.style_args(native), [])

    def test_a_look_becomes_mpv_flags(self):
        self.write(json.dumps({"presets": {"roboto": {"font": "Roboto Medium", "outline": 1.3}}}))
        args = toranime.style_args("roboto")
        self.assertIn("--sub-ass-override=yes", args)
        self.assertIn("--sub-font=Roboto Medium", args)
        overrides = next(a for a in args if a.startswith("--sub-ass-style-overrides="))
        self.assertIn("FontName=Roboto Medium", overrides)
        self.assertIn("Outline=1.3", overrides)
        self.assertNotIn("FontSize", overrides)            # only the fields the look sets
        self.assertEqual(toranime.style_args("ROBOTO"), args)

    def test_fonts_beside_the_config_are_offered_to_mpv(self):
        fonts = os.path.join(toranime.config_dir(), "fonts")
        os.makedirs(fonts, exist_ok=True)
        self.addCleanup(os.rmdir, fonts)
        self.write('{"presets": {"roboto": {"font": "Roboto Medium"}}}')
        self.assertIn(f"--sub-fonts-dir={fonts}", toranime.style_args("roboto"))

    def test_a_broken_file_or_unknown_name_never_blocks_playback(self):
        self.write("{not json")
        self.assertEqual(toranime.load_styles(), {})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(toranime.style_args("gone"), [])
        self.assertIn("no subtitle style 'gone'", err.getvalue())

    def test_the_look_is_baked_into_a_copy_of_the_subtitle_file(self):
        ass = ("[Script Info]\nPlayResX: 640\nPlayResY: 360\n\n[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
               "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
               "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
               "MarginR, MarginV, Encoding\n"
               "Style: Default,Arial,20,&H00FFFFFF,&H00FFFF00,&H00000000,&H7F404040,-1,0,0,0,"
               "100,100,0,0,1,2,1,2,20,20,23,0\n")
        out = toranime.styled_ass(ass, {"font": "Trebuchet MS", "size": 22,
                                        "bold": -1, "outline": 1, "shadow": 1})
        self.assertIn("Style: Default,Trebuchet MS,22,", out)
        self.assertIn(",1,1,1,2,", out)                    # outline 1, shadow 1, bottom
        self.assertIn("ScaledBorderAndShadow: yes", out)   # so the border scales
        self.assertEqual(toranime.styled_ass("1\n00:00:01,000 --> 00:00:02,000\nhi\n", {}), "")
        self.assertIn("Style: Default,Arial,20,", toranime.styled_ass(ass, {}))

    def test_a_burned_copy_is_the_subtitle_track_mpv_shows(self):
        # the picker must hand the look's file the screen, or the viewer sees the
        # release's own styling and the burned one is loaded but never used
        self.assertIn('if t.external then\n                look = t', toranime.SUB_PICKER)
        self.assertLess(toranime.SUB_PICKER.index("look = t"),
                        toranime.SUB_PICKER.index("elseif is_english(t)"),
                        "the copy has to be found before the release's English track is picked")

    def test_s_flips_between_the_release_and_the_copy(self):
        self.assertIn('mp.add_forced_key_binding("S", "toranime-look"', toranime.SUB_PICKER)
        self.assertIn("subtitles: your look", toranime.SUB_PICKER)
        self.assertIn("subtitles: the release's own", toranime.SUB_PICKER)

    def test_without_the_flag_the_copy_is_loaded_but_not_applied(self):
        self.write(json.dumps({"presets": {"trebuchet": {"font": "Trebuchet MS"}}}))
        self.assertEqual(toranime.default_style(), "trebuchet")   # no flag, still found
        # what loading it gives mpv: the fonts, never the release's own styling overrides
        self.assertTrue(all(a.startswith("--sub-fonts-dir=")
                            for a in toranime.look_load_args("trebuchet")))
        played = []
        with net(), mock.patch.object(toranime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(toranime, "watch", lambda cmd, *rest: played.append(cmd)), \
                contextlib.redirect_stderr(io.StringIO()):
            run_main("one piece", "1151", "--dub", "--no-skip")
        self.assertEqual(played, [["mpv"]])          # the release's own subs, untouched

    def test_a_baked_copy_is_reused_across_rotating_urls(self):
        self.write(json.dumps({"presets": {"trebuchet": {"font": "Trebuchet MS"}}}))
        spec = toranime.style_spec("trebuchet")
        video = {"infoHash": "abc123"}
        done = subprocess.CompletedProcess([], 0, stdout=(
            "[Script Info]\nPlayResY: 360\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,20,&H00FFFFFF,&H00FFFF00,&H00000000,&H7F404040,-1,0,0,0,"
            "100,100,0,0,1,2,1,2,20,20,23,0\n").encode(), stderr=b"")
        calls = []
        with mock.patch.object(toranime.shutil, "which", lambda name: "/usr/bin/ffmpeg"), \
                mock.patch.object(toranime.subprocess, "run",
                                  lambda *a, **k: calls.append(a) or done):
            first = toranime.restyle_subs("https://cdn/x?token=one", spec, video)
            again = toranime.restyle_subs("https://cdn/x?token=two", spec, video)
        self.assertTrue(first and os.path.exists(first))
        self.assertEqual(first, again)          # same file, new token: nothing re-baked
        self.assertEqual(len(calls), 1)

    def test_quick_refuses_to_wait_for_ffmpeg(self):
        spec = {"font": "Trebuchet MS"}
        with mock.patch.object(toranime.shutil, "which", lambda name: "/usr/bin/ffmpeg"), \
                mock.patch.object(toranime.subprocess, "run", lambda *a, **k: self.fail("baked")):
            self.assertIsNone(toranime.restyle_subs("https://cdn/new?token=1", spec,
                                                    {"infoHash": "never"}, quick=True))

    def test_restyling_needs_ffmpeg_and_an_ass_track(self):
        with mock.patch.object(toranime.shutil, "which", lambda name: None):
            self.assertIsNone(toranime.restyle_subs("https://x/needs-ffmpeg",
                                                   {"font": "Trebuchet MS"}))
        self.assertIsNone(toranime.restyle_subs("https://x/needs-ffmpeg", {}))
        done = subprocess.CompletedProcess([], 0, stdout=b"not an ass file", stderr=b"")
        with mock.patch.object(toranime.shutil, "which", lambda name: "/usr/bin/ffmpeg"), \
                mock.patch.object(toranime.subprocess, "run", lambda *a, **k: done):
            self.assertIsNone(toranime.restyle_subs("https://x/needs-ffmpeg",
                                                   {"font": "Trebuchet MS"}))

    def test_a_restyled_copy_is_written_once_and_reused(self):
        ass = ("[Script Info]\nPlayResY: 360\n\n[V4+ Styles]\n"
               "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
               "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
               "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
               "MarginR, MarginV, Encoding\n"
               "Style: Default,Arial,20,&H00FFFFFF,&H00FFFF00,&H00000000,&H7F404040,-1,0,0,0,"
               "100,100,0,0,1,2,1,2,20,20,23,0\n")
        done = subprocess.CompletedProcess([], 0, stdout=ass.encode(), stderr=b"")
        with mock.patch.object(toranime.shutil, "which", lambda name: "/usr/bin/ffmpeg"), \
                mock.patch.object(toranime.subprocess, "run", lambda *a, **k: done):
            first = toranime.restyle_subs("https://x/y", {"font": "Trebuchet MS"})
            again = toranime.restyle_subs("https://x/y", {"font": "Trebuchet MS"})
        self.assertTrue(first and os.path.exists(first))
        self.assertEqual(first, again)                     # same url + look -> same file
        self.assertIn("Trebuchet MS", read(first))

    def test_prefs_turn_a_look_on_for_every_run(self):
        prefs = os.path.join(toranime.config_dir(), "prefs.json")
        with open(prefs, "w", encoding="utf-8") as fh:
            json.dump({"subtitle_style": "roboto", "prefer_groups": ["Judas"]}, fh)
        self.addCleanup(os.remove, prefs)
        self.assertEqual(toranime.pref_style(), "roboto")
        self.assertEqual(toranime.prefs()["prefer_groups"], ["Judas"])   # untouched by styles


class ChapterSkip(unittest.TestCase):
    """the mpv script that skips openings and goes from the ending to the next episode."""

    def kind(self, title):
        """CHAPTER_SKIPPER's matching, in Python: whole words, %d* = digits. "intro" is
        an opening only in files with no chapter named like one."""
        for kind, table in (("opening", "OPENING"), ("ending", "ENDING"), ("intro", "INTRO")):
            words = re.findall(r'"([^"]+)"', re.search(rf"local {table} = \{{(.*)\}}",
                                                       toranime.CHAPTER_SKIPPER)[1])
            if any(re.search(r"(?<![a-z0-9])" + w.replace("%d*", "[0-9]*") + r"(?![a-z0-9])",
                             title.lower()) for w in words):
                return kind
        return None

    def test_chapter_titles(self):
        # chapter titles from toranime's One Piece files (episodes 1 and 1151), and others
        for kind, titles in {
            "opening": ["Opening Song", "OP", "OP2", "Opening Credits"],
            "intro": ["Intro"],
            "ending": ["To Be Continued & Ending Song", "Credits", "On The Next episode",
                       "ED", "End Credits", "Preview", "Next Time"],
            None: ["Gol D Roger Speech", "Part A", "Episode Name", "Eyecatch", "Part C",
                   "Scene 1", "Scene 5", "Introduction", "Prologue", "Edward", "Stop"],
        }.items():
            for title in titles:
                self.assertEqual(self.kind(title), kind, title)

    # real mpv. episodes are built from the fixture with mpv's edl://, one segment (and
    # so one chapter) per part. TORANIME_TEST_MPV makes these mandatory (CI's install stage)

    def mpv(self):
        forced = os.environ.get("TORANIME_TEST_MPV")
        cmd = [forced] if forced else toranime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        return [*cmd, "--no-config", "--vo=null", "--ao=null", "--really-quiet"]

    def edl(self, *parts):
        """an episode of (chapter title, seconds) parts. mpv drops an edl's last segment
        from this fixture, so a short tail goes after them, named like a preview so
        the skipper never stops there."""
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")
        source = f"%{len(fixture.encode())}%{fixture}"
        return "edl://" + ";".join(f"{source},start=0,length={n},title={t}"
                                   for t, n in parts + (("Preview", 0.5),))

    def said(self, log, script="skip_chapters"):
        # (some mpv versions end each log line with a space)
        return re.findall(rf"\[i\]\[{script}\] (.*?) *$", read(log), re.M)

    def test_real_mpv_skips_the_opening_and_the_ending(self):
        layouts = {1: ("Gol D Roger Speech", "Opening Song", "Part A",
                       "To Be Continued & Ending Song", "On The Next episode"),
                   2: ("Scene 1", "Intro", "Scene 3", "Credits", "Scene 5")}

        def edl(anime, ep, lang, verbose=True):
            # 1s story, 1.5s opening, 1.5s story, 1.5s ending, 1.5s preview. the story
            # parts outlast the moment after a seek or a file start, when the skipper
            # takes a chapter change for a jump
            return self.edl(*zip(layouts[ep["number"]], (1, 1.5, 1.5, 1.5, 1.5))), {}

        ending = []
        real_get = toranime.MpvIpc.get

        def spy(ipc, prop):
            got = real_get(ipc, prop)
            if prop == "user-data/toranime/ending" and got:
                ending.append(got)
            return got
        log = os.path.join(CACHE.name, "skip.log")
        with mock.patch.object(toranime, "resolve", edl), \
                mock.patch.object(toranime.MpvIpc, "get", spy), \
                mock.patch.object(toranime, "POLL", 0.1), \
                contextlib.redirect_stderr(io.StringIO()):
            got = toranime.play_session([*self.mpv(), f"--log-file={log}", *toranime.skip_args()],
                                       Session.anime, Session.eps, 0, "dub", stop=1)
        # 1: opening skipped, and after the ending only a preview: on to 2. 2: its
        # "Intro" is the opening (no chapter is named one), and its "Credits" skip to
        # "Scene 5", the chapter after them that isn't an ending or a preview
        said = self.said(log)
        self.assertEqual(said[:5], ["skipped Opening Song", "skipped To Be Continued & Ending Song",
                                    "ending: next episode", "skipped Intro", "skipped Credits"])
        self.assertLessEqual(set(said[5:]), {"skipped Preview"})     # if mpv plays the tail
        self.assertEqual(got, (1, None, "done"))
        self.assertIn(4.0, ending)          # where the ending starts, read over the IPC

    def test_real_mpv_rewind_pgdn_and_the_skip_key(self):
        # episode 1 is laid out like Jujutsu Kaisen: the story before the opening is
        # "Intro", and "Juju Stroll" (an extra) sits between the ending and the preview.
        # a driver script does what a viewer would: rewind to the start once the opening
        # is skipped, then PgDn back to the opening, then Tab. episode 2 has no chapters,
        # and gets Tab once
        driver = os.path.join(CACHE.name, "driver.lua")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write("""
local parts, pressed = 0, false
mp.add_timeout(20, function() mp.command("quit") end)        -- the skipper broke
mp.observe_property("chapter", "number", function(_, ch)
    local c = ch and (mp.get_property_native("chapter-list") or {})[ch + 1]
    if not c then return end
    if c.title == "Part A" then
        parts = parts + 1
        if parts == 1 then mp.commandv("seek", "0", "absolute") end     -- rewind
        if parts == 2 then mp.commandv("add", "chapter", "-1") end      -- PgDn
    elseif c.title == "Opening" and parts == 2 then
        mp.add_timeout(0.5, function() mp.commandv("script-binding", "skip_chapters/skip") end)
    end
end)
mp.register_event("file-loaded", function()
    if mp.get_property_number("chapter-list/count", 0) > 0 then return end
    mp.add_timeout(0.5, function()
        pressed = true
        mp.commandv("script-binding", "skip_chapters/skip")
    end)
end)
mp.register_event("playback-restart", function()
    if not pressed then return end
    mp.msg.info(string.format("at %.1f", mp.get_property_number("time-pos", -1)))
    mp.command("quit")
end)
""")
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")
        long = "edl://!no_chapters" + f";%{len(fixture.encode())}%{fixture}" * 60     # 120s
        log = os.path.join(CACHE.name, "keys.log")
        subprocess.run([*self.mpv(), f"--log-file={log}", *toranime.skip_args(),
                        f"--script={driver}",
                        self.edl(("Intro", 1), ("Opening", 2), ("Part A", 3), ("Ending", 1.5),
                                 ("Juju Stroll", 1), ("Preview", 1)),
                        long], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.assertEqual(self.said(log), [
            "skipped Opening",          # "Intro" played: this file names its opening
            "skipped Opening",          # rewound before it: skipped again
            "skip key: Opening",        # PgDn brought it back, Tab skipped it
            "skipped Ending",           # to the extra after it, which plays
            "skipped Preview",
            "ending: next episode",
            "skip key: 85s",            # no chapters: an opening's worth
        ])
        at = float(self.said(log, "driver")[0].split()[1])
        self.assertTrue(85 <= at < 88, at)


class Setup(unittest.TestCase):
    def patches(self, found, managers=("apt", "flatpak"), tty=False, answer=""):
        """mpv missing until a run of a manager listed in `found` completes."""
        state = {"installed": False, "ran": []}

        def run(cmd, **kw):
            state["ran"].append(cmd)
            if any(m in cmd for m in found):
                state["installed"] = True
            return subprocess.CompletedProcess(cmd, 0)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(toranime, "ANDROID", False))
        stack.enter_context(mock.patch.object(toranime, "_os_key", return_value="linux"))
        stack.enter_context(mock.patch.object(
            toranime, "find_mpv", lambda mpv: ["/usr/bin/mpv"] if state["installed"] else None))
        stack.enter_context(mock.patch.object(
            toranime.shutil, "which", lambda c: f"/usr/bin/{c}" if c in managers + ("sudo",) else None))
        stack.enter_context(mock.patch.object(toranime.os, "geteuid", create=True, return_value=1000))
        stack.enter_context(mock.patch.object(toranime.subprocess, "run", run))
        stack.enter_context(mock.patch.object(toranime, "interactive_ok", return_value=tty))
        stack.enter_context(mock.patch("builtins.input", return_value=answer))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack, state

    def test_noninteractive_without_yes_explains(self):
        stack, state = self.patches(found=())
        with stack, self.assertRaises(SystemExit) as cm:
            toranime.ensure_player("mpv")
        self.assertIn("sudo apt-get update && sudo apt-get install -y mpv", str(cm.exception))
        self.assertIn("--setup --yes", str(cm.exception))
        self.assertEqual(state["ran"], [])

    def test_yes_installs_and_falls_back_to_next_manager(self):
        stack, state = self.patches(found=("io.mpv.Mpv",))      # apt "fails", flatpak works
        with stack:
            self.assertEqual(toranime.ensure_player("mpv", assume_yes=True), ["/usr/bin/mpv"])
        self.assertEqual(state["ran"][:2], [["sudo", "apt-get", "update"],
                                            ["sudo", "apt-get", "install", "-y", "mpv"]])
        self.assertEqual(state["ran"][-1][:3], ["flatpak", "install", "--user"])

    def test_prompt_yes_and_no(self):
        stack, _ = self.patches(found=("mpv",), tty=True, answer="")
        with stack:
            self.assertEqual(toranime.ensure_player("mpv"), ["/usr/bin/mpv"])
        stack, state = self.patches(found=("mpv",), tty=True, answer="n")
        with stack, self.assertRaises(SystemExit):
            toranime.ensure_player("mpv")
        self.assertEqual(state["ran"], [])

    def test_explicit_missing_path_and_no_manager(self):
        stack, _ = self.patches(found=())
        with stack, self.assertRaisesRegex(SystemExit, "mpv not found at /opt/x/mpv"):
            toranime.ensure_player("/opt/x/mpv")
        stack, _ = self.patches(found=(), managers=())
        with stack, self.assertRaisesRegex(SystemExit, "no supported package manager"):
            toranime.ensure_player("mpv", assume_yes=True)

    def test_sudo_is_dropped_for_root_or_when_missing(self):
        cmd = ["sudo", "apt-get", "install", "-y", "mpv"]
        with mock.patch.object(toranime.os, "geteuid", create=True, return_value=0), \
                mock.patch.object(toranime.shutil, "which", return_value="/usr/bin/sudo"):
            self.assertEqual(toranime._sudo(cmd), cmd[1:])
        with mock.patch.object(toranime.os, "geteuid", create=True, return_value=1000), \
                mock.patch.object(toranime.shutil, "which", return_value="/usr/bin/sudo"):
            self.assertEqual(toranime._sudo(cmd), cmd)
        with mock.patch.object(toranime.os, "geteuid", create=True, return_value=1000), \
                mock.patch.object(toranime.shutil, "which", return_value=None):
            self.assertEqual(toranime._sudo(cmd), cmd[1:])

    def test_find_mpv_in_install_dir_and_flatpak(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "mpv.exe" if os.name == "nt" else "mpv")
            open(exe, "w").close()
            with mock.patch.object(toranime.shutil, "which", return_value=None), \
                    mock.patch.dict(toranime.MPV_DIRS, {toranime._os_key(): [tmp]}):
                self.assertEqual(toranime.find_mpv("mpv"), [exe])
        with mock.patch.object(toranime.shutil, "which",
                               lambda c: "/usr/bin/flatpak" if c == "flatpak" else None), \
                mock.patch.dict(toranime.MPV_DIRS, {toranime._os_key(): []}), \
                mock.patch.object(toranime.glob, "glob", return_value=[]), \
                mock.patch.object(toranime.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(toranime.find_mpv("mpv"), ["flatpak", "run", "io.mpv.Mpv"])
        with mock.patch.object(toranime.shutil, "which", return_value=None):
            self.assertIsNone(toranime.find_mpv("/definitely/not/here/mpv"))


class Update(unittest.TestCase):
    """--update reinstalls from main into the environment toranime runs from."""

    def update(self, layout="site", venv=True, uv="/bin/uv", new="9.0.0", fail=False, pkg=None):
        """`toranime --update` with toranime.py in site-packages ("site"), a git checkout
        ("git") or on its own ("file"). -> (exit code, output, commands run)"""
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            if "-c" in argv:                            # asking the new toranime its version
                return subprocess.CompletedProcess(argv, 0, stdout=new + "\n")
            return subprocess.CompletedProcess(argv, 1 if fail else 0)
        with tempfile.TemporaryDirectory() as tmp:
            if layout == "git":
                os.mkdir(os.path.join(tmp, ".git"))
            site = tmp if layout == "site" else os.path.join(tmp, "site-packages")
            with mock.patch.object(toranime, "__file__", os.path.join(tmp, "toranime.py")), \
                    mock.patch.object(toranime.sysconfig, "get_paths",
                                      lambda: {"purelib": site, "platlib": site}), \
                    mock.patch.object(toranime.sys, "prefix", "/venv" if venv else "/usr"), \
                    mock.patch.object(toranime.sys, "base_prefix", "/usr"), \
                    mock.patch.object(toranime.shutil, "which", lambda c: uv if c == "uv" else None), \
                    mock.patch.object(toranime.subprocess, "run", run), \
                    mock.patch.dict(os.environ):
                os.environ.pop("TORANIME_PKG", None)
                if pkg:
                    os.environ["TORANIME_PKG"] = pkg
                code, out, err = run_main("--update")
        return code, out + err, ran

    def test_a_uv_tool_or_pipx_venv_is_reinstalled_in_place(self):
        # in place with uv pip: `uv tool install --force` deletes the venv first, and
        # Windows can't delete the python.exe running toranime
        code, out, ran = self.update()
        self.assertEqual(ran[0], ["/bin/uv", "pip", "install", "--python", sys.executable,
                                  "--reinstall", toranime.PACKAGE])
        self.assertIn("-I", ran[1])                     # not a toranime.py in this folder
        self.assertIn(f"updated toranime {toranime.__version__} -> 9.0.0", out)
        self.assertFalse(code)
        self.assertIn("already the newest", self.update(new=toranime.__version__)[1])

    def test_termux_or_no_uv_uses_pip_like_the_installer(self):
        pip = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall"]
        self.assertEqual(self.update(venv=False)[2][0], pip + [toranime.PACKAGE])
        self.assertEqual(self.update(uv=None)[2][0], pip + [toranime.PACKAGE])
        self.assertEqual(self.update(pkg="/tmp/toranime.whl")[2][0][-1], "/tmp/toranime.whl")

    def test_a_failed_update_says_to_run_the_installer(self):
        code, _, ran = self.update(fail=True)
        self.assertIn("update failed", code)
        self.assertIn(toranime.INSTALLER, code)
        self.assertEqual(len(ran), 1)
        self.assertIn("doesn't start", self.update(new="")[0])

    def test_a_git_checkout_or_a_single_file_is_left_alone(self):
        code, _, ran = self.update("git")
        self.assertIn("git pull", code)
        self.assertEqual(ran, [])
        code, _, ran = self.update("file")
        self.assertIn("single file", code)
        self.assertIn(toranime.INSTALLER, code)
        self.assertEqual(ran, [])


class Android(unittest.TestCase):
    def test_no_mpv_needed_and_setup_points_to_app(self):
        with mock.patch.object(toranime, "ANDROID", True):
            self.assertIsNone(toranime.ensure_player("mpv"))
            code, out, _ = run_main("--setup")
        self.assertEqual(code, 0)
        self.assertIn("is.xyz.mpv", out)

    def test_intent_and_errors(self):
        seen = []
        ok = subprocess.CompletedProcess([], 0, "Starting: Intent", "")
        with mock.patch.object(toranime.subprocess, "run", lambda c, **k: seen.append(c) or ok):
            toranime.android_open("https://x/ep.mkv")
        self.assertEqual(seen[0], ["am", "start", "--user", "0", "-a", "android.intent.action.VIEW",
                                   "-d", "https://x/ep.mkv", "-n", "is.xyz.mpv/.MPVActivity"])
        bad = subprocess.CompletedProcess([], 0, "", "Error: Activity class does not exist")
        with mock.patch.object(toranime.subprocess, "run", return_value=bad), \
                self.assertRaisesRegex(SystemExit, "mpv-android"):
            toranime.android_open("https://x")
        with mock.patch.object(toranime.subprocess, "run", side_effect=FileNotFoundError), \
                self.assertRaisesRegex(SystemExit, "termux-am"):
            toranime.android_open("https://x")


if __name__ == "__main__":
    unittest.main()
