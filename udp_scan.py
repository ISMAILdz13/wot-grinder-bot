import socket, struct, sys

host = "login.p1.worldoftanks.eu"
# Try all possible WoT ports over UDP
ports = [443, 5222, 5223, 20018, 20019, 20020, 50010, 50011, 50012, 50013, 50014]

# Simple BigWorld ping packet
pkt = struct.pack("<IH", 0, 0) + bytes([0x02, 0x01])

print(f"UDP port scan on {host}")
print("=" * 50)

for port in ports:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(pkt, (host, port))
        try:
            data, addr = s.recvfrom(4096)
            print(f"  Port {port}: RESPONSE! {len(data)} bytes - {data[:20].hex()}")
        except socket.timeout:
            print(f"  Port {port}: timeout")
        s.close()
    except Exception as e:
        print(f"  Port {port}: error - {e}")

# Also try login.p2
print(f"\nUDP port scan on login.p2.worldoftanks.eu")
for port in [443, 20018, 50010]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(pkt, ("login.p2.worldoftanks.eu", port))
        try:
            data, addr = s.recvfrom(4096)
            print(f"  Port {port}: RESPONSE! {len(data)} bytes")
        except socket.timeout:
            print(f"  Port {port}: timeout")
        s.close()
    except Exception as e:
        print(f"  Port {port}: error - {e}")

# Also try a raw DNS lookup to confirm the VPN is working
print("\nDNS check:")
try:
    import subprocess
    r = subprocess.run(["nslookup", "login.p1.worldoftanks.eu"], capture_output=True, text=True, timeout=5)
    print(r.stdout[:200])
except:
    pass

# Try ICMP ping to confirm VPN
print("\nPing check (ICMP):")
try:
    import subprocess
    r = subprocess.run(["ping", "-c", "2", "-W", "3", "login.p1.worldoftanks.eu"], capture_output=True, text=True, timeout=10)
    print(r.stdout[:300])
except Exception as e:
    print(f"Ping failed: {e}")
