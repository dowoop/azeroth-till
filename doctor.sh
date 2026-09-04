#!/usr/bin/env bash
# doctor.sh — say which part of the till is broken, instead of making you guess.
#
#     ./doctor.sh              check everything it can without spending money
#     ./doctor.sh --verbose    also print what each check ran
#
# ## Why this exists
#
# Every failure this project has hit so far looks the same from the outside: a
# player types `#till 120` and nothing happens. There is no error, because the
# things that break do not raise. They are:
#
#   * the Lua engine is compiled in but DISABLED (`ALE.Enabled` defaults to
#     false, and the module conf that would enable it ships as `.dist` and is
#     never activated -- the server says "Not found modules config files" and
#     carries on);
#   * the engine is enabled but its SCRIPT PATH is wrong (`ALE.ScriptPath`
#     defaults to a relative `lua_scripts` and the container's working
#     directory is `/azerothcore`, so it searches a directory that does not
#     exist and says nothing);
#   * the engine found the scripts but has NO LOGGER, so a Lua syntax error is
#     invisible and a script that failed to load looks exactly like one that
#     loaded and did nothing;
#   * the script loaded but the TOKEN does not match, so every order comes back
#     401 and the player is told the till refused them;
#   * everything matches but the till is not RUNNING, or is running where the
#     container cannot reach it.
#
# Five silent failures with one symptom. That is what a doctor is for.
#
# ## What it will not do
#
# It will not open an order, spend money, or write to the ledger. The furthest
# it goes is an authenticated `GET /health` and a `--quote`, both of which are
# free and neither of which touches the chain.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERBOSE=0
[ "${1:-}" = "--verbose" ] && VERBOSE=1

ACORE="${ACORE_DIR:-$HOME/Games/World of Warcraft/Server/acore-docker}"
CONTAINER="${AZT_CONTAINER:-acore-docker-ac-worldserver-1}"
CONFIG="$HERE/bridge/till.json"
LUA_CONFIG="$HERE/server/lua_scripts/azeroth_till_config.lua"

pass=0 fail=0 warn=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; fail=$((fail+1)); }
warned(){ printf '  \033[33mwarn\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; warn=$((warn+1)); }
note() { [ "$VERBOSE" = 1 ] && printf '        \033[2m%s\033[0m\n' "$1"; return 0; }

# A fingerprint, never the secret. Eight hex characters is plenty to tell two
# tokens apart and useless to anyone who wants to use one.
fingerprint() { printf '%s' "$1" | sha256sum | cut -c1-8; }

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- the till side
section "the till service"

if [ ! -f "$CONFIG" ]; then
	bad "no bridge/till.json" "cp bridge/till.example.json bridge/till.json, or run ./install.sh"
	TOKEN="" PORT="" BIND=""
else
	ok "bridge/till.json exists"
	mode=$(stat -c '%a' "$CONFIG")
	if [ "$mode" = "600" ]; then
		ok "till.json is mode 600"
	else
		warned "till.json is mode $mode, not 600" \
			"it holds the shared token; any local user or process can read it. chmod 600 bridge/till.json"
	fi
	TOKEN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("token",""))' "$CONFIG" 2>/dev/null)
	PORT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port",""))' "$CONFIG" 2>/dev/null)
	BIND=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("bind",""))' "$CONFIG" 2>/dev/null)
	if [ -z "$TOKEN" ] || [ "$TOKEN" = "CHANGE ME" ]; then
		bad "the token is empty or still the placeholder" "run ./install.sh to generate one into both halves"
	else
		ok "token present (fingerprint $(fingerprint "$TOKEN"))"
	fi
	note "bind=$BIND port=$PORT"
	if [ "$BIND" = "0.0.0.0" ]; then
		warned "the till binds 0.0.0.0" \
			"every interface, and its routes claim, mark delivered and release orders. Bind the docker bridge address instead -- ./install.sh --bind-bridge finds it."
	else
		ok "the till binds $BIND rather than every interface"
	fi
fi

if [ -f "$HERE/bridge/till.sqlite3" ]; then
	if [ -w "$HERE/bridge/till.sqlite3" ]; then
		ok "the ledger exists and is writable"
	else
		bad "the ledger is not writable" "a settled order cannot be recorded, so money moves and nothing remembers"
	fi
	note "$(stat -c '%a %s bytes' "$HERE/bridge/till.sqlite3")"
else
	warned "no ledger yet" "it is created on first use; nothing is wrong if the till has never run"
fi

