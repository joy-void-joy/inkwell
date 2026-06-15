# Sandbox image for academic output: the uv base plus the LaTeX toolchain.
#
# tectonic is a static Rust binary, not an apt package on bookworm-slim, so it
# is installed via the official one-line installer. poppler-utils provides
# pdftoppm, which rasterizes the compiled PDF for the rendered Preview tab.
#
# Build with: uv run lup-devtools dev build-sandbox-image
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        pandoc poppler-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN cd /usr/local/bin \
    && curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh

# Prime tectonic's package cache so the first in-sandbox compile is fast and
# does not need to reach CTAN mid-run. Tolerate a priming failure (e.g. no
# network at build time) — the cache simply fills on first real compile.
RUN printf '\\documentclass{article}\\begin{document}prime\\end{document}' > /tmp/prime.tex \
    && (cd /tmp && tectonic prime.tex || true) \
    && rm -f /tmp/prime.*
