FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="WE.R Lexware Upload Bridge"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /app/requirements.txt \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin bridge
COPY bridge.py run.py archive_import.py config.yaml /app/
COPY static /app/static
COPY LICENSE /usr/share/licenses/wer-lexware-upload/LICENSE
RUN chmod 0555 /app /app/static && chmod 0444 /app/*.py /app/config.yaml /app/requirements.txt /app/static/*
# No published ports. Home Assistant Ingress reaches container port 8099.
# run.py initializes only app-owned storage, then drops to UID/GID 10001.
CMD ["python", "/app/run.py"]
