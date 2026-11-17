"""Rewrite a docker-compose file for the new playlist-script layout, keeping
its comments and formatting (ruamel.yaml round-trip). Used by
scripts/install.sh; runs in a python:3.11-slim container.

    python compose_edit.py COMPOSE_FILE EXAMPLE_FILE SCRIPTS_PATH [--dry]

Finds the On Repeat / Discover Weekly / For You services, in either layout:
  - repo layout:       they mount …/scripts/on-repeat (or load …/scripts/on-repeat/.env)
  - standalone layout: a service or container named plex-on-repeat /
                       plex-discover-weekly that mounts its own folder at /app
                       (e.g. ./plex-discover-weekly:/app)

On Repeat / Discover Weekly: the folder mounted at /app becomes
SCRIPTS_PATH:/scripts, working_dir becomes /scripts/<name>, and an env_file
inside the old folder moves to SCRIPTS_PATH/<name>/.env. Everything else in
the service (networks, mem_limit, restart, command, …) is left as it is.
Missing services are added from the example.

For You: added from the example if missing, with SCRIPTS_PATH, the TZ of the
other script services, their networks and mem_limit (so it reaches Plex and
Tautulli the same way), and the Streamlit app's data folder (found from a
…:/app/data mount) for clusters.

Prints one JSON line: {"services", "old_dirs", "changes", "data", "tz", ...}.
--dry: report what would change without writing. Idempotent.
"""

import copy
import json
import re
import sys

from ruamel.yaml import YAML

SCRIPTS = ("on-repeat", "discover-weekly", "for-you")
COPY_FROM_SIBLING = ("networks", "mem_limit", "extra_hosts", "dns")


def host_of(vol):
    if isinstance(vol, str):
        return vol.split(":", 1)[0]
    return str(vol.get("source", ""))


def target_of(vol):
    if isinstance(vol, str):
        parts = vol.split(":")
        return parts[1] if len(parts) > 1 else parts[0]
    return str(vol.get("target", ""))


def env_files(svc):
    env_file = svc.get("env_file") or []
    if isinstance(env_file, str):
        return [env_file]
    return [e if isinstance(e, str) else e.get("path", "") for e in env_file]


def refs(svc):
    """Strings a service points at: volume sources, env_file paths, working_dir."""
    return ([host_of(v) for v in (svc.get("volumes") or [])] + env_files(svc)
            + [str(svc.get("working_dir", ""))])


def find_service(services, script):
    """(service name, old standalone folder or None)."""
    pat = re.compile(rf"scripts/{re.escape(script)}(/|$|\.env)")
    for name, svc in services.items():
        if any(pat.search(r) for r in refs(svc) if r):
            return name, None
    for name, svc in services.items():
        if f"plex-{script}" in (name, str(svc.get("container_name", ""))):
            old = next((host_of(v) for v in (svc.get("volumes") or [])
                        if target_of(v).rstrip("/") == "/app"), None)
            return name, old
    return None, None


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
    env = env if env is not None else []
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


def main(compose_path, example_path, scripts_host, dry=False):
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

    found, old_dirs = {}, {}
    for s in SCRIPTS:
        found[s], old = find_service(services, s)
        if old:
            old_dirs[s] = old

    # A repo-layout service tells where scripts/ is; otherwise use SCRIPTS_PATH.
    for s in SCRIPTS:
        for r in (refs(services[found[s]]) if found[s] else []):
            if f"scripts/{s}" in r and not r.startswith("/scripts"):
                scripts_host = r.split(f"/{s}")[0]
                break
        else:
            continue
        break
    repo_host = scripts_host[: -len("/scripts")] if scripts_host.endswith("/scripts") else scripts_host

    siblings = [services[found[s]] for s in ("discover-weekly", "on-repeat") if found[s]]
    tz = next((env_value(svc, "TZ") for svc in siblings if env_value(svc, "TZ")), None)
    data_host = None
    for name, svc in services.items():
        for v in svc.get("volumes") or []:
            if target_of(v).rstrip("/") == "/app/data":
                data_host = host_of(v)
        if not tz and env_value(svc, "TZ"):
            tz = env_value(svc, "TZ")

    def from_example(script):
        svc = copy.deepcopy(example[f"plex-{script}"])
        svc["volumes"] = [str(v).replace("./plex-playlist-enhancer/scripts", scripts_host)
                          for v in svc["volumes"]]
        svc["env_file"] = [str(e).replace("./plex-playlist-enhancer", repo_host) for e in svc["env_file"]]
        if tz:
            set_env(svc, "TZ", tz)
        if siblings:
            for key in COPY_FROM_SIBLING:
                if key in siblings[0]:
                    svc[key] = copy.deepcopy(siblings[0][key])
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
        old = old_dirs.get(script)
        vols = list(svc.get("volumes") or [])
        new_vols, has_scripts = [], False
        for v in vols:
            h, t = host_of(v), target_of(v).rstrip("/")
            if t == "/app" and (re.search(rf"scripts/{script}/?$", h) or h == old):
                if not has_scripts:
                    new_vols.append(f"{scripts_host}:/scripts")
                    has_scripts = True
                changes.append(f"{name}: volume {v} -> {scripts_host}:/scripts")
                continue
            if t == "/scripts":
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
        if old:
            new_env = f"{scripts_host}/{script}/.env"
            ef = svc.get("env_file")
            if isinstance(ef, str) and ef.startswith(old.rstrip("/") + "/"):
                svc["env_file"] = new_env
                changes.append(f"{name}: env_file {ef} -> {new_env}")
            elif ef:
                for i, e in enumerate(ef):
                    p = e if isinstance(e, str) else e.get("path", "")
                    if p.startswith(old.rstrip("/") + "/"):
                        if isinstance(e, str):
                            ef[i] = new_env
                        else:
                            e["path"] = new_env
                        changes.append(f"{name}: env_file {p} -> {new_env}")
        if env_value(svc, "STATE_FILE") == "/app/state.json":
            drop_env(svc, "STATE_FILE")
            changes.append(f"{name}: removed STATE_FILE=/app/state.json")

    if found["for-you"] is None:
        name = "plex-for-you"
        services[name] = from_example("for-you")
        found["for-you"] = name
        extra = [k for k in COPY_FROM_SIBLING if k in services[name]]
        changes.append(f"added service {name}"
                       + (f" (clusters from {data_host})" if data_host else " (no app data folder found: no clusters)")
                       + (f"; {', '.join(extra)} copied from the other script services" if extra else ""))

    if changes and not dry:
        with open(compose_path, "w") as f:
            yaml.dump(doc, f)
    print(json.dumps({"services": found, "old_dirs": old_dirs, "changes": changes, "data": data_host,
                      "scripts": scripts_host, "tz": tz}))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry"]
    main(*args[:3], dry="--dry" in sys.argv)
