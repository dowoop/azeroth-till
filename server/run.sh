#!/usr/bin/env bash
# Drive the AzerothCore stack with the till's world server in it.
#
#   ./run.sh up        bring it up   (the base project is never edited)
#   ./run.sh down      stop it
#   ./run.sh logs      follow the world server
#   ./run.sh console   attach to the world server console (Ctrl-P Ctrl-Q to leave)
#   ./run.sh ale       show what the Lua engine said at start-up
#
# THE STACK IS SOMEBODY ELSE'S. This composes three files: the base project's
# own two, plus `docker-compose.azeroth-till.yml`. Nothing in the base project
# is modified, so `docker compose up` there still works exactly as before and
# starts the stock server with no till in it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where the AzerothCore project lives. Overridable, because this path is the
# one thing here that is true only on this workstation.
ACORE="${ACORE_DIR:-~/Games/World of Warcraft/Server/acore-docker}"

if [ ! -f "$ACORE/docker-compose.yml" ]; then
  echo "no AzerothCore project at: $ACORE" >&2
  echo "set ACORE_DIR to the directory holding its docker-compose.yml" >&2
  exit 2
fi

# Absolute, because compose resolves relative paths against the FIRST compose
# file's directory and that is the base project's, not this one's.
export AZEROTH_TILL_LUA="$HERE/lua_scripts"
export AZEROTH_TILL_CONF="$HERE/conf"

if [ ! -f "$AZEROTH_TILL_LUA/azeroth_till_config.lua" ]; then
  echo "missing: $AZEROTH_TILL_LUA/azeroth_till_config.lua" >&2
  echo "copy azeroth_till_config.lua.example to it and put the till's token in." >&2
  echo "The token is in bridge/till.json -- the world server and the till must" >&2
  echo "agree on it or every order comes back 401." >&2
  exit 2
fi

compose() {
  docker compose \
    -f "$ACORE/docker-compose.yml" \
    -f "$ACORE/docker-compose.override.yml" \
    -f "$HERE/docker-compose.azeroth-till.yml" \
    --project-directory "$ACORE" \
    "$@"
}

case "${1:-}" in
  # NAMED SERVICES, NOT `up` ON EVERYTHING. The base project also defines
  # phpmyadmin on port 8080, which on this workstation is the ERPNext frontend
  # -- `up` with no arguments dies on the port clash and leaves the stack half
  # started. Compose brings the database, the import and the client data up
  # anyway because these two depend on them.
  up)      compose up -d ac-worldserver ac-authserver ;;
  down)    compose down ;;
  restart) compose restart ac-worldserver ;;
  ps)      compose ps ;;
  logs)    compose logs -f ac-worldserver ;;
  console) docker attach acore-docker-ac-worldserver-1 ;;
  ale)     docker logs acore-docker-ac-worldserver-1 2>&1 | grep -iE "ALE|lua|azeroth_till" | tail -30 ;;
  *)       sed -n '2,10p' "$0" ; exit 2 ;;
esac
