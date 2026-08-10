"""
Start the server against a local DuckLake, exercise a client, then stop it.

What this covers cannot be checked by `helm test`, because it is about the
process rather than the service:

- SIGTERM stops the server, with exit code 0, both after serving a session and
  without any session at all (a pod that never saw traffic must still stop).
- Shutdown reaches `quack_stop` (a table function, hence `FROM ...`) — misusing
  it fails only in the log, which is easy to miss.

The client roundtrip in between pins the contract clients depend on: remote
sessions start in the server's default database, so `USE <alias>` has to be sent
once per session before unqualified names resolve.

Not covered: `quack_serve` running while the main loop blocks in a wait with no
timeout has been observed to swallow SIGTERM entirely, which is why that loop
polls. That behaviour did not reproduce under a subprocess launcher (direct,
new session, via `uv run`, or detached from a shell), so nothing here guards it.

Run with `just test` (needs the `ducklake` extension, downloaded on first use).
"""

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import duckdb

SERVER = Path(__file__).resolve().parent.parent / "server" / "quack_server.py"
TOKEN = "lifecycle_test_token"
ALIAS = "lake"
STARTUP_TIMEOUT = 90
SHUTDOWN_TIMEOUT = 15


def free_port() -> int:
    """
    Return a port number that is free at the time of the call.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_init_sql(work_dir: Path) -> Path:
    """
    Write an INIT_SQL_FILE attaching a local DuckLake with one seeded table.

    Parameters
    ----------
    work_dir : Path
        Directory holding the catalog file, the data files and the SQL file.

    Returns
    -------
    Path
        Path of the generated SQL file.
    """
    init_sql = work_dir / "init.sql"
    init_sql.write_text(
        "INSTALL ducklake; LOAD ducklake;\n"
        f"ATTACH 'ducklake:{work_dir}/meta.ducklake' AS {ALIAS} "
        f"(DATA_PATH '{work_dir}/data/');\n"
        f"CREATE TABLE {ALIAS}.orders AS SELECT 1 AS id, 'a' AS name;\n"
    )
    return init_sql


def start_server(port: int, init_sql: Path, log_path: Path) -> subprocess.Popen:
    """
    Start the server and wait until it reports that it is serving.

    Parameters
    ----------
    port : int
        Port passed as QUACK_PORT.
    init_sql : Path
        SQL file passed as INIT_SQL_FILE.
    log_path : Path
        File collecting the server's stdout and stderr.

    Returns
    -------
    subprocess.Popen
        The running server process.
    """
    env = {
        **os.environ,
        "QUACK_TOKEN": TOKEN,
        "QUACK_PORT": str(port),
        "INIT_SQL_FILE": str(init_sql),
        "ATTACH_ALIAS": ALIAS,
    }
    log_file = log_path.open("w")
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"server exited during startup:\n{log_path.read_text()}"
            )
        if "quack server started" in log_path.read_text():
            return process
        time.sleep(0.5)
    process.kill()
    raise AssertionError(
        f"server did not start within {STARTUP_TIMEOUT}s:\n{log_path.read_text()}"
    )


def check_client_roundtrip(port: int) -> None:
    """
    Attach as a client, switch to the attached database, then read and write.

    Parameters
    ----------
    port : int
        Port the server listens on.
    """
    con = duckdb.connect()
    con.execute("INSTALL quack; LOAD quack;")
    con.execute(
        f"ATTACH 'quack:localhost:{port}' AS {ALIAS} (TOKEN '{TOKEN}', DISABLE_SSL true)"
    )
    # Without this the names below resolve in the server's empty default database.
    con.execute(f"FROM {ALIAS}.query('USE {ALIAS}')")

    rows = con.execute(f"SELECT id, name FROM {ALIAS}.orders").fetchall()
    assert rows == [(1, "a")], f"unexpected seeded rows: {rows}"

    con.execute(f"INSERT INTO {ALIAS}.orders VALUES (2, 'b')")
    count = con.execute(f"SELECT count(*) FROM {ALIAS}.orders").fetchone()[0]
    assert count == 2, f"expected 2 rows after insert, got {count}"

    # Count server-side: snapshot_time is TIMESTAMPTZ, which the Python client
    # cannot convert without pytz.
    snapshots = con.execute(
        f"FROM {ALIAS}.query('SELECT count(*) FROM {ALIAS}.snapshots()')"
    ).fetchone()[0]
    assert snapshots >= 3, (
        f"expected the insert to commit a snapshot, got {snapshots} in total"
    )
    con.close()


def check_shutdown(process: subprocess.Popen, log_path: Path) -> None:
    """
    Send SIGTERM and require a clean, timely shutdown.

    Parameters
    ----------
    process : subprocess.Popen
        The running server process.
    log_path : Path
        File collecting the server's output.
    """
    process.send_signal(signal.SIGTERM)
    try:
        returncode = process.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        raise AssertionError(
            f"server ignored SIGTERM for {SHUTDOWN_TIMEOUT}s:\n{log_path.read_text()}"
        ) from None
    log = log_path.read_text()
    assert returncode == 0, f"expected exit code 0, got {returncode}:\n{log}"
    assert "received shutdown signal" in log, f"shutdown was not signalled:\n{log}"
    assert "quack server stopped" in log, f"quack_stop did not succeed:\n{log}"


def run_scenario(work_dir: Path, name: str, with_client: bool) -> None:
    """
    Start a server, optionally serve a client, and require a clean shutdown.

    Parameters
    ----------
    work_dir : Path
        Directory holding the DuckLake and the server log.
    name : str
        Scenario name used in the log file name and in the output.
    with_client : bool
        Whether to run the client roundtrip before shutting down. Both are
        covered because a pod that never served a session must still terminate.
    """
    lake_dir = work_dir / name
    lake_dir.mkdir()
    log_path = lake_dir / "server.log"
    port = free_port()
    process = start_server(port, write_init_sql(lake_dir), log_path)
    try:
        if with_client:
            check_client_roundtrip(port)
            print("✅ client roundtrip (USE, read, insert, snapshot)")
        if process.poll() is not None:
            raise AssertionError(f"server exited early:\n{log_path.read_text()}")
        check_shutdown(process, log_path)
        print(f"✅ SIGTERM shutdown, {name} (exit 0, quack_stop succeeded)")
    finally:
        if process.poll() is None:
            process.kill()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="quack-lifecycle-") as tmp:
        work_dir = Path(tmp)
        run_scenario(work_dir, "after-a-session", with_client=True)
        run_scenario(work_dir, "without-any-session", with_client=False)


if __name__ == "__main__":
    main()
