#!/usr/bin/env python3
"""Spotify control CLI for chat/car use. Python 3 stdlib only.

Setup: export SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (from a Spotify developer
app whose Redirect URI includes http://127.0.0.1:8888/callback), then run
`python3 spotify.py login` once. Tokens: ~/.spotify_tokens.json (override with
SPOTIFY_TOKENS). Undo log: ~/.spotify_undo.json. Run with -h for commands.
Playlist names match fuzzily; the name "liked" means your Liked Songs.
"""
import argparse, base64, datetime as dt, difflib, http.server, json, os, random
import secrets, sys, time, urllib.error, urllib.parse, urllib.request

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
REDIRECT = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = ("playlist-read-private playlist-read-collaborative playlist-modify-private "
          "playlist-modify-public user-library-read user-library-modify "
          "user-read-playback-state user-modify-playback-state "
          "user-read-currently-playing user-read-recently-played user-top-read")
TOKENS = os.path.expanduser(os.environ.get("SPOTIFY_TOKENS", "~/.spotify_tokens.json"))
UNDO = os.path.expanduser(os.environ.get("SPOTIFY_UNDO", "~/.spotify_undo.json"))
SKIPS = os.path.expanduser(os.environ.get("SPOTIFY_SKIPS", "~/.spotify_skips.json"))
LIKED = {"id": "__liked__", "name": "Liked Songs", "owned": True}


class SpotifyError(Exception):
    pass


def die(msg):
    print(msg)
    sys.exit(1)


# ---------------------------------------------------------------- auth / http
def creds():
    cid, sec = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not sec:
        die("Spotify app keys are missing. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
    return cid, sec


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def token_request(params):
    cid, sec = creds()
    req = urllib.request.Request(ACCOUNTS + "/api/token", data=urllib.parse.urlencode(params).encode())
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode())
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        die(f"Spotify sign-in failed ({e.code}): {e.read().decode(errors='ignore')[:200]}\n"
            "If this keeps happening, run the 'login' command again.")
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 60
    return tok


def refresh(tok):
    if not tok.get("refresh_token"):
        die("Not signed in to Spotify. Run: python3 spotify.py login")
    new = token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    new.setdefault("refresh_token", tok["refresh_token"])
    save_json(TOKENS, new)
    return new


def access_token(force=False):
    tok = load_json(TOKENS, None)
    if not tok:
        die("Not signed in to Spotify. Run: python3 spotify.py login")
    if force or time.time() >= tok.get("expires_at", 0):
        tok = refresh(tok)
    return tok["access_token"]


def api(method, path, params=None, body=None, ok404=False, _retry=True):
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    if data is None and method in ("PUT", "POST", "DELETE"):
        data = b""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + access_token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            try:
                return json.loads(raw) if raw.strip() else {}
            except ValueError:
                return {}
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="ignore")
        if e.code == 401 and _retry:
            access_token(force=True)
            return api(method, path, params, body, ok404, False)
        if e.code == 429 and _retry:
            time.sleep(min(int(e.headers.get("Retry-After", "2")), 10))
            return api(method, path, params, body, ok404, False)
        try:
            msg = json.loads(txt)["error"].get("message") or txt
        except Exception:
            msg = txt[:200]
        if ok404 and e.code == 404:
            return None
        raise SpotifyError(f"{e.code}: {msg}".strip())
    except urllib.error.URLError as e:
        raise SpotifyError(f"network problem: {e.reason}")


def api_try(calls):
    """Try (method, path, params, body) alternatives (new API first, legacy fallback)."""
    last = None
    for c in calls:
        try:
            return api(*c)
        except SpotifyError as e:
            last = e
            if not str(e).startswith(("400", "403", "404", "405", "410")):
                raise
    raise last


# ---------------------------------------------------------------- helpers
def fmt(t):
    if not t:
        return "(unknown)"
    arts = ", ".join(a["name"] for a in t.get("artists", []))
    return f"{t.get('name')} - {arts}" if arts else t.get("name", "?")


def mins(ms):
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def item_of(entry):
    return entry.get("item") or entry.get("track")


def paged(path, params=None, limit=50, cap=2000):
    out, off = [], 0
    while off < cap:
        p = dict(params or {}, limit=limit, offset=off)
        d = api("GET", path, p)
        items = d.get("items", [])
        out += items
        if len(items) < limit or not d.get("next") and off + limit >= d.get("total", 0):
            break
        off += limit
    return out


_ME = {}


def me():
    if not _ME:
        _ME.update(api("GET", "/me"))
    return _ME


def my_playlists():
    uid = me()["id"]
    pls = []
    for p in paged("/me/playlists"):  # paginate by offset (Spotify's 'next' link can be broken)
        if not p:
            continue
        cnt = (p.get("items") or p.get("tracks") or {}).get("total")
        pls.append({"id": p["id"], "name": p["name"], "count": cnt, "public": p.get("public"),
                    "owned": p["owner"]["id"] == uid or p.get("collaborative"),
                    "owner": p["owner"].get("display_name") or p["owner"]["id"]})
    return pls


def norm(s):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in s).split())


