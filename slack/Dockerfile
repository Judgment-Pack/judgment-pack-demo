# syntax=docker/dockerfile:1
#
# The Slack self-serve demo: one container carrying the whole model-free demo
# engine.
#
#   * `jpack` from the released OCI image (runtime ADR-0013) — the same
#     digest-attested artifact any docker-capable MCP client runs. Verify it:
#       gh attestation verify oci://ghcr.io/judgment-pack/judgment-pack:<version> \
#         --repo Judgment-Pack/judgment-pack-runtime
#   * the attestation gateway, built from a pinned commit, which must replay
#     its own frozen corpus (`gateway conform`) before it may be carried;
#   * the derivation rule, built from a pinned commit, which must agree with
#     its own corpus before it may be carried;
#   * the enterprise demo project, baked read-only — each Slack user's session
#     gets a scratch COPY, so their evaluations land in their own audit trail.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, because the image needs projects/ and
# attestation/ as well as slack/:
#
#   docker build -f slack/Dockerfile -t judgment-pack-slack .
#
# The same release the compose demo pins. It must not lag: this image bakes a
# project that declares a reviewed-set lock, and a runtime that predates the
# lock cannot verify it — the build below refuses rather than shipping that.
ARG JUDGMENT_PACK_VERSION=0.13.0
# The gateway and the derivation rule have no release channels yet, so both
# are pinned by commit and built from source — the same pins the compose demo
# uses. When the gateway cuts a release, GATEWAY_REF becomes an attested image
# pin like the runtime's above.
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

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY slack/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The demo engine.
COPY --from=runtime /jpack /usr/local/bin/jpack
COPY --from=gateway-build /out/gateway /usr/local/bin/gateway
COPY --from=derivation-build /out /usr/local/share/derivation-rule
COPY attestation/attest.py /usr/local/bin/attest
COPY attestation/ofac-screening-source.py /usr/local/libexec/ofac-screening-source.py
COPY attestation/watchlist.json /usr/local/share/ofac/watchlist.json
RUN chmod 0755 /usr/local/bin/jpack /usr/local/bin/gateway /usr/local/bin/attest \
      /usr/local/libexec/ofac-screening-source.py

# The demo project, baked read-only. Sessions copy it; nobody edits this one.
COPY projects/enterprise-demo /opt/judgment-pack/enterprise-demo

# The app.
COPY slack/bot /app/bot

# Build-time proof, in the spirit of the two `conform` steps above: the pinned
# runtime must pass its own published conformance corpus offline, and it must
# accept this project's declarations. An image whose runtime disagrees with
# the project it carries would only discover that in front of an audience.
RUN jpack spec test-conformance --quiet \
 && JPACK_CONFIG=/opt/judgment-pack/enterprise-demo/jpack.json jpack packs validate > /dev/null \
 && JPACK_CONFIG=/opt/judgment-pack/enterprise-demo/jpack.json jpack packs verify > /dev/null \
 && jpack version

RUN useradd --uid 10001 --create-home --home-dir /home/app app \
 && chown -R app:app /app /opt/judgment-pack
USER app

ENV DEMO_PROJECT=/opt/judgment-pack/enterprise-demo \
    SESSION_ROOT=/tmp \
    GEMINI_MODEL=gemini-3.6-flash \
    GATEWAY_AUTHORITY=gateway:judgment-pack-slack \
    PORT=8080

EXPOSE 8080
# Secrets arrive as environment variables at run time (Secret Manager, mounted
# by the deploy script). None is baked into this image.
CMD ["python", "-m", "bot.app"]
