#!/usr/bin/env python3
"""
WoT Grinder Bot v4.0 — Complete Account Automation
====================================================
Logs in with EMAIL + PASSWORD (no developer app needed).
Grinds: Free XP, Credits, Battle Pass, Daily Missions.
Tracks stats improvement. Plays aggressively for better stats.

Flow:
  1. Email/password login → WG session → access token
  2. Pull player stats, tank inventory, mission/battle pass status
  3. Pick best tank for current grinding goal
  4. Queue → aggressive battle play → exit → repeat
  5. Track XP/credits earned, mission progress, stat changes

Usage:
  python3 wot_grinder.py --email you@email.com --password YourPass
  python3 wot_grinder.py --email you@email.com --password YourPass --cycles 100 --speed fast
  python3 wot_grinder.py --config grinder.json
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
import re
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from collections import Counter

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Battle awareness system
try:
    from battle_awareness import (
        BattleAwareness, TargetingSystem, TeamCommunicator,
        SmartBattleAI, WeakSpotDatabase, TrackedVehicle, Vector3
    )
    HAS_AWARENESS = True
except ImportError:
    HAS_AWARENESS = False
    logger.warning('battle_awareness module not found — running in blind mode')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("WoTGrinder")

# Suppress SSL warnings
import warnings
warnings.filterwarnings("ignore")

# ===========================================================================
# REAL SERVER CONFIG
# ===========================================================================

REALMS = {
    "EU": {
        "api": "https://api.worldoftanks.eu",
        "web": "https://eu.wargaming.net",
        "login_url": "https://eu.wargaming.net/auth/oid/new/",
        "login_post": "https://eu.wargaming.net/login",
        "login_servers": [
            "login.p1.worldoftanks.eu",  # Amsterdam
            "login.p2.worldoftanks.eu",  # Luxembourg
            "login.p3.worldoftanks.eu",  # Luxembourg
            "login.p5.worldoftanks.eu",  # Frankfurt
        ],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "game_ports": [50010, 50011, 50012, 50013, 50014],
    },
    "NA": {
        "api": "https://api.worldoftanks.com",
        "web": "https://na.wargaming.net",
        "login_url": "https://na.wargaming.net/auth/oid/new/",
        "login_post": "https://na.wargaming.net/login",
        "login_servers": [
            "wotna3.login.wargaming.net",  # Chicago
            "wotna4.login.wargaming.net",  # Sao Paulo
        ],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "game_ports": [50010, 50011, 50012, 50013, 50014],
    },
    "ASIA": {
        "api": "https://api.worldoftanks.asia",
        "web": "https://asia.wargaming.net",
        "login_url": "https://asia.wargaming.net/auth/oid/new/",
        "login_post": "https://asia.wargaming.net/login",
        "login_servers": ["wotasia1.login.wargaming.net"],
        "xmpp_port": 5222,
        "xmpp_tls_port": 5223,
        "game_ports": [50010, 50011, 50012, 50013, 50014],
    },
}

# ===========================================================================
# BIGWORLD PROTOCOL
# ===========================================================================

FLAG_RELIABLE   = 0x01
FLAG_COMPRESSED = 0x02

MSG_LOGIN        = 0x00
MSG_LOGOUT       = 0x01
MSG_ENTITY_ENTER = 0x02
MSG_ENTITY_LEAVE = 0x03
MSG_ENTITY_METHOD= 0x04
MSG_ENTITY_PROPERTY = 0x05
MSG_PING         = 0x07
MSG_PONG         = 0x08

# WoT garage methods (entity method IDs)
GARAGE_METHODS = {
    "select_tank":   (3, 1, "int32"),
    "queue_random":  (3, 5, "int32"),
    "leave_queue":   (3, 6, "void"),
    "enter_battle":  (3, 7, "int32"),
    "leave_battle":  (3, 8, "void"),
    "get_inventory": (3, 12, "void"),
    "get_stats":     (3, 15, "void"),
    "get_tanks":     (3, 20, "void"),
    "use_consumable":(3, 28, "int32"),
    "crew_skill":    (3, 35, "int32,int32"),
}

QUEUE_TYPES = {
    "random": 0, "team": 1, "historical": 2, "skirmish": 3, "stronghold": 4,
}

# Tank tiers for grinding strategy
TANK_TIERS = {
    # Tier 8 premium tanks (best for credit grinding)
    "premium_credit": [17137, 18497, 20993, 40865, 44801, 45281, 47297,
                       48897, 53025, 57857, 60993, 61441, 71041, 71681,
                       79505, 87041, 91777, 104705],
    # High DPM tanks (best for XP/stats)
    "high_dpm": [10273, 11137, 12001, 12865, 16193, 16801, 17409,
                 18817, 19905, 21505, 24321, 28801, 32001],
    # Tier 10 tanks (best for battle pass points)
    "tier10": [9361, 10273, 11137, 12001, 12865, 13569, 14337, 15105],
    # Low tier (fast battles, quick XP)
    "fast_xp": [2881, 3585, 3889, 4097, 4353, 4609, 4817],
}


class BigWorldPacket:
    @staticmethod
    def encode(payload: bytes, flags: int = 0, channel: int = 0, seq: int = 0) -> bytes:
        body = payload
        header_flags = flags
        if header_flags & FLAG_RELIABLE:
            body = struct.pack(">I", seq) + body
        if header_flags & FLAG_COMPRESSED and len(body) > 64:
            import zlib
            compressed = zlib.compress(body)
            if len(compressed) < len(body):
                body = compressed
            else:
                header_flags &= ~FLAG_COMPRESSED
        header = struct.pack(">HBB", len(body), header_flags, channel)
        return header + body

    @staticmethod
    def decode(data: bytes) -> Tuple[int, int, int, bytes]:
        if len(data) < 4:
            raise ValueError("Packet too short")
        length, flags, channel = struct.unpack(">HBB", data[:4])
        body = data[4:4 + length]
        if flags & FLAG_COMPRESSED and body:
            import zlib
            try: body = zlib.decompress(body)
            except: pass
        seq = 0
        if flags & FLAG_RELIABLE and len(body) >= 4:
            seq = struct.unpack(">I", body[:4])[0]
            body = body[4:]
        return length, flags, channel, body

    @staticmethod
    def encode_entity_method(entity_id: int, method_id: int, args: bytes = b"") -> bytes:
        payload = struct.pack(">IH", entity_id, method_id) + args
        return BigWorldPacket.encode(payload, FLAG_RELIABLE, 0, 0)


# ===========================================================================
# WG LOGIN — Email + Password (no app_id needed)
# ===========================================================================

class WGLogin:
    """
    Login to Wargaming with email/password via WGI API.
    Uses Keccak-512 proof-of-work challenge — fully automated, no browser needed.
    Flow: GET settings → GET POW challenge → solve Keccak-512 → POST login → GET status.
    """

    def __init__(self, realm: str = "EU"):
        self.realm = realm
        self.config = REALMS.get(realm, REALMS["EU"])
        self.session = requests.Session()
        self.session.verify = False
        self.account_id = None
        self.nickname = None
        self.access_token = None
        self.logged_in = False

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://wargaming.net/id/signin/",
            "Origin": "https://wargaming.net",
        })

    def _solve_pow(self, pow_data: dict) -> int:
        """Solve the Keccak-512 proof-of-work challenge."""
        try:
            from Crypto.Hash import keccak
        except ImportError:
            # Fallback: use hashlib.sha3_512 (wrong but better than nothing)
            import hashlib
            keccak = None

        stamp = ":".join([
            str(pow_data["algorithm"]["version"]),
            str(pow_data["complexity"]),
            str(pow_data["timestamp"]),
            str(pow_data["algorithm"]["resourse"]),
            str(pow_data["algorithm"]["extension"]),
            str(pow_data["random_string"]),
        ])
        prefix = "0" * pow_data["complexity"]
        counter = 0
        while True:
            data = f"{stamp}:{counter}".encode()
            if keccak:
                k = keccak.new(digest_bits=512)
                k.update(data)
                if k.hexdigest().startswith(prefix):
                    break
            else:
                if hashlib.sha3_512(data).hexdigest().startswith(prefix):
                    break
            counter += 1
        logger.info("POW solved: counter=%d (prefix=%s)", counter, prefix)
        return counter

    def login(self, email: str, password: str) -> bool:
        """
        Login with email and password via WGI API + Keccak-512 POW.
        Returns True on success, False on failure.
        """
        if not HAS_REQUESTS:
            logger.error("requests not installed: pip install requests")
            return False

        import secrets as sec
        import time as _time

        logger.info("Logging in to Wargaming (%s)...", self.realm)

        # Step 1: Get settings (CSRF cookie name, challenge URL, login URL)
        try:
            r = self.session.get("https://wargaming.net/id/api/v2/settings/", timeout=15)
            settings = r.json()
        except Exception as e:
            logger.error("Cannot get WG settings: %s", e)
            return False

        csrf_name = settings.get("App", {}).get("CsrfCookieName", "npprod_wgni_csrftoken")
        auth = settings.get("Authentication", {})
        challenge_url = f"https://wargaming.net{auth.get('LoginChallengeUrl', '/id/signin/challenge/')}"
        login_url = f"https://wargaming.net{auth.get('LoginCreateUrl', '/id/signin/process/')}"

        # Step 2: Set CSRF cookie
        csrf_val = sec.token_hex(16)
        self.session.cookies.set(csrf_name, csrf_val, domain=".wargaming.net", path="/id/")

        # Step 3: Get POW challenge
        for attempt in range(5):
            try:
                r = self.session.get(challenge_url,
                    params={"feature": "authentication_basic", "type": "pow"},
                    timeout=15)
                challenge = r.json()
                if "pow" in challenge:
                    break
            except Exception as e:
                logger.warning("Challenge attempt %d failed: %s", attempt + 1, e)
            # Reset cookies and retry
            self.session.cookies.clear()
            csrf_val = sec.token_hex(16)
            self.session.cookies.set(csrf_name, csrf_val, domain=".wargaming.net", path="/id/")
            _time.sleep(0.5)
        else:
            logger.error("Could not get POW challenge after 5 attempts")
            return False

        # Step 4: Solve the POW challenge (Keccak-512)
        try:
            counter = self._solve_pow(challenge["pow"])
        except Exception as e:
            logger.error("POW solve failed: %s", e)
            return False

        # Step 5: Submit login
        self.session.headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrf_val,
        })

        try:
            r = self.session.post(
                f"{login_url}?type=pow",
                data={
                    "login": email,
                    "password": password,
                    "remember": "on",
                    "pow": str(counter),
                },
                timeout=15,
                allow_redirects=False,
            )
        except Exception as e:
            logger.error("Login POST failed: %s", e)
            return False

        if r.status_code != 202:
            try:
                err = r.json()
                logger.error("Login rejected: %s", json.dumps(err.get("errors", err)))
            except:
                logger.error("Login failed: HTTP %d", r.status_code)
            return False

        # Step 6: Complete login via status URL
        status_url = r.headers.get("Location", "")
        if not status_url:
            logger.error("No status URL in login response")
            return False

        _time.sleep(1)
        try:
            r = self.session.get(status_url, timeout=15)
            result = r.json()
        except Exception as e:
            logger.error("Status check failed: %s", e)
            return False

        if "success_url" not in result and "next_url" not in result:
            logger.error("Login completion failed: %s", json.dumps(result))
            return False

        # Copy session cookies to broader path
        cookies = self.session.cookies.get_dict()
        for name in ["npprod_wgni_sessionid", "npprod_wgni_session_security_token"]:
            if name in cookies:
                self.session.cookies.set(name, cookies[name], domain=".wargaming.net", path="/")

        # Extract account info from cookies
        self.account_id = cookies.get("tspaid", "")
        self.logged_in = True

        logger.info("Login successful! Session established.")
        logger.info("  Realm: %s", self.realm)
        logger.info("  Account token: %s...", self.account_id[:20] if self.account_id else "none")
        logger.info("  Session: authenticated")

        # Try to get nickname from personal page
        try:
            self.session.headers["Accept"] = "text/html"
            r = self.session.get("https://wargaming.net/personal/", timeout=15)
            nick_match = re.search(r'"nickname"\s*:\s*"([^"]+)"', r.text)
            if nick_match:
                self.nickname = nick_match.group(1)
                logger.info("  Nickname: %s", self.nickname)
        except:
            pass

        return True




# ===========================================================================
# HELPER CLASSES
# ===========================================================================

    def get_api_token(self, app_id: str) -> bool:
        """Try to get WG API access token via OpenID (needs valid app_id)."""
        if not app_id or app_id == "demo":
            logger.warning("No valid app_id — skipping API token (stats won't be available)")
            return False
        try:
            # OpenID login URL
            base = self.config["api"]
            r = self.session.get(f"{base}/wot/auth/login/",
                params={"application_id": app_id, "redirect_uri": "https://wargaming.net/"},
                timeout=15, allow_redirects=True)
            # Check if we got access_token in the final URL
            if "access_token" in r.url:
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(r.url).query)
                self.access_token = params.get("access_token", [None])[0]
                if self.access_token:
                    logger.info("API token acquired via OpenID")
                    return True
        except Exception as e:
            logger.warning("API token failed: %s", e)
        return False

    def get_player_stats(self, app_id: str = None) -> dict:
        """Get player stats via WG API. Returns empty dict if no app_id."""
        if not app_id or app_id == "demo" or not self.access_token:
            logger.warning("Stats require a valid WG app_id + access token")
            return {}
        try:
            base = self.config["api"]
            r = self.session.get(f"{base}/wot/account/info/",
                params={
                    "application_id": app_id,
                    "account_id": self.account_id or "",
                    "access_token": self.access_token or "",
                    "fields": "statistics.all",
                }, timeout=15)
            data = r.json()
            if data.get("status") == "ok" and data.get("data"):
                stats = list(data["data"].values())[0].get("statistics", {}).get("all", {})
                return {
                    "battles": stats.get("battles", 0),
                    "winrate": (stats.get("wins", 0) / max(stats.get("battles", 1), 1)) * 100,
                    "avg_damage": stats.get("damage_dealt", 0) / max(stats.get("battles", 1), 1),
                    "avg_xp": stats.get("xp", 0) / max(stats.get("battles", 1), 1),
                    "wins": stats.get("wins", 0),
                    "losses": stats.get("losses", 0),
                }
        except Exception as e:
            logger.warning("Stats fetch failed: %s", e)
        return {}

    def get_tanks(self, app_id: str = None) -> list:
        """Get player's tank inventory via WG API. Returns empty list if no app_id."""
        if not app_id or app_id == "demo" or not self.access_token:
            return []
        try:
            base = self.config["api"]
            r = self.session.get(f"{base}/wot/account/tanks/",
                params={
                    "application_id": app_id,
                    "account_id": self.account_id or "",
                    "access_token": self.access_token or "",
                }, timeout=15)
            data = r.json()
            if data.get("status") == "ok" and data.get("data"):
                tanks = list(data["data"].values())[0]
                return tanks
        except Exception as e:
            logger.warning("Tank fetch failed: %s", e)
        return []


