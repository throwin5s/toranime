# toranime

[![CI](https://github.com/throwin5s/toranime/actions/workflows/ci.yml/badge.svg)](https://github.com/throwin5s/toranime/actions/workflows/ci.yml)

Stream anime straight into [mpv](https://mpv.io) through [TorBox](https://torbox.app), **English dubs included**. Search a show, pick dub or sub, pick an episode with a fuzzy arrow-key picker, and mpv plays it, then the next one, and the next, in the same window, hands-free.

toranime is a client: it asks a Stremio addon what episodes exist, asks another which releases exist, and has TorBox hand mpv a link to the file. It hosts nothing, downloads nothing to disk, and never torrents from your machine.

- **Zero dependencies** - pure Python standard library.
- **TorBox does the streaming** - only releases TorBox has already cached are used, so playback starts at once.
- **Stremio addons for discovery** - Anime Kitsu for episodes, Torrentio for releases. Both work without a key. Point either at something else (AIOStreams included) in a config file.
- **Interactive picker** - type to filter, `↑`/`↓` to move, `Enter` to select.
- **Binge mode** - one mpv window keeps going: the next episode is added to the playlist while the current one plays, so it starts on its own (or press `Enter` in mpv to skip ahead). Volume and fullscreen stay put.
- **Skips openings and endings** - when the file marks them as chapters, mpv jumps past the opening, and past the ending and preview into the next episode (stopping for an extra like Jujutsu Kaisen's Juju Stroll). `Tab` skips by hand, one key, no `Fn`.
- **Picks up where you left off** - quit mid-episode and `Enter` resumes it. Plain `toranime` offers "continue ONE PIECE 1155 at 12:34", and mpv reopens at your last volume.
- **Remembers your fixes per show** - an audio delay, subtitle size or subtitle position you set in mpv sticks to that show, for dub and sub separately.
- **AniList tracking (optional)** - log in once with `toranime --anilist` and every episode you finish updates your progress on [AniList](https://anilist.co).
- **Ranges** - `1151-1160` plays exactly those, back to back.

## What you need

1. A **TorBox account** with an API key (torbox.app > Settings > API). Cached releases are a paid feature on TorBox's side.
2. **mpv** - `toranime --setup` finds it, or offers to install it with your system's package manager.
3. **Python 3.8+** - or just use the installer, which brings its own.

## Install

[uv](https://docs.astral.sh/uv/) brings its own Python, so nothing else is needed. The
one-line installers do the whole job: uv, toranime, and `toranime --setup` for mpv.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/throwin5s/toranime/main/install.ps1 | iex
```

**macOS / Linux / Steam Deck / Android (Termux)**:

```bash
curl -fsSL https://raw.githubusercontent.com/throwin5s/toranime/main/install.sh | sh
```

From a clone, `uv tool install .` does the same. Without uv, install the wheel attached to
a [release](https://github.com/throwin5s/toranime/releases)
(`uv tool install toranime_mpv-*.whl`), or with Python 3.8+ already there, download
`toranime.py` and run `python toranime.py` — it works without installing.

## Updates

`toranime --update` reinstalls the newest version. The first interactive run of each day
also asks GitHub for the newest release and prints one line when there is one;
`--no-update-check` (or `TORANIME_NO_UPDATE_CHECK=1`) turns that off.

## Setup: your TorBox key

toranime reads the key from `torbox.key` in its config folder (`~/.config/toranime` on macOS and Linux, `%APPDATA%\toranime` on Windows):

```bash
mkdir -p ~/.config/toranime && chmod 700 ~/.config/toranime
printf '%s' 'YOUR_TORBOX_KEY' > ~/.config/toranime/torbox.key
chmod 600 ~/.config/toranime/torbox.key
```

The key is only ever sent to `api.torbox.app`. It is never printed, never logged, and never passed to mpv: the link mpv receives is TorBox's signed CDN url, not a redirect that carries the key. It is never written to your watch history either.

Coming from reanime 2.0? Nothing to do: when `~/.config/toranime` has no key, `sources.json` or history, toranime reads the file in `~/.config/reanime`, and saves history to the new folder from then on.

## Optional: other addons

Discovery uses two addons, both keyless by default:

- **metadata**: [Anime Kitsu](https://anime-kitsu.strem.fun/manifest.json) - search, titles and the episode list,
- **streams**: [Torrentio](https://torrentio.strem.fun/manifest.json) - releases for an episode.

To use something else (a self-hosted Torrentio, or an [AIOStreams](https://aiostreams.org) configuration), write `~/.config/toranime/sources.json`:

```json
{
  "meta": "https://anime-kitsu.strem.fun/manifest.json",
  "stream": "https://aiostreams.example.com/stremio/abc123/manifest.json"
}
```

Anything that speaks the Stremio addon protocol works. An addon that hands back a finished `url` (AIOStreams with a proxy) is played directly; everything else goes through TorBox.

## Usage

```bash
toranime                                   # continue where you left off, or search
toranime "one piece"                       # skip the search prompt
toranime "one piece" 1151 --dub            # play 1151, 1152, 1153... until you quit
toranime "one piece" 1151-1160 --dub       # play just this range
toranime "one piece" 1151- --dub           # 1151 through the newest episode
toranime kitsu:10740 1151 --dub            # an id instead of a search
toranime kitsu:10740 --list                # every episode: number and title
toranime "one piece" 1151 --dub --print    # print the stream url instead of playing
```

### Interactive controls

| Where | Keys |
| --- | --- |
| Anime / episode picker | type to narrow · `↑` / `↓` move · `Enter` select · `Esc` cancel |
| Audio, after-mpv menu | press the number · `Enter` for the default (your last audio, resume or next episode) |
| In mpv | `Enter` or `>` next episode · `<` previous · `Tab` skip the opening (or this chapter) · `PgDn` watch a skipped opening · `q` stop (toranime then offers to resume) |

### Flags

| Flag | Description |
| --- | --- |
| `--dub` | English dub: only releases that advertise an English audio track (`Dual Audio`, `English Dub`, ...) are used |
| `--sub` | Japanese audio, English subs (the default) |
| `--list` | list every episode and exit |
| `--print` | print stream urls, don't launch a player |
| `--m3u [PATH]` | write an `.m3u` playlist instead. Links are signed and last a few hours |
| `--mpv PATH` | path to the mpv binary (default: found automatically) |
| `--no-skip` | play openings and endings instead of skipping them |
| `--setup` | find or install mpv, then exit |
| `-y`, `--yes` | install mpv without asking if it's missing (scripts, CI) |
| `--anilist` | log in to AniList (`--anilist logout` to stop) |
| `--update` | update toranime to the newest version |
| `--no-update-check` | don't ask GitHub for a newer release on startup |

## Your own release preferences

By default toranime takes the best cached release it finds: 2160p over 1080p over 720p. To steer it towards the groups you trust, write `~/.config/toranime/prefs.json`:

```json
{
  "prefer_groups": ["Sokudo", "Breeze"],
  "avoid_groups": ["Judas"],
  "min_height": 1080,
  "max_size": 8
}
```

- `prefer_groups` wins over resolution, so a preferred group at 720p still beats someone else's 2160p. `avoid_groups` is the opposite. Names are matched against the first `[bracket]` of a release name, case-insensitively.
- `min_height` (720, 1080, 2160) and `max_size` (GB per release) are penalties, not filters: a release you capped out is used only when it is the only one cached, so a preference never leaves you with nothing to play.
- Cached still comes first: a preferred group TorBox has not cached cannot play instantly.

### Your own subtitle look

Releases keep their own subtitle styling by default. If you have a look you prefer, save it in `~/.config/toranime/subs.json` (`%APPDATA%\toranime\subs.json` on Windows) and choose it with `--style NAME`:

```json
{
  "presets": {
    "roboto": {"font": "Roboto Medium"}
  }
}
```

```bash
toranime "chitose" 4 --style roboto
```

A look writes those values into a copy of the episode's subtitle file, cached under the toranime cache folder. The first look in `subs.json` is loaded as an extra subtitle track on every episode, but the release's own subtitles stay selected — press **S** in mpv to flip between them mid-episode ("subtitles: your look" / "subtitles: the release's own"). Add `--style NAME` to start on the look instead, or `--style native` to leave it out entirely.

The copy is cached per file (keyed on the release, not its rotating stream URL), so a rewatch costs nothing. On a first play the bake runs in the background while the episode starts, which takes a couple of seconds for a small release and longer for a big one. Press S before it is done and mpv says so on screen, then switches to the copy by itself the moment it arrives — one press is enough. The copy is a rewrite of the track you already picked, so borders, sizes and weights land exactly as written on every release — mpv's own style overrides cannot scale a border the way a release's styles do. This needs `ffmpeg` on your PATH; without it (or for a subtitle track that isn't ASS) toranime falls back to mpv's overrides, which swap the font and size but leave the border as mpv draws it.

A look sets only what you put in it — `font`, `size`, `bold`, `italic`, `outline`, `shadow`, `margin_v`, `border`, `primary`, `outline_colour`, `back_colour` (ASS style fields, colours as `&HAABBGGRR`). Leaving `size` out keeps each release's own sizing, which is what you usually want across different encodes. `--style native` (or no style) keeps the release's styling, and any font files in `~/.config/toranime/fonts/` are handed to mpv so a look can bring its own font.

To use a look every time, set it once in `prefs.json`:

```json
{"subtitle_style": "roboto"}
```

### Picking the release by hand

After you choose an episode, toranime lists the release groups it found as a numbered menu, the same way the audio prompt works:

```
releases for ONE PIECE 1151:
  1. [Erai-raws] cached        1.30 GB  [Erai-raws] One Piece - 1151 [1080p][Multiple Subtitle]
  2. [Judas] cached            645 MB   [Judas] One Piece (Seasons 1-2 + OVAs)[Dual-Audio]
  3. best cached (now [Erai-raws]) - press Enter
press number, or Enter for default:
```

One line per group, showing that group's best release, whether TorBox has it cached, its size and the release name. Press a number to pick that group, or **Enter** to keep the automatic pick. Past eight groups the numbered keys run out, so it turns into the same live-filter picker as the anime search — type part of a group name to narrow, `↑`/`↓` to move, Enter to pick, Esc to keep the automatic choice. Whatever you pick is preferred for the rest of the run, so a binge stays in the same encode. It only appears on a real terminal, needs at least two groups to choose from, and `--list`, `--print` and `--m3u` still work without it.

For the full list instead of one line per group, add `--pick`: a fuzzy picker over the top 25 releases.

```bash
toranime "one punch man" 1 --dub --pick
```

## How it works

1. Searches the meta addon (`/catalog/anime/kitsu-anime-list/search=...`) for the show, then reads `/meta/series/kitsu:<id>.json` for its episode list.
2. Asks the stream addon for `/stream/series/kitsu:<id>:<episode>.json` (falling back to the `tt` id when that returns nothing).
3. Sends the candidates to TorBox `checkcached` in one batch, keeps the cached ones, and prefers 2160p, then 1080p, then 720p.
4. Adds the pick (only if TorBox has it cached), takes the file that names the episode - the right season, for a multi-season batch, else the biggest file for a single-file release - and asks `requestdl` for its link.
5. mpv streams that link with range requests, so seeking works and nothing is saved to disk.

Streams come from Torrentio because it needs no key and already knows which releases exist for an episode. Playback comes from TorBox because it is the part that fetches and serves the torrent: it has no catalog or search of its own, so discovery and playback stay separate.

Links are signed and expire after a few hours, so a binge resolves a couple of episodes ahead instead of the whole range.

### mpv tips

- `#` cycles audio tracks (dub / Japanese on dual-audio files), `j` cycles subtitles. In dub mode dialogue subtitles stay off (S turns the full English ones on, and back off again), but a signs/songs track (or a forced one) plays when the release has it, and nothing plays when it doesn't. When a release has several English subtitle tracks and never says which is which, toranime measures them instead: it samples the first five minutes, and a track with a handful of brief cues (under ~2 a minute, on screen less than 15% of the time) is treated as signs. That verdict is remembered per release, so the sampling happens once.
- Sub mode picks the full English subtitles by track name, not just the default track. Some releases mark the episode-title card or a signs-only track as the default English subtitles, which mpv alone would show. toranime handles this with a small mpv script it keeps in your cache folder, next to the one that skips openings and endings (`%LOCALAPPDATA%\toranime` on Windows, `~/Library/Caches/toranime` on macOS, `~/.cache/toranime` on Linux).
- Where you left off is kept in `%APPDATA%\toranime\history.json` on Windows (`~/Library/Application Support/toranime` on macOS, `~/.local/state/toranime` on Linux). Delete it to forget everything.
- Only cached releases play: if TorBox does not have a release yet, that candidate is skipped. A show with nothing cached on TorBox says so instead of waiting.

### AniList

Tracking is off until you log in:

```bash
toranime --anilist           # opens AniList in your browser: approve, then paste the token it shows
toranime --anilist logout    # stop tracking
```

The token AniList shows is a long string of letters and dots (about a thousand characters), not the short code on that page. toranime strips a stray `Bearer `, quotes or newline, and asks again instead of giving up if a paste gets cut short. It saves the token `0600` in `~/Library/Application Support/toranime/anilist.json` (`~/.local/state/toranime` on Linux, `%APPDATA%\toranime` on Windows); a headless setup can write `{"token": "..."}` there instead.

The episode picker reads that progress back: every episode up to your last watched one is ticked `✓` and dimmed, and the next one you need is marked `▶` in the normal colour, with `✓ watched · ▶ next` in the header. The picker still opens where you left off. Without a login, or when AniList can't be reached, no marks are added and the picker behaves as before.

Every episode you finish sets your progress on that show. The addon works in Kitsu ids, so toranime maps the show to its AniList entry once (and remembers it). An episode counts as finished the same way it does for resuming: played to within 2 minutes of the end, or into the ending. Skipping past one with `>` doesn't count. Progress only moves forward, and the last episode marks a show completed.

## Development

The tests run offline against fixtures shaped like the real responses (Anime Kitsu, Torrentio, TorBox), so they need no network:

```bash
python -m unittest discover -s tests -v
uvx ruff check .
```

Four tests drive a real mpv when one is installed (the subtitle picker, a whole binge session, and two for the opening and ending skip) and are skipped otherwise.

## Disclaimer

toranime is a client for addons and for TorBox. It hosts nothing, and it only automates what those services already do when you use them by hand. You are responsible for how you use this software and for complying with the terms of the services it talks to.

## License

[MIT](LICENSE)
