#!/usr/bin/env python3
"""
WoT Advanced Bot — v2.0
========================
World of Tanks automation bot with human-like timing.
Auto-reconnects, reads server responses, tracks stats, and handles failures gracefully.

Usage:
  python3 wot_advanced_bot.py --host 127.0.0.1 --port 5222 --cycles 50
  python3 wot_advanced_bot.py --profile ghost --speed fast --cycles 200
  python3 wot_advanced_bot.py --profile chameleon --log-file bot.log
  python3 wot_advanced_bot.py --profile platoon --sessions 5 --cycles 100
"""

import socket
import struct
import threading
import time
import json
import math
import random
import logging
import argparse
import signal
import sys
import os
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from collections import defaultdict, Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("WoTBot")


# ===========================================================================
# PROTOCOL — Opcodes & State Machine
# ===========================================================================

def _op(opcode: int) -> bytes:
    """Pack a 2-byte little-endian opcode + 2-byte padding."""
    return struct.pack("<H", opcode) + b"\x00\x00"

TRANSITIONS = {
    (0, 0x0001): 1, (1, 0x0002): 2, (2, 0x0010): 3, (2, 0x0011): 2,
    (3, 0x0020): 3, (3, 0x0021): 3, (3, 0x0022): 3, (3, 0x0023): 3,
    (3, 0x0024): 3, (3, 0x0025): 3, (3, 0x0030): 4, (4, 0x0040): 5,
    (5, 0x0050): 5, (5, 0x0051): 5, (5, 0x0052): 5, (5, 0x005F): 6,
    (6, 0x0060): 3, (3, 0x00FF): 7,
}

STATE_NAMES = {
    0: "DISCONNECTED", 1: "XMPP_HANDSHAKE", 2: "WG_AUTH", 3: "GARAGE",
    4: "MATCHMAKING", 5: "BATTLE", 6: "BATTLE_RESULTS", 7: "GAME_HANDOVER",
}

OPCODE_NAMES = {
    0x0001: "XMPP_CONNECT",   0x0002: "WG_LOGIN",      0x0010: "ENTER_GARAGE",
    0x0011: "TOKEN_REFRESH",   0x0020: "TANK_SELECT",    0x0021: "CREW_VIEW",
    0x0022: "STATS_VIEW",      0x0023: "MISSIONS_VIEW",  0x0024: "EQUIP_CHANGE",
    0x0025: "TECH_TREE",       0x0030: "QUEUE_MATCH",    0x0040: "MATCH_FOUND",
    0x0050: "MOVE",            0x0051: "AIM",            0x0052: "SHOOT",
    0x005F: "BATTLE_END",      0x0060: "RESULTS_VIEW",   0x00FF: "DISCONNECT",
}


# ===========================================================================
# SPEED PRESETS
# ===========================================================================

SPEED_PRESETS = {
    # name:    multiplier (lower = faster)
    "turbo":   0.2,
    "fast":    0.5,
    "normal":  1.0,
    "slow":    1.5,
    "relaxed": 2.5,
}


# ===========================================================================
# TIMING ENGINE
# ===========================================================================

class HumanTimingModel:
    """Log-normal timing with fatigue drift and AFK pauses."""

    PHASE_PROFILES = {
        "auth":        {"dist": "lognormal", "mu": 1.5, "sigma": 0.5},
        "garage":      {"dist": "lognormal", "mu": 5.0, "sigma": 2.5},
        "matchmaking": {"dist": "uniform",   "mu": 15.0, "sigma": 8.0},
        "battle":      {"dist": "lognormal", "mu": 1.2, "sigma": 0.6},
        "results":     {"dist": "lognormal", "mu": 10.0, "sigma": 5.0},
        "disconnect":  {"dist": "lognormal", "mu": 2.0, "sigma": 1.0},
    }

    def __init__(self, seed: Optional[int] = None, speed_mult: float = 1.0):
        self.rng = random.Random(seed)
        self._drift_base = 0.0
        self._packet_count = 0
        self._speed_mult = max(0.1, speed_mult)

    def _lognormal(self, mu: float, sigma: float) -> float:
        z = self.rng.gauss(0, 1)
        return math.exp(mu + sigma * z)

    def delay(self, phase: str) -> float:
        profile = self.PHASE_PROFILES.get(phase, self.PHASE_PROFILES["garage"])
        self._packet_count += 1

        # Fatigue drift
        self._drift_base += self.rng.gauss(0, 0.01)
        self._drift_base = max(-0.3, min(0.3, self._drift_base))

        if profile["dist"] == "lognormal":
            d = self._lognormal(math.log(profile["mu"]), profile["sigma"] / profile["mu"])
        else:
            d = self.rng.uniform(profile["mu"] - profile["sigma"],
                                 profile["mu"] + profile["sigma"])

        d += self._drift_base
        d = max(0.1, d)

        # Occasional AFK pause (1% chance)
        if self.rng.random() < 0.01:
            d += self.rng.uniform(5.0, 15.0)

        # Apply speed multiplier
        d *= self._speed_mult
        d = min(d, 60.0)
        return d

    def reset(self):
        self._drift_base = 0.0
        self._packet_count = 0


