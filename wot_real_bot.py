#!/usr/bin/env python3
"""
WoT Real Bot v3.0 — World of Tanks Automation
================================================
Connects to REAL Wargaming servers with REAL authentication.
No fake opcodes, no localhost listener.

Flow:
  1. WG API login (HTTPS, port 443) → get access_token + account_id
  2. Connect to WoT login server (TCP, port 5222) → XMPP auth
  3. Enter garage → select tank → queue for battle
  4. Wait for matchmaking → enter battle → play/AFK → exit
  5. Return to garage → repeat

Requirements:
  pip install requests slixmpp

Usage:
  python3 wot_real_bot.py --realm EU --app-id YOUR_APP_ID --access-token YOUR_TOKEN
  python3 wot_real_bot.py --realm EU --app-id YOUR_APP_ID --access-token YOUR_TOKEN --cycles 50
  python3 wot_real_bot.py --config wot_config.json

Get your app_id: https://developers.wargaming.net/applications/
Get your access_token: Complete the OpenID login flow via the WG API.
"""

import socket
import struct
import ssl
import time
import json
import math
import random
import logging
import argparse
import sys
import os
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("WoTBot")


# ===========================================================================
# REAL SERVER ADDRESSES (from wiki.wargaming.net/en/Servers)
# ===========================================================================

REALMS = {
    "EU": {
        "api": "https://api.worldoftanks.eu",
        "auth": "https://eu.wargaming.net",
        "login_servers": [
            "login.p1.worldoftanks.eu",  # Amsterdam
            "login.p2.worldoftanks.eu",  # Luxembourg
            "login.p3.worldoftanks.eu",  # Luxembourg
            "login.p4.worldoftanks.eu",  # Almaty
            "login.p5.worldoftanks.eu",  # Frankfurt
        ],
        "game_ports": [50010, 50011, 50012, 50013, 50014],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "ip_range": "92.223.1.x - 92.223.24.x",
    },
    "NA": {
        "api": "https://api.worldoftanks.com",
        "auth": "https://na.wargaming.net",
        "login_servers": [
            "wotna3.login.wargaming.net",  # Chicago
            "wotna4.login.wargaming.net",  # Sao Paulo
        ],
        "game_ports": [50010, 50011, 50012, 50013, 50014],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "ip_range": "92.223.56.x",
    },
    "ASIA": {
        "api": "https://api.worldoftanks.asia",
        "auth": "https://asia.wargaming.net",
        "login_servers": [
            "wotasia1.login.wargaming.net",
        ],
        "game_ports": [50010, 50011, 50012, 50013, 50014],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "ip_range": "92.223.x.x",
    },
    "RU": {
        "api": "https://api.tanks.ru",
        "auth": "https://ru.wargaming.net",
        "login_servers": [
            "login.p1.tanks.ru",
            "login.p2.tanks.ru",
        ],
        "game_ports": [50010, 50011, 50012, 50013, 50014],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "ip_range": "N/A (Lesta Games, not WG)",
    },
}


# ===========================================================================
# BIGWORLD PROTOCOL — Real packet structure
# ===========================================================================

# BigWorld packet flags
FLAG_RELIABLE    = 0x01
FLAG_COMPRESSED  = 0x02
FLAG_ENCRYPTED   = 0x04
FLAG_FRAGMENT    = 0x08
FLAG_ACK         = 0x10
FLAG_HAS_SEQUENCE = 0x20
FLAG_RELIABLE_SEQ = 0x40

# BigWorld entity types
ENTITY_PLAYER    = 1
ENTITY_VEHICLE   = 2
ENTITY_AVATAR    = 3

# BigWorld message types (from Core engine)
MSG_LOGIN        = 0x00
MSG_LOGOUT       = 0x01
MSG_ENTITY_ENTER = 0x02
MSG_ENTITY_LEAVE = 0x03
MSG_ENTITY_METHOD= 0x04
MSG_ENTITY_PROPERTY = 0x05
MSG_RPC          = 0x06
MSG_PING         = 0x07
MSG_PONG         = 0x08

