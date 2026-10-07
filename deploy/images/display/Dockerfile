# syntax=docker/dockerfile:1
# The display image: a virtual screen, a browser, and `swarm-display`, which the `computer` and
# `browser` tools drive. Cases extend it with `FROM swarmeval/display` and add their own apps,
# started from /etc/swarm-display/start.d/. Contract: README.md beside this file. Build from the
# repo root:
#   docker build -f deploy/images/display/Dockerfile -t swarmeval/display:dev deploy/images/display

FROM python:3.12-slim-bookworm

# Xvfb is the screen; matchbox keeps every window maximized without decorations; xdotool moves
# the mouse and types; xauth writes the screen's cookie. No terminal: the only programs on the
# screen are the browser and what a case adds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb xauth xdotool matchbox-window-manager fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/swarm-display/browsers
RUN pip install --no-cache-dir playwright==1.63.0 pillow==12.3.0 \
    && playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# The display user owns the display's state; agents' users cannot read it, so they reach the
# screen and the browser only through the tools.
RUN useradd --uid 990 --create-home --shell /usr/sbin/nologin swarmdisplay \
    && install -d -o swarmdisplay -g swarmdisplay -m 0700 /run/swarm-display \
    && install -d -m 1777 /tmp/.X11-unix \
    && install -d -m 0755 /etc/swarm-display/start.d

COPY swarm_display /opt/swarm-display/swarm_display
COPY swarm-display /usr/local/bin/swarm-display
RUN chmod 0755 /usr/local/bin/swarm-display \
    && python -m compileall -q /opt/swarm-display/swarm_display