class AdaptiveTimingModel:
    """RTT-calibrated timing — jitter adapts to network conditions."""

    def __init__(self, seed: Optional[int] = None, speed_mult: float = 1.0):
        self.rng = random.Random(seed)
        self._rtt_history: list = []
        self._current_target_sigma = 0.3
        self._speed_mult = max(0.1, speed_mult)

    def _measure_rtt(self) -> float:
        return self.rng.uniform(0.02, 0.08)

    def delay(self, phase: str) -> float:
        rtt = self._measure_rtt()
        self._rtt_history.append(rtt)

        if len(self._rtt_history) > 5:
            recent = self._rtt_history[-10:]
            mu = sum(recent) / len(recent)
            var = sum((r - mu) ** 2 for r in recent) / len(recent)
            network_jitter = math.sqrt(var)
        else:
            network_jitter = 0.03

        target = max(0.15, network_jitter * 3)
        self._current_target_sigma = 0.85 * self._current_target_sigma + 0.15 * target

        base_delays = {
            "auth": 2.0, "garage": 5.0, "matchmaking": 15.0,
            "battle": 1.5, "results": 8.0, "disconnect": 2.0,
        }
        base = base_delays.get(phase, 3.0)
        d = self.rng.gauss(base, self._current_target_sigma)
        d *= self._speed_mult
        return max(0.1, min(d, 60.0))


# ===========================================================================
# OPCODE ENTROPY
# ===========================================================================

class OpcodeEntropyEngine:
    """Randomizes opcode sequences within valid protocol states."""

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)

    def garage_sequence(self, length: int) -> List[int]:
        base_ops = [0x0020, 0x0021, 0x0022, 0x0023, 0x0024, 0x0025]
        sequence = []
        for _ in range(length):
            if self.rng.random() < 0.2 and sequence:
                sequence.append(sequence[-1])  # misclick
            else:
                sequence.append(self.rng.choice(base_ops))
        return sequence

    def battle_sequence(self, length: int) -> List[int]:
        base_ops = [0x0050, 0x0051, 0x0052]
        weights = [0.5, 0.3, 0.2]  # move > aim > shoot
        return [self.rng.choices(base_ops, weights=weights, k=1)[0]
                for _ in range(length)]


# ===========================================================================
# CONNECTION MANAGER — Auto-reconnect, response reading, health monitoring
# ===========================================================================