def find_playlist(name, need_edit=True, allow_liked=True):
    q = norm(name)
    if allow_liked and q in ("liked", "liked songs", "likes", "favorites", "favourites", "saved", "saved songs"):
        return LIKED
    pls = my_playlists()
    pool = [p for p in pls if p["owned"]] if need_edit else pls
    tiers = [
        [p for p in pool if norm(p["name"]) == q],
        [p for p in pool if norm(p["name"]).startswith(q)],
        [p for p in pool if q and q in norm(p["name"])],
        [p for p in pool if all(w in norm(p["name"]) for w in q.split())],
    ]
    for t in tiers:
        if len(t) == 1:
            return t[0]
        if len(t) > 1:
            die(f"'{name}' matches several playlists: " + "; ".join(p["name"] for p in t)
                + ". Say which one.")
    close = difflib.get_close_matches(q, [norm(p["name"]) for p in pool], n=3, cutoff=0.6)
    if len(close) == 1:
        return next(p for p in pool if norm(p["name"]) == close[0])
    hint = (" Did you mean: " + "; ".join(next(p["name"] for p in pool if norm(p["name"]) == c) for c in close) + "?") if close else ""
    extra = "" if not need_edit or not any(q in norm(p["name"]) for p in pls) else \
        " (A playlist with that name exists but you don't own it, so it can't be edited.)"
    die(f"No playlist you can edit matches '{name}'.{hint}{extra}")


def liked_contains(uri):
    r = api_try([("GET", "/me/library/contains", {"uris": uri}),
                 ("GET", "/me/tracks/contains", {"ids": uri.split(":")[2]})])
    return bool(r and r[0])


def playlist_tracks(pl, cap=10000):
    if pl["id"] == LIKED["id"]:
        return [item_of(e) for e in paged("/me/tracks", cap=min(cap, 500))]
    try:
        ents = paged(f"/playlists/{pl['id']}/items", cap=10000)
    except SpotifyError:
        ents = paged(f"/playlists/{pl['id']}/tracks", cap=10000)
    return [item_of(e) for e in ents]


def current():
    d = api("GET", "/me/player", {"additional_types": "track,episode"}) or {}
    if not d.get("item"):
        d = api("GET", "/me/player/currently-playing", {"additional_types": "track,episode"}) or {}
    return d if d.get("item") else None


def search(q, typ="track", n=5, offset=0):
    out = []
    while len(out) < n:
        lim = min(10, n - len(out))  # Spotify dev-mode search max is 10 per page
        d = api("GET", "/search", {"q": q, "type": typ, "limit": lim, "offset": offset + len(out)})
        got = (d.get(typ + "s") or {}).get("items", [])
        got = [g for g in got if g]
        out += got
        if len(got) < lim:
            break
    return out


def resolve_track(arg):
    """arg: None -> current song; spotify uri/url; or search text."""
    if not arg:
        c = current()
        if not c:
            die("Nothing is playing on Spotify right now, so tell me the song name instead.")
        return c["item"]
    m = arg.strip()
    if "open.spotify.com/track/" in m:
        m = "spotify:track:" + m.split("/track/")[1].split("?")[0]
    if m.startswith("spotify:track:"):
        return api("GET", "/tracks/" + m.split(":")[2])
    res = search(m, "track", 10)
    if not res:
        die(f"Couldn't find a song matching '{arg}'.")
    words = set(norm(m).split())
    def score(t):  # prefer results whose title+artist cover the words asked for
        have = set(norm(t["name"] + " " + " ".join(x["name"] for x in t["artists"])).split())
        return (len(words & have), -res.index(t))
    return max(res, key=score)


def match_in(tracks, text):
    """Find a track in a list by fuzzy 'song' or 'song - artist' text. Returns track or dies."""
    q = norm(text)
    cand = [t for t in tracks if t and (q in norm(fmt(t)) or norm(t["name"]) == q)]
    if not cand:
        names = {norm(fmt(t)): t for t in tracks if t}
        close = difflib.get_close_matches(q, list(names), n=1, cutoff=0.5)
        cand = [names[close[0]]] if close else []
    uris = {t["uri"] for t in cand}
    if not cand:
        die(f"No song matching '{text}' in that playlist.")
    if len(uris) > 1:
        die("Several songs match: " + "; ".join(sorted({fmt(t) for t in cand})[:6]) + ". Be more specific.")
    return cand[0]


# ---------------------------------------------------------------- playlist edits
def add_to(pl, uris, position=None):
    if pl["id"] == LIKED["id"]:
        api_try([("PUT", "/me/library", {"uris": ",".join(uris)}),
                 ("PUT", "/me/tracks", {"ids": ",".join(u.split(":")[2] for u in uris)})])
        return
    for i in range(0, len(uris), 100):
        body = {"uris": uris[i:i + 100]}
        if position is not None:
            body["position"] = position + i
        api_try([("POST", f"/playlists/{pl['id']}/items", None, body),
                 ("POST", f"/playlists/{pl['id']}/tracks", None, body)])


def remove_from(pl, uri, tracks=None):
    """Remove every copy of uri. Returns positions it was at (for undo)."""
    if pl["id"] == LIKED["id"]:
        api_try([("DELETE", "/me/library", {"uris": uri}),
                 ("DELETE", "/me/tracks", {"ids": uri.split(":")[2]})])
        return [0]
    tracks = tracks if tracks is not None else playlist_tracks(pl)
    pos = [i for i, t in enumerate(tracks) if t and t["uri"] == uri]
    if pos:
        api_try([("DELETE", f"/playlists/{pl['id']}/items", None, {"items": [{"uri": uri}]}),
                 ("DELETE", f"/playlists/{pl['id']}/tracks", None, {"tracks": [{"uri": uri}]})])
    return pos


