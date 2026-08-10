"""
simple-quack-server: a thin, long-lived runtime for DuckDB's `quack_serve`.

DuckDB's quack extension turns any DuckDB session into a server, but provides
no daemon: serving lasts only while the calling process holds its connection.
This script is that process. It attaches a database (DuckLake first-class),
starts `quack_serve`, and stays alive until SIGTERM.

Configuration is taken from environment variables (typically injected from a
Kubernetes Secret):

    QUACK_TOKEN            required. Client authentication token.
    QUACK_PORT             listen port (default: 9494).

    Database attachment, either:
    INIT_SQL_FILE          path to a SQL file executed at startup (generic), or
    CATALOG_HOST           DuckLake catalog (PostgreSQL) coordinates:
    CATALOG_PORT           (default: 5432)
    CATALOG_DB
    CATALOG_USER
    CATALOG_PASSWORD
    S3_ENDPOINT            object storage for DuckLake data files
    S3_ACCESS_KEY
    S3_SECRET_KEY
    DATA_PATH              e.g. s3://ducklake/main
    S3_USE_SSL             (default: false)
    S3_REGION              (default: us-east-1)
    ATTACH_ALIAS           (default: lake)
"""

import os
import signal
import sys
import threading

import duckdb


def env(key: str, default: str | None = None) -> str:
    value = os.environ.get(key, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable: {key}")
    return value


def env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes")


def attach_database(con: duckdb.DuckDBPyConnection) -> str:
    """
    Attach the served database and return its alias.
    """
    init_sql_file = os.environ.get("INIT_SQL_FILE")
    if init_sql_file:
        con.execute(open(init_sql_file).read())
        print(f"executed init SQL from {init_sql_file}", flush=True)
        return env("ATTACH_ALIAS", "lake")

    con.execute("INSTALL ducklake; INSTALL postgres; INSTALL httpfs;")
    endpoint = env("S3_ENDPOINT").removeprefix("http://").removeprefix("https://")
    use_ssl = "true" if env_bool("S3_USE_SSL", False) else "false"
    con.execute(
        f"""
        CREATE OR REPLACE SECRET quack_storage (
            TYPE s3,
            KEY_ID '{env("S3_ACCESS_KEY")}',
            SECRET '{env("S3_SECRET_KEY")}',
            ENDPOINT '{endpoint}',
            URL_STYLE 'path',
            USE_SSL {use_ssl},
            REGION '{env("S3_REGION", "us-east-1")}'
        )
        """
    )
    alias = env("ATTACH_ALIAS", "lake")
    attach = (
        f"ducklake:postgres:host={env('CATALOG_HOST')} port={env('CATALOG_PORT', '5432')} "
        f"dbname={env('CATALOG_DB')} user={env('CATALOG_USER')} password={env('CATALOG_PASSWORD')}"
    )
    con.execute(f"ATTACH '{attach}' AS {alias} (DATA_PATH '{env('DATA_PATH')}/')")
    print(f"attached DuckLake as '{alias}'", flush=True)
    return alias


def main() -> None:
    con = duckdb.connect()
    alias = attach_database(con)

    con.execute("INSTALL quack; LOAD quack;")
    port = env("QUACK_PORT", "9494")
    uri = f"quack:0.0.0.0:{port}"
    token = env("QUACK_TOKEN")
    # allow_other_hostname: binding beyond localhost is required inside a
    # container; TLS termination is expected at the ingress/proxy layer.
    info = con.execute(
        f"CALL quack_serve('{uri}', token = '{token}', allow_other_hostname = true)"
    ).fetchall()
    print(f"quack server started: {info[0][1]}", flush=True)
    # Remote sessions start in the server's (empty) default database, so each
    # client has to switch to the attached one before unqualified names resolve.
    print(
        f"clients: ATTACH 'quack:<host>' AS {alias} (TOKEN '...'); "
        f"FROM {alias}.query('USE {alias}');",
        flush=True,
    )

    stop = threading.Event()

    # Only set the event here: I/O from a signal handler can collide with a
    # server thread writing to stdout (RuntimeError: reentrant call).
    def handle_term(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    # Poll instead of a plain wait(): while quack_serve is running, a blocking
    # wait() with no timeout never lets the main thread process SIGTERM, so the
    # pod would hang until SIGKILL.
    while not stop.wait(timeout=1):
        pass
    print("received shutdown signal, stopping...", flush=True)

    try:
        con.execute(f"FROM quack_stop('{uri}')")
        print("quack server stopped", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"quack_stop failed: {e}", flush=True)
    con.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