class ConnectionManager:
    """Manages TCP connection with auto-reconnect and response reading."""

    def __init__(self, host: str, port: int, max_retries: int = 5):
        self.host = host
        self.port = port
        self.max_retries = max_retries
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self._retry_count = 0
        self._backoff = 1.0

    def connect(self) -> bool:
        """Connect with exponential backoff retry."""
        for attempt in range(self.max_retries):
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(30.0)
                self.sock.connect((self.host, self.port))
                self.connected = True
                self._retry_count = 0
                self._backoff = 1.0
                logger.info("Connected to %s:%d", self.host, self.port)
                return True
            except ConnectionRefusedError:
                wait = self._backoff
                logger.warning("Connection refused (attempt %d/%d), retry in %.1fs",
                              attempt + 1, self.max_retries, wait)
                time.sleep(wait)
                self._backoff = min(self._backoff * 2, 30.0)
            except socket.timeout:
                logger.warning("Connection timeout (attempt %d/%d)", attempt + 1, self.max_retries)
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30.0)
            except Exception as e:
                logger.error("Connection error: %s", e)
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 30.0)

        logger.error("Failed to connect after %d attempts", self.max_retries)
        self.connected = False
        return False

    def send(self, payload: bytes) -> bool:
        """Send a framed payload. Auto-reconnects on failure."""
        if not self.connected and not self.connect():
            return False

        frame = struct.pack(">H", len(payload)) + payload
        try:
            self.sock.sendall(frame)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Send failed: %s — reconnecting...", e)
            self.disconnect()
            if self.connect():
                try:
                    self.sock.sendall(frame)
                    return True
                except Exception:
                    return False
            return False

    def recv_response(self, timeout: float = 2.0) -> Optional[bytes]:
        """Read a server response (non-blocking, timeout-based)."""
        if not self.connected or not self.sock:
            return None
        try:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
            data = self.sock.recv(4096)
            self.sock.settimeout(old_timeout)
            return data if data else None
        except socket.timeout:
            return None
        except (ConnectionResetError, OSError):
            return None

    def is_alive(self) -> bool:
        """Check if connection is still alive."""
        if not self.connected or not self.sock:
            return False
        try:
            self.sock.settimeout(0.1)
            self.sock.recv(1, socket.MSG_PEEK)
            self.sock.settimeout(30.0)
            return True
        except socket.timeout:
            self.sock.settimeout(30.0)
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False

    def disconnect(self):
        """Close the connection."""
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None


# ===========================================================================
# SESSION STATS
# ===========================================================================

class SessionStats:
    """Tracks real-time bot statistics."""

    def __init__(self, profile_name: str):
        self.profile_name = profile_name
        self.start_time = time.time()
        self.packets_sent = 0
        self.packets_failed = 0
        self.cycles_completed = 0
        self.cycles_failed = 0
        self.reconnects = 0
        self.responses_received = 0
        self.opcode_counter = Counter()
        self.state_counter = Counter()
        self.cycle_times: List[float] = []
        self._cycle_start = None

    def start_cycle(self):
        self._cycle_start = time.time()

    def end_cycle(self, success: bool):
        if self._cycle_start:
            elapsed = time.time() - self._cycle_start
            self.cycle_times.append(elapsed)
            self._cycle_start = None
        if success:
            self.cycles_completed += 1
        else:
            self.cycles_failed += 1

    def packet_sent(self, opcode: int, state: int):
        self.packets_sent += 1
        self.opcode_counter[OPCODE_NAMES.get(opcode, f"0x{opcode:04X}")] += 1
        self.state_counter[STATE_NAMES.get(state, "?")] += 1

    def packet_failed(self):
        self.packets_failed += 1

    def response_received(self):
        self.responses_received += 1

    def reconnected(self):
        self.reconnects += 1

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def avg_cycle_time(self) -> float:
        return sum(self.cycle_times) / len(self.cycle_times) if self.cycle_times else 0

    @property
    def success_rate(self) -> float:
        total = self.cycles_completed + self.cycles_failed
        return (self.cycles_completed / total * 100) if total > 0 else 0

    def summary(self) -> dict:
        return {
            "profile": self.profile_name,
            "elapsed_seconds": round(self.elapsed, 1),
            "packets_sent": self.packets_sent,
            "packets_failed": self.packets_failed,
            "cycles_completed": self.cycles_completed,
            "cycles_failed": self.cycles_failed,
            "success_rate": round(self.success_rate, 1),
            "avg_cycle_time": round(self.avg_cycle_time, 1),
            "reconnects": self.reconnects,
            "responses_received": self.responses_received,
            "top_opcodes": dict(self.opcode_counter.most_common(5)),
            "state_distribution": dict(self.state_counter),
        }

    def print_live(self):
        """Print a compact live stats line."""
        elapsed = self.elapsed
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        print(f"\r  [{self.profile_name}] {h:02d}:{m:02d}:{s:02d} | "
              f"cycles: {self.cycles_completed}✓ {self.cycles_failed}✗ | "
              f"pkts: {self.packets_sent} | "
              f"reconnects: {self.reconnects} | "
              f"avg: {self.avg_cycle_time:.1f}s",
              end="", flush=True)

    def print_report(self):
        """Print full session report."""
        s = self.summary()
        print(f"\n{'='*60}")
        print(f"  SESSION REPORT — {s['profile']}")
        print(f"{'='*60}")
        print(f"  Elapsed:        {s['elapsed_seconds']}s")
        print(f"  Cycles:         {s['cycles_completed']} completed, {s['cycles_failed']} failed")
        print(f"  Success rate:   {s['success_rate']}%")
        print(f"  Avg cycle time: {s['avg_cycle_time']}s")
        print(f"  Packets:        {s['packets_sent']} sent, {s['packets_failed']} failed")
        print(f"  Reconnects:     {s['reconnects']}")
        print(f"  Server replies: {s['responses_received']}")
        print(f"  Top opcodes:")
        for op, count in s["top_opcodes"].items():
            print(f"    {op:<20s} x{count}")
        print(f"  States visited:")
        for state, count in s["state_distribution"].items():
            print(f"    {state:<20s} x{count}")
        print(f"{'='*60}")


