#!/usr/bin/env python3
"""
Gossip server that:
 - Takes a DEFAULT_SEEDS list of host:port strings, which might be names (like "gossip1:8765").
 - Repeatedly attempts to resolve (DNS) + connect to each seed until successful.
 - Once resolved to IP:port, we unify references to avoid spurious multiple addresses for the same node.
 - We do a simple handshake (hello) with a "node_id" so we know duplicates.
 - We exchange a "host_list" message so the cluster can expand, but we store everything in IP:port form.
 - If the user reintroduces a name, we re-resolve it to IP.
 - We keep trying seeds that fail, in case they're offline at first.
"""

import asyncio
import json
import os
import socket
import ssl
import time
import uuid
import logging
from typing import Dict, Any, List, Set, Tuple, Optional

import websockets
from websockets import serve, connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    ConnectionClosedError,
    InvalidHandshake,
)

# -------------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------------
DEFAULT_SEEDS = ["gossip1:8765", "gossip2:8766", "gossip3:8767"]  # or override by env/args
DEFAULT_PORT = 8765

USE_SSL = bool(os.environ.get("USE_SSL", False))
SSL_CERT_PATH = os.environ.get("SSL_CERT_PATH", "/app/cert.pem")
SSL_KEY_PATH = os.environ.get("SSL_KEY_PATH", "/app/key.pem")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")

MAX_OUTBOUND_CONCURRENCY = 20
CONNECT_BACKOFF_MIN = 1.0
CONNECT_BACKOFF_MAX = 30.0
PING_INTERVAL = 5.0
SEEDS_MAINT_INTERVAL = 2.0
SHUTDOWN_WAIT = 3.0

# -------------------------------------------------------------------------
# Helper: parse host:port
# -------------------------------------------------------------------------
def parse_host_port(raw: str) -> Tuple[str, int]:
    """Parses 'somehost:1234' or '[::1]:1234' into (host, port)."""
    raw = raw.strip()
    if raw.startswith("["):
        # IPv6 form like [::1]:8765
        bracket_idx = raw.rindex("]")
        host = raw[1:bracket_idx]
        port_str = raw[bracket_idx + 2 :]
    else:
        parts = raw.split(":")
        if len(parts) < 2:
            raise ValueError(f"Cannot parse host:port from '{raw}'")
        host = ":".join(parts[:-1])
        port_str = parts[-1]
    return (host, int(port_str))

# -------------------------------------------------------------------------
# DNS Resolve
# -------------------------------------------------------------------------
async def resolve_host_once(host: str) -> Optional[str]:
    """Try to DNS-resolve 'host' -> an IP (string).
       Returns None if resolution fails, or the first resolved IP otherwise.
    """
    try:
        loop = asyncio.get_running_loop()
        # getaddrinfo can return multiple addresses (IPv4, IPv6).
        # Let's pick the first AF_INET or AF_INET6
        info_list = await loop.getaddrinfo(host, None)
        for family, socktype, proto, canonname, sockaddr in info_list:
            if family in (socket.AF_INET, socket.AF_INET6):
                ip = sockaddr[0]
                return ip
        return None  # No IPv4/IPv6 found
    except:
        return None

async def resolve_host_repeatedly(host: str) -> str:
    """Repeatedly attempt to DNS-resolve until success, with a small backoff."""
    attempt_backoff = 1.0
    while True:
        ip = await resolve_host_once(host)
        if ip:
            return ip
        logging.info(f"DNS fail for '{host}', retry in {attempt_backoff} sec...")
        await asyncio.sleep(attempt_backoff)
        attempt_backoff = min(attempt_backoff * 1.5, 10.0)

# -------------------------------------------------------------------------
# GossipServer
# -------------------------------------------------------------------------
class Message:
    def __init__(
        self,
        seq_num: int,
        sender_ip_port: str,
        timestamp: float,
        metadata: Dict[str, Any],
        num_of_datas: int,
        payload: List[Any],
    ):
        self.seq_num = seq_num
        self.sender_ip_port = sender_ip_port
        self.timestamp = timestamp
        self.metadata = metadata
        self.num_of_datas = num_of_datas
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq_num": self.seq_num,
            "sender_ip_port": self.sender_ip_port,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "num_of_datas": self.num_of_datas,
            "payload": self.payload,
        }

