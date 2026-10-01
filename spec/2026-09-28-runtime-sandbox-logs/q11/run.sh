#!/usr/bin/env bash
# Runtime spec Open question 11: checks the docker behavior decision 24 depends on.
# Run as root on a Linux docker host with runsc registered. Leaves nothing behind.
set -euo pipefail
cd "$(dirname "$0")"

cleanup() {
  docker rm -f q11-gw >/dev/null 2>&1 || true
  for n in a b c; do docker network rm "q11-$n" >/dev/null 2>&1 || true; done
  [[ -n "${HS:-}" ]] && kill "$HS" 2>/dev/null || true
}
trap cleanup EXIT
cleanup

docker build -q -t q11-probe . >/dev/null
echo "docker $(docker version --format '{{.Server.Version}}'), $(runsc --version | head -1), kernel $(uname -r)"

# a, b: ordinary bridges with no host address. c: the same, but --internal.
net() {
  docker network create --driver bridge $3 --subnet "10.231.$2.0/24" --gateway "10.231.$2.1" \
    -o com.docker.network.bridge.inhibit_ipv4=true -o "com.docker.network.bridge.name=q11-$1" \
    "q11-$1" >/dev/null
}
net a 1 ""; net b 2 ""; net c 3 --internal

echo; echo "## 1. Host side of the bridges"
ip -4 -br addr show | grep q11- || echo "no IPv4 address on any q11-* bridge"
ip route | grep 10.231. || echo "no host route to 10.231.0.0/16"

echo; echo "## 2. Can a container take the gateway address?"
docker run -d --name q11-gw --network q11-a --ip 10.231.1.1 q11-probe true 2>&1 | grep -i error || true
docker rm -f q11-gw >/dev/null 2>&1 || true

echo; echo "## 3. Gateway stand-in: takes .1 itself, TPROXY to :15001"
docker run -d --name q11-gw --network q11-a --ip 10.231.1.2 --cap-add NET_ADMIN \
  --sysctl net.ipv4.conf.all.rp_filter=0 --sysctl net.ipv4.conf.default.rp_filter=0 \
  q11-probe sleep infinity >/dev/null
docker network connect --ip 10.231.2.2 q11-b q11-gw
docker network connect --ip 10.231.3.2 q11-c q11-gw
docker exec q11-gw sh -c '
  ip addr add 10.231.1.1/24 dev eth0; ip addr add 10.231.2.1/24 dev eth1; ip addr add 10.231.3.1/24 dev eth2
  ip rule add fwmark 1 lookup 100; ip route add local 0.0.0.0/0 dev lo table 100
  nft -f - <<NFT
table ip q11 {
  chain pre {
    type filter hook prerouting priority mangle; policy accept;
    ip saddr 10.231.0.0/16 meta l4proto { tcp, udp } meta mark set 1 tproxy to :15001 accept
  }
}
NFT
  ip -4 -br addr | grep -v ^lo'
docker exec -d q11-gw sh -c 'python3 /probe.py gateway > /tmp/gw.log 2>&1'
sleep 1

echo; echo "## 4. Sandbox addressing and routes"
for n in a c; do for rt in runc runsc; do
  echo "-- q11-$n, $rt"
  docker run --rm --runtime $rt --network q11-$n q11-probe sh -c \
    'ip -4 -br addr | grep -v ^lo; ip -4 route | grep default || echo "no default route"; ip -6 addr | grep -v "::1/128" | grep inet6 || echo "no IPv6 beyond loopback"'
done; done

HOSTIP=$(ip -4 -br addr show eth0 | awk '{print $3}' | cut -d/ -f1)
python3 -m http.server 8999 --bind 0.0.0.0 >/dev/null 2>&1 & HS=$!
sleep 1
probes="
  python3 /probe.py dial tcp 93.184.215.14 443
  python3 /probe.py dial tcp 10.231.2.99 22
  python3 /probe.py dial tcp $HOSTIP 8999
  python3 /probe.py dial tcp 172.17.0.1 8999
  python3 /probe.py dial tcp 10.231.1.1 8999
  python3 /probe.py dial udp 1.1.1.1 53
  python3 /probe.py dial udp 203.0.113.7 9999
  python3 /probe.py dial udp 10.231.1.1 53"

echo; echo "## 5. TPROXY: any destination, original address kept (host listens on $HOSTIP:8999)"
for rt in runc runsc; do
  echo "-- $rt"; docker run --rm --runtime $rt --network q11-a q11-probe sh -c "$probes"
done
echo "-- gateway log"; docker exec q11-gw cat /tmp/gw.log

echo; echo "## 6. Gateway frozen: does anything reach the host?"
docker pause q11-gw >/dev/null
for rt in runc runsc; do
  echo "-- $rt"
  docker run --rm --runtime $rt --network q11-a q11-probe sh -c \
    "python3 /probe.py dial tcp $HOSTIP 8999; python3 /probe.py dial tcp 10.231.1.1 8999"
done
docker unpause q11-gw >/dev/null

echo; echo "## 7. Docker's embedded DNS with resolv.conf bind-mounted"
resolv=$(mktemp); echo "nameserver 10.231.1.1" > "$resolv"; chmod 644 "$resolv"
for rt in runc runsc; do
  echo "-- $rt"
  docker run --rm --runtime $rt --network q11-a -v "$resolv:/etc/resolv.conf:ro" q11-probe sh -c '
    echo "resolv.conf: $(cat /etc/resolv.conf)"
    echo "sockets on 127.0.0.11:"; grep -i " 0B00007F:" /proc/net/udp /proc/net/tcp || echo "  none"
    echo "dig @127.0.0.11 q11-gw: $(dig +time=2 +tries=1 +short @127.0.0.11 q11-gw 2>&1 | tail -1)"'
done
rm -f "$resolv"
