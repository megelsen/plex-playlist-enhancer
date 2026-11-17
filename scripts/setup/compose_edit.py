"""Rewrite a docker-compose file for the new playlist-script layout, keeping
its comments and formatting (ruamel.yaml round-trip). Used by
scripts/install.sh; runs in a python:3.11-slim container.

    python compose_edit.py COMPOSE_FILE EXAMPLE_FILE DEFAULT_SCRIPTS_PATH

- Finds the On Repeat / Discover Weekly / For You services by what they
  mount or load (scripts/on-repeat, scripts/discover-weekly, scripts/for-you),
  whatever the services are called.
- On Repeat / Discover Weekly: the per-script mount (…/scripts/on-repeat:/app)
  becomes …/scripts:/scripts and working_dir becomes /scripts/<name>.
  Services that don't exist yet are added from the example.
- For You: added from the example if missing, with your scripts path, your TZ
  and the Streamlit app's data folder (found from a …:/app/data mount); the
  cluster lines are dropped when there is no app data folder.
- Prints one JSON line: {"services": {...}, "changes": [...], "data": ...}.
Idempotent: running it on an already converted file changes nothing.
"""

import json
import re
import sys

from ruamel.yaml import YAML

SCRIPTS = ("on-repeat", "discover-weekly", "for-you")


def host_of(vol):
    if isinstance(vol, str):
        return vol.split(":", 1)[0]
    return str(vol.get("source", ""))


def target_of(vol):
    if isinstance(vol, str):
        parts = vol.split(":")
        return parts[1] if len(parts) > 1 else parts[0]
    return str(vol.get("target", ""))


def refs(svc):
    """Strings a service points at: volume sources, env_file paths, working_dir."""
    out = [host_of(v) for v in (svc.get("volumes") or [])]
    env_file = svc.get("env_file") or []
    out += [env_file] if isinstance(env_file, str) else [e if isinstance(e, str) else e.get("path", "")
                                                         for e in env_file]
    out.append(str(svc.get("working_dir", "")))
    return out


def find_service(services, script):
    pat = re.compile(rf"scripts/{re.escape(script)}(/|$|\.env)")
    for name, svc in services.items():
        if any(pat.search(r) for r in refs(svc) if r):
            return name
    return None


def env_value(svc, key):
    env = svc.get("environment") or []
    if isinstance(env, dict):
        return env.get(key)
    for e in env:
        if isinstance(e, str) and e.startswith(key + "="):
            return e.split("=", 1)[1]
    return None


def set_env(svc, key, value):
    env = svc.get("environment")
    if isinstance(env, dict):
        env[key] = value
        return
    env = env or []
    for i, e in enumerate(env):
        if isinstance(e, str) and e.startswith(key + "="):
            env[i] = f"{key}={value}"
            return
    env.append(f"{key}={value}")
    svc["environment"] = env


def drop_env(svc, key):
    env = svc.get("environment")
    if isinstance(env, dict):
        env.pop(key, None)
    elif env:
        for e in [e for e in env if isinstance(e, str) and e.startswith(key + "=")]:
            env.remove(e)


def main(compose_path, example_path, default_scripts):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096          # never wrap long lines such as command: sh -c "..."
    with open(compose_path) as f:
        doc = yaml.load(f)
    with open(example_path) as f:
        example = yaml.load(f)["services"]
    services = doc.setdefault("services", {})
    changes = []

    found = {s: find_service(services, s) for s in SCRIPTS}

    # Where scripts/ lives, as seen from the compose file.
    # (from a host path like ./plex-playlist-enhancer/scripts/on-repeat or
    # …/scripts/on-repeat/.env; the container side /scripts/… is skipped)
    scripts_host = None
    for s in SCRIPTS:
        for r in (refs(services[found[s]]) if found[s] else []):
            if f"scripts/{s}" in r and not r.startswith("/scripts"):
                scripts_host = r.split(f"/{s}")[0]
                break
        if scripts_host:
            break
    scripts_host = scripts_host or default_scripts
    repo_host = scripts_host[: -len("/scripts")] if scripts_host.endswith("/scripts") else scripts_host

    # Time zone and the Streamlit app's data folder.
    tz = None
    for s in ("on-repeat", "discover-weekly", "for-you"):
        if found[s] and env_value(services[found[s]], "TZ"):
            tz = env_value(services[found[s]], "TZ")
            break
    data_host = None
    for name, svc in services.items():
        for v in svc.get("volumes") or []:
            if target_of(v).rstrip("/") == "/app/data":
                data_host = host_of(v)
        if not tz and env_value(svc, "TZ"):
            tz = env_value(svc, "TZ")

    def from_example(script):
        svc = example[f"plex-{script}"]
        svc["volumes"] = [str(v).replace("./plex-playlist-enhancer/scripts", scripts_host)
                          for v in svc["volumes"]]
        svc["env_file"] = [str(e).replace("./plex-playlist-enhancer", repo_host) for e in svc["env_file"]]
        if tz:
            set_env(svc, "TZ", tz)
        if script == "for-you":
            if data_host:
                svc["volumes"] = [v if not v.startswith("./data:") else f"{data_host}:/appdata:ro"
                                  for v in svc["volumes"]]
            else:
                svc["volumes"] = [v for v in svc["volumes"] if ":/appdata" not in v]
                drop_env(svc, "CLUSTER_REGISTRY_PATH")
        return svc

    for script in ("on-repeat", "discover-weekly"):
        name = found[script]
        if name is None:
            name = f"plex-{script}"
            services[name] = from_example(script)
            found[script] = name
            changes.append(f"added service {name}")
            continue
        svc = services[name]
        vols = list(svc.get("volumes") or [])
        new_vols, has_scripts = [], False
        for v in vols:
            if re.search(rf"scripts/{script}/?$", host_of(v)) and target_of(v).rstrip("/") == "/app":
                if not has_scripts:
                    new_vols.append(f"{scripts_host}:/scripts")
                    has_scripts = True
                changes.append(f"{name}: volume {v} -> {scripts_host}:/scripts")
                continue
            if target_of(v).rstrip("/") == "/scripts":
                has_scripts = True
            new_vols.append(v)
        if not has_scripts:
            new_vols.append(f"{scripts_host}:/scripts")
            changes.append(f"{name}: added volume {scripts_host}:/scripts")
        if new_vols != vols:
            svc["volumes"] = new_vols
        wd = f"/scripts/{script}"
        if str(svc.get("working_dir", "")) != wd:
            changes.append(f"{name}: working_dir {svc.get('working_dir')} -> {wd}")
            svc["working_dir"] = wd
        if env_value(svc, "STATE_FILE") == "/app/state.json":
            drop_env(svc, "STATE_FILE")
            changes.append(f"{name}: removed STATE_FILE=/app/state.json")

    if found["for-you"] is None:
        name = "plex-for-you"
        services[name] = from_example("for-you")
        found["for-you"] = name
        changes.append(f"added service {name}"
                       + (f" (clusters from {data_host})" if data_host else " (no app data folder found: no clusters)"))

    if changes:
        with open(compose_path, "w") as f:
            yaml.dump(doc, f)
    print(json.dumps({"services": found, "changes": changes, "data": data_host,
                      "scripts": scripts_host, "tz": tz}))


if __name__ == "__main__":
    main(*sys.argv[1:4])
