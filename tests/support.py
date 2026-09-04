"""Fixtures that keep every till test off the live ledger and real rail."""

import json
from pathlib import Path

from cryptopos_core.plugin import RecipientBaseline
from cryptopos_core.testing import MemoryRail

import till


COMPONENT = "component_" + "0" * 64
RECIPIENT = "mem1merchant"


def config(**overrides):
    values = dict(till.DEFAULTS)
    values.update({
        "endpoint": "memory://scripted-chain",
        "token": "test-token",
        "recipient": RECIPIENT,
        "payment_component": COMPONENT,
        "network": "testnet",
        "micro_xtr_per_gold": 2_003,
        "min_gold_per_order": 1,
        "max_gold_per_order": 50,
        "window_seconds": 10,
    })
    values.update(overrides)
    return values


class ScriptedMemoryRail(MemoryRail):
    """MemoryRail with its scripted provider state supplied by the fixture.

    The production till only forwards configuration keys declared by the
    Ootle adapter. MemoryRail deliberately declares a different test-only
    shape (`tip`, `page`, and `transfers`), so this adapter supplies that shape
    while leaving all observation and settlement behavior in MemoryRail.
    """

    def __init__(self, *, tip=0, page=1_000, transfers=()):
        self.chain = {
            "endpoint": "memory://scripted-chain",
            "tip": tip,
            "page": page,
            "transfers": list(transfers),
        }
        self.calls = []

    def capture_baseline(self, recipient, configuration):
        self.calls.append("capture_baseline")
        return super().capture_baseline(recipient, self.chain)

    def create_request(self, intent):
        self.calls.append("create_request")
        return super().create_request(intent)

    def observe(self, intent, configuration, previous=None):
        self.calls.append("observe")
        return super().observe(intent, self.chain, previous)


def order_record(ref, *, account_id=1, char_guid=10, char_name="Buyer", gold=5,
                 amount_native=10_015, created_at=100, expires_at=110):
    baseline = RecipientBaseline(
        rail_key=MemoryRail.key,
        recipient=RECIPIENT,
        provider="memory",
        tip=0,
    )
    return {
        "ref": ref,
        "account_id": account_id,
        "char_guid": char_guid,
        "char_name": char_name,
        "gold": gold,
        "copper": gold * till.COPPER_PER_GOLD,
        "amount_native": amount_native,
        "rail_key": MemoryRail.key,
        "recipient": RECIPIENT,
        "component": COMPONENT,
        "created_at": created_at,
        "expires_at": expires_at,
        "baseline": till.dump_baseline(baseline),
    }


def write_config(path, **overrides):
    values = config(**overrides)
    Path(path).write_text(json.dumps(values), encoding="utf-8")
    return path
