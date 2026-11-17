"""Run a playlist script once against the fake Plex/Tautulli with a fixed
clock and random seed, and capture what it prints.

    python scripts/tests/harness.py discover-weekly [--seed N]
    python scripts/tests/harness.py on-repeat
    python scripts/tests/harness.py for-you [--days N]

Prints the script's output; deterministic for a given seed.
"""

import argparse
import contextlib
import importlib.util
import io
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_plex  # noqa: E402

NOW_TS = 1_763_000_000          # fixed "now", unix seconds


def load_script(name, env):
    for k, v in env.items():
        os.environ[k] = str(v)
    path = os.path.join(SCRIPTS, name, "script.py")
    spec = importlib.util.spec_from_file_location(f"script_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_ENV = {
    "TAUTULLI_URL": "http://tautulli:8181", "TAUTULLI_APIKEY": "x",
    "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "x", "MUSIC_SECTION_ID": "1",
}


def setup(lib_seed=7, rng_seed=0, clock=None):
    clock = clock if clock is not None else [NOW_TS]
    time.time = lambda: clock[0]
    random.seed(rng_seed)
    plex, history = fake_plex.build_library(seed=lib_seed, now_ts=NOW_TS)
    fake_plex.install(plex, history)
    return plex, history


def run(name, lib_seed=7, rng_seed=0, extra_env=None, plex_history=None):
    if plex_history is None:
        plex, history = setup(lib_seed, rng_seed)
    else:
        plex, history = plex_history
        time.time = lambda: NOW_TS
        random.seed(rng_seed)
        fake_plex.install(plex, history)
    tmp = tempfile.mkdtemp()
    env = dict(BASE_ENV, STATE_FILE=os.path.join(tmp, "state.json"), **(extra_env or {}))
    mod = load_script(name, env)
    random.seed(rng_seed)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if name == "discover-weekly":
            mod.run_once(mod.load_state())
        else:
            mod.run_once() if name == "on-repeat" else mod.run_once(mod.load_state())
    return out.getvalue(), plex, mod


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rng", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    env = {"DRY_RUN": "true"} if a.dry else {}
    text, plex, _ = run(a.name, a.seed, a.rng, env)
    print(text)
    for p in plex.playlists():
        print(f"PLAYLIST {p.title!r}: {[i.ratingKey for i in p.items()]}")
