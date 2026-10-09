# grok-spotify-bot

Chat control for Spotify, built for Tesla owners using a Grok Bot from their Tesla. **Requires Spotify Premium.** It covers the things the Tesla Spotify app can't: add or remove the song that's playing from a specific playlist, move it between playlists, undo, build a playlist on request, clean up a playlist, and queue or play music on your Tesla.

It's one Python 3 script (`spotify.py`), standard library only. No pip installs.

## Setup

1. **Create a Spotify developer app** at <https://developer.spotify.com/dashboard>: click **Create app**, add `http://127.0.0.1:8888/callback` as a Redirect URI, tick **Web API**, and save. Open the app's settings to see its Client ID and Client Secret.
   - Spotify's development mode needs the app owner to have Spotify **Premium**, and allows up to 5 users (add others under the app's User Management).
2. **Set your keys** as environment variables (keep them out of files you share):
   ```sh
   export SPOTIFY_CLIENT_ID=...
   export SPOTIFY_CLIENT_SECRET=...
   ```
3. **Get the script:**
   ```sh
   mkdir -p /workspace/spotify
   curl -fsSL https://raw.githubusercontent.com/bigc5012/grok-spotify-bot/main/spotify.py -o /workspace/spotify/spotify.py
   ```
4. **Sign in once:** `python3 spotify.py login` prints an authorize link and waits on `127.0.0.1:8888`. Open the link in a browser on the same machine and tap **Agree**. If the browser is on another machine, use `login --url-only`, approve, then run `login --redirected-url '<the 127.0.0.1 address you landed on>'`.

Tokens are saved to `~/.spotify_tokens.json` (permissions 600) and refresh automatically. The undo history goes to `~/.spotify_undo.json`. You can change these paths with the env vars `SPOTIFY_TOKENS`, `SPOTIFY_UNDO`, `SPOTIFY_SKIPS` and `SPOTIFY_REDIRECT_URI`.

## Commands

Playlist names match loosely (case-insensitive, partial). `liked` means Liked Songs. Run `python3 spotify.py <command> -h` for options.

| Command | What it does |
| --- | --- |
| `now` | Song, artist, album, playlist it's playing from, and device |
| `add <playlist> [song]` | Add the current song (or a searched song / Spotify link) |
| `remove <playlist> [song]` | Remove the current song (or a named song in that playlist) |
| `move <from> <to> [song]` | Move the current (or named) song between playlists |
| `undo` | Reverse the last add/remove/move/like/rename/reorder/cleanup, restoring positions |
| `like [song]` | Save to Liked Songs |
| `list [--all]` / `show <playlist>` | List playlists / show songs |
| `rename <playlist> "<name>"` | Rename (optional `--description`) |
| `reorder <playlist> <song or #> --to N` | Move a song to position N |
| `create "<name>" ...` | Build a playlist: `--query`, `--artists`, `--like-current`, `--from-top`, `--track`, `--minutes N` or `--count N`, `--dry-run` |
| `cleanup <playlist>` | Reports duplicates and likely-skipped songs. Removes only with `--confirm` (`--include-skipped` for skips) |
| `delete <playlist> --confirm` | Delete (unfollow) a playlist |
| `play [what]`, `pause`, `resume`, `next`, `previous`, `shuffle on/off` | Playback |
| `queue <what> [--type track/album/artist]` | Add to the queue |
| `devices`, `history` | Online devices, recent plays |
| `login` | One-time sign-in |

**Device choice** (play/resume/queue/shuffle, unless `--device <name>` is given): the active device. If none is active, the only online device; if several are online, the Tesla (name/type contains "tesla", or type Automobile). Otherwise it lists the devices and asks.

## Spotify limits (development mode, 2026)

- Recommendations, related artists, audio features, artist top tracks and browse/new releases aren't available to new apps. "More like this" playlists therefore come from the current song's artists, artists you name, your top tracks/artists, and search.
- Search returns at most 10 results per page.
- Songs can be listed or edited only in playlists you own or collaborate on.
- Spotify has no skip-history API. `cleanup` estimates skips from your last 50 plays (a play much shorter than the song) plus skips made with this script's `next`.
- Playback control needs Premium and an online device (open Spotify on your Tesla's screen).

## License

MIT
