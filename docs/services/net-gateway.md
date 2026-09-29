# net-gateway

Go, one instance per run. It is the only neighbor and the default route of every sandbox in its
run. It sees every packet a sandbox sends, applies the case's network policy, intercepts TLS,
answers DNS, and reports every connection, request, and query to the run's worker. Its place
among the services is in [architecture.md](../architecture.md).

**Status:** not built. Arrives in M1, after the docker prerequisites in runtime spec Q11 are
verified. Items marked *(proposed)* go beyond what the specs decided; they are listed under
[Not settled](#not-settled).

## Topology

| Network | Members | Purpose |
|---|---|---|
| One per sandbox | That sandbox and net-gateway, which holds the gateway address | The sandbox's only route. The host has no address here |
| Honeypot network | net-gateway and the run's honeypot and mock containers | Reached only through net-gateway |
| Upstream network | net-gateway | Internet traffic that the policy allows |
| Platform link | net-gateway and its run's worker | The event stream, over mTLS with a per-run certificate |

Putting sandboxes on one shared internal network was rejected for three reasons:

- Sandboxes could reach each other and the honeypots directly, with no event.
- The host's bridge address exposed anything on the host listening on `0.0.0.0`.
- Packets to other addresses were dropped silently, with nobody recording the attempt.

With net-gateway as the router, all three go through it. It does not connect to model-gateway,
Postgres, or any other platform service.

On k8s, NetworkPolicy can only allow or drop and cannot reroute. There, a Pod needs an
in-namespace redirect to net-gateway *(open, runtime spec Q12)*.

## Startup

sandboxd starts the container with a read-only config mount holding the following:

- The policy compiled from `env.yaml` by the worker.
- Sandbox subnets and their `sandbox_id`s.
- Service addresses on the honeypot network.
- The interception CA's certificate and key.
- The per-run mTLS certificate.

The config is fixed for the life of the run; a different policy is a different run *(proposed)*.
net-gateway runs with `NET_ADMIN` in its own network namespace. Sandboxes never get it.

## Packet path

1. **Capture.** At start, nftables rules in net-gateway's own namespace send every TCP and UDP
   packet from a sandbox subnet to local TPROXY listeners, keeping the original destination. The
   host's iptables are never touched.
2. **Accept.** TCP listens with `IP_TRANSPARENT`. UDP reads the original destination with
   `IP_RECVORIGDSTADDR`. If UDP original destinations turn out not to work, the fallback is a
   user-space stack built on gVisor's netstack *(open, runtime spec Q11)*. ICMP and other
   protocols are logged and dropped.
3. **Classify.** Peek at the first bytes: a TLS ClientHello (take the SNI), HTTP (take `Host`),
   DNS on port 53, or raw.
4. **Decide.** Match the policy and act (below). Every TCP connection, UDP datagram flow, HTTP
   request, and DNS query produces a `net.*` event carrying `sandbox_id` and the original
   destination IP and port.

## Policy

Rules match first-hit on sandbox, host or IP, port, protocol, HTTP method and path, and DNS qtype.
A connection with SNI matches by host. One made straight to an IP, or without SNI, matches by IP.
Anything no rule matches follows `default_egress: deny`.

| Action | Behavior |
|---|---|
| `allow` | Forward upstream. `record_body` controls body capture, up to a cap |
| `deny` | Refuse, and record |
| `log_and_deny` | Same as `deny`, marked as a hit worth attention (for example, DNS TXT) |
| `route` | Serve from a service on the honeypot network. The sandbox sees the original host name |
| `delay` | Add latency, then apply the rest of the rule |
| `rewrite` | Modify the response according to the rule |
| `read_only` | Answer from a mock or a recorded snapshot. The real host is never contacted, whatever the method |

Ingress rules inject payloads into responses from named services. Bandwidth limits apply per
sandbox, because every agent in a shared sandbox shares its source address.

## TLS interception

The worker generates the CA per run. With `network_stealth: full`, its subject, validity, and
extensions look like an ordinary enterprise root, with nothing naming an interception tool. With
`disclosed`, the subject says what it is. sandboxd installs the CA in the sandbox's trust store.

net-gateway mints an ECDSA P-256 leaf certificate on demand per SNI and caches it for the run.
After the handshake, the connection is served by `net/http`:

- `allow` forwards through an `http.Transport`.
- `route` forwards to the service's address.

When `tls_intercept` is off, the connection is spliced as is, and only the SNI and byte counts are
recorded.

## DNS

net-gateway serves DNS on the gateway address, over UDP and TCP. A query sent to any other
resolver reaches net-gateway anyway through the packet path.

| Name | Answer |
|---|---|
| Routed to a mock or honeypot | An address from a per-run synthetic pool, which net-gateway maps back to the name when a connection arrives *(proposed)* |
| Allowed | Resolved upstream |
| Anything else | `NXDOMAIN`, with latency close to a real resolver's, so the absence of a name does not give the gateway away |

Every query is a `net.dns` event, qtype included.

## pcap

net-gateway captures on its sandbox-facing interfaces with an `AF_PACKET` socket and writes
standard pcap. The file is cut into chunks, and each chunk goes to the worker as a blob. The
event refers to it by hash.

## Event stream

gRPC service `swarmeval.netgw.v1.NetEvents` on the platform link. Mutual TLS uses a certificate
issued for this run only, so the connection can carry this run's events and nothing else. The
worker dials net-gateway, and on takeover the new owner dials again with a higher `owner_epoch`,
which replaces the old stream *(direction proposed)*.

Each connection, request, or query is held until its event is acked. The worker commits `net.*`
events in groups about every 100 ms and acks each group *(open, runtime spec Q2)*. With no stream
attached, new traffic is not let through. Whether it waits or is refused while no worker is
attached is *(open, runtime spec Q13)*.

## Tech choices

| Need | Choice | Why |
|---|---|---|
| TLS interception, HTTP | Standard library: `crypto/tls`, `crypto/x509`, `net/http`, `net/http/httputil` | Complete, and nothing a proxy library would add is needed |
| DNS | `codeberg.org/miekg/dns` (v2) | The standard Go DNS library. v1 on GitHub is in maintenance mode, and new work goes to v2 |
| nftables rules | `sigs.k8s.io/knftables` | Maintained by Kubernetes SIG Network and used by kube-proxy. It drives the `nft` binary, which ships in the image |
| Sockets (`IP_TRANSPARENT`, `AF_PACKET`) | `golang.org/x/sys/unix` | The standard syscall package |
| pcap writing | Our own code | The file format is a 24-byte header plus 16 bytes per packet. `google/gopacket` is inactive, and its forks are fragmented |
| Bandwidth limits | `golang.org/x/time/rate` | The standard token bucket |
| RPC | `google.golang.org/grpc` | Shared with the other Go services |

mitmproxy was the original plan and is not used: its default CA names itself, and the gateway
needs a transparent layer below HTTP that a Python proxy would not give us.

## Not settled

1. The policy fixed for the life of a run.
2. Synthetic DNS answers for routed names.
3. The worker dialing net-gateway, rather than the reverse.
4. Waiting versus refusing when no worker is attached (runtime spec Q13).