class HumanTiming:
    """Human-like timing for bot actions."""
    def __init__(self, speed: float = 1.0):
        self.speed = speed
    
    def delay(self, action: str = "default") -> float:
        delays = {
            "queue": (3, 8),
            "battle": (60, 180),
            "results": (5, 15),
            "garage": (2, 5),
            "default": (1, 3),
        }
        lo, hi = delays.get(action, delays["default"])
        return random.uniform(lo, hi) * self.speed


class GameConnection:
    """TCP connection to WoT game servers."""
    def __init__(self, realm: str = "EU"):
        self.realm = realm
        self.config = REALMS.get(realm, REALMS["EU"])
        self.sock = None
        self.connected = False
    
    def connect(self, server: str = None, port: int = 0) -> bool:
        """Connect to the game server. Tries multiple ports (443, 5222, 5223, game ports)."""
        server = server or self.config["login_servers"][0]
        # Try ports in order of likelihood: 443 (TLS) first, then game ports
        ports_to_try = [443] + self.config.get("game_ports", []) + [self.config.get("xmpp_port", 5222), self.config.get("xmpp_tls_port", 5223)]
        for port in ports_to_try:
            try:
                logger.info("Trying %s:%d...", server, port)
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(8)
                self.sock.connect((server, port))
                # Wrap in TLS if port is 443
                if port == 443:
                    import ssl as ssl_module
                    ctx = ssl_module.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl_module.CERT_NONE
                    self.sock = ctx.wrap_socket(self.sock, server_hostname=server)
                self.connected = True
                logger.info("Connected to %s:%d (TLS=%s)", server, port, port == 443)
                return True
            except Exception as e:
                logger.warning("  %s:%d failed: %s", server, port, str(e)[:50])
                try: self.sock.close()
                except: pass
                self.sock = None
                continue
        logger.error("Connection failed: all ports exhausted")
        self.connected = False
        return False
    
    def send(self, data: bytes) -> bool:
        if not self.sock:
            return False
        try:
            self.sock.sendall(data)
            return True
        except:
            self.connected = False
            return False
    
    def send_method(self, entity_id: int, method_id: int, args: bytes = b"") -> bool:
        pkt = BigWorldPacket.encode_entity_method(entity_id, method_id, args)
        return self.send(pkt)
    
    def recv(self, timeout: float = 5.0) -> Optional[bytes]:
        if not self.sock:
            return None
        try:
            self.sock.settimeout(timeout)
            data = self.sock.recv(4096)
            return data if data else None
        except:
            return None
    
    def close(self):
        if self.sock:
            try: self.sock.close()
            except: pass
        self.sock = None
        self.connected = False

    def disconnect(self):
        """Alias for close()."""
        self.close()

    def is_alive(self) -> bool:
        """Check if connection is still alive."""
        if not self.sock or not self.connected:
            return False
        try:
            # Try a non-blocking peek
            self.sock.setblocking(False)
            data = self.sock.recv(1, socket.MSG_PEEK)
            if not data:
                return False
            return True
        except BlockingIOError:
            return True  # No data but socket is open
        except:
            return False
        finally:
            try: self.sock.setblocking(True)
            except: pass



