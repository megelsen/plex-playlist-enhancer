"""End-to-end checks of the playlist scripts against the fake Plex/Tautulli.

    python scripts/tests/test_scripts.py

No real server is touched. Exits non-zero on the first failed check.
"""

import contextlib
import io
import os
import random
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_plex  # noqa: E402
import harness  # noqa: E402

DAY = 86400


def quiet(fn, *a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*a, **k)
    return result, out.getvalue()


def setup(lib_seed):
    clock = [harness.NOW_TS]
    time.time = lambda: clock[0]
    random.seed(0)
    plex, history = fake_plex.build_library(seed=lib_seed, now_ts=harness.NOW_TS)
    fake_plex.install(plex, history)
    tmp = tempfile.mkdtemp()
    paths = {name: os.path.join(tmp, f"{name}.json") for name in ("dw", "fy", "or")}
    env = dict(harness.BASE_ENV, DISCOVER_STATE_FILE=paths["dw"], FOR_YOU_STATE_FILE=paths["fy"])
    return plex, history, clock, paths, env


def load(name, env, state_file, **extra):
    os.environ.pop("DRY_RUN", None)
    os.environ.pop("FULL_REBUILD", None)
    os.environ.pop("CLUSTER_REGISTRY_PATH", None)
    os.environ.pop("BACKTEST_DAYS", None)
    return harness.load_script(name, dict(env, STATE_FILE=state_file, **extra))


def listen(plex, history, rk, ts, full=True):
    """Simulate one play: a Tautulli row, and Plex viewCount / lastViewedAt
    if it was listened to the end."""
    t = plex._tracks[int(rk)]
    secs = t.duration // 1000 if full else 20
    history.insert(0, {"rating_key": t.ratingKey, "date": ts, "started": ts, "stopped": ts + secs,
                       "paused_counter": 0, "watched_status": 1 if full else 0,
                       "title": t.title, "grandparent_title": t.grandparentTitle})
    if full:
        t.viewCount += 1
        t.lastViewedAt = datetime.fromtimestamp(ts + secs)
    else:
        t.skipCount += 1


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok  {msg}")


def test_for_you_rotation(lib_seed=7):
    print(f"For You: build + 4 days of rotation (library {lib_seed})")
    plex, history, clock, paths, env = setup(lib_seed)

    on_repeat = load("on-repeat", env, paths["or"])
    quiet(on_repeat.run_once)
    dw = load("discover-weekly", env, paths["dw"])
    quiet(dw.run_once, dw.load_state())
    fy = load("for-you", env, paths["fy"])
    ok, out = quiet(fy.run_once, fy.load_state())
    check(ok, "first For You run writes a playlist")

    def lists():
        pl = {p.title: [str(i.ratingKey) for i in p.items()] for p in plex.playlists()}
        return pl["For You"], set(pl["On Repeat"]), set(pl["Discover Weekly"])

    foryou, rep, disc = lists()
    state = fy.load_state()
    check(len(foryou) == 50, f"50 tracks (got {len(foryou)})")
    check(not (set(foryou) & rep), "no On Repeat track in For You")
    check(not (set(foryou) & disc), "no Discover Weekly track in For You")
    buckets = [c["bucket"] for c in state["current"]]
    check(buckets[0] == "classic", "opens with a Classic")
    run = worst = 0
    for b in buckets:
        run = run + 1 if b == "unheard" else 0
        worst = max(worst, run)
    check(worst <= 2, f"never more than 2 unheard in a row (max {worst})")
    songs = [plex._tracks[int(rk)] for rk in foryou]
    keys = [fake_plex_song(t) for t in songs]
    check(len(set(keys)) == len(keys), "one copy per song")
    artists = [t.grandparentRatingKey for t in songs]
    check(all(a != b for a, b in zip(artists, artists[1:])), "no artist twice in a row")
    check(all(plex._tracks[int(rk)].duration >= 90_000 for rk in foryou), "no tracks under 90 s")

    # Rename the playlist in Plex: it must still be found by ratingKey.
    for p in plex.playlists():
        if p.title == "For You":
            p.title = "For You (mine)"

    prev = foryou
    for day in range(1, 5):
        clock[0] += DAY
        played, skipped = prev[3], prev[7]
        listen(plex, history, played, clock[0] - 3600)
        listen(plex, history, skipped, clock[0] - 3000, full=False)
        fy = load("for-you", env, paths["fy"])
        ok, out = quiet(fy.run_once, fy.load_state())
        check(ok, f"day {day}: rotation run")
        titles = [p.title for p in plex.playlists()]
        check(titles.count("For You") == 0 and "For You (mine)" in titles,
              f"day {day}: renamed playlist reused, no duplicate")
        cur = [str(i.ratingKey) for p in plex.playlists() if p.title == "For You (mine)" for i in p.items()]
        state = fy.load_state()
        check(len(cur) == 50, f"day {day}: still 50 tracks")
        check(played not in cur and skipped not in cur, f"day {day}: played and skipped tracks left")
        new = set(cur) - set(prev)
        check(12 <= len(new) <= 15, f"day {day}: about 25% new ({len(new)})")
        kept = [rk for rk in prev if rk in cur]
        check(kept == [rk for rk in cur if rk in set(kept)], f"day {day}: kept tracks keep their order")
        check(all(rk in state["cooldown"] for rk in set(prev) - set(cur)), f"day {day}: removed tracks cool down")
        removed_before = set(state["cooldown"])
        check(not (new & (removed_before - (set(prev) - set(cur)))) or day == 1,
              f"day {day}: nothing back from cooldown")
        check(len(state["runs"][-1]["picks"]) == len(new), f"day {day}: new picks recorded for feedback")
        prev = cur

    clock[0] += 3 * DAY
    dw = load("discover-weekly", env, paths["dw"])
    ok, out = quiet(dw.run_once, dw.load_state())
    check(ok, "Discover Weekly a week later still builds, reading For You's picks as feedback")
    check("artists re-weighted" in out, "shared feedback reported")
    print()


