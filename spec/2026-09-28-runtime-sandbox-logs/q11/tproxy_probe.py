"""Stand-in for net-gateway in the Q11 checks.

`gateway`: TPROXY listeners on :15001. TCP replies with the original destination it accepted;
UDP reads the original destination from IP_RECVORIGDSTADDR and replies *from* that address.
`dial`: run inside a sandbox to connect somewhere and print what came back.
"""

import select
import socket
import struct
import sys

IP_TRANSPARENT = 19
IP_RECVORIGDSTADDR = 20
PORT = 15001


def gateway() -> None:
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.setsockopt(socket.SOL_IP, IP_TRANSPARENT, 1)
    tcp.bind(("0.0.0.0", PORT))
    tcp.listen()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_IP, IP_TRANSPARENT, 1)
    udp.setsockopt(socket.SOL_IP, IP_RECVORIGDSTADDR, 1)
    udp.bind(("0.0.0.0", PORT))
    print("gateway listening", flush=True)
    while True:
        ready, _, _ = select.select([tcp, udp], [], [])
        if tcp in ready:
            conn, peer = tcp.accept()
            dst = conn.getsockname()
            print(f"tcp {peer[0]}:{peer[1]} -> {dst[0]}:{dst[1]}", flush=True)
            conn.sendall(f"gw saw tcp to {dst[0]}:{dst[1]}\n".encode())
            conn.close()
        if udp in ready:
            data, anc, _, peer = udp.recvmsg(2048, 1024)
            dst = None
            for level, kind, raw in anc:
                if level == socket.SOL_IP and kind == IP_RECVORIGDSTADDR:
                    port, addr = struct.unpack("!2xH4s8x", raw[:16])
                    dst = (socket.inet_ntoa(addr), port)
            print(f"udp {peer[0]}:{peer[1]} -> {dst} {len(data)}B", flush=True)
            assert dst is not None, "no IP_RECVORIGDSTADDR on a TPROXY'd datagram"
            reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            reply.setsockopt(socket.SOL_IP, IP_TRANSPARENT, 1)
            reply.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reply.bind(dst)
            reply.sendto(f"gw saw udp to {dst[0]}:{dst[1]}\n".encode(), peer)
            reply.close()


def dial(proto: str, host: str, port: int) -> None:
    kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    s = socket.socket(socket.AF_INET, kind)
    s.settimeout(3)
    try:
        if proto == "tcp":
            s.connect((host, port))
            print(s.recv(200).decode().strip())
        else:
            s.sendto(b"ping", (host, port))
            data, src = s.recvfrom(200)
            print(f"{data.decode().strip()} (reply from {src[0]}:{src[1]})")
    except OSError as err:
        print(f"{proto} {host}:{port}: {err!r}")


if __name__ == "__main__":
    if sys.argv[1] == "gateway":
        gateway()
    else:
        dial(sys.argv[2], sys.argv[3], int(sys.argv[4]))