def log_undo(entry):
    log = load_json(UNDO, [])
    entry["at"] = dt.datetime.now().isoformat(timespec="seconds")
    log.append(entry)
    save_json(UNDO, log[-50:])


def pl_ref(pl):
    return {"id": pl["id"], "name": pl["name"]}


# ---------------------------------------------------------------- commands
def cmd_now(a):
    c = current()
    if not c:
        return print("Nothing is playing right now.")
    t = c["item"]
    print(f"Now playing: {t['name']}")
    print("Artist: " + ", ".join(x["name"] for x in t.get("artists", [])))
    if t.get("album"):
        print(f"Album: {t['album']['name']} ({(t['album'].get('release_date') or '')[:4]})")
    if c.get("progress_ms") is not None:
        print(f"Position: {mins(c['progress_ms'])} of {mins(t['duration_ms'])}"
              + ("" if c.get("is_playing") else " (paused)"))
    ctx = c.get("context") or {}
    if ctx.get("type") == "playlist":
        pid = ctx["uri"].split(":")[-1]
        try:
            name = api("GET", f"/playlists/{pid}", {"fields": "name"})["name"]
        except SpotifyError:
            name = pid
        print(f"Playing from playlist: {name}")
    elif ctx.get("type"):
        print(f"Playing from: {ctx['type']}")
    if c.get("device"):
        print(f"Device: {c['device']['name']}")
    print(f"Link: {t.get('external_urls', {}).get('spotify', t['uri'])}")


def cmd_add(a):
    pl = find_playlist(a.playlist)
    t = resolve_track(" ".join(a.song) if a.song else None)
    if pl["id"] != LIKED["id"] and not a.allow_duplicate:
        if any(x and x["uri"] == t["uri"] for x in playlist_tracks(pl)):
            return print(f"'{fmt(t)}' is already in {pl['name']}. Nothing changed.")
    add_to(pl, [t["uri"]])
    log_undo({"action": "add", "playlist": pl_ref(pl), "uri": t["uri"], "label": fmt(t)})
    print(f"Added '{fmt(t)}' to {pl['name']}.")


def cmd_remove(a):
    pl = find_playlist(a.playlist)
    tracks = playlist_tracks(pl) if pl["id"] != LIKED["id"] else None
    if a.song:
        t = match_in(tracks if tracks is not None else playlist_tracks(pl, 500), " ".join(a.song))
    else:
        t = resolve_track(None)
    if pl["id"] == LIKED["id"] and not liked_contains(t["uri"]):
        return print(f"'{fmt(t)}' isn't in Liked Songs. Nothing changed.")
    pos = remove_from(pl, t["uri"], tracks)
    if not pos:
        return print(f"'{fmt(t)}' isn't in {pl['name']}. Nothing changed.")
    log_undo({"action": "remove", "playlist": pl_ref(pl), "uri": t["uri"], "label": fmt(t), "positions": pos})
    extra = f" ({len(pos)} copies)" if len(pos) > 1 else ""
    print(f"Removed '{fmt(t)}' from {pl['name']}{extra}. Say 'undo' to put it back.")


def cmd_move(a):
    src, dst = find_playlist(a.src), find_playlist(a.dst)
    if src["id"] == dst["id"]:
        die("Those are the same playlist.")
    stracks = playlist_tracks(src) if src["id"] != LIKED["id"] else None
    t = match_in(stracks if stracks is not None else playlist_tracks(src, 500), " ".join(a.song)) if a.song else resolve_track(None)
    there = liked_contains(t["uri"]) if src["id"] == LIKED["id"] else any(x and x["uri"] == t["uri"] for x in stracks)
    if not there:
        die(f"'{fmt(t)}' isn't in {src['name']}, so there's nothing to move. Use 'add' instead.")
    already = dst["id"] != LIKED["id"] and any(x and x["uri"] == t["uri"] for x in playlist_tracks(dst))
    if not already:
        add_to(dst, [t["uri"]])
    pos = remove_from(src, t["uri"], stracks)
    log_undo({"action": "move", "from": pl_ref(src), "to": pl_ref(dst), "uri": t["uri"],
              "label": fmt(t), "positions": pos, "added": not already})
    print(f"Moved '{fmt(t)}' from {src['name']} to {dst['name']}."
          + (" (It was already in the destination.)" if already else ""))


def restore(pl, uri, positions):
    for p in sorted(positions):
        add_to(pl, [uri], None if pl["id"] == LIKED["id"] else p)