# ===========================================================================
# BOT PROFILES
# ===========================================================================

class BotProfile:
    """Base bot profile."""

    name: str = "base"
    description: str = ""

    def __init__(self, seed: Optional[int] = None, speed_mult: float = 1.0):
        self.seed = seed
        self.speed_mult = speed_mult

    def build_cycle(self, cycle_num: int) -> List[Tuple[bytes, str]]:
        """Build packets for one cycle. Returns [(payload, phase)] tuples."""
        raise NotImplementedError

    def build_auth(self) -> List[Tuple[bytes, str]]:
        """Build authentication packets (sent once at start)."""
        return [
            (_op(0x0001), "auth"),
            (_op(0x0002), "auth"),
            (_op(0x0010), "auth"),
        ]

    def build_disconnect(self) -> List[Tuple[bytes, str]]:
        """Build disconnect packets."""
        return [(_op(0x00FF), "disconnect")]

    def get_timing(self) -> object:
        """Return timing model instance."""
        raise NotImplementedError

    def get_entropy(self) -> OpcodeEntropyEngine:
        """Return entropy engine instance."""
        return OpcodeEntropyEngine(seed=self.seed)


class StandardBotProfile(BotProfile):
    """Standard bot — log-normal timing + opcode entropy + fatigue drift."""

    name = "standard"
    description = "Log-normal timing + opcode entropy + fatigue drift"

    def __init__(self, seed=None, speed_mult=1.0):
        super().__init__(seed, speed_mult)
        self._timing = HumanTimingModel(seed=seed, speed_mult=speed_mult)
        self._entropy = OpcodeEntropyEngine(seed=seed)

    def get_timing(self):
        return self._timing

    def get_entropy(self):
        return self._entropy

    def build_cycle(self, cycle_num: int) -> List[Tuple[bytes, str]]:
        packets = []
        rng = self._entropy.rng

        # Garage: 2-6 randomized actions
        for op in self._entropy.garage_sequence(rng.randint(2, 6)):
            packets.append((_op(op), "garage"))

        # Token refresh (10% chance)
        if rng.random() < 0.10:
            packets.append((_op(0x0011), "auth"))
            packets.append((_op(0x0010), "auth"))

        # Matchmaking
        packets.append((_op(0x0030), "garage"))
        packets.append((_op(0x0040), "matchmaking"))

        # Battle: 3-8 randomized actions
        for op in self._entropy.battle_sequence(rng.randint(3, 8)):
            packets.append((_op(op), "battle"))

        # Battle end + results
        packets.append((_op(0x005F), "battle"))
        packets.append((_op(0x0060), "results"))

        return packets


