"""Weekly cluster curation (Monday 03:00 Europe/Paris by default — see
live_playlists.ensure_scheduler_started) plus the other registry-changing
entry points that also need to reconcile Live Mixes:

  - run_weekly_curation(): snapshot -> scan library -> read listening
    history -> cluster_registry.curate(mode="weekly") -> guardrails ->
    commit generation N+1 -> re-point/rename/mark-stale Live Mixes ->
    DELETE stale Live Mix playlists -> write report.
  - commit_build(): turns a Library Clusters build into a new registry
    generation (UI button). Marks stale mixes but does NOT delete them —
    deletion only ever happens at the weekly curation.
  - rollback_to_generation(): restores a registry snapshot and recreates
    any Live Mix playlists deleted by the curations being undone.

Applies automatically; the guardrails abort a run (nothing committed, no
playlists touched) if it would move too many albums or restructure too
many clusters at once.

Listening habits are deliberately NOT used to decide what belongs
together (a day of listening mixes everything). They only weight cluster
centroids toward albums you actually play, make heavily played albums
harder to move, and feed a weak Live-Mix fit penalty.
"""

import copy
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from plexapi.server import PlexServer

import cluster_registry as cr
import live_playlists as lp
from clustering import apply_merge_plan, load_build_cache, load_cluster_results_cache

logger = logging.getLogger("curation")

CURATION_REPORTS_PATH = os.environ.get("CURATION_REPORTS_PATH", "/app/data/curation_reports.json")
REPORTS_KEEP = 12
HEARTBEAT_PATH = CURATION_REPORTS_PATH + ".heartbeat"


def _writer(debug):
    return debug.write if debug else (lambda *a, **k: None)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _connect(plex_url=None, plex_token=None):
    plex_url = plex_url or os.environ.get("PLEX_URL")
    plex_token = plex_token or os.environ.get("PLEX_TOKEN")
    if not plex_url or not plex_token:
        raise ValueError("PLEX_URL/PLEX_TOKEN not available (checked env vars and arguments).")
    return PlexServer(plex_url, plex_token)


# --- Reports / heartbeat ------------------------------------------------

def load_reports():
    try:
        with open(CURATION_REPORTS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_report(report):
    reports = [r for r in load_reports() if r.get("id") != report["id"]]
    reports.append(report)
    reports = reports[-REPORTS_KEEP:]
    try:
        cr._atomic_write_json(CURATION_REPORTS_PATH, reports)
    except Exception:
        logger.exception("Couldn't save curation report")


def record_heartbeat():
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH) or ".", exist_ok=True)
        with open(HEARTBEAT_PATH, "w") as f:
            f.write(_now_iso())
    except Exception:
        pass


def get_last_heartbeat():
    try:
        with open(HEARTBEAT_PATH, "r") as f:
            return datetime.fromisoformat(f.read().strip())
    except Exception:
        return None


# --- Listening history --------------------------------------------------

def _album_key_of(item):
    key = getattr(item, "parentRatingKey", None)
    if key:
        return str(key)
    parent = getattr(item, "parentKey", None)
    return parent.rstrip("/").split("/")[-1] if parent else None


def listening_stats(plex, days, debug=None):
    """Plays per album (integer counts) from Plex history over the last
    `days` days, plus the 28-day and 7-day subsets. Single-account server,
    so no account filter."""
    d = _writer(debug)
    now = datetime.now()
    try:
        items = plex.history(mindate=now - timedelta(days=days))
    except Exception as e:
        d(f"⚠️ Couldn't read Plex play history ({e}) — curating without listening data.")
        return {}, {}, {}
    total, p28, p7 = defaultdict(int), defaultdict(int), defaultdict(int)
    for h in items:
        if getattr(h, "type", "track") not in ("track", None):
            continue
        album = _album_key_of(h)
        if not album:
            continue
        total[album] += 1
        viewed = getattr(h, "viewedAt", None)
        if viewed is not None:
            age = now - viewed.replace(tzinfo=None)
            if age <= timedelta(days=28):
                p28[album] += 1
            if age <= timedelta(days=7):
                p7[album] += 1
    d(f"**Listening history:** {sum(total.values())} plays across {len(total)} albums in the last {days} days.")
    return dict(total), dict(p28), dict(p7)


# --- Guardrails ---------------------------------------------------------

def _check_guardrails(before, after, stats):
    p = before["params"]
    prior_assigned = sum(1 for e in before["albums"].values() if cr._assigned(e.get("cluster")))
    moved_share = stats["moved"] / prior_assigned if prior_assigned else 0.0
    prior_clusters = [c for c in before["clusters"] if c != cr.UNCLUSTERED_ID]
    # Merges forced by the cluster cap are what you asked for, so they don't count.
    structural = sum(1 for e in stats["events"]
                     if e["type"] in ("retired", "merged") and e.get("reason") != "cap")
    allowed = max(1, int(len(prior_clusters) * p["guard_max_structural_share"]))
    problems = []
    if moved_share > p["guard_max_moved_share"]:
        problems.append(f"{stats['moved']} albums would move ({moved_share:.0%} > "
                        f"{p['guard_max_moved_share']:.0%} limit)")
    if prior_clusters and structural > allowed:
        problems.append(f"{structural} of {len(prior_clusters)} clusters would be retired/merged "
                        f"(limit {allowed})")
    return problems


# --- Entry points -------------------------------------------------------