# Is it listening, and does it accept the token we think it has?
if [ -n "${PORT:-}" ]; then
	target="127.0.0.1:$PORT"
	[ "$BIND" != "0.0.0.0" ] && [ -n "$BIND" ] && target="$BIND:$PORT"
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$target/health" 2>/dev/null)
	if [ "$code" = "000" ]; then
		bad "nothing answers on http://$target" "start it: cd bridge && ../.venv/bin/python till.py --config till.json --serve"
	elif [ "$code" = "401" ]; then
		ok "the till answers and demands a token (401 without one)"
		auth=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "X-Till-Token: $TOKEN" "http://$target/health" 2>/dev/null)
		if [ "$auth" = "200" ]; then
			ok "the token in till.json is the one the till accepts"
		else
			bad "the till refused the token from its own config file (HTTP $auth)" \
				"it was started with a different till.json than the one here"
		fi
	elif [ "$code" = "200" ]; then
		bad "/health answered 200 WITHOUT a token" \
			"order counts are readable by anyone who can reach the port; this build should require the token"
	else
		warned "the till answered HTTP $code on /health" "expected 401 without a token"
	fi
fi

# ------------------------------------------------------------- the server side
section "the world server"

if [ ! -f "$ACORE/docker-compose.yml" ]; then
	bad "no AzerothCore project at $ACORE" "set ACORE_DIR to the directory holding its docker-compose.yml"
else
	ok "AzerothCore project found at $ACORE"
fi

if ! command -v docker >/dev/null 2>&1; then
	bad "docker is not on PATH" "every check below needs it"
elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
	bad "the world server container '$CONTAINER' is not running" \
		"./server/run.sh up  (or set AZT_CONTAINER if yours is named differently)"
else
	ok "world server container '$CONTAINER' is running"
	image=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)
	note "image: $image"

	# THE ENGINE, in the image rather than in the docs. Measured 2026-09-04:
	# the stock `:master` image now carries these strings too, so an image
	# override is no longer the only way to get a Lua engine -- but presence in
	# the binary is not the same as running, which is what the log check below
	# is for.
	if docker exec "$CONTAINER" sh -c 'grep -c -a "ALE.Enabled" /azerothcore/env/dist/bin/worldserver' >/dev/null 2>&1; then
		n=$(docker exec "$CONTAINER" sh -c 'grep -c -a "ALE.Enabled" /azerothcore/env/dist/bin/worldserver' 2>/dev/null)
		if [ "${n:-0}" -gt 0 ]; then
			ok "the worldserver binary has the ALE engine compiled in"
		else
			bad "no ALE engine in this image" \
				"the till is an ALE script and cannot run without it. Use an image built with modules/mod-ale."
		fi
	else
		warned "could not read the worldserver binary" "skipping the engine check"
	fi

	# THE WHOLE LOG, NOT A TAIL. The engine says "Searching scripts from" once,
	# at start-up, and this server had been up 38 hours the first time this
	# script ran -- so a tail reported the engine had "never" started when it
	# had, 38 hours earlier. A check whose answer depends on how long the
	# service has been up is a check that lies to whoever has the longest
	# uptime, which is production.
	log=$(docker logs "$CONTAINER" 2>&1)
	started=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null)
	note "container started $started; reading the whole log ($(wc -l <<<"$log") lines)"

	if grep -q "Not found modules config files" <<<"$log"; then
		warned "the server found no module config files" \
			"mod_ale.conf ships as .dist and is never activated, so ALE.Enabled falls back to its compiled default of FALSE. The compose overlay mounts a real mod_ale.conf; the AC_ALE_* environment variables are the other way."
	elif grep -qE "Using modules configuration" <<<"$log"; then
		ok "the server loaded a module configuration"
		note "$(grep -A3 'Using modules configuration' <<<"$log" | tail -3 | tr -d '\r')"
	fi

	# THE ENGINE IS CONFIGURED, read from the log rather than from the compose
	# file, because the compose file says what was INTENDED.
	for key in ALE.Enabled ALE.ScriptPath; do
		if grep -qE "Found config value '$key'" <<<"$log"; then
			ok "$key was set (from the environment)"
		else
			warned "$key was never reported as set" \
				"it falls back to its compiled default. ALE.Enabled defaults to FALSE and ALE.ScriptPath to a relative path that resolves to a directory that does not exist."
		fi
	done

	# THE SCRIPT'S OWN WORD FOR IT, and this is the check that matters.
	#
	# The first version of this test looked for the engine's "Searching scripts
	# from" line. That string is never emitted by this ALE build, so the doctor
	# reported FAILURE against a world server that had already mailed real gold
	# -- caught by running it against a system known to work, which is the only
	# way that class of mistake surfaces. A gate that fails on a working system
	# is worse than no gate, because the next person learns to ignore it.
	#
	# `azeroth_till.lua` prints `[azeroth_till] loaded; trigger is ...` at the
	# end of its own top level. Nothing else can print that: if it is in the
	# log, the engine found the file, compiled it and ran it to completion.
	if grep -qE "\\[azeroth_till\\] loaded" <<<"$log"; then
		ok "the engine ran the till script to completion (it said so itself)"
		note "$(grep -E '\[azeroth_till\] loaded' <<<"$log" | tail -1 | tr -d '\r')"
	elif grep -qiE "azeroth_till.*(error|failed|attempt to)" <<<"$log"; then
		bad "the till script was found and FAILED" \
			"$(grep -iE "azeroth_till.*(error|failed|attempt to)" <<<"$log" | tail -1)"
	elif grep -qi "azeroth_till" <<<"$log"; then
		warned "azeroth_till appears in the log but never announced itself as loaded" \
			"it may have been reloaded since, or the ALE logger may have no appender. ./server/run.sh ale"
	else
		bad "the engine never ran the till script" \
			"nothing in this container's log mentions azeroth_till. The script is not on ALE.ScriptPath -- which defaults to a RELATIVE 'lua_scripts' while the working directory is /azerothcore, so it must be set absolutely."
	fi

	# Has it ever actually delivered? Not a failure if not -- a new install has
	# not -- but it is the single most useful fact in the log.
	delivered=$(grep -cE "\\[azeroth_till\\] mailed" <<<"$log")
	if [ "${delivered:-0}" -gt 0 ]; then
		ok "this server has mailed gold $delivered time(s)"
		note "$(grep -E '\[azeroth_till\] mailed' <<<"$log" | tail -1 | tr -d '\r')"
	else
		note "no deliveries in this container's log yet"
	fi