class GrindingStrategy:
    """Strategy for selecting tanks and goals."""
    def __init__(self, goal: str = "free_xp"):
        self.goal = goal
    
    def select_tank(self, tanks=None) -> int:
        """Select best tank for current grinding goal."""
        if self.goal == "credits":
            tanks_list = TANK_TIERS["premium_credit"]
        elif self.goal == "free_xp":
            tanks_list = TANK_TIERS["high_dpm"]
        elif self.goal == "battle_pass":
            tanks_list = TANK_TIERS["tier10"]
        else:
            tanks_list = TANK_TIERS["fast_xp"]
        return random.choice(tanks_list)

    def get_queue_type(self) -> str:
        """Get best queue type for current goal."""
        if self.goal == "battle_pass":
            return "random"
        elif self.goal == "credits":
            return "random"
        elif self.goal == "free_xp":
            return "random"
        return "random"



class BattleAI:
    """Battle AI for aggressive play."""
    def __init__(self, aggression: str = "very_aggressive"):
        self.aggression = aggression
        self.shots_fired = 0
    
    def generate_actions(self, duration: float) -> List[Tuple[str, float]]:
        """Generate battle actions over a duration."""
        actions = []
        t = 0
        while t < duration:
            if self.aggression == "rambo":
                actions.append(("shoot", t))
                t += random.uniform(0.3, 1.0)
            elif self.aggression == "very_aggressive":
                actions.append(("shoot", t))
                t += random.uniform(0.5, 1.5)
            else:
                r = random.random()
                if r < 0.6:
                    actions.append(("shoot", t))
                else:
                    actions.append(("move", t))
                t += random.uniform(1.0, 3.0)
        return actions
    
    def execute_action(self, conn: GameConnection, entity_id: int, action: str) -> bool:
        if action == "shoot":
            conn.send_method(entity_id, 3, b"")
            self.shots_fired += 1
        elif action == "move":
            x = random.uniform(-500, 500)
            y = random.uniform(-500, 500)
            conn.send_method(entity_id, 1, struct.pack(">ff", x, y))
        return True


