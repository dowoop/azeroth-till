import concurrent.futures
import threading
import unittest

import till

from tests.support import order_record


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = till.Ledger(":memory:")
        self.addCleanup(self.ledger._db.close)

    def open(self, ref):
        self.ledger.open_order(order_record(ref))

    def test_concurrent_settlements_of_one_order_claim_exactly_once(self):
        self.open("sale-race")
        barrier = threading.Barrier(2)

        def settle(transaction_id):
            barrier.wait()
            return self.ledger.settle("sale-race", (transaction_id,), 10_015, 120)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(settle, ("tx-left", "tx-right")))

        self.assertCountEqual(results, (True, False))
        claims = self.ledger.claimed(MemoryRailKey, Recipient, "sale-race")
        self.assertEqual(1, len(claims))
        self.assertEqual(till.SETTLED, self.ledger.order("sale-race")["state"])

    def test_losing_claim_insert_rolls_back_sale_state_and_prior_inserts(self):
        self.open("sale-rollback")

        won = self.ledger.settle(
            "sale-rollback", ("tx-duplicate", "tx-duplicate"), 10_015, 120
        )

        self.assertFalse(won)
        self.assertEqual(till.OPEN, self.ledger.order("sale-rollback")["state"])
        self.assertEqual(
            frozenset(),
            self.ledger.claimed(MemoryRailKey, Recipient, "sale-rollback"),
        )

    def test_claim_key_allows_one_transaction_for_two_sales_but_not_one_sale_twice(self):
        self.open("sale-one")
        self.open("sale-two")

        self.assertTrue(self.ledger.settle("sale-one", ("tx-shared",), 10_015, 120))
        self.assertTrue(self.ledger.settle("sale-two", ("tx-shared",), 10_015, 121))
        self.assertFalse(self.ledger.settle("sale-one", ("tx-shared",), 10_015, 122))

        self.assertEqual(
            frozenset({"tx-shared"}),
            self.ledger.claimed(MemoryRailKey, Recipient, "sale-one"),
        )
        self.assertEqual(
            frozenset({"tx-shared"}),
            self.ledger.claimed(MemoryRailKey, Recipient, "sale-two"),
        )


MemoryRailKey = "memory:testnet/native:tok"
Recipient = "mem1merchant"


if __name__ == "__main__":
    unittest.main()