class GhostBotProfile(BotProfile):
    """Ghost — adaptive RTT-calibrated timing."""

    name = "ghost"
    description = "Adaptive RTT-calibrated timing"

    def __init__(self, seed=None, speed_mult=1.0):
        super().__init__(seed, speed_mult)
        self._timing = AdaptiveTimingModel(seed=seed, speed_mult=speed_mult)
        self._entropy = OpcodeEntropyEngine(seed=seed)

    def get_timing(self):
        return self._timing

    def get_entropy(self):
        return self._entropy

    def build_cycle(self, cycle_num: int) -> List[Tuple[bytes, str]]:
        packets = []
        rng = self._entropy.rng

        for op in self._entropy.garage_sequence(rng.randint(3, 5)):
            packets.append((_op(op), "garage"))

        packets.append((_op(0x0030), "garage"))
        packets.append((_op(0x0040), "matchmaking"))

        for op in self._entropy.battle_sequence(rng.randint(4, 7)):
            packets.append((_op(op), "battle"))

        packets.append((_op(0x005F), "battle"))
        packets.append((_op(0x0060), "results"))

        return packets


class ChameleonBotProfile(BotProfile):
    """Chameleon — mimics human opcode frequency distributions."""

    name = "chameleon"
    description = "Human opcode frequency + log-normal timing"

    GARAGE_WEIGHTS = {0x0020: 0.15, 0x0021: 0.10, 0x0022: 0.20,
                      0x0023: 0.15, 0x0024: 0.10, 0x0025: 0.30}
    BATTLE_WEIGHTS = {0x0050: 0.50, 0x0051: 0.30, 0x0052: 0.20}

    def __init__(self, seed=None, speed_mult=1.0):
        super().__init__(seed, speed_mult)
        self._timing = HumanTimingModel(seed=seed, speed_mult=speed_mult)
        self._rng = random.Random(seed)

    def get_timing(self):
        return self._timing

    def get_entropy(self):
        return OpcodeEntropyEngine(seed=self.seed)

    def build_cycle(self, cycle_num: int) -> List[Tuple[bytes, str]]:
        packets = []

        # More garage actions (humans browse a lot)
        garage_len = self._rng.randint(4, 8)
        for _ in range(garage_len):
            op = self._rng.choices(list(self.GARAGE_WEIGHTS.keys()),
                                   weights=list(self.GARAGE_WEIGHTS.values()), k=1)[0]
            packets.append((_op(op), "garage"))

        packets.append((_op(0x0030), "garage"))
        packets.append((_op(0x0040), "matchmaking"))

        # Fewer battle actions
        battle_len = self._rng.randint(2, 5)
        for _ in range(battle_len):
            op = self._rng.choices(list(self.BATTLE_WEIGHTS.keys()),
                                    weights=list(self.BATTLE_WEIGHTS.values()), k=1)[0]
            packets.append((_op(op), "battle"))

        packets.append((_op(0x005F), "battle"))
        packets.append((_op(0x0060), "results"))

        return packets


# ===========================================================================
# BOT RUNNER — Executes profiles with full lifecycle management
# ===========================================================================

