#!/usr/bin/env bash
# Installs / upgrades the playlist scripts (On Repeat, Discover Weekly, For You)
# in your docker compose stack — steps 1-7 of docs/playlist-scripts-setup.md.
#
# Run it from the folder that holds docker-compose.yml (and the
# plex-playlist-enhancer checkout):
#
#   bash plex-playlist-enhancer/scripts/install.sh              # interactive
#   bash plex-playlist-enhancer/scripts/install.sh --backtest   # also run the backtest
#   bash plex-playlist-enhancer/scripts/install.sh --yes        # no questions
#   bash plex-playlist-enhancer/scripts/install.sh --undo       # put everything back
#
# Options: --compose FILE   compose file (default: docker-compose.yml / compose.yaml here)
#          --repo DIR       the plex-playlist-enhancer checkout (default: found next to the compose file)
#          --skip-git       don't fetch/checkout the branch
#          --no-start       stop after the dry run
#
# Safe by design: backups of every file it changes (*.bak-<time>), a dry run
# that writes nothing to Plex before anything starts, a question before
# starting (unless --yes), and --undo to restore the backups.

set -euo pipefail

BRANCH="for-you-playlist"
YES=0; BACKTEST=0; UNDO=0; SKIP_GIT=0; NO_START=0; COMPOSE=""; REPO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --backtest) BACKTEST=1 ;;
    --undo) UNDO=1 ;;
    --skip-git) SKIP_GIT=1 ;;
    --no-start) NO_START=1 ;;
    --compose) COMPOSE="$2"; shift ;;
    --repo) REPO="$2"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done

