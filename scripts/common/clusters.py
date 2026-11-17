"""Optional: album -> cluster from the Streamlit app's album cluster
registry (cluster_registry.json, see docs/weekly-recluster.md). Mount the
app's data folder read-only and point CLUSTER_REGISTRY_PATH at the file.
Albums in the Unclustered system cluster or marked "never cluster" are left
out."""

import json

UNCLUSTERED_ID = "c_unclustered"


def load_album_clusters(path, log=print):
    """({album ratingKey (str): cluster_id}, {cluster_id: name}); empty dicts
    when no path is set or the file can't be read."""
    if not path:
        return {}, {}
    try:
        with open(path) as f:
            reg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  warning: couldn't read cluster registry {path}: {e}")
        return {}, {}
    names = {cid: c.get("name", cid) for cid, c in (reg.get("clusters") or {}).items()
             if cid != UNCLUSTERED_ID}
    album_cluster = {}
    for album, entry in (reg.get("albums") or {}).items():
        cid = entry.get("cluster")
        if cid in names and not entry.get("excluded"):
            album_cluster[str(album)] = cid
    return album_cluster, names
