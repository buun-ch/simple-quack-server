FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/buun-ch/simple-quack-server" \
      org.opencontainers.image.description="A thin, long-lived runtime for DuckDB's Quack protocol (DuckLake first-class)" \
      org.opencontainers.image.licenses="MIT"

RUN useradd --uid 1000 --create-home quack
USER quack
ENV HOME=/home/quack \
    PATH=/home/quack/.local/bin:$PATH

RUN pip install --no-cache-dir --user duckdb==1.5.5

# Bake the required extensions into the image so startup needs no network.
RUN python -c "import duckdb; c = duckdb.connect(); \
    c.execute('INSTALL ducklake; INSTALL postgres; INSTALL httpfs; INSTALL quack;')"

COPY server/quack_server.py /app/quack_server.py

EXPOSE 9494
ENTRYPOINT ["python", "/app/quack_server.py"]
