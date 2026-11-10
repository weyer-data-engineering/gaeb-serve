FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build

WORKDIR /src
RUN python -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-cache-dir . \
	&& /opt/venv/bin/pip uninstall -y pip

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

LABEL org.opencontainers.image.source="https://github.com/weyer-data-engineering/gaeb-serve" \
	org.opencontainers.image.description="Reads GAEB DA XML tenders and writes priced X84 bids" \
	org.opencontainers.image.licenses="MIT"

COPY --from=build /opt/venv /opt/venv
RUN useradd --system --uid 10001 --no-create-home gaeb

ENV PATH="/opt/venv/bin:$PATH" \
	PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	GAEB_WORKERS=2

USER 10001
EXPOSE 5002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
	CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5002/health', timeout=4)"]
CMD ["sh", "-c", "exec uvicorn gaeb_serve.app:app --host 0.0.0.0 --port 5002 --workers ${GAEB_WORKERS} --no-access-log"]
