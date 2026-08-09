# simple-quack-server

A thin, long-lived runtime for DuckDB's [Quack protocol](https://duckdb.org/docs/current/quack/overview), packaged as a container image and a Helm chart.

DuckDB's quack extension turns any DuckDB session into a server (`CALL quack_serve(...)`), but ships no daemon: serving lasts only while the calling process holds its connection open. This project is that process — nothing more. It attaches a database ([DuckLake](https://ducklake.select/) first-class), starts `quack_serve`, and stays alive until SIGTERM. Authentication, authorization and the protocol itself belong to [duckdb-quack](https://github.com/duckdb/duckdb-quack) and its ecosystem (e.g. [quack-oauth](https://github.com/DataZooDE/quack-oauth)); keeping this server *simple* is a design goal, not a limitation.

Serving a DuckLake this way means clients need only a URL and a token — PostgreSQL (catalog) and object storage (data files) stay private:

```sql
LOAD quack;
ATTACH 'quack:lake.example.com:443' AS remote (TOKEN '<token>');
SELECT * FROM remote.my_table;                                -- reads (mirror views)
SELECT * FROM remote.query('INSERT INTO lake.my_table ...');  -- writes / DDL
```

## How it works

- The server attaches your DuckLake (or runs an arbitrary `INIT_SQL_FILE`) and calls `quack_serve` with `allow_other_hostname = true`. TLS termination is expected at the ingress/proxy layer, as recommended by the quack documentation.
- Remote quack sessions do not inherit the server's default database, so the server mirrors attached tables as views (refreshed periodically) — clients read `remote.<table>` directly. Writes and DDL go through `remote.query('... lake.<table> ...')`.
- One replica = one writer. DuckLake's optimistic concurrency handles concurrent commits from other writers, but this server is intentionally a single process.

## Configuration

All configuration is via environment variables. In Kubernetes they are injected from an existing Secret (`envFrom`); see the Helm section below.

| Variable               | Required | Default     | Description                            |
| ---------------------- | -------- | ----------- | -------------------------------------- |
| `QUACK_TOKEN`          | yes      |             | Client authentication token            |
| `QUACK_PORT`           |          | `9494`      | Listen port                            |
| `CATALOG_HOST`         | yes*     |             | DuckLake catalog (PostgreSQL) host     |
| `CATALOG_PORT`         |          | `5432`      | Catalog port                           |
| `CATALOG_DB`           | yes*     |             | Catalog database name                  |
| `CATALOG_USER`         | yes*     |             | Catalog user                           |
| `CATALOG_PASSWORD`     | yes*     |             | Catalog password                       |
| `S3_ENDPOINT`          | yes*     |             | Object storage endpoint                |
| `S3_ACCESS_KEY`        | yes*     |             | Object storage access key              |
| `S3_SECRET_KEY`        | yes*     |             | Object storage secret key              |
| `DATA_PATH`            | yes*     |             | Data path, e.g. `s3://ducklake/main`   |
| `S3_USE_SSL`           |          | `false`     | Use TLS for object storage             |
| `S3_REGION`            |          | `us-east-1` | Object storage region                  |
| `ATTACH_ALIAS`         |          | `lake`      | Alias of the attached database         |
| `INIT_SQL_FILE`        |          |             | SQL file to run instead of `CATALOG_*` |
| `MIRROR_VIEWS`         |          | `true`      | Mirror attached tables as views        |
| `VIEW_REFRESH_SECONDS` |          | `60`        | Mirror refresh interval                |

\* required unless `INIT_SQL_FILE` is set.

## Helm

The chart references an **existing Secret** for the token and connection settings (`auth.existingSecret`, injected via `envFrom`). Create it with your own tooling — plain `kubectl create secret`, External Secrets Operator, etc.

```bash
kubectl create secret generic quack-server -n quack \
    --from-literal=QUACK_TOKEN=... \
    --from-literal=CATALOG_HOST=postgres-cluster-rw.postgres \
    --from-literal=CATALOG_PORT=5432 \
    --from-literal=CATALOG_DB=ducklake_catalog \
    --from-literal=CATALOG_USER=ducklake \
    --from-literal=CATALOG_PASSWORD=... \
    --from-literal=S3_ENDPOINT=http://rustfs.rustfs:9000 \
    --from-literal=S3_ACCESS_KEY=... \
    --from-literal=S3_SECRET_KEY=... \
    --from-literal=DATA_PATH=s3://ducklake/main

helm install quack charts/simple-quack-server -n quack \
    --set auth.existingSecret=quack-server
```

Expose it with the standard ingress values (`ingress.enabled`, `ingress.hosts`). Remote clients must specify the ingress port explicitly (`quack:host:443`) because the quack default port 9494 is usually not proxied.

## Development

Tools are managed with [mise](https://mise.jdx.dev/): `mise install`.

```bash
just serve          # run locally (configuration via environment variables)
just image-build    # build the image
just helm-lint      # lint the chart
just helm-template  # render the chart
just client-test localhost:9494 <token>   # smoke test a running server
```

## Notes

- Clients assume HTTPS for non-localhost hosts. Inside a cluster (plain HTTP), attach with `(DISABLE_SSL true)`; behind a TLS-terminating ingress, omit it.
- Authentication is a single shared token for now. For OAuth 2.1 / OIDC (per-user tokens, claim-based authorization, audit), see [quack-oauth](https://github.com/DataZooDE/quack-oauth) — planned as an optional integration.

## License

[MIT](./LICENSE)