fi

# ------------------------------------------------------- do the halves agree?
section "the two halves of the token"

if [ ! -f "$LUA_CONFIG" ]; then
	bad "no server/lua_scripts/azeroth_till_config.lua" \
		"cp azeroth_till_config.lua.example to it, or run ./install.sh which writes both halves from one generated secret"
else
	mode=$(stat -c '%a' "$LUA_CONFIG")
	[ "$mode" = "600" ] && ok "the lua config is mode 600" || \
		warned "the lua config is mode $mode, not 600" "it carries the same token"
	LUA_TOKEN=$(sed -n 's/.*token[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$LUA_CONFIG" | head -1)
	if [ -z "$LUA_TOKEN" ]; then
		bad "no token found in the lua config"
	elif [ "$LUA_TOKEN" = "PUT THE TOKEN FROM bridge/till.json HERE" ]; then
		bad "the lua config still has the placeholder token" "every order will come back 401"
	elif [ -n "${TOKEN:-}" ] && [ "$LUA_TOKEN" = "$TOKEN" ]; then
		ok "both halves carry the same token (fingerprint $(fingerprint "$TOKEN"))"
	else
		bad "the two halves carry DIFFERENT tokens" \
			"till.json $(fingerprint "${TOKEN:-}") vs lua $(fingerprint "$LUA_TOKEN") -- every order comes back 401 and the player is told the till refused them"
	fi
fi

# ------------------------------------------------------------- free end to end
section "pricing, without opening an order"

if [ -x "$HERE/.venv/bin/python" ] && [ -f "$CONFIG" ]; then
	if quote=$("$HERE/.venv/bin/python" "$HERE/bridge/till.py" --config "$CONFIG" --quote 120 2>&1 | head -3); then
		ok "a 120 gold quote priced without touching the chain"
		note "$(head -1 <<<"$quote")"
	else
		bad "--quote failed" "$(head -2 <<<"$quote")"
	fi
else
	warned "no .venv/bin/python" "skipping the pricing check"
fi

printf '\n\033[1mverdict\033[0m\n'
printf '  %d ok, %d warning(s), %d failure(s)\n' "$pass" "$warn" "$fail"
if [ "$fail" -gt 0 ]; then
	printf '  the till will not deliver gold until the failures above are fixed.\n'
	exit 1
fi
if [ "$warn" -gt 0 ]; then
	printf '  it should work. The warnings are things that will bite later.\n'
	exit 0
fi
printf '  everything this can check from here is right.\n'
