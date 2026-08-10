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

# Build the container image
image-build:
    docker build -t ${IMAGE}:${TAG} .

# Push the container image
image-push:
    docker push ${IMAGE}:${TAG}

# Cut a release: GitHub release (tag vX.Y.Z) + image tags X.Y.Z and latest
release version:
    #!/bin/bash
    set -euo pipefail
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
    echo "✅ Released v{{ version }} (image tags: {{ version }}, latest)"

# Log in to ghcr.io using the gh CLI token (needs: gh auth refresh -s write:packages)
registry-login:
    #!/bin/bash
    set -euo pipefail
    gh auth token | docker login ghcr.io --username "$(gh api user --jq .login)" --password-stdin

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
