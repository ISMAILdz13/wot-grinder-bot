import socket,struct,sys

host = sys.argv[1] if len(sys.argv)>1 else "login.p1.worldoftanks.eu"
port = int(sys.argv[2]) if len(sys.argv)>2 else 443

# BigWorld Ping: prefix(4)+flags(2)+element_id(1=PING)+ping_num(1)
pkt = struct.pack("<IH", 0, 0) + bytes([0x02, 0x01])

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(10)
print(f"Sending UDP ping to {host}:{port}...")
s.sendto(pkt, (host, port))
try:
    data, addr = s.recvfrom(4096)
    print(f"RESPONSE: {len(data)} bytes from {addr}")
    print(f"Hex: {data.hex()}")
    print("UDP WORKS!")
except socket.timeout:
    print("TIMEOUT - UDP blocked or server didn't respond")
s.close()
