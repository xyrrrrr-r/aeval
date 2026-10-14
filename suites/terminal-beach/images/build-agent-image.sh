#!/usr/bin/env bash
# Build + push every image terminal-beach needs, in the order the pins depend on,
# then write the resulting digests back into the suite.
#
# Two levels of images, because the e2b backend uses `[environment] docker_image`
# AS-IS (harbor/environments/definition.py:should_use_prebuilt_docker_image is
# true whenever docker_image is set and --force-build is absent) — the task's own
# environment/Dockerfile never runs on a trial:
#
#   1. terminal-beach-agent        the shared runtime base (deepAgent + the
#                                  offline verifier path), from
#                                  images/agent-image.Dockerfile;
#   2. terminal-beach-<task>       one image per task, built from that task's
#                                  environment/Dockerfile on top of (1), which
#                                  is what actually puts task inputs (sqlite's
#                                  trunc.db) in the sandbox.
#
# All source images are local to the cluster host and every COPY --from
# reference resolves offline, so the build needs no egress.
#
# Usage (on the cluster host, from the suite directory):
#     bash images/build-agent-image.sh
#     TAG=0.1.0 bash images/build-agent-image.sh
set -euo pipefail

cd "$(dirname "$0")/.."
TAG="${TAG:-0.1.0}"
PUSH_HOST="${PUSH_HOST:-harbor:443}"
TASKS=(hello-world openssl-selfsigned-cert sqlite-db-truncate)

for ref in \
    "harbor:443/t-bench/agentlab-deepagent@sha256:89fa77f8df603cd9fb42560efb0b9a7a7ca298cc024d7651bc506b90fc4ddb20" \
    "harbor:443/t-bench/base-pilot@sha256:df100e56e0919321627d16df47edfda3438c04eb40e463c0c0e98c8dac18e3f1"
do
    docker image inspect "$ref" >/dev/null 2>&1 \
        || { echo "FATAL: source image missing locally: $ref" >&2; exit 1; }
done

digest_of() {
    # `docker image inspect` reports RepoDigests for the IMAGE, and two tags can
    # point at one image id (hello-world and openssl-signed-cert differ only by
    # ENV/WORKDIR, so they can share a content digest). Index 0 is therefore not
    # "this repository's" digest — select the entry that names the pushed repo,
    # or the suite would pin openssl's task at hello-world's repository.
    local ref="$1" fallback="$2" repo="${1%:*}" digest
    digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
        "$ref" 2>/dev/null | grep -F "${repo}@" | head -1)
    if [ -n "$digest" ]; then
        printf '%s\n' "$digest"
        return
    fi
    docker image inspect --format '{{index .RepoDigests 0}}' "$fallback"
}

echo "########## 1/3 shared base: terminal-beach-agent ##########"
BASE_LOCAL="harbor:443/t-bench/terminal-beach-agent:${TAG}"
BASE_REMOTE="${PUSH_HOST}/t-bench/terminal-beach-agent:${TAG}"
docker build -t "$BASE_LOCAL" -f images/agent-image.Dockerfile images/

echo "== build-time self-check (the verifier path a sealed trial takes) =="
docker run --rm --entrypoint bash "$BASE_LOCAL" -lc '
  set -euo pipefail
  cd /tmp && cp /opt/verifier/pyproject.toml . && uv sync --offline && uv run pytest --version
  python3 --version; python3.12 --version; node --version; dcode --version | head -1
  test -x /opt/harbor-acp-tools/uv && echo "harbor acp uv: OK"
  test -x /opt/harbor-acp-venv/bin/python && echo "harbor acp venv: OK"
  test "$(cat /workspace/ready)" = "true" && echo "ready seed: OK"
  openssl version >/dev/null && echo "openssl (openssl task needs it): OK"
'

echo "== push base =="
docker tag "$BASE_LOCAL" "$BASE_REMOTE"
docker push "$BASE_REMOTE" >/dev/null
BASE_REF=$(digest_of "$BASE_REMOTE" "$BASE_LOCAL")
echo "shared base: $BASE_REF"

echo "########## 2/3 point the task Dockerfiles at the base digest ##########"
python3 images/pin-digests.py base "$BASE_REF"

echo "########## 3/3 one image per task ##########"
TASK_DIGESTS=()
for task in "${TASKS[@]}"; do
    local_ref="harbor:443/t-bench/terminal-beach-${task}:${TAG}"
    remote_ref="${PUSH_HOST}/t-bench/terminal-beach-${task}:${TAG}"
    docker build -t "$local_ref" -f "tasks/${task}/environment/Dockerfile" "tasks/${task}/environment/"
    # task inputs must be present in the image, not merely in the Dockerfile:
    # the Dockerfile is never executed on a trial (see the header).
    case "$task" in
        sqlite-db-truncate)
            docker run --rm --entrypoint sh "$local_ref" -lc \
                'test -s /app/trunc.db && echo "trunc.db present: $(stat -c %s /app/trunc.db) bytes"' ;;
        openssl-selfsigned-cert)
            docker run --rm --entrypoint sh "$local_ref" -lc 'openssl version' ;;
        hello-world)
            docker run --rm --entrypoint sh "$local_ref" -lc 'test -d /app && test -x /usr/local/bin/dcode && echo "app+cli: OK"' ;;
    esac
    docker tag "$local_ref" "$remote_ref"
    docker push "$remote_ref" >/dev/null
    TASK_DIGESTS+=("$task=$(digest_of "$remote_ref" "$local_ref")")
done

echo "########## 4/4 pin the task images into task.toml ##########"
for entry in "${TASK_DIGESTS[@]}"; do
    python3 images/pin-digests.py task "${entry%%=*}" "${entry#*=}"
done

echo "== pinned =="
python3 images/pin-digests.py show
