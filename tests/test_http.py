import io
import json
from types import SimpleNamespace
import unittest

import till

from tests.support import ScriptedMemoryRail, config, order_record


class QuietHandler(till.Handler):
    def log_message(self, fmt, *args):
        pass


class InProcessConnection:
    """The stream interface BaseHTTPRequestHandler expects, without a socket."""

    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, mode, buffering=None):
        if mode == "rb":
            return self.request
        raise AssertionError(f"unexpected makefile mode {mode!r}")

    def sendall(self, data):
        self.response.extend(data)

    def shutdown(self, how):
        pass

    def close(self):
        pass


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = till.Ledger(":memory:")
        self.addCleanup(self.ledger._db.close)
        self.rail = ScriptedMemoryRail()
        self.till = till.Till(config(), self.rail, self.ledger)
        QuietHandler.till = self.till
        QuietHandler.token = "test-token"

    def request(self, method, path, body=None, token=None):
        encoded = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = ["Host: 127.0.0.1", "Connection: close"]
        if body is not None:
            headers.extend(("Content-Type: application/json", f"Content-Length: {len(encoded)}"))
        if token is not None:
            headers.append(f"X-Till-Token: {token}")
        raw_request = (
            f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
        ).encode("ascii") + encoded
        connection = InProcessConnection(raw_request)
        QuietHandler(connection, ("127.0.0.1", 12345), SimpleNamespace())
        head, raw_body = bytes(connection.response).split(b"\r\n\r\n", 1)
        status = int(head.splitlines()[0].split()[1])
        return status, json.loads(raw_body)

    def open_settled_order(self, ref="delivery-sale"):
        self.ledger.open_order(order_record(ref))
        self.assertTrue(self.ledger.settle(ref, ("tx-delivery",), 10_015, 120))
        return ref

    def test_every_route_requires_the_token(self):
        routes = (
            ("GET", "/health", None),
            ("GET", "/stuck", None),
            ("GET", "/order?ref=anything", None),
            ("POST", "/order", {}),
            ("POST", "/claim", {}),
            ("POST", "/delivered", {}),
            ("POST", "/release", {}),
        )

        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                status, response = self.request(method, path, body)
                self.assertEqual(401, status)
                self.assertIn("x-till-token", response["error"].lower())

    def test_claimed_but_unacknowledged_delivery_is_not_reissued_and_is_stuck(self):
        ref = self.open_settled_order()

        first_status, first = self.request("POST", "/claim", {}, "test-token")
        second_status, second = self.request("POST", "/claim", {}, "test-token")
        stuck_status, stuck = self.request("GET", "/stuck?seconds=0", token="test-token")

        self.assertEqual(200, first_status)
        self.assertEqual([ref], [item["ref"] for item in first["deliveries"]])
        self.assertEqual(200, second_status)
        self.assertEqual([], second["deliveries"])
        self.assertEqual(till.DELIVERING, self.ledger.order(ref)["state"])
        self.assertEqual(200, stuck_status)
        self.assertEqual([ref], [item["ref"] for item in stuck["stuck"]])

    def test_delivery_is_closed_only_after_acknowledgement(self):
        ref = self.open_settled_order()
        self.request("POST", "/claim", {}, "test-token")

        status, response = self.request(
            "POST", "/delivered", {"ref": ref}, "test-token"
        )

        self.assertEqual(200, status)
        self.assertEqual({"ok": True, "ref": ref}, response)
        self.assertEqual(till.DELIVERED, self.ledger.order(ref)["state"])
        _, stuck = self.request("GET", "/stuck?seconds=0", token="test-token")
        self.assertEqual([], stuck["stuck"])

    def test_order_route_rejects_fractional_gold_instead_of_truncating_it(self):
        status, response = self.request(
            "POST",
            "/order",
            {"account_id": 7, "char_guid": 11, "char_name": "Buyer", "gold": 1.5},
            "test-token",
        )

        self.assertEqual(400, status)
        self.assertIn("whole number", response["error"])
        self.assertEqual({}, self.ledger.counts())


if __name__ == "__main__":
    unittest.main()