def fake_plex_song(t):
    sys.path.insert(0, os.path.dirname(HERE))
    from common.library import song_key
    return song_key(t.grandparentTitle, t.title)


def test_for_you_clusters(lib_seed=11):
    print(f"For You: cluster registry signal and cap (library {lib_seed})")
    plex, history, clock, paths, env = setup(lib_seed)
    import json
    reg = {"clusters": {"c_unclustered": {"name": "Unclustered"}}, "albums": {}}
    for album in plex._albums.values():
        cid = "c_" + album.genres[0].tag.replace(" ", "_")
        reg["clusters"].setdefault(cid, {"name": album.genres[0].tag.title()})
        reg["albums"][str(album.ratingKey)] = {"cluster": cid}
    reg_path = os.path.join(os.path.dirname(paths["fy"]), "cluster_registry.json")
    with open(reg_path, "w") as f:
        json.dump(reg, f)
    fy = load("for-you", env, paths["fy"], CLUSTER_REGISTRY_PATH=reg_path)
    ok, out = quiet(fy.run_once, fy.load_state())
    check(ok and "5 clusters" in out, "registry read (5 clusters)")
    cur = [i for p in plex.playlists() if p.title == "For You" for i in p.items()]
    per = {}
    for t in cur:
        g = plex._albums[t.parentRatingKey].genres[0].tag
        per[g] = per.get(g, 0) + 1
    check(max(per.values()) <= 18, f"no cluster above 35% of 50 = 18 ({per})")
    print()


def test_backtest(lib_seed=23):
    print(f"For You: backtest (library {lib_seed})")
    plex, history, clock, paths, env = setup(lib_seed)
    fy = load("for-you", env, paths["fy"], BACKTEST_DAYS="14")
    _, out = quiet(fy.backtest, 14)
    print("    " + out.replace("\n", "\n    ").rstrip())
    check("engine, bias 0.6 *" in out and "@50" in out, "backtest compares recency biases")
    os.environ.pop("BACKTEST_DAYS", None)
    print()


def test_recency_bias(lib_seed=23):
    print(f"For You: recency bias on a library whose taste moved on (library {lib_seed})")
    clock = [harness.NOW_TS]
    time.time = lambda: clock[0]
    random.seed(0)
    plex, history = fake_plex.build_library(seed=lib_seed, now_ts=harness.NOW_TS, taste_shift=True)
    fake_plex.install(plex, history)
    fy = load("for-you", dict(harness.BASE_ENV), os.path.join(tempfile.mkdtemp(), "fy.json"))
    tracks, albums, similar, hist = quiet(fy.gather, plex, harness.NOW_TS, {})[0]
    p = quiet(fy.Profile, plex, tracks, albums, similar, hist, harness.NOW_TS, [])[0]
    scene = {a.title: int(a.title[-2:]) % 5 for a in plex._artists.values()}

    def old_style(bias, bucket):
        """Top-20 picks from the styles played only 150-400 days ago."""
        buckets, _ = fy.build_candidates(p, set(), set(), bias)
        top = sorted(buckets[bucket], key=lambda x: x[0], reverse=True)[:20]
        return sum(scene[t["artist"]] in (0, 1) for _, t in top)

    u0, u6 = old_style(0.0, "unheard"), old_style(0.6, "unheard")
    r0, r9 = old_style(0.0, "rare"), old_style(0.9, "rare")
    check(u0 > u6, f"Unheard: old-style picks drop with the bias (bias 0: {u0}/20, bias 0.6: {u6}/20)")
    check(r0 > r9, f"Rarely played: old-style picks drop with the bias (bias 0: {r0}/20, bias 0.9: {r9}/20)")
    print()


def test_dry_run_writes_nothing(lib_seed=31):
    print(f"For You: DRY_RUN (library {lib_seed})")
    plex, history, clock, paths, env = setup(lib_seed)
    fy = load("for-you", env, paths["fy"], DRY_RUN="true")
    quiet(fy.run_once, fy.load_state())
    check(not plex.playlists(), "no playlist written")
    check(not os.path.exists(paths["fy"]), "no state written")
    os.environ.pop("DRY_RUN", None)
    print()


if __name__ == "__main__":
    test_for_you_rotation(7)
    test_for_you_rotation(23)
    test_for_you_clusters(11)
    test_recency_bias(23)
    test_dry_run_writes_nothing(31)
    test_backtest(23)
    print("All checks passed.")
