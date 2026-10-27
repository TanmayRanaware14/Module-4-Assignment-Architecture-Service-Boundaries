#!/usr/bin/env python3
"""Notification service. Owns delivery records for order-created events."""

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("NOTIFICATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOTIFICATION_PORT", "8003"))

lock = threading.Lock()
notifications = []
next_id = 0


class NotificationHandler(BaseHTTPRequestHandler):
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
            if self.path == "/health":
                self._send(200, {"status": "ok", "service": "notification"})
                return
            if self.path == "/notifications":
                with lock:
                    body = list(notifications)
                self._send(200, {"notifications": body})
                return
            self._send(404, {"error": "not_found"})
        except Exception as exc:
            print(f"[notification] request failed: {exc}")
            self._send(500, {"error": "internal_error"})

    def do_POST(self):
        try:
            if self.path != "/events/order-created":
                self._send(404, {"error": "not_found"})
                return

            event, error = self._read_json()
            if error:
                self._send(400, {"error": "bad_request", "message": error})
                return

            order = event.get("order")
            if not isinstance(order, dict) or not order.get("order_id"):
                self._send(400, {"error": "bad_request", "message": "order.order_id is required."})
                return

            global next_id
            with lock:
                next_id += 1
                record = {
                    "notification_id": f"ntf-{next_id}",
                    "type": "order_created",
                    "recipient": order.get("customer_email"),
                    "order_id": order.get("order_id"),
                    "message": (
                        f"Order {order.get('order_id')} was created for "
                        f"{order.get('quantity')} x {order.get('item_id')}."
                    ),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
                notifications.append(record)

            print(f"[notification] delivered {record['notification_id']} for {record['order_id']}")
            self._send(202, record)
        except Exception as exc:
            print(f"[notification] request failed: {exc}")
            self._send(500, {"error": "internal_error"})

    def log_message(self, fmt, *args):
        print(f"[notification] {self.address_string()} {fmt % args}")


def main():
    server = ThreadingHTTPServer((HOST, PORT), NotificationHandler)
    print(f"Notification service listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
