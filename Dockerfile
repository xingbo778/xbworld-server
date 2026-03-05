###############################################################################
# XBWorld Backend Docker image
#
# Contains the freeciv C server + Python agent orchestrator + WS proxy.
#
# Build:
#   docker build -t xbworld-backend .
#
# Run:
#   docker run -p 8080:8080 xbworld-backend
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
COPY freeciv/ /build/freeciv/
WORKDIR /build/freeciv
RUN if [ ! -f freeciv/meson.build ]; then \
        rm -rf freeciv && \
        git clone --depth 1 --branch xbworld \
            https://github.com/xingbo778/freeciv.git freeciv; \
    fi
RUN meson setup build freeciv -Dserver='freeciv-web' \
        -Dclients=[] -Dfcmp=cli -Djson-protocol=true -Dnls=false \
        -Daudio=none -Dtools=manual \
        -Dproject-definition=../freeciv-web.fcproj \
        -Ddefault_library=static -Dprefix=/opt/freeciv \
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
     agent.py agent_tools.py game_client.py decision_engine.py \
     llm_providers.py state_api.py main.py multi_main.py \
     run_remote.py requirements.txt /app/
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