def cmd_undo(a):
    log = load_json(UNDO, [])
    if not log:
        return print("Nothing to undo.")
    e = log.pop()
    if e["action"] == "add":
        remove_from(e["playlist"], e["uri"])
        msg = f"Undid add: removed '{e['label']}' from {e['playlist']['name']}."
    elif e["action"] == "remove":
        restore(e["playlist"], e["uri"], e["positions"])
        msg = f"Undid remove: put '{e['label']}' back in {e['playlist']['name']}."
    elif e["action"] == "move":
        restore(e["from"], e["uri"], e["positions"])
        if e.get("added"):
            remove_from(e["to"], e["uri"])
        msg = f"Undid move: '{e['label']}' is back in {e['from']['name']}" + \
              (f" and out of {e['to']['name']}." if e.get("added") else ".")
    elif e["action"] == "cleanup":
        for r in e["removed"]:
            restore(e["playlist"], r["uri"], r["positions"])
        msg = f"Undid cleanup: restored {len(e['removed'])} song(s) to {e['playlist']['name']}."
    elif e["action"] == "rename":
        api("PUT", f"/playlists/{e['playlist']['id']}", body={"name": e["old"]})
        msg = f"Undid rename: playlist is called '{e['old']}' again."
    elif e["action"] == "reorder":
        api_try([("PUT", f"/playlists/{e['playlist']['id']}/items", None, e["reverse"]),
                 ("PUT", f"/playlists/{e['playlist']['id']}/tracks", None, e["reverse"])])
        msg = f"Undid reorder in {e['playlist']['name']}."
    else:
        msg = "That action can't be undone."
    save_json(UNDO, log)
    print(msg)


def cmd_like(a):
    t = resolve_track(" ".join(a.song) if a.song else None)
    add_to(LIKED, [t["uri"]])
    log_undo({"action": "add", "playlist": pl_ref(LIKED), "uri": t["uri"], "label": fmt(t)})
    print(f"Saved '{fmt(t)}' to Liked Songs.")


def cmd_list(a):
    pls = my_playlists()
    if not a.all:
        pls = [p for p in pls if p["owned"]]
    print(f"{len(pls)} playlist(s){'' if a.all else ' you can edit'}:")
    for p in pls:
        cnt = "" if p["count"] is None else f" ({p['count']} song{'' if p['count'] == 1 else 's'})"
        who = "" if p["owned"] else f" [by {p['owner']}, read-only]"
        print(f"- {p['name']}{cnt}{who}")


def cmd_show(a):
    pl = find_playlist(a.playlist, need_edit=False)
    try:
        tracks = playlist_tracks(pl, a.limit if pl["id"] == LIKED["id"] else 10000)
    except SpotifyError as e:
        die(f"Can't list songs in {pl['name']} ({e}). Spotify only shares song lists for playlists you own or collaborate on.")
    total = sum((t or {}).get("duration_ms", 0) for t in tracks)
    if pl["id"] == LIKED["id"]:
        print(f"Liked Songs (newest first, showing up to {a.limit}):")
    else:
        print(f"{pl['name']}: {len(tracks)} songs, {total // 60000} min")
    for i, t in enumerate(tracks[: a.limit], 1):
        print(f"{i:3}. {fmt(t)} ({mins((t or {}).get('duration_ms', 0))})")
    if len(tracks) > a.limit:
        print(f"... and {len(tracks) - a.limit} more (use --limit).")


def cmd_rename(a):
    pl = find_playlist(a.playlist, allow_liked=False)
    body = {"name": a.new_name}
    if a.description is not None:
        body["description"] = a.description
    api("PUT", f"/playlists/{pl['id']}", body=body)
    log_undo({"action": "rename", "playlist": pl_ref(pl), "old": pl["name"], "label": a.new_name})
    print(f"Renamed '{pl['name']}' to '{a.new_name}'.")


def cmd_reorder(a):
    pl = find_playlist(a.playlist, allow_liked=False)
    tracks = playlist_tracks(pl)
    n = len(tracks)
    src = a.song_or_pos
    if src.isdigit():
        i = int(src) - 1
    else:
        t = match_in(tracks, src)
        i = next(k for k, x in enumerate(tracks) if x and x["uri"] == t["uri"])
    j = max(1, min(a.to, n)) - 1
    if not 0 <= i < n:
        die(f"Position {src} is out of range (playlist has {n} songs).")
    if i == j:
        return print("That song is already at that spot.")
    ins = j + 1 if j > i else j
    body = {"range_start": i, "insert_before": ins}
    api_try([("PUT", f"/playlists/{pl['id']}/items", None, body),
             ("PUT", f"/playlists/{pl['id']}/tracks", None, body)])
    rev = {"range_start": j, "insert_before": i + 1 if i > j else i}
    log_undo({"action": "reorder", "playlist": pl_ref(pl), "reverse": rev, "label": fmt(tracks[i])})
    print(f"Moved '{fmt(tracks[i])}' from #{i + 1} to #{j + 1} in {pl['name']}.")


def cmd_delete(a):
    pl = find_playlist(a.playlist, need_edit=False, allow_liked=False)
    if not a.confirm:
        return print(f"This would delete (unfollow) the playlist '{pl['name']}'. "
                     "Re-run with --confirm after the owner says yes.")
    api_try([("DELETE", "/me/library", {"uris": f"spotify:playlist:{pl['id']}"}),
             ("DELETE", f"/playlists/{pl['id']}/followers")])
    print(f"Deleted '{pl['name']}' from your library. (Spotify keeps deleted playlists recoverable "
          "for a while at spotify.com > Account > Recover playlists.)")


# --- playlist building
def top_items(kind, n=50):
    out = []
    for term in ("short_term", "medium_term"):
        try:
            out += api("GET", f"/me/top/{kind}", {"limit": n, "time_range": term}).get("items", [])
        except SpotifyError:
            pass
    return out


