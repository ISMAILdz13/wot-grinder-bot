#!/usr/bin/env python3
"""
BigWorld UDP Protocol Client for WoT
Implements the real BigWorld login flow over UDP:
  1. Ping → server pong
  2. LoginRequest (RSA or plaintext) → LoginResponse (base app addr + Blowfish key)
  3. Connect to base app with Blowfish encryption
  4. Entity tracking, battle awareness

Protocol reference: wg-toolkit-rs by theorzr (github.com/theorzr/wg-toolkit-rs)
"""

import socket
import struct
import os
import time
import random
import hashlib
import logging
from typing import Optional, Tuple, Dict, List

logger = logging.getLogger('bigworld')

# Try to import crypto libraries
try:
    from Crypto.Cipher import Blowfish, PKCS1_v1_5, PKCS1_OAEP
    from Crypto.PublicKey import RSA
    from Crypto.Hash import SHA1
    HAS_CRYPTO = True
except ImportError:
    try:
        from Cryptodome.Cipher import Blowfish as _BF
        from Cryptodome.PublicKey import RSA
        from Cryptodome.Cipher import PKCS1_OAEP
        from Cryptodome.Hash import SHA1
        HAS_CRYPTO = True
    except ImportError:
        HAS_CRYPTO = False
        logger.warning("pycryptodome not found — RSA encryption unavailable")


# ===========================================================================
# BigWorld Packet Format
# ===========================================================================

PACKET_CAP = 1472          # Max UDP packet size
PACKET_PREFIX_LEN = 4      # 4 bytes prefix (u32 LE)
PACKET_FLAGS_LEN = 2       # 2 bytes flags (u16 LE)
PACKET_HEADER_LEN = PACKET_PREFIX_LEN + PACKET_FLAGS_LEN  # 6 bytes

# Element IDs (from login/element.rs)
EL_LOGIN_REQUEST = 0x00
EL_LOGIN_RESPONSE = 0x01   # Server → client
EL_PING = 0x02
EL_CHALLENGE_RESPONSE = 0x03

# Base app element IDs (from client/element.rs)
EL_UPDATE_FREQ = 0x00
EL_TICK_SYNC = 0x01
EL_RESET_ENTITIES = 0x02
EL_LOGGED_OFF = 0x03
EL_CREATE_BASE_PLAYER = 0x04
EL_CREATE_CELL_PLAYER = 0x05
EL_SELECT_PLAYER_ENTITY = 0x06

# Login response codes
LOGIN_SUCCESS = 1
LOGIN_CHALLENGE = 66
LOGIN_ERROR_MALFORMED = 64
LOGIN_ERROR_BAD_PROTOCOL = 65
LOGIN_ERROR_INVALID_USER = 67
LOGIN_ERROR_INVALID_PASSWORD = 68
LOGIN_ERROR_ALREADY_LOGGED = 69
LOGIN_ERROR_RATE_LIMITED = 83
LOGIN_ERROR_BANNED = 84

# WoT login servers
LOGIN_SERVERS = {
    "EU": [
        ("login.p1.worldoftanks.eu", 443),
        ("login.p2.worldoftanks.eu", 443),
        ("login.p3.worldoftanks.eu", 443),
        ("login.p5.worldoftanks.eu", 443),
    ],
    "NA": [
        ("wotna3.login.wargaming.net", 443),
        ("wotna4.login.wargaming.net", 443),
    ],
    "ASIA": [
        ("wotasia1.login.wargaming.net", 443),
    ],
}

# WoT protocol version (needs to match current client)
# This is updated with each game patch — try common values
WOT_PROTOCOL_VERSION = 0x0000012C  # 300 — placeholder, may need updating


