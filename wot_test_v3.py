#!/usr/bin/env python3
"""BigWorld protocol UDP test with correct packet format."""
import socket, struct, sys

def compute_prefix(buf, offset=0):
    """Compute BigWorld packet prefix (xorshift-based checksum)."""
    p0 = struct.unpack_from("<I", buf, 4)[0]
    p1 = struct.unpack_from("<I", buf, 8)[0]
    a = (offset + p0 + p1) & 0xFFFFFFFF
    b = (a << 13) & 0xFFFFFFFF
    c = ((b ^ a) >> 17) & 0xFFFFFFFF
    d = (c ^ b ^ a ^ ((c ^ b ^ a) << 5)) & 0xFFFFFFFF
    return d

def build_ping(num=0):
    """Build a correct BigWorld PING packet."""
    buf = bytearray(12)
    # flags at offset 4 (2 bytes, =0)
    # body at offset 6: element_id=0x02 (PING), num
    buf[6] = 0x02
    buf[7] = num
    prefix = compute_prefix(buf)
    struct.pack_into("<I", buf, 0, prefix)
    return bytes(buf[:8])

def build_login_request(protocol=0x0144, username="", password="", nonce=0):
    """Build a BigWorld LoginRequest packet (plaintext, no RSA)."""
    # LoginRequest body:
    # u32 protocol, bool encrypted(false=u8 0), u8 flags(0), 
    # string username, string password, blob blowfish_key, string context, u32 nonce
    
    body = bytearray()
    body += struct.pack("<I", protocol)  # protocol version
    body += bytes([0x00])  # not encrypted
    body += bytes([0x00])  # flags (no digest)
    # write_string_variable: u16 LE length + data
    uname = username.encode() if username else b"guest"
    body += struct.pack("<H", len(uname)) + uname
    pword = password.encode() if password else b"guest"
    body += struct.pack("<H", len(pword)) + pword
    # write_blob_variable: u16 LE length + data
    bf_key = bytes(56)  # empty blowfish key
    body += struct.pack("<H", len(bf_key)) + bf_key
    # context string
    ctx = b"guest"
    body += struct.pack("<H", len(ctx)) + ctx
    # nonce
    body += struct.pack("<I", nonce)
    
    # Build packet: prefix(4) + flags(2) + element_id(1) + body
    pkt_len = 4 + 2 + 1 + len(body)
    buf = bytearray(pkt_len + 8)  # extra padding for prefix computation
    struct.pack_into("<H", buf, 4, 0)  # flags=0
    buf[6] = 0x00  # LOGIN_REQUEST element ID
    buf[7:7+len(body)] = body
    
    prefix = compute_prefix(buf)
    struct.pack_into("<I", buf, 0, prefix)
    return bytes(buf[:pkt_len])

# Test
print("=== BigWorld Protocol Test ===")
print()

# DNS resolution
servers = {
    "EU": ["login.p1.worldoftanks.eu", "login.p2.worldoftanks.eu", 
           "login.p3.worldoftanks.eu", "login.p5.worldoftanks.eu"],
}

for region, hosts in servers.items():
    print(f"[{region}]")
    for host in hosts:
        try:
            ip = socket.getaddrinfo(host, None)[0][4][0]
            print(f"  {host} -> {ip}")
        except:
            print(f"  {host} -> DNS FAILED")
    print()

# Build packets
ping = build_ping(0)
print(f"Ping packet: {ping.hex()} ({len(ping)} bytes)")

login = build_login_request(protocol=0x0144)
print(f"Login packet: {login.hex()[:40]}... ({len(login)} bytes)")
print()

# Test key ports
host = "login.p3.worldoftanks.eu"  # EU server 3
print(f"Testing {host}:")

# UDP ports
for port in [443, 5222, 20018, 20019, 20020]:
    for pkt_name, pkt in [("ping", ping), ("login", login)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.sendto(pkt, (host, port))
            try:
                data, addr = s.recvfrom(4096)
                print(f"  UDP {port} [{pkt_name}]: RESPONSE! {len(data)} bytes: {data[:30].hex()}")
            except socket.timeout:
                pass
            s.close()
        except:
            pass
    print(f"  UDP {port}: no response")

# TCP ports  
print()
for port in [443, 5222]:
    try:
        s = socket.create_connection((host, port), timeout=3)
        print(f"  TCP {port}: CONNECTED!")
        s.close()
    except Exception as e:
        print(f"  TCP {port}: {e}")

print()
print("=== DONE ===")