def artist_tracks(name, n=10):
    res = search(f'artist:"{name}"', "track", n)
    return [t for t in res if any(norm(x["name"]) == norm(name) for x in t["artists"])] or res[:3]


def cmd_create(a):
    seeds = []          # list of (source label, [tracks])
    explicit = []
    for s in a.track or []:
        explicit.append(resolve_track(s))
    if explicit:
        seeds.append(("requested songs", explicit))
    artists = [x.strip() for x in (a.artists or "").split(",") if x.strip()]
    if a.like_current:
        c = current()
        if not c:
            die("Nothing is playing, so there's no 'this' to base the playlist on. Name a song or artist instead.")
        t = c["item"]
        seeds.append(("current song", [t]))
        artists = [x["name"] for x in t["artists"]] + artists
    for q in a.query or []:
        seeds.append((f"search '{q}'", search(q, "track", a.per_query)))
    for name in artists:
        seeds.append((f"artist {name}", artist_tracks(name, 10)))
    if a.from_top:
        tt = top_items("tracks")
        seeds.append(("your top tracks", tt))
        for ar in {x["name"]: x for x in top_items("artists")}.values().__iter__():
            if len(seeds) > 30:
                break
            seeds.append((f"top artist {ar['name']}", artist_tracks(ar["name"], 5)))
    if not seeds:
        die("Tell me what to put in it: --query, --artists, --like-current, --from-top, or --track.")
    # interleave sources, dedupe, cap songs per artist for variety
    seen, per_artist, picked = set(), {}, []
    keyed = lambda t: (norm(t["name"]).split(" feat")[0], norm(t["artists"][0]["name"]) if t["artists"] else "")
    pools = [list(ts) for _, ts in seeds]
    for (label, _), p in zip(seeds, pools):
        if not label.startswith(("search", "requested")):
            random.shuffle(p)
    target_ms = a.minutes * 60000 if a.minutes else None
    max_n = a.count or (10**6 if target_ms else 25)
    total = 0
    for t in explicit:
        seen.add(keyed(t)); picked.append(t); total += t["duration_ms"]
    while any(pools) and len(picked) < max_n and (not target_ms or total < target_ms):
        for p in pools:
            while p:
                t = p.pop(0)
                if not t or t.get("type") != "track" or t.get("is_local"):
                    continue
                k = keyed(t)
                ar = k[1]
                if k in seen or per_artist.get(ar, 0) >= a.max_per_artist:
                    continue
                seen.add(k); per_artist[ar] = per_artist.get(ar, 0) + 1
                picked.append(t); total += t["duration_ms"]
                break
            if len(picked) >= max_n or (target_ms and total >= target_ms):
                break
    if target_ms and total > target_ms + 120000 and len(picked) > 1:
        # drop the last song if it overshoots by more than ~2 minutes and that gets closer
        if abs(total - picked[-1]["duration_ms"] - target_ms) < abs(total - target_ms):
            total -= picked.pop()["duration_ms"]
    if not picked:
        die("Couldn't find any songs for that request. Try different keywords or artists.")
    if a.dry_run:
        print(f"Would create '{a.name}' with {len(picked)} songs ({total // 60000} min):")
        for i, t in enumerate(picked, 1):
            print(f"{i:3}. {fmt(t)}")
        return
    body = {"name": a.name, "public": bool(a.public), "description": a.description or ""}
    pl = api_try([("POST", "/me/playlists", None, body), ("POST", f"/users/{me()['id']}/playlists", None, body)])
    add_to({"id": pl["id"], "name": a.name}, [t["uri"] for t in picked])
    print(f"Created '{a.name}' with {len(picked)} song{'' if len(picked) == 1 else 's'} ({total // 60000} min).")
    for i, t in enumerate(picked[:15], 1):
        print(f"{i:3}. {fmt(t)}")
    if len(picked) > 15:
        print(f"... and {len(picked) - 15} more.")
    print(f"Link: {pl.get('external_urls', {}).get('spotify', '')}")


# --- cleanup
def skip_estimates():
    """Heuristic only: Spotify has no skip API. Recently played (last 50 plays, each
    logged when it stops) lets us estimate how long each play lasted from the gap to the
    previous entry; a play much shorter than the song suggests a skip. Plus skips made
    through this tool's 'next' command, which are logged locally."""
    counts = {}
    try:
        items = api("GET", "/me/player/recently-played", {"limit": 50}).get("items", [])
    except SpotifyError:
        items = []
    ts = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    items.sort(key=lambda x: x["played_at"])
    for prev, cur in zip(items, items[1:]):
        gap = (ts(cur["played_at"]) - ts(prev["played_at"])).total_seconds()
        dur = cur["track"]["duration_ms"] / 1000
        if gap < 0.5 * dur:
            counts[cur["track"]["uri"]] = counts.get(cur["track"]["uri"], 0) + 1
    for s in load_json(SKIPS, []):
        counts[s["uri"]] = counts.get(s["uri"], 0) + 1
    return counts


