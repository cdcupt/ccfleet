FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ccfleetd ./ccfleetd
COPY ccfleet_agent ./ccfleet_agent
RUN pip install --no-cache-dir . && useradd --system --uid 10001 ccfleet && mkdir -p /data && chown ccfleet /data
USER ccfleet
ENV CCFLEET_BIND=0.0.0.0:8110 CCFLEET_DB=/data/ccfleet.db
VOLUME ["/data"]
EXPOSE 8110
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8110/healthz', timeout=4).status == 200 else 1)"
CMD ["ccfleetd", "serve"]
