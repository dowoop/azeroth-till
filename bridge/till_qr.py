"""
till_qr.py — the payment request, small enough to draw in a 3.3.5a chat frame.

## What this is for

There is a related format that solves a DIFFERENT problem: carrying an
unsigned TRANSACTION to a co-signer and back. This module carries a payment
REQUEST — component, amount, reference — to somebody who has not composed
anything yet. Three fields, no cryptography, no signature, no key.

The two must not be confused, so this format does not borrow `POSQ`'s prefix.
A reader handed one where it expected the other refuses by name instead of
decoding a request into a transaction.

## The payload

    OTLPAY1:<network>:<component>:<microTari>:<sale-ref>

Self-describing and versioned at the front, for the reason `qr_wire` records:
`OTLPAY1` is one character from `OTLPAY2`, so a future change to the frame is a
refusal on old readers rather than a misinterpretation.

**It is not a registered URI scheme and this module does not pretend it is.**
Ootle has no payer deeplink — `rails.py` searched for one and recorded its
absence, and `cryptopos-rail-ootle` says so in the payer notice it returns.
What a camera gets off this symbol is readable text naming the component, the
amount and the sale; what turns that into a payment is a wallet holding a key,
and no wallet on a phone speaks Ootle today. The QR is a transport for the
three facts, and the honest claim stops there.

## Why the bitmap travels rather than the text

The addon draws the symbol; it does not encode it. A QR encoder in Lua would be
a second implementation of the thing this repository already has working, and
the two would drift — the client's copy silently, because nothing on a player's
machine runs a gate. So the server encodes with `qrcodegen` (Nayuki's, vendored
beside this file) and sends the MODULES. The addon's whole job is painting
black rectangles, which is a job it cannot get subtly wrong.

## The wire, and why it is chunked

An addon message in 3.3.5a carries at most 255 bytes including its prefix, so
anything larger arrives as several messages or not at all. The frame is:

    H|<ref>|<size>|<chunks>      one header, then
    D|<i>|<base64>               `chunks` data messages, 0-indexed, then
    E|<ref>                      one end marker

The end marker is not decoration. A client that renders on the last chunk it
happens to receive would draw a half-decoded symbol, and a half-decoded QR
still scans — into a DIFFERENT payload, or into nothing, with no way for the
player to tell which. Rendering is gated on `E` and a complete chunk set.

Bits are row-major, MSB first, `1` = dark, padded to a byte boundary at the end
of the whole matrix rather than per row: a row boundary that costs padding
bytes would buy nothing, since the decoder knows `size` and can index.
"""

import base64

import qrcodegen

# The frame's own name and generation. Bumping GENERATION is how an
# incompatible frame announces itself.
MAGIC = "OTLPAY"
GENERATION = "1"
PREFIX = MAGIC + GENERATION

# The addon message prefix. Sixteen characters is the client's limit; this is
# well inside it and names the project rather than the format, because a second
# format would still belong to the same addon.
ADDON_PREFIX = "AZTILL"

# What one addon message may carry, payload only. The client's ceiling is 255
# bytes for prefix + separator + message; 180 leaves room for the header fields
# in front of the base64 and for the prefix the client prepends.
CHUNK_LIMIT = 180

# Error correction. MEDIUM recovers 15% and is what a symbol read off a lit
# monitor at an angle actually needs; QUARTILE would cost a version for a
# medium this project cannot get dirty.
ECC = qrcodegen.QrCode.Ecc.MEDIUM

# Above this the symbol stops being drawable in a chat-sized frame. A version
# 10 symbol is 57x57 modules; at the 5 physical pixels a module needs to
# survive a phone camera that is 285 pixels plus a quiet zone, which is already
# a quarter of a 1080p client's height. `receipt.COMFORTABLE_VERSION` makes the
# same kind of judgement for a printed strip and lands elsewhere, because the
# medium is different.
COMFORTABLE_VERSION = 10


class QrTooBig(ValueError):
    """The request does not fit in a symbol this screen can show.

    Raised rather than returned, and raised BEFORE a player is shown anything,
    because the failure mode it prevents is a symbol that renders, does not
    scan, and looks like the player's camera is at fault.
    """


def payload(network, component, amount_native, sale_ref):
    """The text the symbol carries.

    Every field is checked here rather than at the far end. A malformed payload
    that reaches a QR is a payload somebody has to photograph to discover is
    wrong.
    """
    if not network or ":" in network:
        raise ValueError("the network must be named and cannot contain ':'")
    if not component.startswith("component_"):
        raise ValueError("the component address must be a component_ address")
    if ":" in component:
        raise ValueError("a component address cannot contain ':'")
    if not isinstance(amount_native, int) or isinstance(amount_native, bool):
        raise ValueError("the amount must be a whole number of microTari")
    if amount_native <= 0:
        raise ValueError("a payment of nothing is not a payment")
    if not sale_ref:
        raise ValueError("a payment request must name the sale it is for")
    if ":" in sale_ref:
        # The separator is the frame. A reference containing one would split
        # into fields the far end reassembles wrongly, and the wrong sale is
        # the one failure this whole component exists to prevent.
        raise ValueError("a sale reference cannot contain ':'")
    return f"{PREFIX}:{network}:{component}:{amount_native}:{sale_ref}"


