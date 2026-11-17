# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org).

## [2.2.8] - 2026-11-17

### Added

- **`toranime --torbox` saves your TorBox API key for you.** It asks for the key without
  echoing it, checks it with `GET /user/me` *before* writing anything, and stores it `0600`
  in `torbox.key` — no `mkdir` + `printf` + `chmod` by hand. It prints the account and plan
  it found, never the key, and re-asks up to three times when TorBox rejects a paste.
  Enter on an existing key keeps it; three bad keys leave it untouched.
  `toranime --torbox logout` removes it, including the copy the old `reanime` folder left.
- The "no TorBox API key" error now points at `toranime --torbox`, with the by-hand file
  instructions after it for scripts.

## [2.2.7] - 2026-11-16

### Changed

- **Windows installs mpv from mpv's own releases.** With no mpv on the machine, toranime
  now downloads the official Windows build from github.com/mpv-player/mpv — the same zip
  Scoop installs, hash for hash — checks the SHA-256 GitHub publishes for it, and unpacks
  it into the app's cache folder. One zip, no installer, no admin rights, nothing
  unpacked unless the hash matches. Scoop and Chocolatey stay as fallbacks.
- **winget's `shinchiro.mpv` is gone.** It was not mpv's release: it is an unrelated
  project's repack of it in its own unsigned installer, which is the opposite of what
  toranime should offer a first-time Windows user.

## [2.2.6] - 2026-11-16

### Added

- **A newer release tells you, once a day.** The first interactive run of each day asks
  GitHub for the newest release (2.5 s at most, and the answer is cached in the cache
  folder) and prints one line when there is one: `toranime --update`.
  `--no-update-check` or `TORANIME_NO_UPDATE_CHECK=1` turns it off; `--list`, `--print`
  and `--m3u` never ask, and a failed check stays silent.

### Changed

- The repository is public again, so `toranime --update` and the one-line installers
  work without a token.

## [2.2.5] - 2026-11-16

### Fixed

- **Searching works in a terminal a killed player left behind.** mpv drives the
  terminal itself, and when it is killed instead of quit it never puts it back: after
  that, Enter stops ending a line, so `search anime:` just echoes `^M` and waits
  forever. Playback now restores the terminal modes it started with, and every prompt
  puts the terminal back into plain line mode before reading.

## [2.2.4] - 2026-11-16

### Fixed

- **The missing-key message shows a command your shell can run.** On Windows it now
  gives the PowerShell `New-Item` / `Set-Content` pair instead of `mkdir -p` and
  `chmod`, which a fresh Windows install does not have.

## [2.2.3] - 2026-11-16

### Fixed

- **S switches subtitles even when something else owns the key.** mpv's own `S` takes a
  screenshot, and an `S` in a config wins over a plain script binding, so the subtitles
  never switched and a screenshot appeared instead. Both pickers now claim the key
  outright (`add_forced_key_binding`), so S always flips the release's subtitles and your
  restyled copy, over a dub too.

## [2.2.2] - 2026-11-16

### Fixed

- **Release groups past the eighth are reachable.** The numbered menu only has keys
  `1`-`9`, so a ninth group could not be picked at all. With more than eight groups the
  same list is now the live-filter picker the anime search uses: type part of a group
  name to narrow, `↑`/`↓` to move, Enter to pick, Esc to keep the automatic choice. The
  picked group is still preferred for the rest of the run.

## [2.2.1] - 2026-11-16

### Fixed

- **Windows installs prefer a package with a verifiable chain to shinchiro's build.**
  `--setup` now tries Scoop, then Chocolatey, then winget. Scoop's `extras/mpv` and
  Chocolatey's `mpvio` unpack the Windows build shinchiro publishes (the one mpv.io
  links to) with a pinned hash; the Chocolatey id also moves off the deprecated `mpv`
  package. winget's `shinchiro.mpv` is a repack of that build by an unrelated project
  (`0GMou/mpv2winget`) in its own unsigned installer, so it is now the last resort for
  a bare Windows with no other package manager.

## [2.2.0] - 2026-11-15

### Added

- **Release preferences** in `~/.config/toranime/prefs.json`: `prefer_groups`,
  `avoid_groups`, `min_height` and `max_size` (GB per release). A preferred group outranks
  resolution, and the caps are penalties rather than filters, so a capped-out release is
  still used when it is the only one TorBox has cached.
- **The release menu** appears after an episode is chosen: one numbered line per release
  group with its best release, cache state and size. Press a number to pick that group,
  Enter keeps the automatic pick, and the choice is preferred for the rest of the run. It
  needs a real terminal and at least two groups, so scripts are unaffected.
- **`--pick`** is the full-list version: a fuzzy picker over the top 25 releases.
- **Subtitle looks** in `~/.config/toranime/subs.json`, applied with `--style NAME` or a
  `"subtitle_style"` line in `prefs.json`. A look sets any ASS style fields you want
  (`font`, `size`, `bold`, `outline`, `shadow`, `margin_v`, colours), fonts in the config
  folder's `fonts/` are passed to mpv, and playback without a style keeps each release's
  own look.
