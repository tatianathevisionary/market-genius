#!/usr/bin/env python3
"""
BTC Genius — always-on whale stream (launchd KeepAlive daemon).

The 5-min market_collector only *samples* the mempool, so bursts of exchange
flow between samples are missed. This daemon holds live websockets to
mempool.space and reacts the instant a tracked exchange wallet transacts.

Why address-tracking and not a full firehose: a complete free firehose of
every mempool tx no longer exists (blockchain.info disabled theirs;
mempool.space throttles its `transactions` feed to a homepage sample). But
mempool.space WILL push every transaction touching a tracked address in real
time, with full inputs/outputs — capped at 10 addresses per connection. Since
the high-signal data IS the exchange flow (inflow/outflow on our labeled
wallets), we track the whole label book across as many 10-address connections
as it takes (one worker thread each) and classify every hit live. The 5-min
REST collector stays as the backstop for unknown↔unknown transfers.

No third-party packages — a minimal RFC-6455 websocket client is implemented
over the stdlib socket/ssl. Each worker reconnects with backoff; survives drops.

Stdlib only; Python 3.9+.
"""

import base64
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import db
import market_collector as mc

BASE_DIR = Path(__file__).resolve().parent.parent
LATEST = BASE_DIR / "data" / "market" / "latest.json"
LOG_FILE = BASE_DIR / "logs" / "whale_stream.log"

WS_HOST = "mempool.space"
WS_PORT = 443
WS_PATH = "/api/v1/ws"

ADDR_PER_CONN = 10     # mempool.space hard cap per websocket connection
PRICE_TTL = 120        # re-read BTC price from latest.json at most this often
LABELS_TTL = 300       # re-read label book (picks up newly CIOH-learned wallets)
PRICE_FALLBACK = 100_000  # only used if latest.json is missing AND fetch fails

_seen = set()          # txids processed this session (shared, lock-guarded)
_seen_lock = threading.Lock()
SEEN_MAX = 20000


# --- Minimal RFC-6455 websocket client (stdlib only) --------------------------


class WebSocket:
    """Just enough websocket to subscribe and read text frames."""

    def __init__(self, host, port, path):
        raw = socket.create_connection((host, port), timeout=30)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.buf = bytearray()
        self._handshake(host, path)

    def _handshake(self, host, path):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: https://www.blockchain.com\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        # read headers up to the blank line
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            self.buf.extend(chunk)
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"bad handshake: {head[:80]!r}")
        self.buf = bytearray(rest)

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed")
            self.buf.extend(chunk)

    def send(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x81])  # FIN + text opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        """Return the next text-frame payload (str), handling control frames."""
        while True:
            self._need(2)
            b0, b1 = self.buf[0], self.buf[1]
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            idx = 2
            if length == 126:
                self._need(4)
                length = struct.unpack(">H", self.buf[2:4])[0]
                idx = 4
            elif length == 127:
                self._need(10)
                length = struct.unpack(">Q", self.buf[2:10])[0]
                idx = 10
            mask = b""
            if masked:
                self._need(idx + 4)
                mask = self.buf[idx:idx + 4]
                idx += 4
            self._need(idx + length)
            payload = bytes(self.buf[idx:idx + length])
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            del self.buf[:idx + length]

            if opcode == 0x8:                      # close
                raise ConnectionError("server closed")
            if opcode == 0x9:                      # ping -> pong
                self._pong(payload)
                continue
            if opcode in (0x1, 0x0):               # text / continuation
                return payload.decode("utf-8", "replace")
            # 0xA pong or anything else: ignore

    def _pong(self, payload):
        mask = os.urandom(4)
        frame = bytearray([0x8A, 0x80 | len(payload)]) + mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def ping(self):
        mask = os.urandom(4)            # empty masked ping frame (keepalive)
        self.sock.sendall(bytes(bytearray([0x89, 0x80]) + mask))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- Price + labels (cheaply cached) ------------------------------------------

_price = {"v": None, "at": 0.0}
_labels = {"v": None, "at": 0.0}


def btc_price():
    now = time.time()
    if _price["v"] and now - _price["at"] < PRICE_TTL:
        return _price["v"]
    price = None
    try:
        price = json.loads(LATEST.read_text()).get("btc", {}).get("price")
    except (OSError, json.JSONDecodeError):
        pass
    if not price:                       # latest.json missing — fetch once
        try:
            price = (mc.fetch_btc() or {}).get("price")
        except Exception:               # noqa: BLE001
            price = None
    _price.update(v=price or _price["v"] or PRICE_FALLBACK, at=now)
    return _price["v"]