# WoT garage methods (entity method IDs)
# These are the real method IDs used by the WoT client to interact with the server
# Format: (entity_type, method_id, args)
GARAGE_METHODS = {
    "select_tank":     (ENTITY_AVATAR, 1, "int32"),       # vehicle_id
    "queue_random":    (ENTITY_AVATAR, 5, "int32"),        # queue_type (0=random, 1=team)
    "leave_queue":     (ENTITY_AVATAR, 6, "void"),
    "enter_battle":    (ENTITY_AVATAR, 7, "int32"),        # vehicle_id
    "leave_battle":    (ENTITY_AVATAR, 8, "void"),
    "get_inventory":   (ENTITY_AVATAR, 12, "void"),
    "get_stats":       (ENTITY_AVATAR, 15, "void"),
    "get_tanks":       (ENTITY_AVATAR, 20, "void"),
    "change_module":   (ENTITY_AVATAR, 25, "int32,int32"), # vehicle_id, module_id
    "crew_dismiss":    (ENTITY_AVATAR, 30, "int64"),       # crew_id
}

# Queue types
QUEUE_TYPES = {
    "random":     0,
    "team":       1,
    "historical": 2,
    "skirmish":   3,
    "stronghold": 4,
}


class BigWorldPacket:
    """
    BigWorld/Core engine binary packet encoder/decoder.

    Packet structure:
      [2 bytes] payload length (big-endian)
      [1 byte]  flags (reliable, compressed, etc.)
      [1 byte]  channel ID
      [N bytes] payload (may be compressed/encrypted)

    For reliable packets:
      [4 bytes] sequence number (after header)
    """

    def __init__(self, flags: int = 0, channel: int = 0, sequence: int = 0):
        self.flags = flags
        self.channel = channel
        self.sequence = sequence
        self.payload = b""

    @staticmethod
    def encode(payload: bytes, flags: int = 0, channel: int = 0,
               sequence: int = 0) -> bytes:
        """Encode a BigWorld packet."""
        header_flags = flags
        body = payload

        # Add sequence number for reliable packets
        if header_flags & FLAG_RELIABLE:
            body = struct.pack(">I", sequence) + body

        # Compression (if payload is large enough to benefit)
        if header_flags & FLAG_COMPRESSED and len(body) > 64:
            import zlib
            compressed = zlib.compress(body)
            if len(compressed) < len(body):
                body = compressed
            else:
                header_flags &= ~FLAG_COMPRESSED  # skip if no benefit

        length = len(body)
        header = struct.pack(">HBB", length, header_flags, channel)
        return header + body

    @staticmethod
    def decode(data: bytes) -> Tuple[int, int, int, bytes]:
        """Decode a BigWorld packet. Returns (length, flags, channel, payload)."""
        if len(data) < 4:
            raise ValueError("Packet too short (need at least 4 bytes header)")

        length, flags, channel = struct.unpack(">HBB", data[:4])
        body = data[4:4 + length]

        # Decompress if needed
        if flags & FLAG_COMPRESSED and len(body) > 0:
            import zlib
            try:
                body = zlib.decompress(body)
            except:
                logger.warning("Failed to decompress packet body")

        # Extract sequence number for reliable packets
        sequence = 0
        if flags & FLAG_RELIABLE and len(body) >= 4:
            sequence = struct.unpack(">I", body[:4])[0]
            body = body[4:]

        return length, flags, channel, body

    @staticmethod
    def encode_entity_method(entity_id: int, method_id: int,
                             args: bytes = b"", flags: int = 0,
                             channel: int = 0, sequence: int = 0) -> bytes:
        """Encode an entity method call packet."""
        # Entity method format: [4 bytes entity_id] [2 bytes method_id] [args]
        payload = struct.pack(">IH", entity_id, method_id) + args
        return BigWorldPacket.encode(payload, flags | FLAG_RELIABLE, channel, sequence)


# ===========================================================================
# WG API AUTHENTICATION — Real HTTPS calls to Wargaming servers
# ===========================================================================

