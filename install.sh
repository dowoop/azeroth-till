#!/usr/bin/env bash
# install.sh — set up both halves of the till from one generated secret.
#
#     ./install.sh                  set it up; refuses to overwrite a token
#     ./install.sh --rotate         replace the token in BOTH halves at once
#     ./install.sh --addon <dir>    also copy the client addon there
#     ./install.sh --show           what it would do, and change nothing
#
# ## The one thing this exists to delete
#
# The token has to be identical in `bridge/till.json` and in
# `server/lua_scripts/azeroth_till_config.lua`. Copying it by hand is the single
# most common way this install fails, and the failure is not obvious: the world
# server reaches the till, the till answers 401, and the player is told the till
# refused them. The README used to say "put the same token in it, or every order
# comes back 401" -- which is accurate, and is documentation standing in for a
# thing the machine should do.
#
# So: ONE secret, generated once, written to both places, mode 600, never
# printed. `--rotate` changes both together, because changing one is the same
# failure by a different route.
#
# ## What it does NOT decide
#
# The recipient account and the payment component are yours -- they name where
# money goes and which contract binds it to an order. This script will not
# invent them, and `doctor.sh` will tell you if they are missing.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROTATE=0 SHOW=0 ADDON=""
while [ $# -gt 0 ]; do
	case "$1" in
		--rotate) ROTATE=1 ;;
		--show)   SHOW=1 ;;
		--addon)  ADDON="${2:?--addon needs a directory}"; shift ;;
		-h|--help) sed -n '2,9p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
	shift
done

CONFIG="$HERE/bridge/till.json"
EXAMPLE="$HERE/bridge/till.example.json"
LUA="$HERE/server/lua_scripts/azeroth_till_config.lua"
LUA_EXAMPLE="$HERE/server/lua_scripts/azeroth_till_config.lua.example"

say()  { printf '%s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
did()  { printf '  \033[32m%s\033[0m %s\n' "done" "$1"; }
skip() { printf '  \033[2m%s\033[0m %s\n' "kept" "$1"; }

# THE BIND ADDRESS IS THE DOCKER BRIDGE, NOT LOOPBACK, and this is the one place
# the obvious hardening is wrong. The world server runs in a container and
# reaches the host through `host.docker.internal` mapped to `host-gateway`,
# which on Linux is the host's address ON THE DOCKER BRIDGE -- not 127.0.0.1. A
# till bound to loopback is invisible to the container, so "bind localhost" would
# harden it into not working. The bridge address is reachable by containers and
# not by the network the machine is on, which is the property actually wanted.
bridge_address() {
	local addr
	addr=$(docker network inspect bridge \
		--format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null | head -1)
	[ -n "$addr" ] && { printf '%s' "$addr"; return 0; }
	addr=$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
	[ -n "$addr" ] && { printf '%s' "$addr"; return 0; }
	return 1
}

step "1. the shared secret"

existing=""
if [ -f "$CONFIG" ]; then
	existing=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("token","") or "")
except Exception: print("")' "$CONFIG")
fi

if [ -n "$existing" ] && [ "$ROTATE" = 0 ]; then
	skip "a token is already set. --rotate replaces it in both halves at once."
	TOKEN="$existing"
else
	# `secrets.token_urlsafe(32)` is 256 bits of urandom. Not a uuid: a uuid4 is
	# 122 bits and is meant to be unique rather than unguessable, and this value
	# authorises `/claim`, `/delivered` and `/release`.
	TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
	if [ "$SHOW" = 1 ]; then
		say "  would generate a new 256-bit token and write it to both halves"
	else
		did "generated a new 256-bit token (fingerprint $(printf '%s' "$TOKEN" | sha256sum | cut -c1-8))"
	fi
fi

step "2. bridge/till.json"

if [ "$SHOW" = 1 ]; then
	say "  would write the token, and bind $(bridge_address || echo '0.0.0.0 -- no docker bridge found')"
else
	[ -f "$CONFIG" ] || { cp "$EXAMPLE" "$CONFIG"; did "created from till.example.json"; }
	BIND=$(bridge_address || echo "")
	python3 - "$CONFIG" "$TOKEN" "$BIND" <<'PY'
import json, sys
path, token, bind = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as handle:
    config = json.load(handle)
config["token"] = token
if bind:
    config["bind"] = bind
with open(path, "w") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
PY
	chmod 600 "$CONFIG"
	did "token written, mode 600"
	[ -n "$BIND" ] && did "bind set to $BIND (the docker bridge: containers can reach it, the LAN cannot)" \
	              || say "  \033[33mwarn\033[0m no docker bridge address found; bind left as it was"
fi

step "3. server/lua_scripts/azeroth_till_config.lua"

if [ "$SHOW" = 1 ]; then
	say "  would write the SAME token into the world server's half"
else
	[ -f "$LUA" ] || { cp "$LUA_EXAMPLE" "$LUA"; did "created from the example"; }
	# `python3` rather than `sed -i`, because the token is base64url and can
	# contain `/` and `-`, which turn a sed expression into a different sed
	# expression. A secret that breaks the tool writing it is a bad afternoon.
	python3 - "$LUA" "$TOKEN" <<'PY'
import re, sys
path, token = sys.argv[1], sys.argv[2]
text = open(path).read()
new, count = re.subn(r'(token\s*=\s*")[^"]*(")', lambda m: m.group(1) + token + m.group(2), text, count=1)
if count != 1:
    sys.exit(f"could not find a single `token = \"...\"` line in {path}")
open(path, "w").write(new)
PY
	chmod 600 "$LUA"
	did "same token written, mode 600"
fi

step "4. the client addon"

if [ -n "$ADDON" ]; then
	if [ "$SHOW" = 1 ]; then
		say "  would copy addon/AzerothTill to $ADDON"
	elif [ -d "$ADDON" ]; then
		cp -r "$HERE/addon/AzerothTill" "$ADDON/"
		did "copied to $ADDON/AzerothTill"
	else
		say "  \033[31mno such directory\033[0m: $ADDON"
	fi
else
	say "  not requested. It is optional -- without it a player gets the payment"
	say "  instruction as text and no QR code. To install it:"
	say "    ./install.sh --addon '<wow>/Interface/AddOns'"
fi

step "what is left, and only you can do it"

python3 - "$CONFIG" <<'PY'
import json, sys
try:
    config = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
missing = [k for k in ("recipient", "payment_component") if not config.get(k)
           or str(config.get(k)).startswith(("PUT", "CHANGE", "your"))]
if missing:
    print("  set these in bridge/till.json -- they name where money goes and")
    print("  which contract binds a payment to an order:")
    for key in missing:
        print(f"    {key}")
else:
    print("  recipient and payment_component are set.")
PY

cat <<'NEXT'

  then:
    ./server/run.sh up                                   the world server
    cd bridge && ../.venv/bin/python till.py --config till.json --serve
    ./doctor.sh                                          check all of it

  `doctor.sh` is the one to run when a player says nothing happened. It knows
  the five silent failures this install has, and which one you have.
NEXT
