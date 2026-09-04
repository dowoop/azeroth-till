"""
till.py — the gold till. One payment component, one player per sale.

## What this is

An HTTP service the AzerothCore world server talks to. It turns "this player
wants 500 gold" into an Ootle payment request bound to that player, watches the
chain for a payment that NAMES that request, and hands the world server a
delivery to make. It holds no key, signs nothing and spends nothing: the player
pays, this watches.

    worldserver (Eluna)                    till.py                  Ootle
    ───────────────────                    ───────                  ─────
    .till 500          ── POST /order ──>  capture_baseline
                                           create_request
                       <── QR + notice ──  (order open)
    draws the symbol
                                           observe ────────────────> indexer
                                           settle                    events
    POST /claim        <── settled ─────   (claim tx, atomically)
    SendMail(gold)                         (order -> delivering)
                       ── POST /delivered ─> (order closed)

## The four things this gets right on purpose

**1. The baseline is captured before the player sees anything.** `create_request`
is called after `capture_baseline` and never before, because a baseline taken
late lets a payment that predates the order be credited to it. That is
obligation 3 of `cryptopos-core`'s five, and the ordering here is the whole of
what honours it.

**2. The claim is scoped to the SALE, not to the recipient.** Core's obligation
2 says to key the claimed set on `(rail, recipient, transaction_id)` and
justifies it with "two sales never share an address". Every order here shares
one address — the payment component — so that key would be wrong in a way that
costs a player their gold: one transaction can carry two `pay` calls naming two
different sales, and a claim on the bare `(rail, recipient, id)` would settle
the first and leave the second, whose player really paid, looking unpaid.

The binding on this rail is the reference, so the key is
`(rail, recipient, sale_ref, transaction_id)`. That is still exclusive where it
must be: `_referenced_payments` in the adapter only counts events whose
`sale_ref` matches, so an order can never be credited from a payment that named
a different one, whatever the amount and whichever order polls first.

**3. Delivery is claimed, then acknowledged.** The world server *claims* a
settled order — which moves it to `delivering` in the same lock that hands it
out — mails the gold, and posts back that it did. An order that settles while
the server is down is still waiting when it comes back up. Neither one-phase
ordering is safe: mailing before marking pays a player twice from one payment
when an acknowledgement is lost, and marking before mailing loses the gold of a
player who really paid. So the middle state exists, `/stuck` lists anything
that sat in it, and a person decides. See `Ledger.claim_deliveries`.

**4. The price is PICKED and says so.** No exchange lists an XTR pair —
`rails.py` records the search and its result — so `micro_xtr_per_gold` is the
operator's number and nothing here pretends it is a market rate. It is integer
arithmetic end to end: microTari and copper are both whole units, and a float
anywhere in this file would be a defect.

## What this deliberately does not do

It does not credit gold. It cannot: it has no connection to the character
database and inventing one would put a second writer on money the world server
owns. It hands the world server a delivery and the world server does it, which
also means a delivery can be audited in-game as mail from a sender with a name.

It also does not decide what an order is worth in dollars. There is no feed to
ask, and a till that invented one would be pricing real money off a number
somebody typed.
"""

import argparse
import http.server
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse

import till_qr

from cryptopos_core.plugin import PaymentIntent, RecipientBaseline
from cryptopos_core.registry import RailRegistry

RAIL_KEY = "ootle:esmeralda/native:xtr"

# A gold piece is 100 silver is 10,000 copper. The client's money field is a
# uint32 of copper, so 429,496 gold is the ceiling the PROTOCOL imposes; the
# operator's own ceiling lives in the configuration and is lower.
COPPER_PER_GOLD = 10_000
PROTOCOL_MAX_COPPER = 0xFFFFFFFF

DEFAULTS = {
    "endpoint": "https://ootle-indexer-a.tari.com",
    "timeout_seconds": 10,
    "network": "esmeralda",
    "bind": "0.0.0.0",
    "port": 8099,
    "token": "",
    "recipient": "",
    "payment_component": "",
    "micro_xtr_per_gold": 2_000,
    "max_gold_per_order": 5_000,
    "min_gold_per_order": 1,
    "window_seconds": 900,
    "poll_seconds": 20,
    "database": "till.sqlite3",
    "mail_sender_guid": 0,
    "mail_subject": "Azeroth Till",
}