def parse(text):
    """The inverse, for anything that reads a symbol back.

    Refuses by name in every case rather than returning a half-filled
    dictionary: there is no partially-valid payment request.
    """
    if not isinstance(text, str):
        raise ValueError("a payload is text")
    head, _, rest = text.partition(":")
    if head != PREFIX:
        if head.startswith(MAGIC):
            raise ValueError(f"{head} is a generation this build does not read")
        raise ValueError("this is not an OTLPAY payload")
    fields = rest.split(":")
    if len(fields) != 4:
        raise ValueError("an OTLPAY1 payload has four fields after the prefix")
    network, component, amount, sale_ref = fields
    if not component.startswith("component_"):
        raise ValueError("the third field is not a component address")
    if not amount.isdigit():
        raise ValueError("the amount is not a whole number")
    if not sale_ref:
        raise ValueError("the payload names no sale")
    return {
        "network": network,
        "component": component,
        "amount_native": int(amount),
        "sale_ref": sale_ref,
    }


def matrix(text):
    """Encode, and refuse a symbol too big to draw.

    Returns `(size, rows)` where `rows[y][x]` is True for a dark module.
    """
    code = qrcodegen.QrCode.encode_text(text, ECC)
    if code.get_version() > COMFORTABLE_VERSION:
        raise QrTooBig(
            f"this request needs a version {code.get_version()} symbol"
            f" ({code.get_size()}x{code.get_size()} modules) and"
            f" {COMFORTABLE_VERSION} is the largest this screen can show"
        )
    size = code.get_size()
    rows = [[code.get_module(x, y) for x in range(size)] for y in range(size)]
    return size, rows


def pack(size, rows):
    """The matrix as bytes, row-major, MSB first, 1 = dark."""
    bits = bytearray()
    accumulator = 0
    held = 0
    for row in rows:
        for module in row:
            accumulator = (accumulator << 1) | (1 if module else 0)
            held += 1
            if held == 8:
                bits.append(accumulator)
                accumulator = 0
                held = 0
    if held:
        bits.append(accumulator << (8 - held))
    expected = (size * size + 7) // 8
    if len(bits) != expected:
        # Not reachable from `matrix`, which always returns a square. It is
        # checked anyway because the far end indexes on `size` alone and a
        # short buffer would draw a symbol that is wrong rather than absent.
        raise ValueError(f"packed {len(bits)} bytes for a {size}x{size} matrix, expected {expected}")
    return bytes(bits)


def unpack(size, packed):
    """The inverse of `pack`, so the wire can be checked without a client."""
    expected = (size * size + 7) // 8
    if len(packed) != expected:
        raise ValueError(f"a {size}x{size} matrix needs {expected} bytes, got {len(packed)}")
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            index = y * size + x
            row.append(bool(packed[index // 8] & (0x80 >> (index % 8))))
        rows.append(row)
    return rows


def wire(sale_ref, size, rows, limit=CHUNK_LIMIT):
    """The addon messages, in order, for one symbol.

    The header names the chunk count, so a client that loses one message knows
    it is short rather than drawing what arrived.
    """
    if "|" in sale_ref:
        # `|` is the field separator AND the client's own escape character in
        # chat. A reference carrying one would be reassembled wrongly at best.
        raise ValueError("a sale reference cannot contain '|'")
    encoded = base64.b64encode(pack(size, rows)).decode("ascii")
    chunks = [encoded[at:at + limit] for at in range(0, len(encoded), limit)]
    messages = [f"H|{sale_ref}|{size}|{len(chunks)}"]
    messages.extend(f"D|{index}|{chunk}" for index, chunk in enumerate(chunks))
    messages.append(f"E|{sale_ref}")
    over = [message for message in messages if len(message) > 250]
    if over:
        raise ValueError("an addon message exceeded what the client will carry")
    return messages


def read_wire(messages):
    """Reassemble `wire`'s output. The gate for the Lua the client runs.

    This exists so the format has one executable definition on the side that
    can be tested. The addon implements the same steps in Lua and
    `h_till_qr.py` compares the two against the same fixtures.
    """
    header = None
    chunks = {}
    ended = None
    for message in messages:
        kind, _, rest = message.partition("|")
        if kind == "H":
            ref, size, count = rest.split("|")
            header = (ref, int(size), int(count))
        elif kind == "D":
            index, _, data = rest.partition("|")
            chunks[int(index)] = data
        elif kind == "E":
            ended = rest
        else:
            raise ValueError(f"unknown wire message kind {kind!r}")
    if header is None:
        raise ValueError("no header")
    ref, size, count = header
    if ended is None:
        raise ValueError("the symbol never ended; nothing may be drawn")
    if ended != ref:
        raise ValueError("the end marker names a different sale than the header")
    if sorted(chunks) != list(range(count)):
        raise ValueError(f"expected {count} chunks, got {sorted(chunks)}")
    encoded = "".join(chunks[index] for index in range(count))
    return ref, size, unpack(size, base64.b64decode(encoded))


def ascii_art(size, rows, quiet=2):
    """The symbol as text, for a terminal and for a log.

    Two rows per character cell with the half-block glyphs, so a 41x41 symbol
    is 21 lines instead of 41 and stays square-ish in a normal font.
    """
    lines = []
    width = size + quiet * 2
    blank = [False] * width
    padded = [blank[:] for _ in range(quiet)]
    padded.extend([False] * quiet + row + [False] * quiet for row in rows)
    padded.extend(blank[:] for _ in range(quiet))
    if len(padded) % 2:
        padded.append(blank[:])
    for top, bottom in zip(padded[0::2], padded[1::2]):
        line = []
        for upper, lower in zip(top, bottom):
            # Dark modules print as the FOREGROUND, so this reads correctly on
            # a light terminal. On a dark one it scans inverted, which most
            # phone readers handle; `--invert` in the self-test flips it.
            line.append("█" if upper and lower else "▀" if upper else "▄" if lower else " ")
        lines.append("".join(line))
    return "\n".join(lines)
