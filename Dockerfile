###############################################################################
# XBWorld Game Server Docker image
#
# Contains the freeciv C server + WebSocket proxy. No LLM/agent dependencies.
#
# Build:
#   docker build -t xbworld-server .
#
# Run:
#   docker run -p 8080:8080 xbworld-server
###############################################################################

###############################################################################
# Stage 1: Build the freeciv-web C server binary
###############################################################################
FROM debian:bookworm AS builder

RUN DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        build-essential meson ninja-build pkg-config git ca-certificates \
        libcurl4-openssl-dev libjansson-dev libicu-dev liblzma-dev \
        libzstd-dev libsqlite3-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project definition file (small, stable layer cache)
COPY freeciv/freeciv-web.fcproj /build/freeciv/freeciv-web.fcproj

# Clone freeciv source code from GitHub (deterministic, cacheable)
# Using --branch xbworld-3.4 to get the specific version
RUN git clone --depth 1 --branch xbworld-3.4 \
        https://github.com/xingbo778/freeciv.git /build/freeciv

WORKDIR /build
RUN meson setup build freeciv \
        -Dserver='freeciv-web' \
        -Dclients=[] \
        -Dfcmp=cli \
        -Djson-protocol=true \
        -Dnls=false \
        -Daudio=none \
        -Dtools=manual \
        -Dproject-definition=freeciv-web.fcproj \
        -Ddefault_library=static \
        -Dprefix=/opt/freeciv \
        -Doptimization=3 && \
    ninja -C build && \
    ninja -C build install

###############################################################################
# Stage 2: Slim runtime image (backend only)
###############################################################################
FROM python:3.11-slim-bookworm

RUN DEBIAN_FRONTEND=noninteractive apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        libcurl4 libjansson4 libicu72 libsqlite3-0 liblzma5 libzstd1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/freeciv /opt/freeciv

COPY server.py ws_proxy.py standalone_proxy.py config.py \
     game_client.py requirements.txt /app/
COPY static/ /app/static/
COPY data/ /app/data/

WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -s /bin/bash xbworld && \
    chown -R xbworld:xbworld /app /opt/freeciv

ENV FREECIV_BIN=/opt/freeciv/bin/freeciv-web \
    FREECIV_DATA_PATH=/opt/freeciv/share/freeciv/ \
    PYTHONUNBUFFERED=1

USER xbworld
EXPOSE 8080

CMD ["python3", "server.py", "--host", "0.0.0.0"]
