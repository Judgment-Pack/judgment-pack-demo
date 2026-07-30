# syntax=docker/dockerfile:1
# The OpenHands agent-canvas GUI with the pinned judgment-pack release baked in.
# The binary comes from the released OCI image (runtime ADR-0013) — the same
# digest-attested artifact any docker-capable MCP client runs — so this sandbox
# consumes the distribution channel it exists to demonstrate. Verify it with:
#   gh attestation verify oci://ghcr.io/judgment-pack/judgment-pack:<version> \
#     --repo Judgment-Pack/judgment-pack-runtime

ARG AGENT_CANVAS_VERSION=1.6.1
ARG JUDGMENT_PACK_VERSION=0.10.0

FROM ghcr.io/judgment-pack/judgment-pack:${JUDGMENT_PACK_VERSION} AS runtime

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
# The image ships one binary: jpack. MCP settings seeded into ./state before
# the rename launch judgment-pack, which no longer exists here: delete ./state
# (or its agent-canvas settings) so mcp-seed registers jpack on the next boot.
COPY --from=runtime --chmod=755 /jpack /usr/local/bin/jpack
USER openhands

