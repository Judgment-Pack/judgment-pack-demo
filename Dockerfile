# syntax=docker/dockerfile:1
# The OpenHands agent-canvas GUI with the pinned judgment-pack release baked in.
# The binary comes from the released OCI image (runtime ADR-0013) — the same
# digest-attested artifact any docker-capable MCP client runs — so this sandbox
# consumes the distribution channel it exists to demonstrate. Verify it with:
#   gh attestation verify oci://ghcr.io/judgment-pack/judgment-pack:<version> \
#     --repo Judgment-Pack/judgment-pack-runtime

ARG AGENT_CANVAS_VERSION=1.6.1
ARG JUDGMENT_PACK_VERSION=0.12.0
# The attestation gateway and the derivation rule have no release channels yet,
# so both are pinned by commit and built from source. When the gateway cuts a
# release, GATEWAY_REF becomes an attested image pin like the runtime's above.
ARG GATEWAY_REF=3a9f8a89dbb2a87ebdc25155afe5ea90d2185498
ARG DERIVATION_REF=6f5500fb8e61014632f9b4b85e9ce68fcebf0e39

FROM ghcr.io/judgment-pack/judgment-pack:${JUDGMENT_PACK_VERSION} AS runtime

FROM golang:1.26-alpine AS gateway-build
ARG GATEWAY_REF
ADD https://codeload.github.com/Judgment-Pack/judgment-pack-gateway/tar.gz/${GATEWAY_REF} /tmp/gateway.tar.gz
RUN mkdir -p /src && tar -xzf /tmp/gateway.tar.gz -C /src --strip-components=1
WORKDIR /src/go
# `conform` replays the frozen corpus: this image cannot be built from a
# gateway commit that disagrees with it.
RUN CGO_ENABLED=0 go build -buildvcs=false -o /out/gateway . && /out/gateway conform

FROM python:3.13-alpine AS derivation-build
ARG DERIVATION_REF
ADD https://codeload.github.com/Judgment-Pack/judgment-pack-evaluator-experiments/tar.gz/${DERIVATION_REF} /tmp/experiments.tar.gz
RUN mkdir -p /src && tar -xzf /tmp/experiments.tar.gz -C /src --strip-components=1
WORKDIR /src/derivation-rule
# The reference implementation must agree with its own frozen corpus before
# this image may carry it.
RUN python3 agreement.py && python3 -m unittest test_derive -q \
 && mkdir -p /out/rules && cp derive.py derive_cli.py /out/ \
 && cp rules/screening.rule.json /out/rules/

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
# The attested-screening desk: the gateway binary, the verifier's derivation
# rule, and the demo's glue. Both compose services run this same image; the
# gateway container consults its own copies of the source and watchlist, so
# nothing the sandbox edits feeds them.
COPY --from=gateway-build --chmod=755 /out/gateway /usr/local/bin/gateway
COPY --from=derivation-build /out /usr/local/share/derivation-rule
COPY --chmod=755 attestation/attest.py /usr/local/bin/attest
COPY --chmod=755 attestation/ofac-screening-source.py /usr/local/libexec/ofac-screening-source.py
COPY --chmod=755 attestation/decision-desk-source.py /usr/local/libexec/decision-desk-source.py
COPY --chmod=755 attestation/gateway-up.sh /usr/local/libexec/gateway-up.sh
COPY attestation/watchlist.json /usr/local/share/ofac/watchlist.json
# The decision desk's law: a copy of the project laid down from the BUILD
# CONTEXT — the checkout this image is built from, not HEAD. Build from a reset
# tree (./scripts/reset-demo.sh first) or you bake whatever is in the working
# copy at that moment; the guard below turns that discipline into a build gate
# as soon as the project carries a reviewed-set lock.
#
# Both compose services run this image, so the sandbox holds a copy too — and
# that copy is inert and read-only: the law is left root-owned, so neither
# container's copy can be edited by the runtime user. The desk consults the one
# in ITS container. The copy you can edit is not the copy that judges.
COPY projects/enterprise-demo /usr/local/share/desk/enterprise-demo
# SELF-ARMING GUARD. Inert while the project carries no jpack.lock.json, and a
# hard build gate the moment one is committed (runtime ADR-0019): a build whose
# baked law does not match the reviewed set it declares fails here rather than
# shipping a desk that judges under law nobody reviewed. This is what upgrades
# "build from a clean tree" from operational discipline to a verified bound.
RUN sh -c 'cd /usr/local/share/desk/enterprise-demo \
 && if [ -f jpack.lock.json ]; then jpack packs verify --config jpack.json; fi'
# The desk's project declares an audit directory, so the desk keeps its own
# decision book beside its own law. Without a writable directory here every desk
# evaluation fails closed on the audit write — correctly, and uselessly. Only
# this directory is chowned: it is also the mount point the gateway service
# binds a host path over, so the book survives a container recreate.
RUN mkdir -p /usr/local/share/desk/enterprise-demo/audit \
 && chown "${HOST_UID}:${HOST_GID}" /usr/local/share/desk/enterprise-demo/audit
USER openhands