class ConfigError(ValueError):
    """A configuration this service will not start on.

    Every check below runs at start-up rather than at first order, because a
    till that accepts an order it cannot price has already shown a player a
    number.
    """


def load_config(path):
    config = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            config.update(json.load(handle))
    for key, value in os.environ.items():
        if not key.startswith("TILL_"):
            continue
        name = key[len("TILL_"):].lower()
        if name not in DEFAULTS:
            raise ConfigError(f"{key} names no setting this build has")
        config[name] = type(DEFAULTS[name])(value) if isinstance(DEFAULTS[name], int) else value
    if not config["token"]:
        raise ConfigError(
            "a token is required. This service takes orders that become payment"
            " requests, so an unauthenticated caller could open orders in another"
            " player's name. Set `token` in the config file or TILL_TOKEN."
        )
    if not config["recipient"]:
        raise ConfigError("`recipient` must be the merchant's Ootle account address")
    if not config["payment_component"]:
        raise ConfigError(
            "`payment_component` is required. Without it this rail runs in its"
            " shared-account mode, where two orders open at once cannot be told"
            " apart -- which is exactly what a till full of players is."
        )
    if config["micro_xtr_per_gold"] <= 0:
        raise ConfigError("`micro_xtr_per_gold` must be a positive whole number")
    if config["min_gold_per_order"] < 1:
        raise ConfigError("`min_gold_per_order` must be at least 1")
    if config["max_gold_per_order"] < config["min_gold_per_order"]:
        raise ConfigError("`max_gold_per_order` is below `min_gold_per_order`")
    if config["max_gold_per_order"] * COPPER_PER_GOLD > PROTOCOL_MAX_COPPER:
        raise ConfigError(
            f"`max_gold_per_order` of {config['max_gold_per_order']} exceeds what the"
            f" client's uint32 copper field can hold ({PROTOCOL_MAX_COPPER // COPPER_PER_GOLD} gold)"
        )
    return config


def dump_baseline(baseline):
    """The baseline as text, with every field it carries.

    All of them, not the two a human would want to read. The baseline is an
    input to `observe` and to `settle`, and a partial one reconstructs into a
    DIFFERENT baseline that the rail then refuses -- correctly, because a sale
    judged against a position it did not start from is a sale that can credit
    money it never should have seen.
    """
    return json.dumps({
        "rail_key": baseline.rail_key,
        "recipient": baseline.recipient,
        "provider": baseline.provider,
        "tip": baseline.tip,
        "transaction_ids": list(baseline.transaction_ids),
        "balance_native": baseline.balance_native,
        "payment_component": baseline.payment_component,
    })


def load_baseline(text):
    """The inverse. `transaction_ids` becomes a tuple again because the
    dataclass refuses a list, and JSON has no tuples."""
    data = json.loads(text)
    return RecipientBaseline(
        rail_key=data["rail_key"],
        recipient=data["recipient"],
        provider=data["provider"],
        tip=data["tip"],
        transaction_ids=tuple(data["transaction_ids"]),
        balance_native=data["balance_native"],
        payment_component=data["payment_component"],
    )


def chain_config(config):
    """What the rail adapter is handed. Only the keys it declares."""
    return {
        "endpoint": config["endpoint"],
        "timeout_seconds": config["timeout_seconds"],
        "payment_component": config["payment_component"],
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    ref             TEXT PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    char_guid       INTEGER NOT NULL,
    char_name       TEXT NOT NULL,
    gold            INTEGER NOT NULL,
    copper          INTEGER NOT NULL,
    amount_native   INTEGER NOT NULL,
    rail_key        TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    component       TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    state           TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    credited_native INTEGER NOT NULL DEFAULT 0,
    baseline        TEXT NOT NULL,
    settled_at      INTEGER,
    delivering_at   INTEGER,
    delivered_at    INTEGER
);

