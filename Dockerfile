# syntax=docker/dockerfile:1
FROM python:3.12-slim AS source-wheel
ARG SOURCE_COMMIT=
WORKDIR /build
COPY pyproject.toml MANIFEST.in ./
COPY mino_nexus/ ./mino_nexus/
COPY scripts/prepare_build.py ./scripts/prepare_build.py
RUN SOURCE_COMMIT="$SOURCE_COMMIT" python scripts/prepare_build.py \
 && python -m pip install --no-cache-dir build \
 && python -m build --wheel --outdir /wheels

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MINO_NEXUS_MODE=production MINO_NEXUS_DATA_DIR=/data MINO_NEXUS_MDNS=0
COPY --from=source-wheel /wheels/ /wheels/
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels \
 && useradd --uid 10001 --create-home nexus \
 && mkdir /data \
 && chown nexus:nexus /data
WORKDIR /home/nexus
USER 10001:10001
EXPOSE 50104
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:50104/health', timeout=4); assert r.status == 200 and json.load(r)['ok']"]
ENTRYPOINT ["mino-nexus"]
CMD ["--prod", "--host", "0.0.0.0", "--port", "50104", "--data-dir", "/data", "--no-mdns"]
