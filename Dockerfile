# Copyright 2025 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

# RootCanal virtual Bluetooth controller.
#
# This image builds the full controller from source with Bazel, then runs it
# through the bundled Python wrapper (`python -m rootcanal`).
#
# Build context = repository root:
#     docker build -f Dockerfile -t rootcanal .
# or, via the bundled compose file:
#     docker compose -f docker-compose.yml up --build

###############################################################################
# Stage 1: builder - compile the `:rootcanal` binary (and FFI) with Bazel and
# assemble the Python `rootcanal` package with the binaries as resources.
###############################################################################
FROM ubuntu:24.04 AS builder

# Bazel version pinned to match the project (Bzlmod). Keep in sync with
# docker-compose.yml.
ARG BAZEL_VERSION=9.2.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
        build-essential \
        python3 \
        python3-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install bazelisk (versioned bazel launcher) as `bazel`.
RUN curl -fsSL -o /usr/local/bin/bazel \
        "https://github.com/bazelbuild/bazelisk/releases/download/v1.19.0/bazelisk-linux-amd64" \
    && chmod +x /usr/local/bin/bazel

ENV USE_BAZEL_VERSION=${BAZEL_VERSION}

WORKDIR /src

# Copy the whole repository (respecting .dockerignore).
COPY . .

# Compile the controller and the FFI library. The first build fetches the
# C++/Rust toolchains and third-party dependencies via Bzlmod (network needed).
RUN bazel build //:rootcanal //:librootcanal_ffi.so

# Assemble the Python package layout that `binaries.py` expects:
#   bin/linux-x86_64/rootcanal
#   bin/linux-x86_64/librootcanal_ffi.so
RUN set -eux; \
    mkdir -p /pkg/rootcanal/packets /pkg/rootcanal/bin/linux-x86_64; \
    cp py/src/rootcanal/*.py /pkg/rootcanal/; \
    cp py/src/rootcanal/packets/*.py /pkg/rootcanal/packets/; \
    cp bazel-bin/rootcanal /pkg/rootcanal/bin/linux-x86_64/rootcanal; \
    cp bazel-bin/librootcanal_ffi.so /pkg/rootcanal/bin/linux-x86_64/librootcanal_ffi.so; \
    chmod +x /pkg/rootcanal/bin/linux-x86_64/rootcanal; \
    test -x /pkg/rootcanal/bin/linux-x86_64/rootcanal

###############################################################################
# Stage 2: runtime - python + the assembled package; entrypoint is the Python
# wrapper `python -m rootcanal`.
###############################################################################
FROM python:3.12-slim

# Bundle the assembled `rootcanal` package (with binaries as resources).
COPY --from=builder /pkg/rootcanal /opt/rootcanal/rootcanal

# Make the `rootcanal` package importable and its binary resources discoverable.
ENV PYTHONPATH=/opt/rootcanal

# Default ports exposed by RootCanal (see desktop/root_canal_main.cc):
#   6401  Test channel
#   6402  HCI channel
#   6403  BR/EDR link channel
#   6404  BLE link channel
EXPOSE 6401 6402 6403 6404

# Run as a non-root user with a writable working directory so sniffers can
# write PCAP files.
RUN useradd --uid 3500 --create-home rootcanal \
    && mkdir -p /data \
    && chown rootcanal:rootcanal /data
WORKDIR /data
USER rootcanal

# `python -m rootcanal` forwards its arguments to the bundled controller binary.
ENTRYPOINT ["python", "-m", "rootcanal"]