bold() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { [ "$YES" = 1 ] && return 0; read -r -p "  $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

ROOT="$(pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

# --- Locate things -----------------------------------------------------------
if [ -z "$COMPOSE" ]; then
  for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
    [ -f "$f" ] && COMPOSE="$f" && break
  done
fi
[ -n "$COMPOSE" ] && [ -f "$COMPOSE" ] || die "No docker-compose.yml here. Run this from the folder with your compose file, or pass --compose FILE."
if [ -z "$REPO" ]; then
  SELF_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  REPO="$SELF_REPO"
fi
REPO="$(cd "$REPO" && pwd)"
case "$REPO/" in "$ROOT"/*) ;; *) die "The checkout ($REPO) must be inside $ROOT (next to the compose file).";; esac
REPO_REL="./${REPO#"$ROOT"/}"
STATE="$REPO/scripts/setup/.last-install"
docker compose version >/dev/null 2>&1 || die "'docker compose' not found."
# Plain 'docker compose' for the default file names, so a docker-compose.override.yml
# next to it keeps being applied, exactly as when you run docker compose yourself.
case "$COMPOSE" in
  docker-compose.yml|docker-compose.yaml|compose.yml|compose.yaml) DC=(docker compose) ;;
  *) DC=(docker compose -f "$COMPOSE") ;;
esac
# Optional extra 'docker run' flags for the YAML helper (only needed behind a proxy).
read -r -a HELPER_FLAGS <<< "${INSTALL_DOCKER_FLAGS:-}"

# --- Undo ----------------------------------------------------------------------
if [ "$UNDO" = 1 ]; then
  [ -f "$STATE" ] || die "Nothing to undo (no $STATE)."
  # shellcheck disable=SC1090
  source "$STATE"
  bold "Undo the install of $INSTALL_TS"
  "${DC[@]}" stop "$SVC_FY" >/dev/null 2>&1 || true
  "${DC[@]}" rm -f "$SVC_FY" >/dev/null 2>&1 || true
  ok "stopped $SVC_FY"
  for b in $BACKUPS; do
    orig="${b%.bak-$INSTALL_TS}"
    cp -p "$b" "$orig" && ok "restored $orig"
  done
  if [ -n "${PREV_BRANCH:-}" ] && [ "$PREV_BRANCH" != "$BRANCH" ]; then
    git -C "$REPO" checkout -q "$PREV_BRANCH" && ok "checkout back to $PREV_BRANCH"
  fi
  "${DC[@]}" up -d $SVC_OR $SVC_DW && ok "restarted $SVC_OR and $SVC_DW in the old layout"
  echo; echo "Done. The For You playlist stays in Plex until you delete it."
  exit 0
fi

echo "Compose file: $ROOT/$COMPOSE"
echo "Checkout:     $REPO"

# --- 1. Code -------------------------------------------------------------------
bold "1. Code: branch $BRANCH"
PREV_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
if [ "$SKIP_GIT" = 1 ]; then
  warn "skipped (--skip-git); on branch ${PREV_BRANCH:-?}"
else
  git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "$REPO is not a git checkout. Clone it (see docs/playlist-scripts-setup.md) or use --skip-git."
  if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
    git -C "$REPO" status --short --untracked-files=no
    die "There are uncommitted changes in $REPO (listed above). Commit or 'git stash' them, then run again."
  fi
  git -C "$REPO" fetch -q origin "$BRANCH"
  git -C "$REPO" checkout -q "$BRANCH" 2>/dev/null || git -C "$REPO" checkout -q -b "$BRANCH" "origin/$BRANCH"
  git -C "$REPO" pull -q --ff-only origin "$BRANCH"
  ok "on $BRANCH at $(git -C "$REPO" log -1 --format='%h %s')"
fi
[ -f "$REPO/scripts/for-you/script.py" ] || die "scripts/for-you is missing in $REPO."

BACKUPS=""
# --- 2. .env files (before the compose check: compose needs for-you/.env) ---
bold "2. Environment files"
SCRIPTS="$REPO/scripts"
for s in on-repeat discover-weekly; do
  f="$SCRIPTS/$s/.env"
  [ -f "$f" ] || die "$f is missing. Create it from $s/.env.example first."
  if grep -q '^STATE_FILE=/app/state.json' "$f"; then
    cp -p "$f" "$f.bak-$TS"; BACKUPS="$BACKUPS $f.bak-$TS"
    sed -i '/^STATE_FILE=\/app\/state.json/d' "$f"
    ok "$s/.env: removed STATE_FILE=/app/state.json (backup kept)"
  else
    ok "$s/.env: fine"
  fi
done
FY_ENV="$SCRIPTS/for-you/.env"
if [ -f "$FY_ENV" ]; then
  ok "for-you/.env exists, left as is"
else
  cp "$SCRIPTS/for-you/.env.example" "$FY_ENV"
  for k in TAUTULLI_URL TAUTULLI_APIKEY PLEX_URL PLEX_TOKEN MUSIC_SECTION_ID USER_ID; do
    line="$(grep -E "^$k=" "$SCRIPTS/discover-weekly/.env" | tail -n 1 || true)"
    [ -z "$line" ] && continue
    if grep -qE "^#?$k=" "$FY_ENV"; then
      python3 - "$FY_ENV" "$k" "$line" <<'PY'
import re, sys
path, key, line = sys.argv[1:4]
text = open(path).read()
text = re.sub(rf"^#?{key}=.*$", lambda m: line, text, count=1, flags=re.M)
open(path, "w").write(text)
PY
    else
      printf '%s\n' "$line" >> "$FY_ENV"
    fi
  done
  chmod --reference="$SCRIPTS/discover-weekly/.env" "$FY_ENV" 2>/dev/null || true
  ok "for-you/.env created with the Tautulli/Plex settings of discover-weekly/.env"
fi
for k in TAUTULLI_URL TAUTULLI_APIKEY PLEX_URL PLEX_TOKEN MUSIC_SECTION_ID; do
  grep -qE "^$k=." "$FY_ENV" || die "for-you/.env has no $k. Fill it in and run again."
done

# --- 3. Compose file ------------------------------------------------------------
bold "3. docker-compose: new layout"
cp -p "$COMPOSE" "$COMPOSE.bak-$TS"
ok "backup: $COMPOSE.bak-$TS"
OUT="$(docker run --rm "${HELPER_FLAGS[@]}" --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$ROOT:/work" -w /work python:3.11-slim sh -c \
        "pip install -q --user --disable-pip-version-check ruamel.yaml >/dev/null 2>&1 && \
         python '$REPO_REL/scripts/setup/compose_edit.py' '$COMPOSE' '$REPO_REL/scripts/docker-compose.example.yml' '$REPO_REL/scripts'")" \
  || { cp -p "$COMPOSE.bak-$TS" "$COMPOSE"; die "Couldn't rewrite $COMPOSE (restored the backup). Edit it by hand: docs/playlist-scripts-setup.md step 3."; }
JSON="$(printf '%s\n' "$OUT" | tail -n 1)"
field() { printf '%s' "$JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
SVC_OR="$(field 'd["services"]["on-repeat"]')"
SVC_DW="$(field 'd["services"]["discover-weekly"]')"
SVC_FY="$(field 'd["services"]["for-you"]')"
CHANGES="$(field '"\n".join(d["changes"])')"
if [ -n "$CHANGES" ]; then
  printf '%s\n' "$CHANGES" | sed 's/^/  - /'
  BACKUPS="$BACKUPS $ROOT/$COMPOSE.bak-$TS"
else
  rm -f "$COMPOSE.bak-$TS"
  ok "already in the new layout, nothing changed"
fi
DATA="$(field 'd["data"] or ""')"
[ -n "$DATA" ] && ok "For You will read clusters from $DATA/cluster_registry.json" \
               || warn "no Streamlit app data folder found in $COMPOSE: For You runs without clusters"
if ! "${DC[@]}" config -q 2>/tmp/compose-err.$$; then
  cat /tmp/compose-err.$$; rm -f /tmp/compose-err.$$
  cp -p "$COMPOSE.bak-$TS" "$COMPOSE"
  die "The new $COMPOSE didn't validate (restored the backup)."
fi
rm -f /tmp/compose-err.$$
ok "services: $SVC_OR, $SVC_DW, $SVC_FY — config valid"

# Remember what to undo, but only when this run changed something: a second
# run must not replace the record of the original files with converted ones.
if [ -n "${BACKUPS// /}" ] || [ ! -f "$STATE" ]; then
cat > "$STATE" <<EOF
INSTALL_TS="$TS"
PREV_BRANCH="$PREV_BRANCH"
BACKUPS="$BACKUPS"
SVC_OR="$SVC_OR"
SVC_DW="$SVC_DW"
SVC_FY="$SVC_FY"
EOF
fi

# --- 4. Stop the old containers -----------------------------------------------
bold "4. Stop the old script containers"
"${DC[@]}" stop "$SVC_OR" "$SVC_DW" >/dev/null 2>&1 || true
ok "stopped $SVC_OR and $SVC_DW (their playlists stay in Plex)"

# --- 5. Dry run ----------------------------------------------------------------
bold "5. For You dry run (writes nothing)"
DRY="$("${DC[@]}" run --rm -e DRY_RUN=true "$SVC_FY" 2>&1 || true)"
if ! printf '%s' "$DRY" | grep -q "DRY_RUN set"; then
  printf '%s\n' "$DRY" | grep -v -i "pip\|^$" | tail -n 25
  die "The dry run failed (output above). See Troubleshooting in docs/playlist-scripts-setup.md. Nothing was started; --undo puts the files back."
fi
printf '%s\n' "$DRY" | grep -E "^  (library|thresholds|candidates|recency|[0-9]+ recent)" || true
printf '%s\n' "$DRY" | grep -E "^ +[0-9]+\.\+" | head -n 8
echo "   …"
ok "dry run ok: $(printf '%s\n' "$DRY" | grep -cE '^ +[0-9]+\.\+') tracks"

# --- 6. Backtest (optional) ----------------------------------------------------
if [ "$BACKTEST" = 1 ]; then
  bold "6. Backtest (writes nothing)"
  "${DC[@]}" run --rm -e BACKTEST_DAYS=14 "$SVC_FY" 2>&1 | grep -v -i "pip\|^$\|Container " || true
fi

[ "$NO_START" = 1 ] && { echo; echo "Stopped before starting (--no-start)."; exit 0; }
bold "7. Start"
ask "Start On Repeat, Discover Weekly and For You now?" || { echo "Not started. Run this again, or 'docker compose up -d $SVC_OR $SVC_DW $SVC_FY'."; exit 0; }

wait_for() {  # service, success pattern
  local svc="$1" pat="$2" i log
  for i in $(seq 1 120); do
    log="$("${DC[@]}" logs --no-log-prefix "$svc" 2>&1 || true)"
    if printf '%s' "$log" | grep -qE "Run failed|Traceback|Missing required|No module named"; then
      printf '%s\n' "$log" | grep -v -i "pip\|^$" | tail -n 20
      die "$svc failed (log above)."
    fi
    printf '%s' "$log" | grep -qE "$pat" && return 0
    sleep 3
  done
  die "$svc didn't finish within 6 minutes; check: docker compose logs $svc"
}

"${DC[@]}" up -d "$SVC_OR" "$SVC_DW" >/dev/null
wait_for "$SVC_OR" "updated with"
ok "$SVC_OR: $("${DC[@]}" logs --no-log-prefix "$SVC_OR" | grep -E "updated with" | tail -n 1)"
wait_for "$SVC_DW" "Next build at"
ok "$SVC_DW: $("${DC[@]}" logs --no-log-prefix "$SVC_DW" | grep -E "updated with|Next build" | tail -n 1)"
"${DC[@]}" up -d "$SVC_FY" >/dev/null
wait_for "$SVC_FY" "Next build at"
ok "$SVC_FY: $("${DC[@]}" logs --no-log-prefix "$SVC_FY" | grep -E "updated with" | tail -n 1)"
ok "$SVC_FY: $("${DC[@]}" logs --no-log-prefix "$SVC_FY" | grep -E "Next build" | tail -n 1)"

bold "Done"
cat <<EOF
  Open Plexamp → Playlists → "For You" (you can rename or pin it).
  It swaps about a quarter of its tracks every day; settings: $REPO_REL/scripts/for-you/.env
  Logs:  docker compose logs -f $SVC_FY
  Undo:  bash $REPO_REL/scripts/install.sh --undo
EOF