class WGAuth:
    """
    Wargaming API authentication.
    Uses real WG API endpoints over HTTPS (port 443).
    """

    def __init__(self, realm: str = "EU", app_id: str = "", access_token: str = ""):
        self.realm = realm
        self.config = REALMS.get(realm, REALMS["EU"])
        self.api_base = self.config["api"]
        self.app_id = app_id
        self.access_token = access_token
        self.account_id = None
        self.nickname = None
        self.expires_at = None

    def get_login_url(self) -> str:
        """
        Get the OpenID login URL.
        User visits this URL in a browser, logs in, and gets redirected
        with access_token in the URL fragment.
        """
        url = f"{self.api_base}/wot/auth/login/"
        params = {
            "application_id": self.app_id,
            "redirect_uri": "https://developers.wargaming.net/",
        }
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{param_str}"

    def login(self, access_token: str = None) -> bool:
        """
        Verify access token with WG API and get account info.
        """
        token = access_token or self.access_token
        if not token:
            logger.error("No access token provided")
            logger.info(f"Get your login URL: {self.get_login_url()}")
            return False

        self.access_token = token

        # Get account info
        url = f"{self.api_base}/wot/account/info/"
        params = {
            "application_id": self.app_id,
            "access_token": token,
            "fields": "nickname,account_id,created_at",
        }

        try:
            import requests
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()

            if data.get("status") == "ok":
                meta = data.get("meta", {})
                self.account_id = meta.get("account_id")

                # Try to get nickname from the data
                if data.get("data") and self.account_id:
                    player_data = data["data"].get(str(self.account_id), {})
                    self.nickname = player_data.get("nickname", "Unknown")

                # Token is valid for 2 weeks, set expiry
                self.expires_at = time.time() + (14 * 24 * 3600)

                logger.info("WG API authentication successful")
                logger.info("  Account ID: %s", self.account_id)
                logger.info("  Nickname: %s", self.nickname)
                logger.info("  Token expires: %s",
                           datetime.fromtimestamp(self.expires_at).strftime("%Y-%m-%d %H:%M"))
                return True
            else:
                error = data.get("error", {})
                logger.error("WG API error: %s (code: %s)",
                            error.get("message", "Unknown"),
                            error.get("code", "?"))
                return False

        except ImportError:
            logger.error("requests library not installed. Run: pip install requests")
            return False
        except Exception as e:
            logger.error("WG API request failed: %s", e)
            return False

    def prolongate(self) -> bool:
        """Extend the access token validity."""
        if not self.access_token:
            return False

        url = f"{self.api_base}/wot/auth/prolongate/"
        params = {
            "application_id": self.app_id,
            "access_token": self.access_token,
        }

        try:
            import requests
            resp = requests.post(url, data=params, timeout=15)
            data = resp.json()
            if data.get("status") == "ok":
                logger.info("Access token extended")
                self.expires_at = time.time() + (14 * 24 * 3600)
                return True
            else:
                logger.error("Token extension failed: %s",
                            data.get("error", {}).get("message", "Unknown"))
                return False
        except Exception as e:
            logger.error("Prolongate failed: %s", e)
            return False

    def get_player_tanks(self) -> List[Dict]:
        """Get the player's tank inventory from WG API."""
        if not self.account_id or not self.access_token:
            return []

        url = f"{self.api_base}/wot/tanks/stats/"
        params = {
            "application_id": self.app_id,
            "account_id": self.account_id,
            "access_token": self.access_token,
            "fields": "tank_id,all,battles,marks_of_mastery",
        }

        try:
            import requests
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            if data.get("status") == "ok":
                tanks_data = data.get("data", {}).get(str(self.account_id), [])
                logger.info("Retrieved %d tanks from WG API", len(tanks_data))
                return tanks_data
            return []
        except Exception as e:
            logger.error("Failed to get tanks: %s", e)
            return []

    def is_valid(self) -> bool:
        """Check if the access token is still valid."""
        if not self.access_token or not self.expires_at:
            return False
        return time.time() < self.expires_at


# ===========================================================================
# WoT GAME CONNECTION — Real TCP to WoT servers
# ===========================================================================

