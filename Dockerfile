# syntax=docker/dockerfile:1
# The OpenHands agent-canvas GUI with the pinned judgment-pack release baked in.
# The runtime binary is fetched from the official GitHub release so this image
# installs judgment-pack exactly the way any user would.

ARG AGENT_CANVAS_VERSION=1.6.1

FROM alpine:3.20 AS fetch
ARG JUDGMENT_PACK_VERSION=0.5.0
ARG TARGETARCH=amd64
RUN mkdir -p /out \
 && wget -qO /tmp/jp.tar.gz "https://github.com/Judgment-Pack/judgment-pack-runtime/releases/download/v${JUDGMENT_PACK_VERSION}/judgment-pack_${JUDGMENT_PACK_VERSION}_linux_${TARGETARCH}.tar.gz" \
 && tar -xzf /tmp/jp.tar.gz -C /out judgment-pack jpack

FROM ghcr.io/openhands/agent-canvas:${AGENT_CANVAS_VERSION}
# Align the container user with the host user so the bind mounts (state/ and
# your packs directory) are writable, and files the agent writes land on your
# disk owned by you.
ARG HOST_UID=1000
ARG HOST_GID=1000
USER root
RUN if [ "$(id -u openhands)" != "${HOST_UID}" ] || [ "$(id -g openhands)" != "${HOST_GID}" ]; then \
      u="$(getent passwd "${HOST_UID}" | cut -d: -f1 || true)"; \
      if [ -n "$u" ] && [ "$u" != "openhands" ]; then usermod -u 61000 "$u"; fi; \
      g="$(getent group "${HOST_GID}" | cut -d: -f1 || true)"; \
      if [ -n "$g" ] && [ "$g" != "openhands" ]; then groupmod -g 61000 "$g"; fi; \
      groupmod -g "${HOST_GID}" openhands; \
      usermod -u "${HOST_UID}" -g "${HOST_GID}" openhands; \
      chown -R "${HOST_UID}:${HOST_GID}" /home/openhands /openhands /workspace /projects; \
    fi
COPY --from=fetch --chmod=755 /out/judgment-pack /out/jpack /usr/local/bin/
USER openhands
