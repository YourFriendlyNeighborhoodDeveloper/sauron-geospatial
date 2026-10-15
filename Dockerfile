ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
COPY tools ./tools
COPY config ./config
COPY tests/fixtures ./tests/fixtures

RUN useradd --create-home --uid 10001 sauron \
    && mkdir -p /app/data /app/reports \
    && chown -R sauron:sauron /app/data /app/reports
USER sauron

EXPOSE 8050 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8050/healthz', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "python -m tools.fetch_gazetteer || echo 'gazetteer unavailable; news locations will stay unresolved'; exec python -m src"]
