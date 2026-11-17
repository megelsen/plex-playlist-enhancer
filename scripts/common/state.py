"""Per-script JSON state (atomic writes) and path helpers."""

import json
import os


def script_dir(file):
    return os.path.dirname(os.path.abspath(file))


def load_state(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(default) if default is not None else {"runs": []}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def read_runs(*paths):
    """All `runs` from other scripts' state files that exist (for shared feedback)."""
    runs = []
    for path in paths:
        if path and os.path.exists(path):
            runs.extend(load_state(path).get("runs", []))
    return runs
