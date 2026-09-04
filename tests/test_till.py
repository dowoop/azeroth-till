import os
import tempfile
import unittest
from unittest import mock

import till

from tests.support import ScriptedMemoryRail, config, write_config


class TillTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = till.Ledger(":memory:")
        self.addCleanup(self.ledger._db.close)
        self.rail = ScriptedMemoryRail()
        self.till = till.Till(config(), self.rail, self.ledger)

    def test_create_captures_baseline_before_building_payment_request(self):
        self.till.create(7, 11, "Buyer", 2, now=100)

        self.assertEqual(["capture_baseline", "create_request"], self.rail.calls[:2])

    def test_price_and_copper_remain_exact_integers(self):
        order = self.till.create(7, 11, "Buyer", 3, now=100)
        stored = self.ledger.order(order["ref"])

        self.assertEqual(6_009, self.till.price(3))
        self.assertEqual(30_000, self.till.copper(3))
        for value in (
            self.till.price(3), self.till.copper(3), order["amount_native"],
            order["copper"], stored["amount_native"], stored["copper"],
        ):
            self.assertIs(type(value), int)

    def test_create_refuses_float_and_bool_gold_before_opening_an_order(self):
        for bad_gold in (1.5, True):
            with self.subTest(gold=bad_gold):
                with self.assertRaisesRegex(ValueError, "whole number"):
                    self.till.create(7, 11, "Buyer", bad_gold, now=100)

        self.assertEqual({}, self.ledger.counts())
        self.assertEqual([], self.rail.calls)

    def test_maximum_gold_is_accepted_and_one_above_is_refused(self):
        limited = till.Till(config(max_gold_per_order=2), self.rail, self.ledger)

        with self.assertRaisesRegex(ValueError, "between 1 and 2"):
            limited.create(7, 11, "Buyer", 3, now=100)
        accepted = limited.create(7, 11, "Buyer", 2, now=100)

        self.assertEqual(2, accepted["gold"])
        self.assertEqual({till.OPEN: 1}, self.ledger.counts())

    def test_fractional_price_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(os.path.join(directory, "test-config.json"),
                                micro_xtr_per_gold=2_000.5)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(till.ConfigError, "positive whole number"):
                    till.load_config(path)

    def test_complete_timely_payment_settles_and_claims_transaction(self):
        order = self.till.create(7, 11, "Buyer", 2, now=100)
        self.rail.chain.update({
            "tip": 3,
            "page": 1,
            "transfers": [{
                "id": "tx-timely", "to": "mem1merchant", "amount": 4_006,
                "confs": 2, "height": 2, "at": 109,
            }],
        })

        changes = self.till.poll(now=110)

        self.assertEqual("settled", changes[0][0])
        self.assertEqual(till.SETTLED, self.ledger.order(order["ref"])["state"])
        self.assertEqual(
            frozenset({"tx-timely"}),
            self.ledger.claimed(self.rail.key, "mem1merchant", order["ref"]),
        )
        self.assertGreaterEqual(self.rail.calls.count("observe"), 3)

    def test_late_payment_never_settles_and_remains_visible_for_review(self):
        order = self.till.create(7, 11, "Buyer", 2, now=100)
        self.rail.chain.update({
            "tip": 1,
            "transfers": [{
                "id": "tx-late", "to": "mem1merchant", "amount": 4_006,
                "confs": 1, "height": 1, "at": 111,
            }],
        })

        changes = self.till.poll(now=112)
        stored = self.ledger.order(order["ref"])

        self.assertEqual("needs-review", changes[0][0])
        self.assertIn("after the payment window", changes[0][2])
        self.assertEqual(till.REVIEW, stored["state"])
        self.assertIn("after the payment window", stored["reason"])
        self.assertEqual(0, stored["credited_native"])
        self.assertEqual(
            frozenset(),
            self.ledger.claimed(self.rail.key, "mem1merchant", order["ref"]),
        )

    def test_unpaid_order_expires_after_its_window(self):
        order = self.till.create(7, 11, "Buyer", 2, now=100)

        changes = self.till.poll(now=111)

        self.assertEqual([("expired", order["ref"], "window closed")], changes)
        self.assertEqual(till.EXPIRED, self.ledger.order(order["ref"])["state"])


if __name__ == "__main__":
    unittest.main()