def cmd_cleanup(a):
    pl = find_playlist(a.playlist, allow_liked=False)
    tracks = playlist_tracks(pl)
    by_uri, by_name = {}, {}
    for i, t in enumerate(tracks):
        if not t:
            continue
        by_uri.setdefault(t["uri"], []).append(i)
        by_name.setdefault((norm(t["name"]), norm(t["artists"][0]["name"]) if t["artists"] else ""), []).append(t)
    exact = {u: p for u, p in by_uri.items() if len(p) > 1}
    near = [v for v in by_name.values() if len({t["uri"] for t in v}) > 1]
    skips = skip_estimates()
    skipped = [(skips[u], tracks[p[0]]) for u, p in by_uri.items() if skips.get(u, 0) >= a.min_skips]
    skipped.sort(key=lambda x: -x[0])
    print(f"Cleanup report for {pl['name']} ({len(tracks)} songs):")
    if exact:
        print("Exact duplicates (extra copies would be removed, first one kept):")
        for u, p in exact.items():
            print(f"- {fmt(tracks[p[0]])}: {len(p)} copies at #" + ", #".join(str(x + 1) for x in p))
    else:
        print("No exact duplicates.")
    if near:
        print("Same song, different versions (not auto-removed; mention to the owner):")
        for v in near:
            print(f"- {fmt(v[0])}: {len(v)} versions")
    if skipped:
        print(f"Possibly skipped recently (estimate from your last 50 plays{' and skips made here' if os.path.exists(SKIPS) else ''}):")
        for n, t in skipped:
            print(f"- {fmt(t)}: cut short {n}x")
    else:
        print("No skip candidates found in recent listening.")
    print("Note: Spotify doesn't share skip history, so skip candidates are a rough estimate.")
    if not a.confirm:
        if exact or (skipped and a.include_skipped):
            print("Nothing was removed. Re-run with --confirm (add --include-skipped for skip candidates) after the owner agrees.")
        return
    removed = []
    for u, p in exact.items():
        # remove all copies then re-add one at the first position (API removes every copy of a uri)
        remove_from(pl, u, tracks)
        add_to(pl, [u], p[0])
        removed.append({"uri": u, "positions": p[1:], "label": fmt(tracks[p[0]])})
    if a.include_skipped:
        for _, t in skipped:
            if t["uri"] in exact:
                continue
            pos = remove_from(pl, t["uri"], tracks)
            removed.append({"uri": t["uri"], "positions": pos, "label": fmt(t)})
    if removed:
        log_undo({"action": "cleanup", "playlist": pl_ref(pl), "removed": removed, "label": "cleanup"})
    print(f"Removed {len(removed)} item(s)." + (" Say 'undo' to restore them." if removed else ""))


# --- playback
def devices():
    return api("GET", "/me/player/devices").get("devices", [])


def is_car(d):
    t, n = (d.get("type") or "").lower(), (d.get("name") or "").lower()
    return "tesla" in n or "tesla" in t or t == "automobile"


def choose_device(ds, name=None):
    """Pure selection logic. Returns (device, notice or None); calls die() if the owner must choose.
    Order: --device name match > active device > the only online device > a single car/Tesla device."""
    listing = ", ".join(f"{d['name']} ({d.get('type', '?')})" for d in ds) or "none"
    if name:
        m = [d for d in ds if norm(name) in norm(d["name"])] or [d for d in ds if norm(name) in norm(d.get("type", ""))]
        if not m:
            die(f"No device matching '{name}'. Online devices: {listing}")
        if len(m) > 1 and not any(norm(d["name"]) == norm(name) for d in m):
            die(f"'{name}' matches several devices: " + ", ".join(d["name"] for d in m) + ". Say which one.")
        exact = [d for d in m if norm(d["name"]) == norm(name)]
        return (exact or m)[0], None
    act = [d for d in ds if d.get("is_active")]
    if act:
        return act[0], None
    if not ds:
        die("No Spotify device is online. Open Spotify in the car (or on a phone/computer) and try again.")
    if len(ds) == 1:
        return ds[0], f"(No device was active, so using {ds[0]['name']}.)"
    cars = [d for d in ds if is_car(d)]
    if len(cars) == 1:
        return cars[0], f"(No device was active, so using the car: {cars[0]['name']}.)"
    die("No device is active and several are online: " + listing
        + ". Which one should I use? (re-run with --device <name>)")


def pick_device(name=None):
    dev, notice = choose_device(devices(), name)
    if notice:
        print(notice)
    return dev


def player(method, path, params=None, body=None, device=None):
    params = dict(params or {})
    if device:
        params["device_id"] = device["id"]
    try:
        return api(method, "/me/player" + path, params, body)
    except SpotifyError as e:
        s = str(e)
        if s.startswith("404"):
            die("No active Spotify device. Start playing something in the car (or use --device), then try again.")
        if s.startswith("403"):
            die("Spotify said that can't be done right now (for example it's already paused, there's no "
                f"previous song, or the account isn't Premium). Details: {s}")
        raise


def cmd_devices(a):
    ds = devices()
    if not ds:
        return print("No Spotify devices are online. Open Spotify in the car or on a phone.")
    for d in ds:
        print(f"- {d['name']} ({d['type']}){' <- active' if d['is_active'] else ''}")