- **Looks are written into a copy of each episode's subtitle file** (when `ffmpeg` is on
  PATH) instead of only overriding mpv's renderer, so a border, weight or size means the
  same thing on every release. The copy lives in the cache folder; the original track is
  untouched, and without ffmpeg toranime falls back to mpv's style overrides.
- **Playback starts immediately**: the baked copy is cached per release (not per stream URL,
  which carries a fresh token every run), so it is made once, and a copy that isn't ready
  is baked in the background while the episode plays and attached when done. Before, every
  launch waited for the whole subtitle extraction: 14s on a 1.35 GB release.
- **Your look is always one key away**: the first look in `subs.json` is now baked and loaded
  as an extra subtitle track on every episode, with the release's own subtitles still
  selected. **S** in mpv flips between the two mid-episode. `--style NAME` starts on the
  look, `--style native` leaves it out; `"subtitle_style"` in `prefs.json` picks which look.
- **Dubs keep the signs and songs**: `--dub` turns the dialogue subtitles off, but picks a
  signs/songs (or forced) track when the release has one, and leaves subtitles off when
  it doesn't. A subtitle look is skipped on dubs, since there is nothing to restyle.
- **Unlabelled signs tracks are found by measuring them**: when a dub's release has several
  English subtitle tracks and no titles or forced flags saying which is which, the first
  five minutes of each are sampled and the sparse one (a couple of cues a minute, on
  screen under 15% of the time) is used as the signs track. The verdict is cached per
  release, so the sampling cost is paid once; labelled releases are never sampled.
- **The episode picker shows your AniList progress**: episodes up to the last one you watched
  are ticked `✓` and dimmed, the next one is marked `▶` with no colour change, and the header says what
  the marks mean. Progress is read once per show, nothing is added without a login, and the
  picker still opens on the episode you left off.

### Fixed

- **AniList login** takes a token pasted with `Bearer `, quotes or a trailing newline,
  retries in place instead of exiting, and says whether a paste looks like the short code
  instead of the long token. Three failures print the path to write `anilist.json` by hand.

## [2.1.0] - 2026-11-15

The app is **toranime** now. Nothing about playback changed.

### Changed

- **`reanime` is `toranime`**: the command, the module (`toranime.py`), the package
  (`toranime-mpv`), the installer env vars (`TORANIME_PKG`, `TORANIME_YES`) and the
  per-user folders. `python toranime.py` replaces `python reanime.py` one for one.
- **Existing installs keep their data.** When the `toranime` folder has no `torbox.key`,
  `sources.json`, `history.json` or AniList token, the one under the old name is read
  instead. Nothing is copied: history is read from the old folder and saved to the new
  one on the next write, so that first save is the migration.
- The mpv scripts and the IPC socket live under `toranime` now, so the mpv scripts are
  rewritten once. The old copies in the cache folder are unused and safe to delete.

## [2.0.0] - 2026-11-15

reanime is no longer a reanime.to client. It streams through **TorBox**: metadata comes
from a Stremio catalog addon, releases from a Stremio stream addon, and the file from
your TorBox account. The picker, binge mode, resume, opening/ending skip and AniList
tracking are unchanged.

### Added

- **TorBox playback.** Releases TorBox already have play at once; `--dub` picks a
  dual-audio release by name. Nothing is torrented from your machine and nothing is
  written to disk: mpv streams TorBox's CDN link. A season pack's file is chosen by
  its season and episode number, so a multi-season batch gives the right episode.
- **Stremio addons for discovery.** The defaults are Anime Kitsu (episodes) and
  Torrentio (releases), both keyless. `~/.config/reanime/sources.json` points either at
  anything else, AIOStreams included, without a code change.
- **`kitsu:10740`** works as the show argument, replacing pasted reanime.to urls.
- **Kitsu to AniList mapping** for tracking: resolved once per show and cached.

### Changed

- The TorBox API key is read from `~/.config/reanime/torbox.key` (`chmod 600`), and is
  never part of the link mpv is given.
- Watch history is keyed by the addon id (`kitsu:10740`) instead of a reanime slug; old
  entries are ignored.
- `--list` prints episode numbers and titles only: a torrent's language belongs to the
  release, not to the episode.
- No mpv means no playback, but `--list` still works without a player.

### Removed

- reanime.to, flixcloud, the SvelteKit page parser and the flix server lookup.

## [1.3.0] - 2026-11-08

### Added

- **AniList tracking.** Log in once with `reanime --anilist`, and every episode you
  finish sets your progress on AniList. reanime.to already names each show's AniList
  entry, so nothing is matched by title. Progress only moves forward: rewatches and
  completed shows are left alone, and the last episode marks a show completed. On
  Android, choosing "next" counts the episode before it. `--anilist logout` stops it.
- **`reanime --update`** installs the newest reanime over the one you have, with uv
  (pip on Termux, or when uv is missing). It updates in place, so it works on Windows
  while reanime is running. A git checkout is told to `git pull` instead.

### Changed

- CI jobs time out (15 minutes for tests, 30 for installs) instead of hanging for six
  hours when mpv or a test gets stuck.

## [1.2.1] - 2026-11-08

