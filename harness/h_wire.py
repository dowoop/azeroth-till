"""
h_wire.py — does the ADDON draw the symbol the till encoded?

## Why this gate exists

`till_qr.py` encodes a QR and `AzerothTill.lua` draws one. Those are two
implementations of one format in two languages, and only the Python half has a
test suite. The Lua half runs on a player's machine where nothing checks it,
and its failure mode is not a crash: it is a symbol that renders, looks like a
QR, and decodes to something else. Nobody would notice by looking.

So this runs the addon's own code -- unmodified, loaded from the file the
player installs -- against a stubbed client, reads back the rectangles it drew,
and compares them module for module with `qrcodegen`'s matrix.

## What it covers that a round-trip in Python would not

`till_qr.read_wire` already proves the Python side can undo its own packing.
That check cannot fail while both halves are wrong in the same way, because it
is one implementation talking to itself. This one crosses the language
boundary, and it covers three separate things the Lua does that the Python
never does:

  * base64 decoded with arithmetic, because 3.3.5a's Lua has no bit library
  * the bit index into a row-major bitmap, MSB first
  * horizontal RUN coalescing, which is what turns 2,025 modules into ~250
    rectangles and is the step most able to be subtly wrong

## Every check has been seen red

`H_WIRE_MUTATION` reinstalls a specific defect in the wire before the addon
sees it, and each one must turn this run FAIL:

    bitflip     one module inverted -- the smallest wrong picture there is
    truncate    the last chunk withheld, which must draw NOTHING
    reorder     two chunks swapped, which base64 will happily decode
    endless     the end marker withheld, which must draw NOTHING

A gate that has only ever been seen green is a gate of unknown direction.
"""

import base64
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "bridge"))

import till_qr                                                  # noqa: E402

ADDON = ROOT / "addon" / "AzerothTill" / "AzerothTill.lua"
CHECKER = HERE / "wire_check.lua"

# The Lua the client runs is 5.1. Nothing on this workstation has an
# interpreter, so the gate brings its own and pins the image: a check that
# silently ran on a different language version would be checking something the
# player never runs.
LUA_IMAGE = "alpine:3.20"
LUA_SETUP = "apk add --no-cache lua5.1 >/dev/null 2>&1 && lua5.1"

PAYLOADS = [
    ("esmeralda", "component_" + "a2208e00baa392cd1a6d6ef8336e083fac01499ec19dacde0f245114f0f37aab",
     1_000_000, "AZT-1-4-2-9f3c"),
    ("esmeralda", "component_" + "0" * 64, 1, "A"),
    ("esmeralda", "component_" + "f" * 64, 4_294_967_295, "AZT-999999-888888-123456-deadbeef"),
]


def mutate(messages, mode):
    """Reinstall a known defect in the wire. Returns the messages the addon
    will actually be given."""
    if not mode:
        return messages
    data = [m for m in messages if m.startswith("D|")]
    if mode == "truncate":
        return [m for m in messages if m is not data[-1]]
    if mode == "endless":
        return [m for m in messages if not m.startswith("E|")]
    if mode == "reorder":
        if len(data) < 2:
            raise SystemExit("reorder needs at least two chunks; widen the payloads")
        first, second = messages.index(data[0]), messages.index(data[1])
        swapped = list(messages)
        swapped[first], swapped[second] = swapped[second], swapped[first]
        # The INDEX travels in the message, so a swap of position alone would
        # be undone by the reader. The payloads are exchanged instead, which is
        # the corruption a lossy transport actually produces.
        left = swapped[first].split("|", 2)
        right = swapped[second].split("|", 2)
        swapped[first] = f"D|{left[1]}|{right[2]}"
        swapped[second] = f"D|{right[1]}|{left[2]}"
        return swapped
    if mode == "bitflip":
        index = messages.index(data[0])
        _, number, encoded = messages[index].split("|", 2)
        raw = bytearray(base64.b64decode(encoded + "=" * (-len(encoded) % 4)))
        raw[0] ^= 0x01
        flipped = base64.b64encode(bytes(raw)).decode("ascii").rstrip("=")
        return messages[:index] + [f"D|{number}|{flipped}"] + messages[index + 1:]
    raise SystemExit(f"unknown mutation {mode!r}")


def drawn_rows(messages, workdir):
    """What the addon draws, as rows of '0'/'1', or None if it drew nothing."""
    wire = workdir / "wire.txt"
    wire.write_text("\n".join(messages) + "\n", encoding="utf-8")
    # ONE MOUNT, AND IT IS THE REPOSITORY. The fixture is written inside the
    # tree rather than into /tmp because Docker here shares only paths it has
    # been told about, and a gate that depends on the daemon's file-sharing
    # settings is a gate that fails for reasons having nothing to do with the
    # code. Read-only: this container has no business writing anything.
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT}:{ROOT}:ro",
         "-w", str(ROOT), LUA_IMAGE, "sh", "-c",
         f"{LUA_SETUP} {CHECKER} {ADDON} {wire}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout.strip().splitlines(), ""


def expected_rows(size, rows):
    return ["".join("1" if module else "0" for module in row) for row in rows]


def main():
    mutation = os.environ.get("H_WIRE_MUTATION", "")
    workdir = HERE / ".work"
    workdir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for network, component, amount, ref in PAYLOADS:
        text = till_qr.payload(network, component, amount, ref)
        size, rows = till_qr.matrix(text)
        messages = till_qr.wire(ref, size, rows)
        want = expected_rows(size, rows)

        got, why = drawn_rows(mutate(messages, mutation), workdir)
        label = f"{size}x{size} {ref}"
        if got is None:
            print(f"FAIL {label}: the addon drew nothing ({why})")
            failures += 1
            continue
        if got != want:
            differing = sum(1 for a, b in zip(got, want) if a != b)
            print(f"FAIL {label}: {differing} of {len(want)} rows differ")
            failures += 1
            continue
        print(f"ok   {label}: {len(want)} rows identical, {len(messages)} messages")

    if mutation:
        # Under a mutation the run must FAIL. A mutation that passes is the
        # gate reporting that it cannot see the defect it names.
        if failures:
            print(f"\nmutation {mutation!r} was caught -- this gate can fail")
            return 0
        print(f"\nMUTATION {mutation!r} WENT UNNOTICED: this gate does not check what it claims")
        return 1
    print(f"\n{'FAIL' if failures else 'PASS'}: {failures} of {len(PAYLOADS)} payloads differ")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