def cmd_play(a):
    q = " ".join(a.what) if a.what else ""
    dev = pick_device(a.device)
    if not q:
        player("PUT", "/play", device=dev)
        return print(f"Resumed playback on {dev['name']}.")
    body, label = None, None
    kinds = [a.type] if a.type else ["playlist", "album"]
    for k in kinds:
        if k == "playlist":
            try:
                pl = find_playlist(q, need_edit=False, allow_liked=False) if not a.type else None
            except SystemExit:
                pl = None
            if a.type == "playlist":
                pls = my_playlists()
                hit = [p for p in pls if norm(q) in norm(p["name"])]
                pl = hit[0] if hit else None
                if not pl:
                    r = search(q, "playlist", 1)
                    pl = {"id": r[0]["id"], "name": r[0]["name"]} if r else None
            if pl:
                body, label = {"context_uri": f"spotify:playlist:{pl['id']}"}, f"playlist {pl['name']}"
                break
        elif k == "album":
            r = search(q, "album", 1)
            if r:
                body, label = {"context_uri": r[0]["uri"]}, f"album {r[0]['name']} - {r[0]['artists'][0]['name']}"
                break
        elif k == "artist":
            r = search(q, "artist", 1)
            if r:
                body, label = {"context_uri": r[0]["uri"]}, f"artist {r[0]['name']}"
                break
        elif k == "track":
            t = resolve_track(q)
            body, label = {"uris": [t["uri"]]}, f"song {fmt(t)}"
            break
    if not body:
        die(f"Couldn't find anything to play for '{q}'.")
    player("PUT", "/play", body=body, device=dev)
    if a.shuffle is not None:
        player("PUT", "/shuffle", {"state": "true" if a.shuffle == "on" else "false"}, device=dev)
    print(f"Playing {label} on {dev['name']}.")


def cmd_queue(a):
    q = " ".join(a.what)
    if a.type == "album":
        r = search(q, "album", 1)
        if not r:
            die(f"No album found for '{q}'.")
        uris = [t["uri"] for t in paged(f"/albums/{r[0]['id']}/tracks", cap=100)]
        label = f"album {r[0]['name']} - {r[0]['artists'][0]['name']} ({len(uris)} songs)"
    elif a.type == "artist":
        r = search(q, "artist", 1)
        if not r:
            die(f"No artist found for '{q}'.")
        ts = artist_tracks(r[0]["name"], 10)[: a.count]
        uris = [t["uri"] for t in ts]
        label = f"{len(uris)} songs by {r[0]['name']}"
    else:
        t = resolve_track(q)
        uris, label = [t["uri"]], fmt(t)
    dev = pick_device(a.device)
    for u in uris:
        player("POST", "/queue", {"uri": u}, device=dev)
    print(f"Queued {label}.")


def cmd_simple(a):
    # pause/next/previous act on the active device unless --device is given; resume may pick one
    dev = pick_device(a.device) if (a.device or a.cmd == "resume") else None
    if a.cmd == "next":
        c = current()
        player("POST", "/next", device=dev)
        if c and c.get("item"):
            log = load_json(SKIPS, [])
            log.append({"uri": c["item"]["uri"], "label": fmt(c["item"]), "at": int(time.time())})
            save_json(SKIPS, log[-500:])
        print("Skipped to the next song.")
    elif a.cmd == "previous":
        player("POST", "/previous", device=dev); print("Went back a song.")
    elif a.cmd == "pause":
        player("PUT", "/pause", device=dev); print("Paused.")
    elif a.cmd == "resume":
        player("PUT", "/play", device=dev); print(f"Resumed on {dev['name']}.")


def cmd_shuffle(a):
    player("PUT", "/shuffle", {"state": "true" if a.state == "on" else "false"}, device=pick_device(a.device))
    print(f"Shuffle {a.state}.")


def cmd_history(a):
    for x in api("GET", "/me/player/recently-played", {"limit": a.limit}).get("items", []):
        when = dt.datetime.fromisoformat(x["played_at"].replace("Z", "+00:00")).astimezone()
        print(f"- {when:%a %I:%M %p}: {fmt(x['track'])}")


# --- login
def cmd_login(a):
    cid, _ = creds()
    state = secrets.token_urlsafe(16)
    url = ACCOUNTS + "/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state, "show_dialog": "true"})

    def finish(qs):
        if qs.get("state", [""])[0] != state and not a.no_state_check:
            die("Sign-in state didn't match; start login again.")
        if "code" not in qs:
            die("Spotify didn't approve access: " + qs.get("error", ["unknown"])[0])
        tok = token_request({"grant_type": "authorization_code", "code": qs["code"][0], "redirect_uri": REDIRECT})
        save_json(TOKENS, tok)
        print(f"Spotify connected as {api('GET', '/me').get('display_name')}. Tokens saved to {TOKENS}.")

    if a.redirected_url:
        a.no_state_check = True
        return finish(urllib.parse.parse_qs(urllib.parse.urlparse(a.redirected_url).query))
    print("Open this link, sign in to Spotify and tap Agree:\n" + url, flush=True)
    if a.url_only:
        return print("\nAfter approving, the browser goes to a 127.0.0.1 page (it may fail to load). Copy that "
                     "full address and run: spotify.py login --redirected-url '<address>'")
    print(f"\nWaiting for Spotify to redirect to {REDIRECT} ... (Ctrl+C to stop)", flush=True)
    pu = urllib.parse.urlparse(REDIRECT)
    result = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != pu.path:
                self.send_response(404); self.end_headers(); return
            result.update(urllib.parse.parse_qs(u.query))
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h2>Spotify connected. You can close this tab.</h2>")

        def log_message(self, *x):
            pass

    srv = http.server.HTTPServer((pu.hostname, pu.port or 80), H)
    srv.timeout = 5
    deadline = time.time() + a.timeout
    while not result and time.time() < deadline:
        srv.handle_request()
    if not result:
        die("Timed out waiting for the Spotify sign-in.")
    finish(result)