class GossipServer:
    def __init__(
        self,
        port: int,
        seeds: List[str],
        server_ssl: Optional[ssl.SSLContext] = None,
        client_ssl: Optional[ssl.SSLContext] = None,
    ):
        self.port = port
        self.server_ssl = server_ssl
        self.client_ssl = client_ssl

        self.node_id = str(uuid.uuid4())
        self.connections: Set[websockets.WebSocketClientProtocol] = set()
        self.ws_to_node_id: Dict[websockets.WebSocketClientProtocol, str] = {}
        self.node_id_to_ws: Dict[str, websockets.WebSocketClientProtocol] = {}

        # store known hosts as IP:port strings.
        self.known_hosts: Set[str] = set()

        # track seeds by their original string -> resolved IP:port
        self.seed_backoff: Dict[str, float] = {}

        # partial attempts
        self.host_in_progress: Set[str] = set()

        # Batching
        self.outbound_queue: List[Message] = []
        self.outbound_sending = False
        self.seq_counter = 0
        self.current_batch_done_event = asyncio.Event()
        self.current_batch_done_event.set()

        # inbound store
        self.inbound_messages: List[Message] = []
        self.inbound_cond = asyncio.Condition()

        # We'll store a "canonical" self_host_str as IP:port too
        self.self_host_str = self._determine_self_ip_port()

        # unify seeds: we do NOT put them directly in known_hosts yet, we want to resolve them in the loop
        self.initial_seeds = seeds

        # Diagnostics counters
        self.inbound_count = 0
        self.outbound_count = 0
        self.batch_in_count = 0
        self.batch_out_count = 0

        self.stopping = False

        # -----------------------------------------------------------------
        # ADDED: track which IP:port we have a currently alive outbound connection to
        # -----------------------------------------------------------------
        self.ip_port_to_ws: Dict[str, websockets.WebSocketClientProtocol] = {}

        logging.info(f"Gossip node_id={self.node_id} on {self.self_host_str}")

    def _determine_self_ip_port(self) -> str:
        """Try to get a stable IP, else fallback to e.g. '127.0.0.1:port'."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return f"{ip}:{self.port}"
        except:
            return f"127.0.0.1:{self.port}"

    # ---------------------------------------------------------------------
    # on inbound
    # ---------------------------------------------------------------------
    async def handle_connection(self, ws: websockets.WebSocketClientProtocol):
        if self.stopping:
            await ws.close()
            return
        # handshake inbound
        try:
            remote_id = await self._do_handshake_inbound(ws)
            self.connections.add(ws)
            logging.debug(f"Inbound connection from node_id={remote_id}")
            await self._reader_loop(ws)
        except (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError) as ce:
            logging.debug(f"Inbound closed: {ce}")
        except Exception as e:
            logging.debug(f"Inbound error: {e}")
        finally:
            self.connections.discard(ws)
            rid = self.ws_to_node_id.pop(ws, None)
            if rid:
                self.node_id_to_ws.pop(rid, None)
            await ws.close()

    async def _do_handshake_inbound(self, ws: websockets.WebSocketClientProtocol) -> str:
        # read a "hello" from them
        raw = await ws.recv()
        hello = json.loads(raw)
        if hello.get("type") != "hello":
            raise ValueError("Inbound handshake: expected type=hello")
        remote_id = hello["node_id"]

        if remote_id in self.node_id_to_ws:
            raise ValueError(f"Duplicate node_id inbound={remote_id}")

        self.ws_to_node_id[ws] = remote_id
        self.node_id_to_ws[remote_id] = ws

        # respond with our hello
        my_hello = {"type": "hello", "node_id": self.node_id}
        await ws.send(json.dumps(my_hello))

        logging.debug(f"Inbound handshake done with remote_id={remote_id}")
        return remote_id

    async def _reader_loop(self, ws: websockets.WebSocketClientProtocol):
        async for raw in ws:
            try:
                batch_data = json.loads(raw)
                if not isinstance(batch_data, list):
                    batch_data = [batch_data]
                self.batch_in_count += 1
                for msg_dict in batch_data:
                    msg = Message(
                        seq_num=msg_dict["seq_num"],
                        sender_ip_port=msg_dict["sender_ip_port"],
                        timestamp=msg_dict["timestamp"],
                        metadata=msg_dict["metadata"],
                        num_of_datas=msg_dict["num_of_datas"],
                        payload=msg_dict["payload"],
                    )
                    self.inbound_count += 1
                    await self._handle_inbound_msg(msg)
            except Exception as e:
                logging.debug(f"reader_loop parse error: {e}")

    async def _handle_inbound_msg(self, msg: Message):
        mtype = msg.metadata.get("type")
        if mtype == "ping":
            # send pong back
            await self.send_message({"type": "pong"}, [f"pong for {msg.seq_num}"], no_wait=True)
        elif mtype == "pong":
            # do nothing
            pass
        elif mtype == "host_list":
            # they've given us new addresses. unify them
            incoming_list = msg.payload[0] if msg.payload else []
            for entry in incoming_list:
                # if entry is "somehost:1234", we try to re-resolve to IP
                resolved = await self._resolve_or_skip(entry)
                if resolved and resolved != self.self_host_str:
                    self.known_hosts.add(resolved)
            # optionally attempt to connect
            asyncio.create_task(self.connect_to_all_known_hosts())

    # ---------------------------------------------------------------------
    # inbound and host references
    # ---------------------------------------------------------------------
    async def _resolve_or_skip(self, raw_host_port: str) -> Optional[str]:
        """Given a string that might be 'hostname:port' or 'IP:port', we:
           - parse
           - if 'host' is already IP, we keep it
           - if 'host' is a name, we DNS resolve until success
           - return final ip:port or None if fail
        """
        try:
            host_part, port_part = parse_host_port(raw_host_port)
        except:
            return None

        # check if it's already an IP
        try:
            socket.inet_pton(socket.AF_INET, host_part)
            # it's IPv4
            ip = host_part
        except OSError:
            # not IPv4, maybe IPv6?
            try:
                socket.inet_pton(socket.AF_INET6, host_part)
                ip = host_part
            except OSError:
                # it's a name, let's do repeated DNS
                ip = await resolve_host_once(host_part)
                if not ip:
                    return None

        return f"{ip}:{port_part}"

    # ---------------------------------------------------------------------
    # connecting outward
    # ---------------------------------------------------------------------
    async def connect_to_all_known_hosts(self):
        """Try to connect to everything in known_hosts that we are not
           already connected to or connecting to.
        """
        for hp in list(self.known_hosts):
            # -----------------------------------------------------------------
            # CHANGED: Skip if we already have a live WS or if in progress
            # -----------------------------------------------------------------
            existing_ws = self.ip_port_to_ws.get(hp)
            if existing_ws and not existing_ws.closed:
                continue
            if hp in self.host_in_progress:
                continue

            asyncio.create_task(self._connect_to_host(hp))

    async def _connect_to_host(self, ip_port: str):
        if self.stopping:
            return

        self.host_in_progress.add(ip_port)

        # -----------------------------------------------------------------
        # CHANGED: if there's already a non-closed ws, skip
        # -----------------------------------------------------------------
        existing_ws = self.ip_port_to_ws.get(ip_port)
        if existing_ws and not existing_ws.closed:
            self.host_in_progress.discard(ip_port)
            return

        try:
            scheme = "wss" if self.client_ssl else "ws"
            host_part, port_part = parse_host_port(ip_port)
            uri = f"{scheme}://{host_part}:{port_part}"

            logging.debug(f"Outbound connect attempt to {ip_port}")
            async with connect(uri, ssl=self.client_ssl) as ws:
                # ADDED: record this connection so we won't reconnect it again
                self.ip_port_to_ws[ip_port] = ws

                # do handshake
                remote_id = await self._do_handshake_outbound(ws)
                self.connections.add(ws)
                logging.debug(f"Outbound connected to node_id={remote_id}")

                # now read
                await self._reader_loop(ws)

        except (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError) as ce:
            logging.debug(f"Outbound {ip_port} closed: {ce}")
        except asyncio.CancelledError:
            logging.debug(f"Outbound {ip_port} cancelled.")
        except Exception as e:
            logging.debug(f"Outbound {ip_port} error: {e}")
        finally:
            # -----------------------------------------------------------------
            # ADDED: remove the reference if closed, so we can attempt again later if needed
            # -----------------------------------------------------------------
            self.host_in_progress.discard(ip_port)
            ws_in_map = self.ip_port_to_ws.get(ip_port)
            if ws_in_map and ws_in_map.closed:
                self.ip_port_to_ws.pop(ip_port, None)

    async def _do_handshake_outbound(self, ws: websockets.WebSocketClientProtocol) -> str:
        my_hello = {"type": "hello", "node_id": self.node_id}
        await ws.send(json.dumps(my_hello))

        raw = await ws.recv()
        hello = json.loads(raw)
        if hello.get("type") != "hello":
            raise ValueError("Outbound handshake: expected type=hello")
        remote_id = hello["node_id"]
        if remote_id in self.node_id_to_ws:
            raise ValueError(f"Duplicate node_id outbound={remote_id}")

        self.node_id_to_ws[remote_id] = ws
        self.ws_to_node_id[ws] = remote_id
        return remote_id

    # ---------------------------------------------------------------------
    # Batching
    # ---------------------------------------------------------------------
    async def send_message(
        self,
        metadata: Dict[str, Any],
        payload: List[Any],
        no_wait: bool = False,
    ):
        msg = Message(
            seq_num=self.seq_counter,
            sender_ip_port=self.self_host_str,
            timestamp=time.time(),
            metadata=metadata,
            num_of_datas=len(payload),
            payload=payload,
        )
        self.seq_counter += 1
        self.outbound_count += 1

        self.outbound_queue.append(msg)
        if not self.outbound_sending:
            asyncio.create_task(self._send_batch())

        if not no_wait:
            seq_needed = msg.seq_num
            while True:
                await self.current_batch_done_event.wait()
                if seq_needed < self.seq_counter:
                    break
                self.current_batch_done_event.clear()

    async def _send_batch(self):
        self.outbound_sending = True
        try:
            batch = self.outbound_queue[:]
            self.outbound_queue.clear()
            if not self.connections:
                logging.debug("No peers to send batch.")
            else:
                data_str = json.dumps([m.to_dict() for m in batch])
                sem = asyncio.Semaphore(MAX_OUTBOUND_CONCURRENCY)
                self.batch_out_count += 1

                async def _send_one(conn):
                    async with sem:
                        try:
                            await conn.send(data_str)
                        except (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError):
                            # drop that conn
                            rid = self.ws_to_node_id.pop(conn, None)
                            if rid:
                                self.node_id_to_ws.pop(rid, None)
                            self.connections.discard(conn)
                        except Exception as ex:
                            logging.debug(f"send batch error: {ex}")

                tasks = [_send_one(c) for c in list(self.connections)]
                await asyncio.gather(*tasks)
        finally:
            self.current_batch_done_event.set()
            self.outbound_sending = False
            if self.outbound_queue:
                asyncio.create_task(self._send_batch())

    # ---------------------------------------------------------------------
    # loops
    # ---------------------------------------------------------------------
    async def maintain_seeds_loop(self):
        """Repeatedly attempt to resolve + connect to seeds. If a seed is a name,
           we re-resolve. If it's offline, we back off & retry, so eventually it
           will succeed once the container is up."""
        while not self.stopping:
            for seed in self.initial_seeds:
                ip_port = await self._resolve_or_skip(seed)
                if not ip_port:
                    continue
                if ip_port != self.self_host_str:
                    self.known_hosts.add(ip_port)
            await self.connect_to_all_known_hosts()
            await asyncio.sleep(SEEDS_MAINT_INTERVAL)

    async def diagnostics_loop(self):
        while not self.stopping:
            await asyncio.sleep(1.0)
            c = len(self.connections)
            logging.info(
                f"[Diag] conns={c} "
                f"inbound={self.inbound_count} outbound={self.outbound_count} "
                f"batch_in={self.batch_in_count} batch_out={self.batch_out_count}"
            )
            self.inbound_count = 0
            self.outbound_count = 0
            self.batch_in_count = 0
            self.batch_out_count = 0

    async def ping_loop(self):
        while not self.stopping:
            await asyncio.sleep(PING_INTERVAL)
            await self.send_message({"type": "ping"}, [f"ping {time.time()}"], no_wait=True)

    async def shutdown(self):
        logging.warning("Shutting down gracefully...")
        self.stopping = True
        for ws in list(self.connections):
            try:
                await ws.close()
            except:
                pass
        await asyncio.sleep(SHUTDOWN_WAIT)
        logging.warning("Shutdown complete.")

# -------------------------------------------------------------------------
# main
# -------------------------------------------------------------------------
async def main():
    port = int(os.environ.get("PORT", DEFAULT_PORT))

    # Possibly read seeds from env or sys.argv
    seeds = list(DEFAULT_SEEDS)

    server_ctx = None
    client_ctx = None
    if USE_SSL:
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(SSL_CERT_PATH, SSL_KEY_PATH)
        client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        logging.warning("Using WSS (server+client TLS)")

    server = GossipServer(port, seeds, server_ssl=server_ctx, client_ssl=client_ctx)

    async def shutdown_handler():
        # On SIGINT or SIGTERM, set an event
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            pass
        await server.shutdown()

    shutdown_task = asyncio.create_task(shutdown_handler())

    # Start the inbound server
    async with serve(
        server.handle_connection,
        "0.0.0.0",
        port,
        ssl=server_ctx,
    ):
        logging.warning(f"Started gossip on port={port}, seeds={seeds}")
        tasks = [
            asyncio.create_task(server.maintain_seeds_loop()),
            asyncio.create_task(server.diagnostics_loop()),
            asyncio.create_task(server.ping_loop()),
        ]
        # Wait for shutdown
        await shutdown_task
        for t in tasks:
            t.cancel()

def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    run()
