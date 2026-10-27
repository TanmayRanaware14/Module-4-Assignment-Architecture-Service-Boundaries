#!/usr/bin/env python3
"""Inventory service. Owns item stock and answers availability queries."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("INVENTORY_HOST", "127.0.0.1")
PORT = int(os.environ.get("INVENTORY_PORT", "8002"))

# Stock is owned by this process. Order Service never writes these numbers.
ITEMS = {
    "laptop": {"name": "Laptop", "quantity_on_hand": 5},
    "keyboard": {"name": "Keyboard", "quantity_on_hand": 12},
    "headphones": {"name": "Headphones", "quantity_on_hand": 0},
}


class InventoryHandler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]

            if parsed.path == "/health":
                self._send(200, {"status": "ok", "service": "inventory"})
                return

            if len(parts) == 3 and parts[0] == "items" and parts[2] == "availability":
                self._availability(parts[1], parsed.query)
                return

            self._send(404, {"error": "not_found"})
        except Exception as exc:
            print(f"[inventory] request failed: {exc}")
            self._send(500, {"error": "internal_error"})

    def _availability(self, item_id, query):
        item = ITEMS.get(item_id)
        if item is None:
            self._send(404, {"error": "item_not_found", "item_id": item_id})
            return

        try:
            quantity = int(parse_qs(query).get("quantity", ["1"])[0])
        except ValueError:
            self._send(400, {"error": "invalid_quantity"})
            return

        if quantity < 1:
            self._send(400, {"error": "invalid_quantity"})
            return

        self._send(
            200,
            {
                "item_id": item_id,
                "name": item["name"],
                "quantity_requested": quantity,
                "quantity_on_hand": item["quantity_on_hand"],
                "available": item["quantity_on_hand"] >= quantity,
            },
        )

    def log_message(self, fmt, *args):
        print(f"[inventory] {self.address_string()} {fmt % args}")


def main():
    server = ThreadingHTTPServer((HOST, PORT), InventoryHandler)
    print(f"Inventory service listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