# ---------------------------------------------------------------- main
def main():
    P = argparse.ArgumentParser(prog="spotify.py", description="Control Spotify: playlists, songs, playback.")
    S = P.add_subparsers(dest="cmd", required=True)
    def sp(name, fn, help):
        p = S.add_parser(name, help=help); p.set_defaults(fn=fn); return p
    sp("now", cmd_now, "what's playing (song, artist, album, playlist)")
    p = sp("add", cmd_add, "add current song (or a named song) to a playlist")
    p.add_argument("playlist"); p.add_argument("song", nargs="*")
    p.add_argument("--allow-duplicate", action="store_true")
    p = sp("remove", cmd_remove, "remove current song (or a named song in it) from a playlist")
    p.add_argument("playlist"); p.add_argument("song", nargs="*")
    p = sp("move", cmd_move, "move current song (or named song) from one playlist to another")
    p.add_argument("src"); p.add_argument("dst"); p.add_argument("song", nargs="*")
    sp("undo", cmd_undo, "undo the last add/remove/move/rename/reorder/cleanup")
    p = sp("like", cmd_like, "save current song (or named song) to Liked Songs"); p.add_argument("song", nargs="*")
    p = sp("list", cmd_list, "list your playlists"); p.add_argument("--all", action="store_true", help="include followed playlists")
    p = sp("show", cmd_show, "show songs in a playlist"); p.add_argument("playlist"); p.add_argument("--limit", type=int, default=50)
    p = sp("rename", cmd_rename, "rename a playlist"); p.add_argument("playlist"); p.add_argument("new_name")
    p.add_argument("--description")
    p = sp("reorder", cmd_reorder, "move a song to a new position: reorder <playlist> <song|#> --to N")
    p.add_argument("playlist"); p.add_argument("song_or_pos"); p.add_argument("--to", type=int, required=True)
    p = sp("delete", cmd_delete, "delete (unfollow) a playlist; needs --confirm")
    p.add_argument("playlist"); p.add_argument("--confirm", action="store_true")
    p = sp("create", cmd_create, "build a new playlist from a request")
    p.add_argument("name"); p.add_argument("--description", default="")
    p.add_argument("--query", action="append", help="search text, e.g. 'chill acoustic' or 'genre:country year:1990-1999' (repeatable)")
    p.add_argument("--artists", help="comma-separated artists to draw from (e.g. similar artists)")
    p.add_argument("--like-current", action="store_true", help="base it on the song playing now")
    p.add_argument("--from-top", action="store_true", help="use your top tracks and artists")
    p.add_argument("--track", action="append", help="specific song to include (repeatable)")
    p.add_argument("--minutes", type=int, help="target length in minutes")
    p.add_argument("--count", type=int, help="number of songs (default 25 if no --minutes)")
    p.add_argument("--max-per-artist", type=int, default=3)
    p.add_argument("--per-query", type=int, default=30)
    p.add_argument("--public", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p = sp("cleanup", cmd_cleanup, "report duplicates and likely-skipped songs; removes only with --confirm")
    p.add_argument("playlist"); p.add_argument("--confirm", action="store_true")
    p.add_argument("--include-skipped", action="store_true"); p.add_argument("--min-skips", type=int, default=1)
    p = sp("queue", cmd_queue, "queue a song, album or artist")
    p.add_argument("what", nargs="+"); p.add_argument("--type", choices=["track", "album", "artist"], default="track")
    p.add_argument("--count", type=int, default=5, help="songs to queue for an artist")
    p.add_argument("--device", help="device name (default: active device, else the only one, else the car)")
    p = sp("play", cmd_play, "play a playlist/album/artist/song (or resume)")
    p.add_argument("what", nargs="*"); p.add_argument("--type", choices=["playlist", "album", "artist", "track"])
    p.add_argument("--device"); p.add_argument("--shuffle", choices=["on", "off"])
    for n in ("pause", "resume", "next", "previous"):
        sp(n, cmd_simple, n).add_argument("--device")
    p = sp("shuffle", cmd_shuffle, "shuffle on/off"); p.add_argument("state", choices=["on", "off"])
    p.add_argument("--device")
    sp("devices", cmd_devices, "list Spotify devices")
    p = sp("history", cmd_history, "recently played songs"); p.add_argument("--limit", type=int, default=10)
    p = sp("login", cmd_login, "one-time Spotify sign-in")
    p.add_argument("--redirected-url", help="paste the full URL the browser was sent to after approving")
    p.add_argument("--url-only", action="store_true"); p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--no-state-check", action="store_true")
    a = P.parse_args()
    try:
        a.fn(a)
    except SpotifyError as e:
        die(f"Spotify error {e}")


if __name__ == "__main__":
    main()