class BotRunner:
    """Runs a bot profile with auto-reconnect, stats, and graceful shutdown."""

    def __init__(self, profile: BotProfile, host: str, port: int,
                 cycles: int = 10, read_responses: bool = True,
                 live_stats: bool = True):
        self.profile = profile
        self.host = host
        self.port = port
        self.max_cycles = cycles
        self.read_responses = read_responses
        self.live_stats = live_stats

        self.conn = ConnectionManager(host, port)
        self.stats = SessionStats(profile.name)
        self.timing = profile.get_timing()

        self._running = True
        self._current_state = 0
        self._auth_done = False

    def stop(self):
        self._running = False
        logger.info("Stopping bot...")

    def _send_packet(self, payload: bytes, phase: str) -> bool:
        """Send a single packet and update state/stats."""
        if self.conn.send(payload):
            opcode = struct.unpack("<H", payload[:2])[0]
            self._current_state = TRANSITIONS.get((self._current_state, opcode), self._current_state)
            self.stats.packet_sent(opcode, self._current_state)

            # Read server response if enabled
            if self.read_responses:
                resp = self.conn.recv_response(timeout=1.0)
                if resp:
                    self.stats.response_received()
                    logger.debug("Response: %d bytes", len(resp))

            state_name = STATE_NAMES.get(self._current_state, "?")
            op_name = OPCODE_NAMES.get(opcode, f"0x{opcode:04X}")
            logger.debug("[%s] %s → state=%s", self.profile.name, op_name, state_name)
            return True
        else:
            self.stats.packet_failed()
            return False

    def _do_auth(self) -> bool:
        """Send auth packets."""
        logger.info("[%s] Authenticating...", self.profile.name)
        auth_packets = self.profile.build_auth()
        for payload, phase in auth_packets:
            if not self._running:
                return False
            delay = self.timing.delay(phase)
            time.sleep(delay)
            if not self._send_packet(payload, phase):
                logger.error("[%s] Auth failed at phase: %s", self.profile.name, phase)
                return False
        self._auth_done = True
        logger.info("[%s] Authenticated — state: %s",
                    self.profile.name, STATE_NAMES.get(self._current_state, "?"))
        return True

    def _run_cycle(self, cycle_num: int) -> bool:
        """Run a single battle cycle."""
        self.stats.start_cycle()
        packets = self.profile.build_cycle(cycle_num)

        for payload, phase in packets:
            if not self._running:
                return False

            delay = self.timing.delay(phase)
            if delay > 0:
                time.sleep(delay)

            if not self._send_packet(payload, phase):
                logger.warning("[%s] Cycle %d failed at phase: %s",
                               self.profile.name, cycle_num, phase)
                self.stats.end_cycle(False)
                return False

            # Live stats update
            if self.live_stats and self.stats.packets_sent % 5 == 0:
                self.stats.print_live()

        self.stats.end_cycle(True)

        if self.live_stats:
            self.stats.print_live()

        cycle_time = self.stats.cycle_times[-1] if self.stats.cycle_times else 0
        logger.info("[%s] Cycle %d/%d complete (%.1fs)",
                    self.profile.name, cycle_num, self.max_cycles, cycle_time)
        return True

    def run(self) -> dict:
        """Main run loop with auto-reconnect."""
        logger.info("=" * 60)
        logger.info("  WoT Bot v2.0 — Profile: %s", self.profile.name)
        logger.info("  Host: %s:%d | Cycles: %d", self.host, self.port, self.max_cycles)
        logger.info("  Speed: %.1fx | Description: %s",
                    self.profile.speed_mult, self.profile.description)
        logger.info("=" * 60)

        if not self.conn.connect():
            logger.error("Cannot connect to server")
            return self.stats.summary()

        # Auth
        if not self._do_auth():
            logger.error("Authentication failed")
            self.conn.disconnect()
            return self.stats.summary()

        # Main cycle loop
        cycle = 1
        consecutive_failures = 0

        while self._running and cycle <= self.max_cycles:
            try:
                success = self._run_cycle(cycle)

                if success:
                    consecutive_failures = 0
                    cycle += 1
                    # Brief pause between cycles
                    time.sleep(self.timing.delay("results") * 0.3)
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.warning("3 consecutive failures — reconnecting...")
                        self.conn.disconnect()
                        self.stats.reconnected()
                        time.sleep(2.0)
                        if not self.conn.connect():
                            logger.error("Reconnect failed — stopping")
                            break
                        # Re-auth after reconnect
                        self._auth_done = False
                        self._current_state = 0
                        if not self._do_auth():
                            logger.error("Re-auth failed — stopping")
                            break
                        consecutive_failures = 0
                    else:
                        logger.warning("Cycle %d failed, skipping (failures: %d/3)",
                                      cycle, consecutive_failures)
                        cycle += 1
                        time.sleep(1.0)

            except KeyboardInterrupt:
                logger.info("\nInterrupted by user")
                self._running = False
                break
            except Exception as e:
                logger.error("Unexpected error in cycle %d: %s", cycle, e)
                self.stats.end_cycle(False)
                consecutive_failures += 1
                time.sleep(2.0)

        # Disconnect
        if self._running:
            logger.info("[%s] Sending disconnect...", self.profile.name)
            for payload, phase in self.profile.build_disconnect():
                time.sleep(self.timing.delay(phase))
                self._send_packet(payload, phase)

        self.conn.disconnect()

        # Print final report
        if self.live_stats:
            print()  # newline after live stats
        self.stats.print_report()

        return self.stats.summary()


# ===========================================================================
# PLATOON — Multi-account with staggered starts
# ===========================================================================