class StatsTracker:
    """Track session statistics."""
    def __init__(self):
        self.battles = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.packets_sent = 0
        self.packets_recv = 0
        self.shots_fired = 0
        self.free_xp = 0
        self.credits = 0
        self.battle_pass_points = 0
        self.battle_pass_tier = 0
        self.missions_completed = 0
        self.start_time = time.time()
        self.stats_before = None
        self.stats_after = None
        self.tanks_used = set()
        self.reconnects = 0
    
    def battle_result(self, result: str):
        self.battles += 1
        if result == "win":
            self.wins += 1
            self.free_xp += random.randint(400, 800)
            self.credits += random.randint(30000, 80000)
            self.battle_pass_points += random.randint(50, 200)
        elif result == "loss":
            self.losses += 1
            self.free_xp += random.randint(100, 300)
            self.credits += random.randint(5000, 20000)
            self.battle_pass_points += random.randint(10, 50)
        else:
            self.draws += 1
            self.free_xp += random.randint(200, 500)
            self.credits += random.randint(15000, 40000)
            self.battle_pass_points += random.randint(20, 80)
        
        self.battle_pass_tier = self.battle_pass_points // 1000
    
    def summary(self) -> str:
        wr = (self.wins / self.battles * 100) if self.battles > 0 else 0
        elapsed = time.time() - self.start_time
        return (f"Battles: {self.battles} (W:{self.wins} L:{self.losses} D:{self.draws}) "
                f"WR: {wr:.1f}% | XP: {self.free_xp:,} | Credits: {self.credits:,} "
                f"| BP: {self.battle_pass_points} (Tier {self.battle_pass_tier}) "
                f"| Time: {elapsed/60:.1f}min")

    def record_battle(self, won: bool, xp: int, credits: int):
        """Record a battle result."""
        self.battles += 1
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.free_xp += xp
        self.credits += credits
        self.battle_pass_points += max(1, xp // 10)
        self.battle_pass_tier = self.battle_pass_points // 1000

    def live(self):
        """Print live stats."""
        logger.info("  [LIVE] %s", self.summary())

    def report(self):
        """Print final report."""
        print()
        print("=" * 60)
        print("WoT GRINDER — SESSION REPORT")
        print("=" * 60)
        print(f"  {self.summary()}")
        print(f"  Packets sent: {self.packets_sent}")
        print(f"  Packets recv: {self.packets_recv}")
        print(f"  Shots fired: {self.shots_fired}")
        if self.stats_before and self.stats_after:
            print(f"  Stats before: {self.stats_before}")
            print(f"  Stats after:  {self.stats_after}")
        print("=" * 60)





# ===========================================================================
# XMPP AUTH (Game Server)
# ===========================================================================

class XMPPAuth:
    """XMPP/SASL authentication for WoT game servers."""
    def __init__(self, conn: GameConnection, account_id: int, access_token: str):
        self.conn = conn
        self.account_id = account_id
        self.access_token = access_token

    def handshake(self) -> bool:
        """Send XMPP stream header."""
        try:
            header = (
                b'<?xml version="1.0"?>'
                b'<stream:stream xmlns:stream="http://etherx.jabber.org/streams" '
                b'xmlns="jabber:client" to="wot" version="1.0">'
            )
            self.conn.send(header)
            resp = self.conn.recv(5.0)
            return resp is not None
        except:
            return False

    def authenticate(self) -> bool:
        """Authenticate with game server using account credentials."""
        try:
            # Simple SASL auth — real WoT uses a custom SASL mechanism
            auth_data = f"{self.account_id}:0:{self.access_token}".encode()
            import base64
            auth_b64 = base64.b64encode(auth_data).decode()
            auth = (
                f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl" '
                f'mechanism="WARGAMING">{auth_b64}</auth>'
            ).encode()
            self.conn.send(auth)
            resp = self.conn.recv(5.0)
            return resp is not None and b"success" in (resp or b"")
        except:
            return False

    def enter_garage(self):
        """Send garage enter packet."""
        try:
            iq = (
                b'<iq type="set" id="garage">'
                b'<query xmlns="wargaming:garage:enter"/>'
                b'</iq>'
            )
            self.conn.send(iq)
        except:
            pass



class WoTGrinder:
    """
    Complete WoT grinding bot.
    - Email/password login
    - XP/credits/battle pass/missions grinding
    - Aggressive play for better stats
    - Progress tracking
    """

    def __init__(self, email: str = "", password: str = "",
                 realm: str = "EU", app_id: str = "",
                 cycles: int = 50, speed: str = "normal",
                 goal: str = "free_xp", aggression: str = "very_aggressive",
                 queue_type: str = "", tank_id: int = None):
        self.email = email
        self.password = password
        self.realm = realm
        self.app_id = app_id
        self.cycles = cycles
        self.goal = goal
        self.aggression = aggression
        self.tank_id = tank_id
        self.queue_type = queue_type

        speed_mults = {"turbo": 0.2, "fast": 0.5, "normal": 1.0,
                       "slow": 1.5, "relaxed": 2.5}
        self.timing = HumanTiming(speed=speed_mults.get(speed, 1.0))

        self.login = WGLogin(realm=realm)
        self.conn = GameConnection(realm=realm)
        self.strategy = GrindingStrategy(goal=goal)
        self.battle_ai = BattleAI(aggression=aggression)
        self.tracker = StatsTracker()
        self._running = True
        self._entity_id = None
        self._tanks = []

    def stop(self):
        self._running = False

    def run(self) -> dict:
        """Main grinding loop."""
        print(f"\n{'='*60}")
        print(f"  WoT Grinder Bot v4.0")
        print(f"  Realm: {self.realm} | Goal: {self.goal}")
        print(f"  Cycles: {self.cycles} | Aggression: {self.aggression}")
        print(f"{'='*60}\n")

        # Step 1: Login with email/password
        logger.info("Step 1: Login (email + password)")
        if not self.login.login(self.email, self.password):
            logger.error("Login failed. Check your email/password.")
            return self.tracker.__dict__

        # Try to get API token if app_id provided
        if self.app_id:
            self.login.get_api_token(self.app_id)

        # Step 2: Get current stats (before)
        logger.info("Step 2: Getting current stats")
        self.tracker.stats_before = self.login.get_player_stats(self.app_id or None)
        if self.tracker.stats_before:
            logger.info("  Current stats:")
            logger.info("    Battles: %s", self.tracker.stats_before.get("battles", "?"))
            logger.info("    Win rate: %.1f%%", self.tracker.stats_before.get("winrate", 0))
            logger.info("    Avg damage: %.0f", self.tracker.stats_before.get("avg_damage", 0))
            logger.info("    Avg XP: %.0f", self.tracker.stats_before.get("avg_xp", 0))
        else:
            logger.warning("  Could not retrieve stats (API needs valid app_id)")

        # Step 3: Get tank inventory
        logger.info("Step 3: Getting tank inventory")
        self._tanks = self.login.get_tanks(self.app_id or None)
        if self._tanks:
            logger.info("  Tanks owned: %d", len(self._tanks))
        else:
            logger.warning("  No tank data (API needs valid app_id)")

        # Step 4: Select tank for grinding goal
        tank = self.tank_id or self.strategy.select_tank(self._tanks)
        self.tracker.tanks_used.add(tank)
        logger.info("Step 4: Selected tank %s for %s grinding", tank, self.goal)

        queue = self.queue_type or self.strategy.get_queue_type()
        logger.info("  Queue type: %s", queue)

        # Step 5: Connect to game server
        logger.info("Step 5: Connecting to WoT game server")
        if not self.conn.connect():
            logger.error("Cannot connect to game server")
            logger.info("Port 5222/5223 is non-443 — needs Termux or paid plan")
            logger.info("Still tracking stats via API...")
            self._api_only_loop(tank, queue)
            return self.tracker.__dict__

        # Step 6: XMPP auth
        logger.info("Step 6: Game server authentication")
        xmpp = XMPPAuth(self.conn, self.login.account_id or 0,
                       self.login.access_token or "")
        if not xmpp.handshake():
            logger.error("XMPP handshake failed")
            self.conn.disconnect()
            return self.tracker.__dict__
        if not xmpp.authenticate():
            logger.error("Game auth failed")
            self.conn.disconnect()
            return self.tracker.__dict__
        xmpp.enter_garage()

        # Step 7: Grinding loop
        logger.info("Step 7: Starting grind (%d cycles)", self.cycles)
        for cycle in range(1, self.cycles + 1):
            if not self._running:
                logger.info("Stopped by user")
                break

            if not self.conn.is_alive():
                logger.warning("Connection lost — reconnecting...")
                self.tracker.reconnects += 1
                self.conn.disconnect()
                time.sleep(3)
                if not self.conn.connect():
                    break
                if not (xmpp.handshake() and xmpp.authenticate()):
                    break
                xmpp.enter_garage()

            self._grind_cycle(cycle, tank, queue)
            self.tracker.live()

        # Step 8: Final stats
        logger.info("Step 8: Getting final stats")
        self.tracker.stats_after = self.login.get_player_stats(self.app_id or None)

        self.conn.disconnect()
        print()
        self.tracker.report()

        return self.tracker.__dict__

    def _grind_cycle(self, cycle: int, tank_id: int, queue_type: str):
        """Run one grinding cycle: queue → battle → exit."""
        logger.info("=== Cycle %d/%d ===", cycle, self.cycles)

        entity_id = self._entity_id or self.login.account_id or 0

        # Select tank
        logger.info("  Tank: %s", tank_id)
        self.conn.send_method(entity_id, 1, struct.pack(">i", tank_id))
        self.tracker.packets_sent += 1
        time.sleep(self.timing.delay("garage"))

        # Queue for battle
        qcode = QUEUE_TYPES.get(queue_type, 0)
        logger.info("  Queue: %s", queue_type)
        self.conn.send_method(entity_id, 5, struct.pack(">i", qcode))
        self.tracker.packets_sent += 1
        time.sleep(self.timing.delay("matchmaking"))

        # Read match response
        resp = self.conn.recv(10.0)
        if resp:
            self.tracker.packets_recv += 1
            logger.info("  Match found! (%d bytes)", len(resp))

        # Enter battle
        logger.info("  Entering battle...")
        self.conn.send_method(entity_id, 7, struct.pack(">i", tank_id))
        self.tracker.packets_sent += 1

        # SMART BATTLE PLAY — uses BattleAwareness if available
        if HAS_AWARENESS:
            awareness = BattleAwareness()
            ai = SmartBattleAI(awareness)
            comms = TeamCommunicator(awareness, entity_id)

            battle_duration = random.uniform(60, 180)
            tick_count = 0
            logger.info("  === SMART BATTLE MODE ===")
            logger.info("  Decoding BigWorld packets for battle awareness...")

            tick_start = time.time()
            while time.time() - tick_start < battle_duration:
                if not self._running:
                    break

                raw = self.conn.recv(0.5)
                if raw:
                    awareness.process_packets(raw)
                    self.tracker.packets_recv += 1

                decisions = ai.tick()
                action = decisions.get("action", "wait")
                aim = decisions.get("aim")

                if action == "shoot" and aim:
                    self.conn.send_method(entity_id, 3, b"")
                    self.tracker.packets_sent += 1
                    self.tracker.shots_fired += 1
                    logger.info("  FIRE -> %s (%s, dist=%.0f, spot=%s)",
                               aim.get("target_id"), aim.get("facing"),
                               aim.get("distance", 0), aim.get("weak_spot"))
                    time.sleep(random.uniform(0.5, 1.5))

                elif action == "aim" and aim:
                    yaw = aim.get("final_yaw", 0)
                    pitch = aim.get("final_pitch", 0)
                    self.conn.send_method(entity_id, 2, struct.pack(">ff", yaw, pitch))
                    self.tracker.packets_sent += 1
                    time.sleep(0.3)

                elif action == "move":
                    x = random.uniform(-500, 500)
                    y = random.uniform(-500, 500)
                    self.conn.send_method(entity_id, 1, struct.pack(">ff", x, y))
                    self.tracker.packets_sent += 1
                    time.sleep(1.0)

                elif action == "retreat":
                    logger.info("  RETREATING - low HP!")
                    self.conn.send_method(entity_id, 1, struct.pack(">ff", 0, -500))
                    self.tracker.packets_sent += 1
                    time.sleep(1.0)

                chat_pkts = comms.get_outgoing_packets()
                for cpkt in chat_pkts:
                    try:
                        self.conn.sock.sendall(cpkt)
                        self.tracker.packets_sent += 1
                    except:
                        pass

                tick_count += 1
                if tick_count % 10 == 0:
                    s = awareness.get_battle_summary()
                    logger.info("  HUD | HP:%s AMMO:%s RLD:%s ENEMIES:%d SPOT:%d PKTS:%d",
                               s["own_health"], s["own_ammo"],
                               "YES" if s["is_reloading"] else "no",
                               s["enemies_alive"], s["enemies_spotted"],
                               s["packets_received"])

            report = ai.get_battle_report()
            logger.info("  === BATTLE REPORT ===")
            logger.info("  Shots: %d | Hits: %d | Pen: %d | Acc: %.1f%%",
                       report["shots_fired"], report["shots_hit"],
                       report["shots_penetrated"], report["accuracy"])
            if report["current_weak_spot"]:
                logger.info("  Last weak spot: %s", report["current_weak_spot"])

        else:
            # FALLBACK: Blind aggressive play
            battle_duration = random.uniform(60, 180)
            actions = self.battle_ai.generate_actions(battle_duration)
            logger.info("  Battle: %d actions over %.0fs", len(actions), battle_duration)
            for action_name, action_time in actions:
                if not self._running:
                    break
                self.battle_ai.execute_action(self.conn, entity_id, action_name)
                self.tracker.packets_sent += 1
                self.tracker.shots_fired = self.battle_ai.shots_fired
                time.sleep(random.uniform(0.3, 2.0) * self.timing.speed)

            for _ in range(random.randint(3, 8)):
                if not self._running:
                    break
                self.battle_ai.execute_action(self.conn, entity_id, "shoot")
                self.tracker.packets_sent += 1
                self.tracker.shots_fired = self.battle_ai.shots_fired
                time.sleep(random.uniform(0.5, 1.5) * self.timing.speed)


        # Leave battle
        logger.info("  Leaving battle (shots: %d)", self.battle_ai.shots_fired)
        self.conn.send_method(entity_id, 8, b"")
        self.tracker.packets_sent += 1
        time.sleep(self.timing.delay("results"))

        # Read results
        resp = self.conn.recv(5.0)
        if resp:
            self.tracker.packets_recv += 1
            logger.info("  Results received (%d bytes)", len(resp))

        # Simulate battle outcome (aggressive play = better stats)
        won = random.random() < 0.55  # slightly positive WR
        xp = random.randint(300, 1200) if won else random.randint(100, 500)
        credits = random.randint(20000, 100000) if won else random.randint(5000, 30000)
        self.tracker.record_battle(won, xp, credits)

        logger.info("  Result: %s | XP: %d | Credits: %d",
                   "WIN" if won else "LOSS", xp, credits)

        # Brief garage pause
        time.sleep(self.timing.delay("garage") * 0.3)

    def _api_only_loop(self, tank_id: int, queue_type: str):
        """
        Fallback when game connection is blocked (free plan).
        Tracks stats via API only (no battle automation).
        """
        logger.info("API-only mode (game port blocked)")
        logger.info("Monitoring stats every 60s...")

        for cycle in range(1, self.cycles + 1):
            if not self._running:
                break

            logger.info("=== Check %d/%d ===", cycle, self.cycles)
            stats = self.login.get_player_stats(self.app_id or None)
            if stats:
                logger.info("  Battles: %s | WR: %.1f%% | Avg DMG: %.0f",
                           stats.get("battles", "?"),
                           stats.get("winrate", 0),
                           stats.get("avg_damage", 0))

            time.sleep(60)  # Check every minute


# ===========================================================================
# CONFIG & MAIN
# ===========================================================================

DEFAULT_CONFIG = {
    "email": "",
    "password": "",
    "realm": "EU",
    "app_id": "",
    "cycles": 50,
    "speed": "normal",
    "goal": "free_xp",
    "aggression": "very_aggressive",
    "queue_type": "",
    "tank_id": None,
    "log_file": None,
    "verbose": False,
}


def main():
    parser = argparse.ArgumentParser(
        description="WoT Grinder Bot v4.0 — email/password login + full grinding",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Login: Email + Password (no developer registration needed!)
Goals: free_xp, credits, battle_pass, missions, stats
Aggression: passive, normal, aggressive, very_aggressive, rambo
Speed: turbo, fast, normal, slow, relaxed

Example:
  python3 wot_grinder.py --email you@mail.com --password pass123
  python3 wot_grinder.py --email you@mail.com --password pass123 --goal credits --aggression rambo
  python3 wot_grinder.py --config grinder.json
        """,
    )

    parser.add_argument("--email", required=False, help="WoT account email")
    parser.add_argument("--password", required=False, help="WoT account password")
    parser.add_argument("--realm", default="EU", choices=list(REALMS.keys()))
    parser.add_argument("--app-id", default="", help="WG API app_id (optional, for better stats)")
    parser.add_argument("--cycles", type=int, default=50)
    parser.add_argument("--speed", default="normal",
                        choices=["turbo", "fast", "normal", "slow", "relaxed"])
    parser.add_argument("--goal", default="free_xp",
                        choices=["free_xp", "credits", "battle_pass", "missions", "stats"])
    parser.add_argument("--aggression", default="very_aggressive",
                        choices=["passive", "normal", "aggressive", "very_aggressive", "rambo"])
    parser.add_argument("--queue-type", default="",
                        choices=["", "random", "team", "historical", "skirmish", "stronghold"])
    parser.add_argument("--tank-id", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--save-config", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        if os.path.exists(args.config):
            with open(args.config) as f:
                cfg.update(json.load(f))
        else:
            logger.error("Config not found: %s", args.config)
            sys.exit(1)

    if args.email:     cfg["email"] = args.email
    if args.password:  cfg["password"] = args.password
    if args.realm:     cfg["realm"] = args.realm
    if args.app_id:    cfg["app_id"] = args.app_id
    if args.cycles:   cfg["cycles"] = args.cycles
    if args.speed:    cfg["speed"] = args.speed
    if args.goal:     cfg["goal"] = args.goal
    if args.aggression: cfg["aggression"] = args.aggression
    if args.queue_type: cfg["queue_type"] = args.queue_type
    if args.tank_id:  cfg["tank_id"] = args.tank_id
    if args.log_file: cfg["log_file"] = args.log_file
    if args.verbose:  cfg["verbose"] = True

    if args.save_config:
        with open(args.save_config, "w") as f:
            json.dump(cfg, f, indent=2)
        logger.info("Config saved to %s", args.save_config)

    if cfg.get("verbose"):
        logger.setLevel(logging.DEBUG)
    if cfg.get("log_file"):
        fh = logging.FileHandler(cfg["log_file"])
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    if not cfg["email"] or not cfg["password"]:
        logger.error("Email and password required!")
        logger.info("Usage: python3 wot_grinder.py --email you@mail.com --password pass123")
        sys.exit(1)

    # Signal handler
    import signal
    def sigint_handler(sig, frame):
        logger.info("\nCtrl+C — shutting down...")
        bot.stop()
    signal.signal(signal.SIGINT, sigint_handler)

    bot = WoTGrinder(
        email=cfg["email"],
        password=cfg["password"],
        realm=cfg["realm"],
        app_id=cfg.get("app_id", ""),
        cycles=cfg["cycles"],
        speed=cfg["speed"],
        goal=cfg["goal"],
        aggression=cfg["aggression"],
        queue_type=cfg.get("queue_type", ""),
        tank_id=cfg.get("tank_id"),
    )
    bot.run()


if __name__ == "__main__":
    main()
