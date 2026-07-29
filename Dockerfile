# syntax=docker/dockerfile:1
# The OpenHands agent-canvas GUI with the pinned judgment-pack release baked in.
# The runtime binary is fetched from the official GitHub release so this image
# installs judgment-pack exactly the way any user would.

ARG AGENT_CANVAS_VERSION=1.6.1

FROM alpine:3.20 AS fetch
ARG JUDGMENT_PACK_VERSION=0.4.0
ARG TARGETARCH=amd64
RUN mkdir -p /out \
 && wget -qO /tmp/jp.tar.gz "https://github.com/Judgment-Pack/judgment-pack-runtime/releases/download/v${JUDGMENT_PACK_VERSION}/judgment-pack_${JUDGMENT_PACK_VERSION}_linux_${TARGETARCH}.tar.gz" \
 && tar -xzf /tmp/jp.tar.gz -C /out judgment-pack jpack

FROM ghcr.io/openhands/agent-canvas:${AGENT_CANVAS_VERSION}
COPY --from=fetch --chmod=755 /out/judgment-pack /out/jpack /usr/local/bin/
