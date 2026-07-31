import socket, struct

host = "login.p1.worldoftanks.eu"

# CORRECT BigWorld PING packet:
# prefix (u32 LE) = number of bytes AFTER the prefix+flags
# flags (u16 LE) = 0
# element = 0x02 (PING)
# So body = [0x02], prefix = 1
CORRECT_PING = struct.pack('<IH', 1, 0) + bytes([0x02])

# Also try with sequence number style some versions use
ALT_PING = struct.pack('<IH', 2, 0) + bytes([0x02, 0x00])

print(f"Correct PING hex: {CORRECT_PING.hex()}")
print(f"UDP scan on {host}")
print("=" * 50)

for port in [443, 5222, 5223, 20018, 20019, 20020, 50010, 50011]:
    for pkt_name, pkt in [("correct", CORRECT_PING), ("alt", ALT_PING)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(5)
            s.sendto(pkt, (host, port))
            try:
                data, addr = s.recvfrom(4096)
                print(f"  Port {port} [{pkt_name}]: RESPONSE! {len(data)} bytes: {data[:20].hex()}")
            except socket.timeout:
                pass
            s.close()
        except Exception as e:
            print(f"  Port {port}: error {e}")
    print(f"  Port {port}: no response")

# Also try TCP on port 443 (some WoT regions use TCP fallback)
print("\nTCP check:")
for port in [443, 5222, 20018]:
    try:
        s = socket.create_connection((host, port), timeout=5)
        print(f"  TCP {port}: CONNECTED!")
        s.sendall(CORRECT_PING)
        try:
            d = s.recv(4096)
            print(f"    Response: {d[:20].hex()}")
        except:
            print(f"    No TCP response")
        s.close()
    except Exception as e:
        print(f"  TCP {port}: {e}")
