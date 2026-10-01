# Probe image for the Q11 checks: the stand-in gateway and the sandboxes both use it.
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends nftables iproute2 dnsutils \
    && rm -rf /var/lib/apt/lists/*
COPY tproxy_probe.py /probe.py