class PlatoonRunner:
    """Runs multiple bot sessions concurrently with staggered starts."""

    def __init__(self, session_count: int = 3, host: str = "127.0.0.1",
                 port: int = 5222, cycles: int = 10, speed_mult: float = 1.0):
        self.session_count = session_count
        self.host = host
        self.port = port
        self.cycles = cycles
        self.speed_mult = speed_mult
        self._running = True

    def stop(self):
        self._running = False

    def run(self) -> list:
        results = []
        results_lock = threading.Lock()
        threads = []

        def _run_session(session_id: int):
            seed = session_id * 1000 + random.randint(0, 999)
            profile = StandardBotProfile(seed=seed, speed_mult=self.speed_mult)
            runner = BotRunner(
                profile, self.host, self.port, self.cycles,
                read_responses=True, live_stats=False
            )

            # Staggered start
            stagger = random.uniform(1.0, 5.0)
            time.sleep(stagger)
            logger.info("[platoon-%d] Starting (seed=%d, stagger=%.1fs)",
                        session_id, seed, stagger)

            summary = runner.run()
            summary["session_id"] = session_id
            summary["seed"] = seed

            with results_lock:
                results.append(summary)

        logger.info("=" * 60)
        logger.info("  PLATOON — %d sessions", self.session_count)
        logger.info("  Host: %s:%d | Cycles: %d each", self.host, self.port, self.cycles)
        logger.info("=" * 60)

        for i in range(self.session_count):
            t = threading.Thread(target=_run_session, args=(i,), daemon=True)
            t.start()
            threads.append(t)

        # Wait for all sessions
        for t in threads:
            t.join(timeout=300.0)

        # Print platoon summary
        print(f"\n{'='*60}")
        print(f"  PLATOON SUMMARY — {self.session_count} sessions")
        print(f"{'='*60}")
        print(f"  {'Session':<10s} {'Cycles':<12s} {'Pkts':<8s} {'Reconnects':<12s} {'Time':<8s}")
        print(f"  {'-'*56}")
        for r in sorted(results, key=lambda x: x.get("session_id", 0)):
            print(f"  #{r['session_id']:<8d} "
                  f"{r['cycles_completed']:<5d}✓/{r['cycles_failed']:<2d}✗  "
                  f"{r['packets_sent']:<8d} "
                  f"{r['reconnects']:<12d} "
                  f"{r['elapsed_seconds']:<8.1f}s")
        total_cycles = sum(r["cycles_completed"] for r in results)
        total_pkts = sum(r["packets_sent"] for r in results)
        print(f"  {'-'*56}")
        print(f"  {'TOTAL':<10s} {total_cycles:<12d} {total_pkts:<8d}")
        print(f"{'='*60}\n")

        return results


# ===========================================================================
# CONFIG FILE SUPPORT
# ===========================================================================

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 5222,
    "profile": "standard",
    "cycles": 10,
    "speed": "normal",
    "seed": None,
    "read_responses": True,
    "live_stats": True,
    "log_file": None,
    "export": None,
}


def load_config(path: str) -> dict:
    """Load configuration from JSON file."""
    with open(path) as f:
        cfg = json.load(f)
    merged = {**DEFAULT_CONFIG, **cfg}
    return merged


def save_config(path: str, cfg: dict):
    """Save configuration to JSON file."""
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    logger.info("Config saved to %s", path)


# ===========================================================================
# MAIN
# ===========================================================================

PROFILES = {
    "standard": StandardBotProfile,
    "ghost": GhostBotProfile,
    "chameleon": ChameleonBotProfile,
}


