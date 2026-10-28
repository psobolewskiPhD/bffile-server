# bffile is installed from git, and Java and Bio-Formats are downloaded on first use:
# do both in a build stage of its own, and copy the results into a slim image

# The Java and Bio-Formats versions, in a stage both others start from: the final image
# must ask for the ones the build stage downloaded, or it downloads others at every start.
# Bio-Formats is pinned: to Zulu Java 21, which Bio-Formats releases are built with.
# Also prevent cjdk from treating its index of Java releases as stale
FROM debian:trixie-slim AS base
ENV BIOFORMATS_VERSION=8.5.0 \
    BFF_JAVA_VENDOR=zulu-jre \
    BFF_JAVA_VERSION=21 \
    CJDK_INDEX_TTL=inf

FROM base AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv

# Python with a precompiled standard library, for faster cold starts
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.14 --no-bin --compile-bytecode --no-cache

WORKDIR /app
COPY serve_bffile_zstd.py serve_bffile_zstd.py.lock ./

# Install the locked dependencies (update with: uv lock --script serve_bffile_zstd.py)
RUN uv venv /opt/venv --python 3.14 --managed-python \
    && VIRTUAL_ENV=/opt/venv uv sync --script serve_bffile_zstd.py --active --locked \
        --compile-bytecode --no-cache

# Java and Bio-Formats (with its dependencies) are downloaded on the first JVM start,
# into ~/.cache/cjdk, ~/.m2 and ~/.jgo: start it now, so a cold start downloads nothing
RUN /opt/venv/bin/python -c "from bffile import BioFile; print('Bio-Formats', BioFile.bioformats_version())"

FROM base

# the same paths as in the build stage: the environment's interpreter links to the
# Python, and the Java caches record their own paths. Cloud Run runs the container as
# root, whose home holds the caches
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/venv /opt/venv
COPY --from=build /root/.cache/cjdk /root/.cache/cjdk
COPY --from=build /root/.m2 /root/.m2
COPY --from=build /root/.jgo /root/.jgo
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY serve_bffile_zstd.py ./

# Fail the build if the install is broken, or the JVM won't start from the copied caches
RUN python serve_bffile_zstd.py --help > /dev/null \
    && python -c "from bffile import BioFile; print('Bio-Formats', BioFile.bioformats_version())"

# Defaults; override any BFFILE_SERVE_<OPTION> at deploy time
ENV BFFILE_SERVE_ROOT=/images \
    BFFILE_SERVE_MAX_OPEN=12 \
    BFFILE_SERVE_STATUS=false
RUN mkdir -p /images

ENV PYTHONUNBUFFERED=1

# exec so the server receives Cloud Run's SIGTERM
CMD ["sh", "-c", "exec python /app/serve_bffile_zstd.py --port \"${PORT:-8080}\""]