class BigWorldPacket:
    """BigWorld UDP packet encoder/decoder."""
    
    @staticmethod
    def create_ping(num: int = 0) -> bytes:
        """Create a Ping packet (element ID 0x02, 1 byte data)."""
        prefix = struct.pack('<I', 0)
        flags = struct.pack('<H', 0)
        element = bytes([EL_PING, num & 0xFF])
        return prefix + flags + element
    
    @staticmethod
    def create_login_request(
        protocol: int,
        username: str,
        password: str,
        blowfish_key: bytes,
        context: str = "",
        nonce: int = 0,
        rsa_key=None
    ) -> bytes:
        """
        Create a LoginRequest packet.
        Format: prefix(4) + flags(2) + element_id(1) + protocol(4) + encrypted(1) + login_data
        """
        prefix = struct.pack('<I', 0)
        flags = struct.pack('<H', 0)
        element_id = bytes([EL_LOGIN_REQUEST])
        
        # Protocol version
        protocol_bytes = struct.pack('<I', protocol)
        
        # Build login data
        login_data = b''
        # flags byte: 0x00 = no digest, 0x01 = has digest
        login_data += bytes([0x00])
        # username (variable string: u16 LE length + UTF-8)
        login_data += BigWorldPacket._write_string(username)
        # password (variable string)
        login_data += BigWorldPacket._write_string(password)
        # blowfish_key (variable blob: u16 LE length + data)
        login_data += BigWorldPacket._write_blob(blowfish_key)
        # context (variable string)
        login_data += BigWorldPacket._write_string(context)
        # nonce (u32 LE)
        login_data += struct.pack('<I', nonce)
        
        if rsa_key and HAS_CRYPTO:
            # RSA-encrypted login
            encrypted_flag = bytes([0x01])
            # Encrypt login_data with RSA OAEP SHA-1
            cipher = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA1)
            # RSA can encrypt up to key_size - 42 bytes at a time
            # For 2048-bit key: 256 - 42 = 214 bytes per block
            block_size = rsa_key.size_in_bytes() - 42
            encrypted_data = b''
            for i in range(0, len(login_data), block_size):
                block = login_data[i:i+block_size]
                encrypted_data += cipher.encrypt(block)
            login_payload = encrypted_data
        else:
            # Unencrypted login
            encrypted_flag = bytes([0x00])
            login_payload = login_data
        
        return prefix + flags + element_id + protocol_bytes + encrypted_flag + login_payload
    
    @staticmethod
    def _write_string(s: str) -> bytes:
        """Write a variable-length string: u16 LE length + UTF-8 data."""
        encoded = s.encode('utf-8')
        return struct.pack('<H', len(encoded)) + encoded
    
    @staticmethod
    def _write_blob(b: bytes) -> bytes:
        """Write a variable-length blob: u16 LE length + data."""
        return struct.pack('<H', len(b)) + b
    
    @staticmethod
    def _read_string(data: bytes, offset: int) -> Tuple[str, int]:
        """Read a variable-length string. Returns (string, new_offset)."""
        length = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        s = data[offset:offset+length].decode('utf-8', errors='replace')
        return s, offset + length
    
    @staticmethod
    def _read_blob(data: bytes, offset: int) -> Tuple[bytes, int]:
        """Read a variable-length blob. Returns (blob, new_offset)."""
        length = struct.unpack_from('<H', data, offset)[0]
        offset += 2
        blob = data[offset:offset+length]
        return blob, offset + length
    
    @staticmethod
    def parse_login_response(data: bytes, blowfish_key: bytes = None) -> dict:
        """
        Parse a LoginResponse from server.
        Returns dict with: type ('success'|'challenge'|'error'), and relevant fields.
        """
        # Skip prefix(4) + flags(2) + element_id(1)
        offset = PACKET_HEADER_LEN + 1
        
        if offset >= len(data):
            return {'type': 'error', 'error': 'Packet too short'}
        
        response_code = data[offset]
        offset += 1
        
        if response_code == LOGIN_SUCCESS:
            # LoginSuccess: addr(SocketAddrV4) + login_key(u32) + server_message(string)
            # If Blowfish encrypted, decrypt first
            success_data = data[offset:]
            
            if blowfish_key and HAS_CRYPTO:
                try:
                    bf = Blowfish.new(blowfish_key, Blowfish.MODE_ECB)
                    # Decrypt in 8-byte blocks
                    decrypted = b''
                    for i in range(0, len(success_data), 8):
                        block = success_data[i:i+8]
                        if len(block) == 8:
                            decrypted += bf.decrypt(block)
                    success_data = decrypted
                except:
                    pass
            
            try:
                # Parse SocketAddrV4: 4 bytes IP + 2 bytes port (LE)
                ip_bytes = success_data[0:4]
                port = struct.unpack_from('<H', success_data, 4)[0]
                ip = '.'.join(str(b) for b in ip_bytes)
                offset2 = 6
                # login_key (u32 LE)
                login_key = struct.unpack_from('<I', success_data, offset2)[0]
                offset2 += 4
                # server_message (variable string)
                msg, offset2 = BigWorldPacket._read_string(success_data, offset2)
                
                return {
                    'type': 'success',
                    'base_app_ip': ip,
                    'base_app_port': port,
                    'login_key': login_key,
                    'server_message': msg,
                }
            except Exception as e:
                return {'type': 'error', 'error': f'Parse success failed: {e}'}
        
        elif response_code == LOGIN_CHALLENGE:
            # Challenge: challenge_name(string) + key_prefix(blob) + max_nonce(u64)
            try:
                challenge_name, offset = BigWorldPacket._read_string(data, offset)
                key_prefix, offset = BigWorldPacket._read_blob(data, offset)
                max_nonce = struct.unpack_from('<Q', data, offset)[0]
                return {
                    'type': 'challenge',
                    'challenge': challenge_name,
                    'key_prefix': key_prefix,
                    'max_nonce': max_nonce,
                }
            except Exception as e:
                return {'type': 'error', 'error': f'Parse challenge failed: {e}'}
        
        else:
            # Error: response_code is the error type, message follows
            error_codes = {
                64: 'MalformedRequest',
                65: 'BadProtocolVersion',
                67: 'InvalidUser',
                68: 'InvalidPassword',
                69: 'AlreadyLoggedIn',
                70: 'BadDigest',
                71: 'DatabaseGeneralFailure',
                72: 'DatabaseNotReady',
                73: 'IllegalCharacters',
                74: 'ServerNotReady',
                75: 'UpdaterNotReady',
                76: 'NoBaseApp',
                77: 'BaseAppOverload',
                78: 'CellAppOverload',
                79: 'BaseAppTimeout',
                80: 'BaseAppManagerTimeout',
                81: 'DatabaseAppOverload',
                82: 'LoginNotAllowed',
                83: 'RateLimited',
                84: 'Banned',
                85: 'ChallengeError',
            }
            error_name = error_codes.get(response_code, f'Unknown({response_code})')
            try:
                message, _ = BigWorldPacket._read_string(data, offset)
            except:
                message = ''
            return {
                'type': 'error',
                'code': response_code,
                'error': error_name,
                'message': message,
            }