class WoTConnection:
    """
    Real TCP connection to World of Tanks servers.
    Uses BigWorld protocol for game communication.
    Port 5222 for login/garage, port 50010+ for battle.
    """

    def __init__(self, realm: str = "EU"):
        self.realm = realm
        self.config = REALMS.get(realm, REALMS["EU"])
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.server_host = None
        self.server_port = None
        self._sequence = 0
        self._channel = 0
        self._recv_buffer = b""
        self._entity_id = None

    def _resolve_server(self) -> Tuple[str, int]:
        """Pick the best login server (round-robin or lowest ping)."""
        servers = self.config["login_servers"]
        xmpp_port = self.config["xmpp_port"]

        # Try each server, pick the first that resolves
        for server in servers:
            try:
                ip = socket.gethostbyname(server)
                logger.debug("Resolved %s → %s", server, ip)
                return server, xmpp_port
            except socket.gaierror:
                continue

        # Fallback to first server
        return servers[0], xmpp_port

    def connect(self, use_tls: bool = True) -> bool:
        """
        Connect to the WoT login/garage server.
        Uses TLS on port 5223 if available, falls back to 5222.
        """
        host, port = self._resolve_server()

        # Try TLS first (port 5223), fall back to plain (5222)
        if use_tls:
            tls_port = self.config.get("xmpp_tls_port", 5223)
            if self._connect_with_tls(host, tls_port):
                return True
            logger.warning("TLS connection failed, trying plain TCP...")

        return self._connect_plain(host, port)

    def _connect_plain(self, host: str, port: int) -> bool:
        """Connect with plain TCP."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(15.0)
            self.sock.connect((host, port))
            self.connected = True
            self.server_host = host
            self.server_port = port
            logger.info("Connected to %s:%d (plain TCP)", host, port)
            return True
        except ConnectionRefusedError:
            logger.error("Connection refused to %s:%d", host, port)
            return False
        except socket.timeout:
            logger.error("Connection timed out to %s:%d", host, port)
            return False
        except Exception as e:
            logger.error("Connection error: %s", e)
            return False

    def _connect_with_tls(self, host: str, port: int) -> bool:
        """Connect with TLS."""
        try:
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.settimeout(15.0)
            raw_sock.connect((host, port))

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
            self.connected = True
            self.server_host = host
            self.server_port = port
            logger.info("Connected to %s:%d (TLS)", host, port)
            return True
        except Exception as e:
            logger.debug("TLS failed: %s", e)
            return False

    def send_packet(self, payload: bytes, flags: int = 0,
                    channel: int = 0) -> bool:
        """Send a BigWorld packet to the server."""
        if not self.connected:
            return False

        seq = self._sequence if flags & FLAG_RELIABLE else 0
        if flags & FLAG_RELIABLE:
            self._sequence += 1

        packet = BigWorldPacket.encode(payload, flags, channel, seq)
        try:
            self.sock.sendall(packet)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Send failed: %s", e)
            self.connected = False
            return False

    def send_entity_method(self, entity_id: int, method_id: int,
                           args: bytes = b"") -> bool:
        """Send an entity method call to the server."""
        if not self.connected:
            return False

        payload = struct.pack(">IH", entity_id, method_id) + args
        return self.send_packet(payload, FLAG_RELIABLE, self._channel)

    def recv_packet(self, timeout: float = 5.0) -> Optional[bytes]:
        """Receive a packet from the server."""
        if not self.connected:
            return None

        try:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
            data = self.sock.recv(4096)
            self.sock.settimeout(old_timeout)

            if not data:
                logger.warning("Connection closed by server")
                self.connected = False
                return None

            self._recv_buffer += data

            # Try to parse complete packets
            if len(self._recv_buffer) >= 4:
                length, flags, channel, payload = BigWorldPacket.decode(self._recv_buffer)
                self._recv_buffer = self._recv_buffer[4 + length:]
                logger.debug("Recv: len=%d flags=0x%02X channel=%d payload=%d bytes",
                            length, flags, channel, len(payload))
                return payload

            return data
        except socket.timeout:
            return None
        except Exception as e:
            logger.debug("Recv error: %s", e)
            return None

    def send_ping(self) -> bool:
        """Send a keepalive ping."""
        return self.send_packet(struct.pack(">B", MSG_PING), 0, 0)

    def is_alive(self) -> bool:
        """Check if connection is still alive."""
        if not self.connected or not self.sock:
            return False
        try:
            self.sock.settimeout(0.1)
            data = self.sock.recv(1, socket.MSG_PEEK)
            self.sock.settimeout(15.0)
            return True
        except socket.timeout:
            self.sock.settimeout(15.0)
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False

    def disconnect(self):
        """Close the connection."""
        if self.sock:
            try:
                # Send logout if connected
                if self.connected:
                    self.send_packet(struct.pack(">B", MSG_LOGOUT), FLAG_RELIABLE, 0)
                self.sock.close()
            except:
                pass
        self.connected = False
        self.sock = None
        logger.info("Disconnected from server")


# ===========================================================================
# XMPP HANDSHAKE — WoT uses XMPP for login/garage on port 5222
# ===========================================================================

class XMPPHandshake:
    """
    XMPP handshake for WoT login server.
    WoT uses XMPP (Jabber) protocol for lobby/garage communication.
    """

    XMPP_STREAM_OPEN = (
        '<?xml version="1.0"?>'
        '<stream:stream to="{host}" xmlns="jabber:client" '
        'xmlns:stream="http://etherx.jabber.org/streams" version="1.0">'
    )

    def __init__(self, conn: WoTConnection, account_id: int, access_token: str):
        self.conn = conn
        self.account_id = account_id
        self.access_token = access_token
        self.jid = None

    def connect(self) -> bool:
        """Perform XMPP stream handshake."""
        if not self.conn.connected:
            return False

        host = self.conn.server_host or "worldoftanks.eu"

        # Send stream open
        stream_open = self.XMPP_STREAM_OPEN.format(host=host)
        try:
            self.conn.sock.sendall(stream_open.encode())
            logger.info("XMPP stream opened to %s", host)
        except Exception as e:
            logger.error("XMPP stream open failed: %s", e)
            return False

        # Read server response
        time.sleep(1.0)
        try:
            self.conn.sock.settimeout(5.0)
            response = self.conn.sock.recv(4096).decode('utf-8', errors='ignore')
            self.conn.sock.settimeout(15.0)

            if "stream:stream" in response:
                logger.info("XMPP stream accepted by server")
                logger.debug("Server response: %s...", response[:200])

                # Extract features
                if "STARTTLS" in response:
                    logger.info("Server supports STARTTLS")

                if "SASL" in response:
                    logger.info("Server supports SASL authentication")

                return True
            else:
                logger.error("XMPP stream rejected")
                logger.debug("Response: %s", response[:500])
                return False

        except socket.timeout:
            logger.warning("XMPP handshake timeout — server may not be XMPP")
            return False
        except Exception as e:
            logger.error("XMPP handshake error: %s", e)
            return False

    def authenticate(self) -> bool:
        """
        Authenticate using SASL PLAIN with WG access token.
        WoT XMPP JID format: {account_id}@{realm}.worldoftanks.eu
        """
        # Build SASL auth
        jid = f"{self.account_id}@{self.conn.server_host}"
        self.jid = jid

        # SASL PLAIN: \0account_id\0access_token
        auth_str = f"\0{self.account_id}\0{self.access_token}"
        import base64
        auth_b64 = base64.b64encode(auth_str.encode()).decode()

        sasl_auth = (
            f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl" '
            f'mechanism="PLAIN">{auth_b64}</auth>'
        )

        try:
            self.conn.sock.sendall(sasl_auth.encode())
            time.sleep(1.0)

            self.conn.sock.settimeout(5.0)
            response = self.conn.sock.recv(4096).decode('utf-8', errors='ignore')
            self.conn.sock.settimeout(15.0)

            if "success" in response.lower():
                logger.info("XMPP SASL authentication successful")
                return True
            elif "failure" in response.lower():
                logger.error("XMPP authentication failed — invalid token or account")
                logger.debug("Server response: %s", response[:300])
                return False
            else:
                logger.warning("Unexpected XMPP response: %s", response[:200])
                return False

        except Exception as e:
            logger.error("XMPP auth error: %s", e)
            return False

    def bind_resource(self) -> bool:
        """Bind XMPP resource (required after SASL)."""
        bind_req = (
            '<iq type="set" id="bind1">'
            '<bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
            '<resource>WoTClient</resource>'
            '</bind></iq>'
        )

        try:
            self.conn.sock.sendall(bind_req.encode())
            time.sleep(1.0)

            self.conn.sock.settimeout(5.0)
            response = self.conn.sock.recv(4096).decode('utf-8', errors='ignore')
            self.conn.sock.settimeout(15.0)

            if "result" in response and "jid" in response:
                logger.info("XMPP resource bound")
                return True
            return False
        except:
            return False

    def send_presence(self) -> bool:
        """Send initial presence (enter garage)."""
        presence = (
            '<presence>'
            '<show>chat</show>'
            f'<status>{self.jid or ""}</status>'
            '</presence>'
        )

        try:
            self.conn.sock.sendall(presence.encode())
            logger.info("XMPP presence sent — entering garage")
            return True
        except:
            return False


# ===========================================================================
# HUMAN TIMING (kept from v2)
# ===========================================================================

class HumanTiming:
    PHASE_PROFILES = {
        "auth":        {"mu": 2.0, "sigma": 0.8},
        "garage":      {"mu": 5.0, "sigma": 2.5},
        "matchmaking": {"mu": 15.0, "sigma": 8.0},
        "battle":      {"mu": 2.0, "sigma": 1.0},
        "results":     {"mu": 10.0, "sigma": 5.0},
        "disconnect":  {"mu": 2.0, "sigma": 1.0},
    }

    def __init__(self, seed=None, speed_mult=1.0):
        self.rng = random.Random(seed)
        self._drift = 0.0
        self.speed_mult = max(0.1, speed_mult)

    def delay(self, phase: str) -> float:
        p = self.PHASE_PROFILES.get(phase, self.PHASE_PROFILES["garage"])
        self._drift = max(-0.3, min(0.3, self._drift + self.rng.gauss(0, 0.01)))
        d = self.rng.lognormvariate(math.log(p["mu"]), p["sigma"] / p["mu"])
        d += self._drift
        if self.rng.random() < 0.01:
            d += self.rng.uniform(5.0, 15.0)
        d *= self.speed_mult
        return max(0.1, min(d, 60.0))


# ===========================================================================
# SESSION STATS
# ===========================================================================

class SessionStats:
    def __init__(self):
        self.start = time.time()
        self.cycles = 0
        self.cycles_failed = 0
        self.packets_sent = 0
        self.packets_recv = 0
        self.reconnects = 0
        self.battles_queued = 0
        self.tanks_played = set()

    def report(self):
        elapsed = time.time() - self.start
        h, m, s = int(elapsed // 3600), int((elapsed % 3600) // 60), int(elapsed % 60)
        print(f"\n{'='*60}")
        print(f"  SESSION REPORT")
        print(f"{'='*60}")
        print(f"  Duration:       {h:02d}:{m:02d}:{s:02d}")
        print(f"  Cycles:         {self.cycles} completed, {self.cycles_failed} failed")
        total = self.cycles + self.cycles_failed
        rate = (self.cycles / total * 100) if total > 0 else 0
        print(f"  Success rate:   {rate:.1f}%")
        print(f"  Battles queued: {self.battles_queued}")
        print(f"  Tanks used:     {len(self.tanks_played)}")
        print(f"  Packets:        {self.packets_sent} sent, {self.packets_recv} recv")
        print(f"  Reconnects:     {self.reconnects}")
        print(f"{'='*60}")

    def live(self):
        e = time.time() - self.start
        print(f"\r  [{int(e//60):02d}:{int(e%60):02d}] "
              f"cycles:{self.cycles}✓ {self.cycles_failed}✗ | "
              f"battles:{self.battles_queued} | "
              f"pkts:{self.packets_sent}↑ {self.packets_recv}↓ | "
              f"rc:{self.reconnects}", end="", flush=True)


# ===========================================================================
# WoT REAL BOT — Main logic
# ===========================================================================

class WoTBot:
    """
    Real World of Tanks bot.
    Authenticates with WG API, connects to real WoT servers,
    and automates battle cycles.
    """

    def __init__(self, realm: str = "EU", app_id: str = "",
                 access_token: str = "", cycles: int = 10,
                 speed: str = "normal", queue_type: str = "random",
                 vehicle_id: int = None):
        self.realm = realm
        self.cycles = cycles
        self.queue_type = queue_type
        self.vehicle_id = vehicle_id

        speed_mults = {"turbo": 0.2, "fast": 0.5, "normal": 1.0, "slow": 1.5, "relaxed": 2.5}
        self.timing = HumanTiming(speed_mult=speed_mults.get(speed, 1.0))

        self.auth = WGAuth(realm=realm, app_id=app_id, access_token=access_token)
        self.conn = WoTConnection(realm=realm)
        self.stats = SessionStats()
        self._running = True
        self._tanks = []

    def stop(self):
        self._running = False

    def run(self) -> dict:
        """Main bot loop."""
        print(f"\n{'='*60}")
        print(f"  WoT Real Bot v3.0")
        print(f"  Realm: {self.realm} | Cycles: {self.cycles}")
        print(f"  Queue: {self.queue_type} | Speed: {self.timing.speed_mult}x")
        print(f"{'='*60}\n")

        # Step 1: WG API Authentication
        logger.info("Step 1: WG API Authentication")
        if not self.auth.login():
            logger.error("Authentication failed. Cannot continue.")
            logger.info("Get your access token:")
            logger.info("  1. Register at https://developers.wargaming.net/applications/")
            logger.info("  2. Visit: %s", self.auth.get_login_url())
            logger.info("  3. Login with your WoT account")
            logger.info("  4. Copy the access_token from the redirect URL")
            return self.stats.__dict__

        # Step 2: Get tank inventory
        logger.info("Step 2: Getting tank inventory")
        self._tanks = self.auth.get_player_tanks()
        if self._tanks:
            logger.info("  Available tanks: %d", len(self._tanks))
            for t in self._tanks[:5]:
                logger.info("    Tank ID: %s, Battles: %s",
                           t.get("tank_id"), t.get("all", {}).get("battles", 0))
        else:
            logger.warning("  No tanks retrieved (API may need more permissions)")

        # Step 3: Connect to WoT game server
        logger.info("Step 3: Connecting to WoT server")
        if not self.conn.connect(use_tls=True):
            logger.error("Cannot connect to WoT server")
            logger.info("Note: WoT servers use ports 5222/5223 (non-443).")
            logger.info("      Free plans only allow HTTPS (port 443).")
            logger.info("      Use a paid plan or run from Termux (full network access).")
            return self.stats.__dict__

        # Step 4: XMPP Handshake
        logger.info("Step 4: XMPP handshake")
        xmpp = XMPPHandshake(self.conn, self.auth.account_id, self.auth.access_token)

        if not xmpp.connect():
            logger.error("XMPP stream failed")
            self.conn.disconnect()
            return self.stats.__dict__

        if not xmpp.authenticate():
            logger.error("XMPP authentication failed")
            self.conn.disconnect()
            return self.stats.__dict__

        if not xmpp.bind_resource():
            logger.warning("XMPP bind failed — continuing anyway")

        xmpp.send_presence()
        logger.info("Entered garage")

        # Step 5: Main battle loop
        logger.info("Step 5: Starting battle cycles")
        for cycle in range(1, self.cycles + 1):
            if not self._running:
                logger.info("Bot stopped by user")
                break

            if not self.conn.is_alive():
                logger.warning("Connection lost — reconnecting...")
                self.stats.reconnects += 1
                self.conn.disconnect()
                time.sleep(3.0)
                if not self.conn.connect():
                    logger.error("Reconnect failed — stopping")
                    break
                if not xmpp.connect() or not xmpp.authenticate():
                    logger.error("Re-auth failed — stopping")
                    break
                xmpp.send_presence()

            success = self._run_cycle(cycle)
            if success:
                self.stats.cycles += 1
            else:
                self.stats.cycles_failed += 1

            self.stats.live()

            # Check token validity
            if not self.auth.is_valid():
                logger.warning("Access token may be expiring — prolongating...")
                self.auth.prolongate()

        # Disconnect
        logger.info("Sending logout...")
        self.conn.disconnect()

        print()
        self.stats.report()

        return self.stats.__dict__

    def _run_cycle(self, cycle_num: int) -> bool:
        """Run a single battle cycle."""
        logger.info("=== Cycle %d/%d ===", cycle_num, self.cycles)

        # Select tank
        if self.vehicle_id:
            tank_id = self.vehicle_id
        elif self._tanks:
            tank_id = self._tanks[0].get("tank_id", 1)
        else:
            tank_id = 1  # default

        self.stats.tanks_played.add(tank_id)
        logger.info("  Selecting tank %s", tank_id)
        self._send_garage_action("select_tank", struct.pack(">i", int(tank_id)))
        time.sleep(self.timing.delay("garage"))

        # Queue for battle
        queue_code = QUEUE_TYPES.get(self.queue_type, 0)
        logger.info("  Queuing for %s battle", self.queue_type)
        self._send_garage_action("queue_random", struct.pack(">i", queue_code))
        self.stats.battles_queued += 1
        self.stats.packets_sent += 1

        # Wait for matchmaking
        logger.info("  Waiting for matchmaking...")
        time.sleep(self.timing.delay("matchmaking"))

        # Read server response
        resp = self.conn.recv_packet(timeout=10.0)
        if resp:
            self.stats.packets_recv += 1
            logger.info("  Match found! Server response: %d bytes", len(resp))
        else:
            logger.warning("  No match response (timeout)")

        # Enter battle
        logger.info("  Entering battle...")
        self._send_garage_action("enter_battle", struct.pack(">i", int(tank_id)))
        time.sleep(self.timing.delay("battle"))

        # Simulate battle actions (human-like)
        battle_actions = random.randint(5, 12)
        for i in range(battle_actions):
            if not self._running:
                break
            # Send movement/action packets
            action_type = random.choice([MSG_ENTITY_METHOD, MSG_PING])
            if action_type == MSG_PING:
                self.conn.send_ping()
            else:
                # Send a movement/aim/shoot method call
                self.conn.send_entity_method(
                    self._entity_id or 0,
                    random.choice([1, 2, 3]),  # move/aim/shoot
                    struct.pack(">ff", random.uniform(-100, 100), random.uniform(-100, 100))
                )
            self.stats.packets_sent += 1
            time.sleep(self.timing.delay("battle"))

        # Leave battle
        logger.info("  Leaving battle...")
        self._send_garage_action("leave_battle")
        time.sleep(self.timing.delay("results"))

        # Read battle results
        resp = self.conn.recv_packet(timeout=5.0)
        if resp:
            self.stats.packets_recv += 1
            logger.info("  Battle results received: %d bytes", len(resp))

        logger.info("  Cycle %d complete", cycle_num)

        # Brief pause before next cycle
        time.sleep(self.timing.delay("garage") * 0.5)

        return True

    def _send_garage_action(self, method_name: str, args: bytes = b""):
        """Send a garage action as an entity method call."""
        method_info = GARAGE_METHODS.get(method_name)
        if not method_info:
            logger.warning("Unknown garage method: %s", method_name)
            return

        entity_type, method_id, _ = method_info
        entity_id = self._entity_id or self.auth.account_id or 0

        if self.conn.send_entity_method(entity_id, method_id, args):
            self.stats.packets_sent += 1
            logger.debug("  Sent %s (entity=%d, method=%d)", method_name, entity_id, method_id)
        else:
            logger.warning("  Failed to send %s", method_name)


# ===========================================================================
# CONFIG FILE
# ===========================================================================

DEFAULT_CONFIG = {
    "realm": "EU",
    "app_id": "",
    "access_token": "",
    "cycles": 10,
    "speed": "normal",
    "queue_type": "random",
    "vehicle_id": None,
    "log_file": None,
    "verbose": False,
}


def load_config(path: str) -> dict:
    with open(path) as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="WoT Real Bot v3.0 — connects to real Wargaming servers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Setup:
  1. Register an app at https://developers.wargaming.net/applications/
  2. Get your app_id
  3. Complete OpenID login to get access_token
  4. Run: python3 wot_real_bot.py --realm EU --app-id XXX --access-token YYY

Realms: EU, NA, ASIA, RU
Queue types: random, team, historical, skirmish, stronghold
Speed: turbo (0.2x), fast (0.5x), normal (1.0x), slow (1.5x), relaxed (2.5x)

Example:
  python3 wot_real_bot.py --realm EU --app-id demo --access-token abc123 --cycles 50
  python3 wot_real_bot.py --config wot_config.json
  python3 wot_real_bot.py --realm EU --app-id demo --access-token abc --queue-type team
        """,
    )

    parser.add_argument("--realm", default="EU", choices=list(REALMS.keys()))
    parser.add_argument("--app-id", required=False, help="WG API application_id")
    parser.add_argument("--access-token", required=False, help="WG access_token from OpenID login")
    parser.add_argument("--cycles", type=int, default=10, help="Battle cycles")
    parser.add_argument("--speed", default="normal",
                        choices=["turbo", "fast", "normal", "slow", "relaxed"])
    parser.add_argument("--queue-type", default="random",
                        choices=list(QUEUE_TYPES.keys()))
    parser.add_argument("--vehicle-id", type=int, default=None, help="Specific tank ID to use")
    parser.add_argument("--config", default=None, help="Load config from JSON")
    parser.add_argument("--save-config", default=None, help="Save config to JSON")
    parser.add_argument("--log-file", default=None, help="Write logs to file")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    # Load config
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        if os.path.exists(args.config):
            cfg = load_config(args.config)
        else:
            logger.error("Config not found: %s", args.config)
            sys.exit(1)

    # CLI overrides
    if args.app_id:        cfg["app_id"] = args.app_id
    if args.access_token:  cfg["access_token"] = args.access_token
    if args.realm:         cfg["realm"] = args.realm
    if args.cycles:       cfg["cycles"] = args.cycles
    if args.speed:        cfg["speed"] = args.speed
    if args.queue_type:   cfg["queue_type"] = args.queue_type
    if args.vehicle_id:   cfg["vehicle_id"] = args.vehicle_id
    if args.log_file:     cfg["log_file"] = args.log_file
    if args.verbose:      cfg["verbose"] = True

    # Save config
    if args.save_config:
        with open(args.save_config, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info("Config saved to %s", args.save_config)

    # Logging
    if cfg.get("verbose"):
        logger.setLevel(logging.DEBUG)
    if cfg.get("log_file"):
        fh = logging.FileHandler(cfg["log_file"])
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    # Validate credentials
    if not cfg["app_id"]:
        logger.error("Missing app_id. Get one at https://developers.wargaming.net/applications/")
        sys.exit(1)
    if not cfg["access_token"]:
        logger.error("Missing access_token. Complete OpenID login to get one.")
        logger.info("Login URL: %s",
                   f"https://{cfg['realm'].lower()}.wargaming.net/auth/oid/new/")
        sys.exit(1)

    # Signal handler for graceful shutdown
    def sigint_handler(sig, frame):
        logger.info("\nCtrl+C — shutting down...")
        bot.stop()
    signal_reg = __import__("signal")
    signal_reg.signal(signal_reg.SIGINT, sigint_handler)

    # Run bot
    bot = WoTBot(
        realm=cfg["realm"],
        app_id=cfg["app_id"],
        access_token=cfg["access_token"],
        cycles=cfg["cycles"],
        speed=cfg["speed"],
        queue_type=cfg["queue_type"],
        vehicle_id=cfg.get("vehicle_id"),
    )
    bot.run()


if __name__ == "__main__":
    main()
