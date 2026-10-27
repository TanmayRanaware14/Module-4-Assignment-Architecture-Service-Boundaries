#!/usr/bin/env python3
"""Order service. Owns orders and calls Inventory before accepting one."""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ORDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ORDER_PORT", "8001"))
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8002").rstrip("/")
NOTIFICATION_URL = os.environ.get("NOTIFICATION_URL", "http://127.0.0.1:8003").rstrip("/")
INVENTORY_TIMEOUT = float(os.environ.get("INVENTORY_TIMEOUT", "2"))
NOTIFICATION_TIMEOUT = float(os.environ.get("NOTIFICATION_TIMEOUT", "1"))

lock = threading.Lock()
orders = {}
next_id = 0


def check_availability(item_id, quantity):
    """Query Inventory. Returns (body, error_kind). error_kind is None on success."""
    quoted = urllib.parse.quote(item_id, safe="")
    url = f"{INVENTORY_URL}/items/{quoted}/availability?quantity={quantity}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=INVENTORY_TIMEOUT) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "not_found"
        return None, "unreachable"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None, "unreachable"

    if not isinstance(body, dict) or "available" not in body:
        return None, "unreachable"
    return body, None


def publish_order_created(order):
    """Best-effort event. A notification failure does not undo the order."""
    payload = json.dumps({"type": "order_created", "order": order}).encode()
    request = urllib.request.Request(
        f"{NOTIFICATION_URL}/events/order-created",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=NOTIFICATION_TIMEOUT) as response:
            response.read()
        return "sent"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "not_delivered"


class OrderHandler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "Content-Length must be an integer."
        if length <= 0:
            return None, "Request body is required."
        if length > 1_000_000:
            return None, "Request body is too large."
        try:
            data = json.loads(self.rfile.read(length).decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "Request body must be JSON."
        if not isinstance(data, dict):
            return None, "Request body must be a JSON object."
        return data, None

    def do_GET(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                self._send(200, {"status": "ok", "service": "order"})
                return
            if path == "/orders":
                with lock:
                    body = list(orders.values())
                self._send(200, {"orders": body})
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "orders":
                with lock:
                    order = orders.get(parts[1])
                if order is None:
                    self._send(404, {"error": "order_not_found", "order_id": parts[1]})
                    return
                self._send(200, order)
                return
            self._send(404, {"error": "not_found"})
        except Exception as exc:
            print(f"[order] request failed: {exc}")
            self._send(500, {"error": "internal_error", "message": "The order service hit an unexpected error."})

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            if path != "/orders":
                self._send(404, {"error": "not_found"})
                return
            self._create_order()
        except Exception as exc:
            print(f"[order] request failed: {exc}")
            self._send(
                500,
                {
                    "error": "internal_error",
                    "message": "The order service hit an unexpected error and is still running.",
                },
            )

    def _create_order(self):
        data, error = self._read_json()
        if error:
            self._send(400, {"error": "bad_request", "message": error})
            return

        item_id = data.get("item_id")
        customer_email = data.get("customer_email")
        quantity = data.get("quantity", 1)

        if not isinstance(item_id, str) or not item_id.strip():
            self._send(400, {"error": "bad_request", "message": "item_id is required."})
            return
        if not isinstance(customer_email, str) or "@" not in customer_email:
            self._send(400, {"error": "bad_request", "message": "customer_email is required."})
            return
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            self._send(400, {"error": "bad_request", "message": "quantity must be a positive integer."})
            return

        item_id = item_id.strip()
        availability, failure = check_availability(item_id, quantity)
        if failure == "unreachable":
            self._send(
                503,
                {
                    "error": "inventory_unavailable",
                    "message": (
                        "Inventory service could not be reached. "
                        "The order was not created. Please retry."
                    ),
                },
            )
            return
        if failure == "not_found":
            self._send(404, {"error": "item_not_found", "item_id": item_id})
            return
        if not availability.get("available"):
            self._send(
                409,
                {
                    "error": "item_unavailable",
                    "item_id": item_id,
                    "quantity_requested": quantity,
                    "quantity_on_hand": availability.get("quantity_on_hand"),
                    "message": "The requested item is not available.",
                },
            )
            return

        global next_id
        with lock:
            next_id += 1
            order = {
                "order_id": f"ord-{next_id}",
                "item_id": item_id,
                "quantity": quantity,
                "customer_email": customer_email.strip(),
                "status": "created",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            orders[order["order_id"]] = order

        notification_status = publish_order_created(order)
        response = dict(order)
        response["notification"] = notification_status
        self._send(201, response)

    def log_message(self, fmt, *args):
        print(f"[order] {self.address_string()} {fmt % args}")


def main():
    server = ThreadingHTTPServer((HOST, PORT), OrderHandler)
    print(f"Order service listening on http://{HOST}:{PORT}")
    print(f"Inventory URL: {INVENTORY_URL}")
    print(f"Notification URL: {NOTIFICATION_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