def labels():
    now = time.time()
    if _labels["v"] is None or now - _labels["at"] > LABELS_TTL:
        _labels.update(v=mc.load_exchange_labels(), at=now)
    return _labels["v"]


# --- Main loop ----------------------------------------------------------------


def to_bcinfo_shape(mtx):
    """Map a mempool.space tx (REST or ws address-event) to the blockchain.info
    shape onchain_entry wants."""
    return {
        "hash": mtx.get("txid"),
        "time": (mtx.get("status") or {}).get("block_time"),  # None in mempool -> now()
        "inputs": [{"prev_out": {
            "addr": (v.get("prevout") or {}).get("scriptpubkey_address"),
            "value": (v.get("prevout") or {}).get("value", 0)}}
            for v in mtx.get("vin", [])],
        "out": [{"addr": o.get("scriptpubkey_address"), "value": o.get("value", 0)}
                for o in mtx.get("vout", [])],
    }


def _txs_from_event(data):
    """Pull tx dicts out of mempool.space address-tracking events.

    Shapes: {'address-transactions': [tx, ...]} (single-address) and
    {'multi-address-transactions': {addr: {mempool:[], confirmed:[], removed:[]}}}.
    We take mempool+confirmed (new + just-mined); 'removed' = dropped, skip.
    """
    out = []
    single = data.get("address-transactions")
    if isinstance(single, list):
        out += single
    multi = data.get("multi-address-transactions")
    if isinstance(multi, dict):
        for buckets in multi.values():
            if isinstance(buckets, dict):
                out += (buckets.get("mempool") or []) + (buckets.get("confirmed") or [])
    return out


def _process_tx(mtx):
    """Classify one tracked tx and append it (deduped). Returns the entry or None."""
    txid = mtx.get("txid")
    if not txid:
        return None
    with _seen_lock:
        if txid in _seen:
            return None
        _seen.add(txid)
        if len(_seen) > SEEN_MAX:
            _seen.clear()
    entry = mc.onchain_entry(
        to_bcinfo_shape(mtx), btc_price(), labels(),
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not entry:
        return None
    db.save_whales([entry])
    mc.append_ledger([entry])
    logging.info("LIVE %-8s $%s %s %s", entry["flow"],
                 f"{entry['usd']:,}", entry.get("venue") or "", txid[:12])
    return entry


def track_worker(addrs, name):
    """One websocket tracking up to 10 addresses; reconnect forever."""
    backoff = 2
    while True:
        ws = None
        try:
            ws = WebSocket(WS_HOST, WS_PORT, WS_PATH)
            ws.sock.settimeout(70)      # idle read window; keepalive feed holds it open
            ws.send(json.dumps({"action": "init"}))
            ws.send(json.dumps({"track-addresses": addrs}))
            # mempoolInfo streams small periodic frames so even all-quiet-wallet
            # connections never go idle (mempool.space doesn't pong client pings).
            ws.send(json.dumps({"action": "want", "data": ["mempoolInfo"]}))
            logging.info("[%s] tracking %d wallets", name, len(addrs))
            backoff = 2
            idle = 0
            while True:
                try:
                    data = json.loads(ws.recv())
                except socket.timeout:  # quiet wallets — keepalive, don't reconnect
                    idle += 1
                    if idle >= 8:       # ~9 min of total silence -> assume dead
                        raise ConnectionError("no data; cycling connection")
                    ws.ping()
                    continue
                idle = 0
                if "track-addresses-error" in data:
                    raise ConnectionError(data["track-addresses-error"])
                for mtx in _txs_from_event(data):
                    _process_tx(mtx)
        except Exception as e:  # noqa: BLE001 - any failure -> reconnect
            logging.warning("[%s] error: %s; reconnect in %ds", name, e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if ws:
                ws.close()


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    addrs = list(labels().keys())
    if not addrs:
        logging.error("no labeled wallets to track; exiting")
        return
    chunks = [addrs[i:i + ADDR_PER_CONN] for i in range(0, len(addrs), ADDR_PER_CONN)]
    logging.info("whale stream starting: %d wallets across %d connection(s)",
                 len(addrs), len(chunks))

    threads = []
    for i, chunk in enumerate(chunks):
        t = threading.Thread(target=track_worker, args=(chunk, f"conn{i+1}"), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()  # workers loop forever; join keeps the process alive


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