-- THE EXCLUSIVE CLAIM. The primary key is the guard, not the code around it:
-- two workers that both decide to credit one payment race to this INSERT and
-- exactly one of them wins. `sale_ref` is in the key because every order on
-- this till shares one recipient -- see the module docstring.
CREATE TABLE IF NOT EXISTS claims (
    rail_key       TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    sale_ref       TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    claimed_at     INTEGER NOT NULL,
    PRIMARY KEY (rail_key, recipient, sale_ref, transaction_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_open ON orders (state, expires_at);
"""

OPEN = "open"
SETTLED = "settled"
DELIVERING = "delivering"
DELIVERED = "delivered"
EXPIRED = "expired"
REVIEW = "needs-review"


class Ledger:
    """Every order this till has ever opened, and every payment it has claimed.

    One connection guarded by one lock rather than a connection per thread:
    the write volume of a game till is a handful of rows a minute, and a single
    serialized writer removes the whole class of bug this file cannot afford.
    """

    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self):
        """Columns added after a ledger already existed.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that is already
        there, so a schema change is invisible until the first query that
        needs the new column -- and here that query is the one that hands out
        gold. A ledger of settled payments is not a file to delete and
        recreate, so the column is added instead.
        """
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(orders)")}
        if "delivering_at" not in have:
            self._db.execute("ALTER TABLE orders ADD COLUMN delivering_at INTEGER")

    def next_sequence(self, name):
        with self._lock:
            row = self._db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
            value = (row["value"] if row else 0) + 1
            self._db.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )
            self._db.commit()
            return value

    def open_order(self, order):
        with self._lock:
            self._db.execute(
                "INSERT INTO orders (ref, account_id, char_guid, char_name, gold, copper,"
                " amount_native, rail_key, recipient, component, created_at, expires_at,"
                " state, baseline) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order["ref"], order["account_id"], order["char_guid"], order["char_name"],
                    order["gold"], order["copper"], order["amount_native"], order["rail_key"],
                    order["recipient"], order["component"], order["created_at"],
                    order["expires_at"], OPEN, order["baseline"],
                ),
            )
            self._db.commit()

    def order(self, ref):
        with self._lock:
            row = self._db.execute("SELECT * FROM orders WHERE ref = ?", (ref,)).fetchone()
            return dict(row) if row else None

    def open_orders(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM orders WHERE state = ?", (OPEN,)).fetchall()
            return [dict(row) for row in rows]

    def claimed(self, rail_key, recipient, sale_ref):
        with self._lock:
            rows = self._db.execute(
                "SELECT transaction_id FROM claims WHERE rail_key = ? AND recipient = ?"
                " AND sale_ref = ?",
                (rail_key, recipient, sale_ref),
            ).fetchall()
            return frozenset(row["transaction_id"] for row in rows)

    def settle(self, ref, transaction_ids, credited_native, now):
        """Claim the payments and mark the order settled, or neither.

        The INSERT and the UPDATE are one transaction on purpose. A settled
        order whose claim did not land could be settled again from the same
        payment; a claim without a settled order strands the money. Returns
        False when another worker got there first, which is a normal outcome
        and not an error.
        """
        with self._lock:
            try:
                with self._db:
                    for transaction_id in transaction_ids:
                        self._db.execute(
                            "INSERT INTO claims (rail_key, recipient, sale_ref, transaction_id,"
                            " claimed_at) SELECT rail_key, recipient, ref, ?, ? FROM orders"
                            " WHERE ref = ?",
                            (transaction_id, now, ref),
                        )
                    self._db.execute(
                        "UPDATE orders SET state = ?, credited_native = ?, settled_at = ?"
                        " WHERE ref = ? AND state = ?",
                        (SETTLED, credited_native, now, ref, OPEN),
                    )
            except sqlite3.IntegrityError:
                return False
            return True

    def close(self, ref, state, reason=""):
        with self._lock:
            self._db.execute(
                "UPDATE orders SET state = ?, reason = ? WHERE ref = ? AND state = ?",
                (state, reason, ref, OPEN),
            )
            self._db.commit()

    def claim_deliveries(self, limit, now):
        """Hand out settled orders AND mark them as being delivered, at once.

        TWO PHASES, BECAUSE NEITHER ONE-PHASE ORDER IS SAFE. The world server
        must mail gold and then say it did, and the mail and the saying cannot
        be one transaction across two processes:

          * mail first, then mark  -- a lost acknowledgement re-delivers, and
            the player is paid twice from one payment. Silent and unbounded.
          * mark first, then mail  -- a crash in between loses the gold of a
            player who really paid. Silent, and it looks delivered.

        So the middle state is real and has a name. An order handed out is
        `delivering`; it comes back `delivered` when the mail is sent. One that
        stays `delivering` is neither paid twice nor quietly lost -- it is
        stuck, `/stuck` lists it, and a person decides. That is the same shape
        as `needs-review`: an outcome, not an error.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT ref, char_guid, char_name, gold, copper, credited_native"
                " FROM orders WHERE state = ? ORDER BY settled_at LIMIT ?",
                (SETTLED, limit),
            ).fetchall()
            claimed = [dict(row) for row in rows]
            for row in claimed:
                self._db.execute(
                    "UPDATE orders SET state = ?, delivering_at = ? WHERE ref = ? AND state = ?",
                    (DELIVERING, now, row["ref"], SETTLED),
                )
            self._db.commit()
            return claimed

    def mark_delivered(self, ref, now):
        with self._lock:
            cursor = self._db.execute(
                "UPDATE orders SET state = ?, delivered_at = ? WHERE ref = ? AND state = ?",
                (DELIVERED, now, ref, DELIVERING),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def stuck(self, older_than):
        """Orders handed out for delivery that never came back."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ref, char_guid, char_name, gold, copper, delivering_at"
                " FROM orders WHERE state = ? AND delivering_at <= ? ORDER BY delivering_at",
                (DELIVERING, older_than),
            ).fetchall()
            return [dict(row) for row in rows]

    def release(self, ref):
        """Put a stuck order back in the queue. An operator's call, never the
        till's: only a person can know whether the mail actually arrived."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE orders SET state = ?, delivering_at = NULL WHERE ref = ? AND state = ?",
                (SETTLED, ref, DELIVERING),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def counts(self):
        with self._lock:
            rows = self._db.execute("SELECT state, COUNT(*) AS n FROM orders GROUP BY state").fetchall()
            return {row["state"]: row["n"] for row in rows}


class Till:
    """The five calls, the ledger, and the arithmetic between them."""

    def __init__(self, config, rail, ledger):
        self.config = config
        self.rail = rail
        self.ledger = ledger
        self._batches = {}
        self._batch_lock = threading.Lock()

    # -- money ------------------------------------------------------------
    #
    # Two conversions, both exact, both integer. `micro_xtr_per_gold` is the
    # operator's picked rate and the only place a price enters this file.

    def price(self, gold):
        return gold * self.config["micro_xtr_per_gold"]

    def copper(self, gold):
        return gold * COPPER_PER_GOLD

    # -- the sale ---------------------------------------------------------

    def create(self, account_id, char_guid, char_name, gold, now=None):
        low = self.config["min_gold_per_order"]
        high = self.config["max_gold_per_order"]
        if not isinstance(gold, int) or isinstance(gold, bool):
            raise ValueError("gold must be a whole number")
        if not low <= gold <= high:
            raise ValueError(f"this till sells between {low} and {high} gold in one order")
        now = int(time.time()) if now is None else now
        sequence = self.ledger.next_sequence("order")

        # THE SUFFIX IS NOT DECORATION, and it was added after a reference was
        # reused here for real. The counter is unique only for as long as the
        # ledger that mints it survives: this database was deleted during
        # development, the counter restarted at 1, and an order was issued the
        # reference of an order that had already been PAID. Every open order
        # on this till shares one payment component, so a reference is the
        # only thing telling two players' money apart -- obligation 1's
        # "never reissue an address", one level up.
        #
        # Nothing was mis-credited, because the baseline had moved past the
        # old payment and `observe` starts at the baseline: measured, not
        # assumed. But that defence is the cursor's, not the reference's, and
        # it would not hold for a payment that arrived late enough to land
        # after the new order's baseline. Four random characters cost one QR
        # version and remove the whole case, whatever happens to this file.
        suffix = secrets.token_hex(2)
        ref = f"AZT-{account_id}-{char_guid}-{sequence}-{suffix}"

        # OBLIGATION 3, and the reason the two calls are adjacent and in this
        # order. Everything the payer will see is derived from `request`, and
        # `request` cannot be built until the baseline pins where the chain
        # stood before the player knew the order existed.
        baseline = self.rail.capture_baseline(self.config["recipient"], chain_config(self.config))
        amount_native = self.price(gold)
        intent = PaymentIntent(
            intent_id=ref,
            rail_key=self.rail.key,
            recipient=self.config["recipient"],
            amount_native=amount_native,
            created_at_epoch=now,
            expires_at_epoch=now + self.config["window_seconds"],
            payment_reference=ref,
            baseline=baseline,
        )
        request = self.rail.create_request(intent)

        text = till_qr.payload(
            self.config["network"], self.config["payment_component"], amount_native, ref
        )
        size, rows = till_qr.matrix(text)

        self.ledger.open_order({
            "ref": ref,
            "account_id": account_id,
            "char_guid": char_guid,
            "char_name": char_name,
            "gold": gold,
            "copper": self.copper(gold),
            "amount_native": amount_native,
            "rail_key": self.rail.key,
            "recipient": self.config["recipient"],
            "component": self.config["payment_component"],
            "created_at": now,
            "expires_at": intent.expires_at_epoch,
            "baseline": dump_baseline(baseline),
        })
        return {
            "ref": ref,
            "gold": gold,
            "copper": self.copper(gold),
            "amount_native": amount_native,
            "network": self.config["network"],
            "component": self.config["payment_component"],
            "uri": request.uri,
            "payer_notice": request.payer_notice,
            "payload": text,
            "expires_at": intent.expires_at_epoch,
            "window_seconds": self.config["window_seconds"],
            "qr": {"size": size, "wire": till_qr.wire(ref, size, rows)},
            "chat": self.chat_lines(ref, gold, amount_native, request),
        }

    def chat_lines(self, ref, gold, amount_native, request):
        """The system message, for a player with no addon.

        The rail's own `payer_notice` travels verbatim rather than being
        summarised. It is the sentence that changes when the binding changes,
        and a till that paraphrased it would keep saying "naming the sale"
        after somebody removed the component from the configuration.
        """
        return [
            f"Order {ref}: {gold}g for {amount_native:,} microTari on Ootle {self.config['network']}.",
            request.payer_notice,
            f"Component: {self.config['payment_component']}",
            f"Pay within {self.config['window_seconds'] // 60} minutes. Gold arrives by mail.",
        ]

    def _intent_for(self, order):
        """The intent this order was opened with, rebuilt exactly.

        THE BASELINE IS READ BACK, NEVER RE-CAPTURED. Capturing a fresh one
        here would be wrong twice over, and the first version of this file did
        it and was caught by the rail: a baseline pins the chain position the
        sale starts from, so re-taking it each poll walks the starting cursor
        forward and a payment made before this poll is a payment the order can
        no longer see. The rail refuses the mismatch outright --
        "observations belong to another payment intent or baseline" -- which
        is the check working, and it is not the reason this is wrong.
        """
        return PaymentIntent(
            intent_id=order["ref"],
            rail_key=order["rail_key"],
            recipient=order["recipient"],
            amount_native=order["amount_native"],
            created_at_epoch=order["created_at"],
            expires_at_epoch=order["expires_at"],
            payment_reference=order["ref"],
            baseline=load_baseline(order["baseline"]),
        )

    def poll(self, now=None):
        """One pass over every open order. Returns what changed, for the log.

        A failure on one order is recorded against that order and does not
        stop the pass: an indexer that refuses one read must not stall every
        other player's payment.
        """
        now = int(time.time()) if now is None else now
        changes = []
        for order in self.ledger.open_orders():
            try:
                changes.extend(self._poll_one(order, now))
            except Exception as failure:                       # noqa: BLE001
                changes.append(("error", order["ref"], f"{type(failure).__name__}: {failure}"))
        return changes

    def _poll_one(self, order, now):
        ref = order["ref"]
        intent = self._intent_for(order)

        # OBLIGATION 4. `observe` returns what one provider call could read,
        # and deciding on a partial read is deciding on a partial payment.
        with self._batch_lock:
            previous = self._batches.get(ref)
        batch = self.rail.observe(intent, chain_config(self.config), previous)
        while not batch.complete:
            batch = self.rail.observe(intent, chain_config(self.config), batch)
        with self._batch_lock:
            self._batches[ref] = batch

        claimed = self.ledger.claimed(order["rail_key"], order["recipient"], ref)
        decision = self.rail.settle(intent, batch, claimed_transaction_ids=claimed)

        if decision.state == "settled":
            if self.ledger.settle(ref, decision.transaction_ids, decision.credited_native, now):
                with self._batch_lock:
                    self._batches.pop(ref, None)
                return [("settled", ref, f"{decision.credited_native} uT"
                                         f" tx {','.join(decision.transaction_ids)}")]
            return [("raced", ref, "another worker settled this order first")]

        if decision.state == "needs-review":
            # OBLIGATION 5. This is a real outcome with a real place to go: the
            # order stops being open, and it does NOT become a delivery.
            self.ledger.close(ref, REVIEW, decision.reason)
            with self._batch_lock:
                self._batches.pop(ref, None)
            return [("needs-review", ref, decision.reason)]

        if now > order["expires_at"]:
            # Expiry closes the ORDER, not the money. A payment that arrives
            # after this still exists on the chain, still names this sale, and
            # is still readable from the event stream -- which is why the
            # reason says so rather than saying the money is gone.
            self.ledger.close(ref, EXPIRED, "the payment window closed before a payment named this order")
            with self._batch_lock:
                self._batches.pop(ref, None)
            return [("expired", ref, "window closed")]
        return []


# ---------------------------------------------------------------------------
# THE HTTP FACE
#
# Small on purpose. Everything above is callable without it, which is what
# `h_till.py` drives -- a harness that had to speak HTTP to check the
# arithmetic would be checking the socket.
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AzerothTill/1.0"
    till = None
    token = None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorised(self):
        supplied = self.headers.get("X-Till-Token", "")
        # Constant-time-ish: compare full strings, never short-circuit on the
        # first differing byte in a way a caller can time. `hmac.compare_digest`
        # is the right primitive and is used rather than `==`.
        import hmac
        return hmac.compare_digest(supplied, self.token)

    def _send(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # -- the line protocol ------------------------------------------------
    #
    # WHY THIS EXISTS AND JSON DOES NOT SUFFICE. The other caller here is a
    # Lua 5.2 script inside the world server, and ALE ships no JSON decoder.
    # The choice was to write one in Lua or to speak a format Lua already
    # parses, and a hand-rolled decoder on the game side would be a parser
    # nothing gates, running in-process with the world, on text from a socket.
    # One `string.match` per line is not a parser.
    #
    # JSON is still what `curl` and a person get. The two renderings come from
    # the same dictionaries directly below each other so they cannot describe
    # different orders.

    def _wants_text(self):
        return "text/plain" in self.headers.get("Accept", "")

    def _send_text(self, code, lines):
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _order_lines(order):
        lines = [
            "OK",
            f"REF {order['ref']}",
            f"GOLD {order['gold']}",
            f"COPPER {order['copper']}",
            f"AMOUNT {order['amount_native']}",
            f"SIZE {order['qr']['size']}",
        ]
        lines.extend(f"CHAT {line}" for line in order["chat"])
        lines.extend(f"WIRE {message}" for message in order["qr"]["wire"])
        lines.append("END")
        return lines

    @staticmethod
    def _delivery_lines(deliveries):
        # The NAME is on the line as well as the guid, and both are used. Mail
        # is addressed by low guid, which is what survives a rename and is the
        # only correct address for it. Finding the player to speak to needs a
        # name, because `GetPlayerByGUID` takes an ObjectGuid and a low guid is
        # not one -- passing the number would look right and find nobody.
        lines = [f"DELIVER {d['ref']} {d['char_guid']} {d['copper']} {d['gold']} {d['char_name']}"
                 for d in deliveries]
        lines.append("END")
        return lines

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, {"ok": True, "orders": self.till.ledger.counts()})
        if not self._authorised():
            if self._wants_text():
                return self._send_text(401, ["ERR bad or missing X-Till-Token"])
            return self._send(401, {"error": "bad or missing X-Till-Token"})
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/stuck":
            seconds = int(query.get("seconds", ["300"])[0])
            return self._send(200, {"stuck": self.till.ledger.stuck(int(time.time()) - seconds)})
        if parsed.path == "/order":
            ref = query.get("ref", [""])[0]
            order = self.till.ledger.order(ref)
            if order is None:
                return self._send(404, {"error": "no such order"})
            return self._send(200, order)
        return self._send(404, {"error": "no such endpoint"})

    def _refuse(self, code, message):
        if self._wants_text():
            return self._send_text(code, [f"ERR {message}"])
        return self._send(code, {"error": message})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorised():
            return self._refuse(401, "bad or missing X-Till-Token")
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._refuse(400, "the body is not JSON")
        if parsed.path == "/order":
            try:
                order = self.till.create(
                    int(body["account_id"]), int(body["char_guid"]),
                    str(body["char_name"]), int(body["gold"]),
                )
            except (KeyError, TypeError) as missing:
                return self._refuse(400, f"the order is missing {missing}")
            except ValueError as refused:
                return self._refuse(400, str(refused))
            except Exception as failure:                       # noqa: BLE001
                # A provider that will not answer is a 502 and says which one.
                # The player sees this sentence, so it names what failed rather
                # than saying the order was refused, which would be a lie about
                # whose fault it is.
                return self._refuse(502, f"{type(failure).__name__}: {failure}")
            if self._wants_text():
                return self._send_text(200, self._order_lines(order))
            return self._send(200, order)
        if parsed.path == "/claim":
            limit = int(body.get("limit", 10))
            claimed = self.till.ledger.claim_deliveries(limit, int(time.time()))
            if self._wants_text():
                return self._send_text(200, self._delivery_lines(claimed))
            return self._send(200, {"deliveries": claimed})
        if parsed.path == "/delivered":
            ref = str(body.get("ref", ""))
            if self.till.ledger.mark_delivered(ref, int(time.time())):
                if self._wants_text():
                    return self._send_text(200, [f"OK {ref}"])
                return self._send(200, {"ok": True, "ref": ref})
            return self._refuse(409, "that order was not being delivered")
        if parsed.path == "/release":
            ref = str(body.get("ref", ""))
            if self.till.ledger.release(ref):
                return self._send(200, {"ok": True, "ref": ref})
            return self._refuse(409, "that order is not stuck in delivery")
        return self._refuse(404, "no such endpoint")


def watcher(till, stop):
    while not stop.is_set():
        for kind, ref, detail in till.poll():
            print(f"[watch] {kind} {ref} {detail}", flush=True)
        stop.wait(till.config["poll_seconds"])


def build(config):
    registry = RailRegistry()
    registry.discover()
    if RAIL_KEY not in registry.keys():
        raise ConfigError(
            f"{RAIL_KEY} is not installed. This till resolves its rail through the"
            " `cryptopos.rails` entry point, so `pip install cryptopos-rail-ootle`"
            " is the integration -- an importable source tree is not."
        )
    rail = registry.get(RAIL_KEY)

    # THE VERDICT IS THREE-VALUED AND `unchecked` IS NOT A SOFT `ok`. Ootle
    # account identifiers carry no local checksum, so `unchecked` is the best
    # any offline check can return for this rail and treating it as a failure
    # would make the till unstartable on the only rail it has. It is printed
    # rather than swallowed: core refuses `unchecked` on MAINNET, and the day
    # this points at one, that refusal is the thing that should be heard.
    verdict, why = rail.validate_recipient(config["recipient"])
    if verdict == "refused":
        raise ConfigError(f"the merchant account is not payable: {why}")
    if verdict == "unchecked":
        print(f"[till] recipient unchecked: {why}", flush=True)
    return Till(config, rail, Ledger(config["database"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="the Azeroth gold till")
    parser.add_argument("--config", default="till.json")
    parser.add_argument("--quote", type=int, metavar="GOLD",
                        help="price one order and print its symbol, without opening it")
    parser.add_argument("--serve", action="store_true", help="run the service")
    arguments = parser.parse_args(argv)

    config = load_config(arguments.config)

    if arguments.quote is not None:
        gold = arguments.quote
        amount = gold * config["micro_xtr_per_gold"]
        text = till_qr.payload(config["network"], config["payment_component"], amount, "AZT-QUOTE")
        size, rows = till_qr.matrix(text)
        print(f"{gold} gold = {amount:,} microTari at {config['micro_xtr_per_gold']:,} uT/g")
        print(f"payload {len(text)} bytes, {size}x{size} modules, "
              f"{len(till_qr.wire('AZT-QUOTE', size, rows))} addon messages")
        print(text)
        print(till_qr.ascii_art(size, rows))
        return 0

    till = build(config)
    Handler.till = till
    Handler.token = config["token"]
    stop = threading.Event()
    thread = threading.Thread(target=watcher, args=(till, stop), daemon=True)
    thread.start()
    server = http.server.ThreadingHTTPServer((config["bind"], config["port"]), Handler)
    print(f"[till] {config['bind']}:{config['port']} rail {till.rail.key}", flush=True)
    print(f"[till] component {config['payment_component']}", flush=True)
    print(f"[till] {config['micro_xtr_per_gold']:,} microTari per gold (a picked rate: no feed lists XTR)",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