def run_weekly_curation(plex_url=None, plex_token=None, api_key=None, debug=None, trigger="manual"):
    """The weekly job. Callers hold cluster_registry.JOB_LOCK. Returns the report dict."""
    d = _writer(debug)
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    report = {"id": f"cur_{datetime.now().strftime('%Y%m%d%H%M%S')}", "started_at": _now_iso(),
              "trigger": trigger, "status": "running"}
    try:
        plex = _connect(plex_url, plex_token)
        section = cr.get_music_section(plex)

        reg = cr.load_registry()
        if reg is None:
            build, merge_plan = load_build_cache()
            if build:
                build = apply_merge_plan(build, merge_plan)
                d("**No registry yet — bootstrapping from the last saved Library Clusters build.**")
                boot = cr.bootstrap_from_seeds(plex, build["clusters"], locked_names=build.get("locked", ()),
                                               api_key=api_key, max_clusters=build.get("total_clusters"),
                                               debug=debug)
            else:
                results, _mapping, _saved = load_cluster_results_cache(plex, debug=debug)
                if not results:
                    report.update(status="skipped", reason="No cluster registry and no saved Library Clusters build yet.")
                    return report
                d("**No registry yet — bootstrapping from the last saved (legacy) Library Clusters build.**")
                boot = cr.bootstrap_from_results(plex, results, api_key=api_key, debug=debug)
            reg = cr.load_registry()
            lp.apply_cluster_events(boot["events"], reg)
            report["bootstrapped_generation"] = boot["generation"]

        from_gen = reg["generation"]
        cr.snapshot_registry(reg)
        report["from_generation"] = from_gen

        store = cr.load_album_features()
        scan = cr.scan_library(section, store, debug=debug)
        cr.save_album_features(store)

        p = reg["params"]
        plays, p28, p7 = listening_stats(plex, p["history_days"], debug=debug)
        penalties = lp.collect_mix_fit_penalties(p["mixfit_min_exposures"], p["mixfit_penalty"])
        if penalties:
            d(f"└ Live-Mix fit penalty applied to {len(penalties)} (album, cluster) pair(s).")

        work = copy.deepcopy(reg)
        for k in scan["removed"]:
            work["albums"].pop(k, None)
        work["generation"] = from_gen + 1
        stats = cr.curate(work, store, mode="weekly", plays=plays, recent_plays=(p28, p7),
                          penalties=penalties, api_key=api_key, debug=debug)
        report.update({
            "new_albums": len(scan["new"]), "removed_albums": len(scan["removed"]),
            "moved": stats["moved"], "assigned": stats["assigned"], "ejected": stats["ejected"],
            "clusters": stats["clusters"], "unclustered": stats["unclustered"], "events": stats["events"],
        })

        problems = _check_guardrails(reg, work, stats)
        if problems:
            report.update(status="aborted", reason="; ".join(problems))
            d(f"⛔ **Curation aborted by guardrails:** {report['reason']} — nothing committed.")
            return report

        work["last_scan_at"] = _now_iso()
        cr.save_registry(work)
        report["to_generation"] = work["generation"]
        report["marked_stale"] = lp.apply_cluster_events(stats["events"], work)

        deleted = lp.delete_stale_subscriptions(plex)
        report["deleted_subscriptions"] = deleted
        d(f"**Committed gen {work['generation']}.** {len(deleted)} stale Live Mix(es) removed.")
        report["status"] = "committed"
        return report
    except Exception as e:
        logger.exception("Weekly curation failed")
        report.update(status="failed", reason=str(e))
        raise
    finally:
        report["finished_at"] = _now_iso()
        _save_report(report)


def commit_build(plex, build, api_key=None, debug=None):
    """UI: turns a Library Clusters build (merge plan already applied) into a
    new registry generation, seeded from the build's FULL album membership
    and capped at the build's cluster count. Stale mixes are only marked;
    the weekly curation deletes them. Callers hold cluster_registry.JOB_LOCK."""
    old = cr.load_registry()
    if old:
        cr.snapshot_registry(old)
    boot = cr.bootstrap_from_seeds(plex, build["clusters"], locked_names=build.get("locked", ()),
                                   api_key=api_key, max_clusters=build.get("total_clusters"), debug=debug)
    reg = cr.load_registry()
    marked = lp.apply_cluster_events(boot["events"], reg)
    _save_report({
        "id": f"build_{datetime.now().strftime('%Y%m%d%H%M%S')}", "trigger": "build", "status": "committed",
        "started_at": _now_iso(), "finished_at": _now_iso(),
        "from_generation": old["generation"] if old else None, "to_generation": boot["generation"],
        "events": boot["events"], "marked_stale": marked, "deleted_subscriptions": [],
        **{k: boot["stats"].get(k) for k in ("moved", "assigned", "ejected", "clusters", "unclustered")},
    })
    return boot


def rollback_to_generation(generation, plex=None, debug=None):
    """Restores the registry snapshot taken at `generation` and undoes every
    committed run that started from it or later: deleted Live Mix playlists
    are recreated from their saved track lists. Callers hold JOB_LOCK."""
    d = _writer(debug)
    snap = cr.load_snapshot(generation)
    if snap is None:
        raise ValueError(f"No snapshot for generation {generation}.")
    plex = plex or _connect()

    restored = 0
    reports = load_reports()
    for r in reports:
        if r.get("status") != "committed" or r.get("rolled_back"):
            continue
        if r.get("from_generation") is None or r["from_generation"] < generation:
            continue
        for record in r.get("deleted_subscriptions") or []:
            lp.restore_deleted_subscription(plex, record)
            restored += 1
        r["rolled_back"] = True
        _save_report(r)

    cr.save_registry(snap)
    lp.apply_cluster_events([], cr.load_registry())
    d(f"**Rolled back to generation {generation}** — {restored} Live Mix(es) restored.")
    return {"generation": generation, "restored": restored}
