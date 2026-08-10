set dotenv-filename := ".env.local"

export IMAGE := env("IMAGE", "ghcr.io/buun-ch/simple-quack-server")
export TAG := env("TAG", "dev")
export CHART := "charts/simple-quack-server"

[private]
default:
    @just --list --unsorted

# Run the server locally (configuration via environment variables)
serve:
    uv run --with duckdb python server/quack_server.py

# Start a server on a local DuckLake, run a client roundtrip, then check shutdown
test:
    uv run --with duckdb python tests/lifecycle_test.py

# Build the container image
image-build:
    docker build -t ${IMAGE}:${TAG} .

# Push the container image
image-push:
    docker push ${IMAGE}:${TAG}

# Cut a release: GitHub release (tag vX.Y.Z) + image tags X.Y.Z and latest + chart X.Y.Z
release version:
    #!/bin/bash
    set -euo pipefail
    # The chart's default image tag is appVersion, so chart and image must carry
    # the same version. Check before anything is published.
    CHART_VERSION=$(helm show chart ${CHART} | sed -n 's/^version: //p')
    CHART_APP_VERSION=$(helm show chart ${CHART} | sed -n 's/^appVersion: //p')
    if [ "${CHART_VERSION}" != "{{ version }}" ] || [ "${CHART_APP_VERSION}" != "{{ version }}" ]; then
        echo "Error: ${CHART}/Chart.yaml has version=${CHART_VERSION} appVersion=${CHART_APP_VERSION}," >&2
        echo "       expected {{ version }} for both. Bump Chart.yaml first." >&2
        exit 1
    fi
    if [ -n "$(git status --porcelain)" ]; then
        echo "Error: working tree is not clean. Commit and push first." >&2
        exit 1
    fi
    git push
    gh release create "v{{ version }}" --title "v{{ version }}" --generate-notes
    TAG={{ version }} just image-build
    TAG={{ version }} just image-push
    docker tag ${IMAGE}:{{ version }} ${IMAGE}:latest
    docker push ${IMAGE}:latest
    # After the image, so the chart never references a tag that does not exist.
    just chart-push
    echo "✅ Released v{{ version }} (image tags: {{ version }}, latest; chart {{ version }})"

# Log in to ghcr.io (docker + helm) using the gh CLI token (needs: gh auth refresh -s write:packages)
registry-login:
    #!/bin/bash
    set -euo pipefail
    USERNAME=$(gh api user --jq .login)
    gh auth token | docker login ghcr.io --username "${USERNAME}" --password-stdin
    gh auth token | helm registry login ghcr.io --username "${USERNAME}" --password-stdin

# Package and push the Helm chart to ghcr.io as an OCI artifact
chart-push:
    #!/bin/bash
    set -euo pipefail
    VERSION=$(helm show chart ${CHART} | sed -n 's/^version: //p')
    helm package ${CHART} --destination /tmp
    helm push "/tmp/simple-quack-server-${VERSION}.tgz" oci://ghcr.io/buun-ch/charts
    rm -f "/tmp/simple-quack-server-${VERSION}.tgz"

# Run the container locally (pass -e/--env-file via args)
image-run *args='':
    docker run --rm -p 9494:9494 {{ args }} ${IMAGE}:${TAG}

# List tags published on the registry (anonymous for public images, gh credentials for private)
image-tags:
    #!/bin/bash
    set -euo pipefail
    REPO=${IMAGE#*/}
    get_token() {
        curl -s "$@" "https://ghcr.io/token?scope=repository:${REPO}:pull" \
            | python3 -c "import json, sys; print(json.load(sys.stdin).get('token', ''))"
    }
    TOKEN=$(get_token)
    if [ -z "${TOKEN}" ] && command -v gh &>/dev/null; then
        TOKEN=$(get_token -u "$(gh api user --jq .login):$(gh auth token)")
    fi
    if [ -z "${TOKEN}" ]; then
        echo "Error: could not get a pull token for ${REPO}." >&2
        echo "The image may not be published yet, or your gh token lacks read:packages." >&2
        exit 1
    fi
    curl -s -H "Authorization: Bearer ${TOKEN}" "https://ghcr.io/v2/${REPO}/tags/list" \
        | python3 -c "import json, sys; print('\n'.join(json.load(sys.stdin).get('tags') or ['(no tags found)']))"

# Lint the Helm chart
helm-lint:
    helm lint ${CHART} --set auth.existingSecret=dummy

# Render the Helm chart (override values via args)
helm-template *args='':
    helm template quack ${CHART} {{ args }}

# Install/upgrade into the current kube context
helm-install namespace release='quack' *args='':
    helm upgrade --install {{ release }} ${CHART} \
        -n {{ namespace }} --create-namespace {{ args }}

# Run the chart's test hook against a deployed release (read-only)
helm-test namespace release='quack':
    helm test {{ release }} -n {{ namespace }} --logs

# Uninstall from the current kube context
helm-uninstall namespace release='quack':
    helm uninstall {{ release }} -n {{ namespace }}

# Smoke test a running server (host like localhost:9494; DISABLE_SSL for plain HTTP)
client-test host token disable_ssl='true':
    duckdb :memory: -c " \
        LOAD quack; \
        ATTACH 'quack:{{ host }}' AS remote (TOKEN '{{ token }}', DISABLE_SSL {{ disable_ssl }}); \
        SELECT * FROM remote.query('SELECT 42 AS answer'); \
    "