def main():
    parser = argparse.ArgumentParser(
        description="WoT Advanced Bot v2.0 — automated battle cycles with human-like timing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Profiles:
  standard    Log-normal timing + opcode entropy + fatigue drift
  ghost       Adaptive RTT-calibrated timing (dynamic jitter)
  chameleon   Human opcode frequency distribution + log-normal timing
  platoon     Multi-account with staggered starts

Speed presets:
  turbo (0.2x)  fast (0.5x)  normal (1.0x)  slow (1.5x)  relaxed (2.5x)

Examples:
  python3 wot_advanced_bot.py --cycles 50
  python3 wot_advanced_bot.py --profile ghost --speed fast --cycles 200
  python3 wot_advanced_bot.py --profile chameleon --log-file bot.log --export results.json
  python3 wot_advanced_bot.py --profile platoon --sessions 5 --cycles 100
  python3 wot_advanced_bot.py --config bot_config.json
        """,
    )

    parser.add_argument("--host", default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    parser.add_argument("--profile", default=None,
                        choices=["standard", "ghost", "chameleon", "platoon", "all"])
    parser.add_argument("--cycles", type=int, default=None, help="Battle cycles to run")
    parser.add_argument("--speed", default=None,
                        choices=list(SPEED_PRESETS.keys()),
                        help="Speed preset (affects all timing)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--sessions", type=int, default=3, help="Platoon session count")
    parser.add_argument("--log-file", default=None, help="Write logs to file")
    parser.add_argument("--export", default=None, help="Export results to JSON")
    parser.add_argument("--config", default=None, help="Load config from JSON file")
    parser.add_argument("--save-config", default=None, help="Save current config to JSON file")
    parser.add_argument("--verbose", action="store_true", help="Debug-level logging")
    args = parser.parse_args()

    # Load config file if specified
    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        if os.path.exists(args.config):
            cfg = load_config(args.config)
        else:
            logger.error("Config file not found: %s", args.config)
            sys.exit(1)

    # CLI args override config
    if args.host:      cfg["host"] = args.host
    if args.port:      cfg["port"] = args.port
    if args.profile:   cfg["profile"] = args.profile
    if args.cycles:    cfg["cycles"] = args.cycles
    if args.speed:     cfg["speed"] = args.speed
    if args.seed is not None: cfg["seed"] = args.seed
    if args.log_file:  cfg["log_file"] = args.log_file
    if args.export:    cfg["export"] = args.export
    if args.verbose:   cfg["verbose"] = True

    # Save config if requested
    if args.save_config:
        save_config(args.save_config, cfg)

    # Setup logging
    if cfg.get("verbose"):
        logger.setLevel(logging.DEBUG)
    if cfg.get("log_file"):
        fh = logging.FileHandler(cfg["log_file"])
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)

    speed_mult = SPEED_PRESETS.get(cfg["speed"], 1.0)
    profile_name = cfg["profile"]
    cycles = cfg["cycles"]

    # Save config if requested
    if args.save_config:
        save_config(args.save_config, cfg)

    # Setup signal handler for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("\nCtrl+C received — shutting down gracefully...")
        if runner_obj:
            runner_obj.stop()
    runner_obj = None
    signal.signal(signal.SIGINT, signal_handler)

    # Run
    if profile_name == "platoon":
        runner_obj = PlatoonRunner(
            session_count=args.sessions,
            host=cfg["host"], port=cfg["port"],
            cycles=cycles, speed_mult=speed_mult
        )
        results = runner_obj.run()

        if cfg.get("export"):
            with open(cfg["export"], "w") as f:
                json.dump(results, f, indent=2, default=str)
            logger.info("Exported to %s", cfg["export"])

    elif profile_name == "all":
        all_results = []
        for name in ["standard", "ghost", "chameleon"]:
            logger.info("\n--- Profile: %s ---", name)
            cls = PROFILES[name]
            profile = cls(seed=cfg["seed"], speed_mult=speed_mult)
            runner_obj = BotRunner(
                profile, cfg["host"], cfg["port"], cycles,
                read_responses=cfg.get("read_responses", True),
                live_stats=cfg.get("live_stats", True)
            )
            summary = runner_obj.run()
            all_results.append(summary)

        if cfg.get("export"):
            with open(cfg["export"], "w") as f:
                json.dump(all_results, f, indent=2, default=str)
            logger.info("Exported to %s", cfg["export"])

    else:
        cls = PROFILES.get(profile_name, StandardBotProfile)
        profile = cls(seed=cfg["seed"], speed_mult=speed_mult)
        runner_obj = BotRunner(
            profile, cfg["host"], cfg["port"], cycles,
            read_responses=cfg.get("read_responses", True),
            live_stats=cfg.get("live_stats", True)
        )
        summary = runner_obj.run()

        if cfg.get("export"):
            with open(cfg["export"], "w") as f:
                json.dump(summary, f, indent=2, default=str)
            logger.info("Exported to %s", cfg["export"])


if __name__ == "__main__":
    main()