### Fixed

- **The arrow keys closed the picker on Linux and macOS** (GNOME Terminal, kitty and
  others): the rest of the key's escape sequence sat in Python's input buffer, so the
  arrow looked like `Esc`. Keys are now read straight from the terminal. Keys like
  `Delete` and `Home` no longer type stray characters into the filter, and keys typed
  quickly in a row are no longer dropped.
- **With mpv 0.42 on Windows, mpv could stay open after the last episode** and
  reanime waited on it: mpv 0.42 idles at the end of the playlist when it runs
  without a console. reanime now tells mpv to close, so the menu comes back.

## [1.2.0] - 2026-11-07

### Added

- **Your fixes stick to the show.** An audio delay, subtitle size or subtitle position
  you change in mpv is saved per show, for dub and sub separately, and set again the
  next time you watch it (Jujutsu Kaisen's dub needs +0.9s).
- **`Tab` skips by hand** in mpv, one key with no `Fn`: past the chapter that's
  playing and any ending or preview right after it (to the next episode if that's
  all that's left), or 85 seconds when the file has no chapters.

### Fixed

- The opening skip no longer skips the story before the opening in files that call
  it "Intro" (Jujutsu Kaisen): "Intro" counts as the opening only when no chapter
  is named like one.
- The ending skip stops at an extra after the ending (Jujutsu Kaisen's "Juju
  Stroll") instead of jumping straight to the next episode.
- Rewinding to before a skipped opening skips it again. Jumping to an opening or
  ending yourself (`PgDn`, a seek) still plays it.

## [1.1.1] - 2026-11-03

### Fixed

- Plain `reanime` crashed on "continue watching?" (`TypeError: list indices must be
  integers`): the episode and the language reached the player swapped.

## [1.1.0] - 2026-11-02

### Added

- **One mpv window for the whole binge.** reanime drives mpv over its JSON IPC and
  adds the next episode to the playlist while the current one plays: episodes
  advance on their own, `Enter` / `>` in mpv skips ahead, `<` goes back, and volume
  and fullscreen stay put. Ranges work the same way instead of batches of 8.
- **Resume.** Quitting mid-episode makes "resume at 12:34" the menu's default. Plain
  `reanime` offers to continue the last show where you stopped, `reanime "one piece"`
  opens the picker on it with your last audio choice, and mpv reopens at the volume
  you last used.
- **Opening and ending skip.** When a file marks them as chapters (One Piece 1 and
  1151 do, 500 doesn't), mpv jumps past the opening, and from the ending or the preview
  straight to the next episode. `PgDn` brings a skipped opening back, quitting
  during the ending counts as watched, and `--no-skip` turns it off.
- **CI pipeline** (GitHub Actions): lint, offline tests on Windows/macOS/Linux with
  Python 3.8 and 3.13, package build, the real installers on fresh machines (plus
  Termux in Docker), a daily live check against reanime.to, and tag-driven releases.
- **Offline test suite** (`tests/`, stdlib `unittest`) covering every pipeline step
  against fixtures, `main()` end to end, the picker redraw, playback, mpv setup and
  Android.
- **`-y` / `--yes`** installs mpv without asking. The installers accept
  `REANIME_YES=1` and `REANIME_PKG=<path or url>` (used by CI).

### Changed

- apt installs refresh the package lists first (`apt-get update`), so mpv installs
  on fresh machines and containers.
- `install.ps1` judges each step by its exit code, so uv's progress output can't
  abort it under Windows PowerShell 5.1.
- `install.sh` also recognises Termux by its `$PREFIX`.

### Fixed

- **Sub mode shows the full English subtitles.** Some files (One Piece episode 1, for
  one) mark the episode-title card as the default English track, so mpv showed only
  the title. A small mpv script now picks the English track by name, skipping
  episode-name, signs, songs, forced and AI "dubtitle" tracks. It runs in desktop
  mpv; mpv-android still picks on its own.
- Episodes without a title no longer leave trailing spaces in the picker and `--list`.

## [1.0.0] - 2026-11-01

### Added

- **Stream any anime on reanime.to into mpv**, English dubs included. Search, pick
  dub or sub, pick an episode, and play. After each episode, a menu offers next /
  replay / previous / pick, and the next episode resolves in the background while
  the current one plays.
- **Ranges.** `1151-1160` and `1151-` (through the newest) play straight through as
  mpv playlists, in batches of 8 so the links (6-hour lifetime) are always fresh.
- **Paste a reanime.to url** to jump straight to that episode and language.
- **`--list`**, **`--print`**, **`--m3u [PATH]`** and **`--mpv PATH`**, like onepace.
- **One-line installers** (`install.ps1`, `install.sh`) that set up uv (and with it
  Python), reanime, and mpv in one go.
- **`--setup`** finds or installs mpv: winget/scoop/choco on Windows, brew/MacPorts
  on macOS, apt/dnf/pacman/zypper/apk/xbps/nix on Linux, and flatpak (no root) for
  Steam Deck and immutable distros. A fresh install is found even before it is on
  `PATH`.
- **Android support.** Under Termux, episodes open in the mpv-android app.