# ===========================================================================
# BigWorld UDP Client
# ===========================================================================

class BigWorldClient:
    """UDP client for BigWorld protocol (WoT login + game)."""
    
    def __init__(self, realm: str = "EU"):
        self.realm = realm
        self.servers = LOGIN_SERVERS.get(realm, LOGIN_SERVERS["EU"])
        self.sock = None
        self.base_app_addr = None  # (ip, port) — from login response
        self.login_key = None
        self.blowfish_key = None  # For base app encryption
        self.rsa_key = None       # Server's RSA public key (if known)
        self.connected = False
        self.entities = {}        # entity_id → entity data
        self.player_entity_id = None
        self.packets_sent = 0
        self.packets_recv = 0
        
    def _connect_udp(self, host: str, port: int) -> bool:
        """Create UDP socket."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(10)
            self.connected = True
            logger.info("UDP socket ready for %s:%d", host, port)
            return True
        except Exception as e:
            logger.error("UDP socket failed: %s", e)
            return False
    
    def _send_udp(self, data: bytes, addr: Tuple[str, int]) -> bool:
        """Send UDP packet."""
        if not self.sock:
            return False
        try:
            self.sock.sendto(data, addr)
            self.packets_sent += 1
            return True
        except Exception as e:
            logger.error("Send failed: %s", e)
            return False
    
    def _recv_udp(self, timeout: float = 10.0) -> Optional[Tuple[bytes, Tuple[str, int]]]:
        """Receive UDP packet."""
        if not self.sock:
            return None
        try:
            self.sock.settimeout(timeout)
            data, addr = self.sock.recvfrom(PACKET_CAP)
            self.packets_recv += 1
            return data, addr
        except socket.timeout:
            return None
        except Exception as e:
            logger.error("Recv failed: %s", e)
            return None
    
    def ping(self, server_addr: Tuple[str, int]) -> bool:
        """Send a BigWorld Ping and wait for response."""
        logger.info("Pinging %s:%d (UDP)...", *server_addr)
        ping_pkt = BigWorldPacket.create_ping(num=1)
        
        if not self._send_udp(ping_pkt, server_addr):
            return False
        
        resp = self._recv_udp(timeout=8.0)
        if resp:
            data, addr = resp
            logger.info("Pong from %s:%d (%d bytes)", addr[0], addr[1], len(data))
            logger.info("  Hex: %s", data[:40].hex())
            return True
        else:
            logger.warning("No pong (timeout)")
            return False
    
    def login(
        self,
        username: str,
        password: str,
        server_addr: Tuple[str, int] = None,
        protocol: int = WOT_PROTOCOL_VERSION,
        use_encryption: bool = False
    ) -> dict:
        """
        Send a BigWorld LoginRequest.
        Returns the parsed LoginResponse dict.
        """
        server_addr = server_addr or self.servers[0]
        
        # Generate a random Blowfish key (16 bytes)
        client_bf_key = os.urandom(16)
        
        # Context string (WoT uses realm info here)
        context = f"wot_{self.realm.lower()}"
        
        # Random nonce
        nonce = random.randint(0, 0xFFFFFFFF)
        
        logger.info("Sending LoginRequest to %s:%d (UDP)...", *server_addr)
        logger.info("  Protocol: 0x%08X", protocol)
        logger.info("  Username: %s", username[:20] + "..." if len(username) > 20 else username)
        logger.info("  Encrypted: %s", use_encryption)
        logger.info("  Blowfish key: %d bytes", len(client_bf_key))
        
        login_pkt = BigWorldPacket.create_login_request(
            protocol=protocol,
            username=username,
            password=password,
            blowfish_key=client_bf_key,
            context=context,
            nonce=nonce,
            rsa_key=self.rsa_key if use_encryption else None
        )
        
        if not self._send_udp(login_pkt, server_addr):
            return {'type': 'error', 'error': 'Send failed'}
        
        # Wait for response
        resp = self._recv_udp(timeout=10.0)
        if not resp:
            return {'type': 'error', 'error': 'No response (timeout)'}
        
        data, addr = resp
        logger.info("Login response: %d bytes from %s:%d", len(data), addr[0], addr[1])
        logger.info("  Hex: %s", data[:60].hex())
        
        # Parse response — try with client BF key first (server encrypts success with it)
        result = BigWorldPacket.parse_login_response(data, client_bf_key)
        
        if result['type'] == 'success':
            self.base_app_addr = (result['base_app_ip'], result['base_app_port'])
            self.login_key = result['login_key']
            self.blowfish_key = client_bf_key  # Use client's BF key for base app
            logger.info("LOGIN SUCCESS!")
            logger.info("  Base app: %s:%d", *self.base_app_addr)
            logger.info("  Login key: 0x%08X", self.login_key)
            logger.info("  Server message: %s", result.get('server_message', ''))
        elif result['type'] == 'challenge':
            logger.info("LOGIN CHALLENGE: %s", result.get('challenge'))
            logger.info("  Key prefix: %d bytes", len(result.get('key_prefix', b'')))
            logger.info("  Max nonce: %d", result.get('max_nonce', 0))
        elif result['type'] == 'error':
            logger.warning("LOGIN ERROR: %s (%s)", result.get('error'), result.get('message', ''))
        
        return result
    
    def connect_base_app(self) -> bool:
        """Connect to the base app using the Blowfish key from login."""
        if not self.base_app_addr:
            logger.error("No base app address — login first")
            return False
        
        logger.info("Connecting to base app %s:%d...", *self.base_app_addr)
        
        # Send a session key / login key to the base app
        # The base app expects the login_key from the login server
        if self.login_key:
            # Send login key as first packet to base app
            pkt = struct.pack('<I', 0) + struct.pack('<H', 0) + struct.pack('<I', self.login_key)
            self._send_udp(pkt, self.base_app_addr)
        
        # Wait for response
        resp = self._recv_udp(timeout=10.0)
        if resp:
            data, addr = resp
            logger.info("Base app response: %d bytes", len(data))
            logger.info("  Hex: %s", data[:40].hex())
            return True
        else:
            logger.warning("No base app response")
            return False
    
    def send_entity_method(self, entity_id: int, method_id: int, args: bytes = b'') -> bool:
        """Send an entity method call to the base app."""
        if not self.sock or not self.base_app_addr:
            return False
        # BigWorld entity method: prefix(4) + flags(2) + entity_method_header + method_id + args
        pkt = struct.pack('<I', 0) + struct.pack('<H', 0)
        pkt += struct.pack('<I', entity_id)
        pkt += bytes([method_id])
        pkt += args
        return self._send_udp(pkt, self.base_app_addr)
    
    def recv_game_packet(self, timeout: float = 1.0) -> Optional[bytes]:
        """Receive a game packet from the base app."""
        resp = self._recv_udp(timeout=timeout)
        if resp:
            return resp[0]
        return None
    
    def close(self):
        if self.sock:
            try: self.sock.close()
            except: pass
        self.sock = None
        self.connected = False


# ===========================================================================
# Main test function
# ===========================================================================

def test_connection(realm: str = "EU", username: str = "", password: str = ""):
    """Test BigWorld UDP connection to WoT login servers."""
    print()
    print("=" * 60)
    print("BigWorld UDP Protocol Test")
    print(f"Realm: {realm}")
    print("=" * 60)
    
    client = BigWorldClient(realm=realm)
    
    # Try each server
    for host, port in client.servers:
        print(f"\n--- Testing {host}:{port} (UDP) ---")
        
        if not client._connect_udp(host, port):
            continue
        
        # Step 1: Ping
        print("\n[1] Ping test...")
        if client.ping((host, port)):
            print("  ✅ Ping succeeded! Server responds to UDP.")
            
            # Step 2: Try unencrypted login
            if username:
                print("\n[2] Login request (unencrypted)...")
                result = client.login(
                    username=username,
                    password=password,
                    server_addr=(host, port),
                    use_encryption=False
                )
                
                if result['type'] == 'success':
                    print("  ✅ LOGIN SUCCESS!")
                    print(f"  Base app: {result['base_app_ip']}:{result['base_app_port']}")
                    print(f"  Login key: 0x{result['login_key']:08X}")
                    
                    # Step 3: Connect to base app
                    print("\n[3] Connecting to base app...")
                    if client.connect_base_app():
                        print("  ✅ Base app connected!")
                    else:
                        print("  ❌ Base app connection failed")
                    
                elif result['type'] == 'challenge':
                    print(f"  ⚠️  Challenge required: {result.get('challenge')}")
                    print(f"  Need to solve cuckoo cycle PoW")
                    
                elif result['type'] == 'error':
                    print(f"  ❌ Login error: {result.get('error')} - {result.get('message', '')}")
                    
                    # If BadProtocolVersion, try other versions
                    if result.get('code') == 65:
                        print("  Trying different protocol versions...")
                        for ver in [0x12C, 0x1F4, 0x2BC, 0x358, 0x3E8, 0x4B0, 0x5DC]:
                            print(f"    Protocol 0x{ver:X}...", end=" ")
                            r = client.login(username, password, (host, port), protocol=ver)
                            if r['type'] == 'success':
                                print("SUCCESS!")
                                break
                            elif r['type'] == 'challenge':
                                print("Challenge (progress!)")
                                break
                            else:
                                print(f"{r.get('error', 'failed')}")
                    
                    # If needs encryption
                    if result.get('code') in [64, 67, 68] and not HAS_CRYPTO:
                        print("  ⚠️  May need RSA encryption (install pycryptodome)")
            else:
                print("\n[2] Skipping login (no username)")
        else:
            print("  ❌ No response — UDP may be blocked")
        
        client.close()
        
        # If we got any response, try the next server too
        if client.packets_recv > 0:
            break
    
    print("\n" + "=" * 60)
    print(f"Packets sent: {client.packets_sent} | recv: {client.packets_recv}")
    print("=" * 60)
    return client


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    parser = argparse.ArgumentParser(description="BigWorld UDP Protocol Test")
    parser.add_argument("--realm", default="EU", help="Server realm (EU/NA/ASIA)")
    parser.add_argument("--username", default="", help="WG username or token")
    parser.add_argument("--password", default="", help="WG password or token")
    parser.add_argument("--ping-only", action="store_true", help="Only test ping")
    
    args = parser.parse_args()
    
    test_connection(
        realm=args.realm,
        username=args.username,
        password=args.password
    )
