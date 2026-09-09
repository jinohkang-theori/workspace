#!/bin/bash
# Auto-generated scenario: the Feature with default options on the base image.
# Runs inside the built container as the remote user via `devcontainer features test`.
set -e

# shellcheck source=/dev/null
source dev-container-features-test-lib

SHARE=/usr/local/share/storage-tuning
WRAPPER="$SHARE/storage-tuning"

check "wrapper installed and on PATH" bash -c "test -x '$WRAPPER' && command -v storage-tuning"
check "script installed" test -x "$SHARE/apply-tunables.py"
check "config has defaults" bash -c "grep -q 'STORAGE_TUNING_DOCKER_SOCKET:=/var/run/docker-host.sock' '$SHARE/config.env' \
    && grep -q 'STORAGE_TUNING_WARM_READERS:=2' '$SHARE/config.env' \
    && grep -q 'STORAGE_TUNING_TIMEOUT:=900' '$SHARE/config.env'"
check "docker CLI present (dependsOn docker-outside-of-docker)" command -v docker
check "apply-tunables.py compiles" python3 -m py_compile "$SHARE/apply-tunables.py"
check "apply-tunables.py --dry-run runs in the container" bash -c "'$SHARE/apply-tunables.py' --dry-run | grep -q 'detected stack'"
check "--help" bash -c "storage-tuning --help | grep -q -- '--prewarm'"

# Gating: with neither secret the hook must do nothing and exit 0.
check "no secrets -> nothing to do, exit 0" bash -c "
    out=\$(env -u ENABLE_STORAGE_TUNING -u ENABLE_STORAGE_PREWARM storage-tuning) && echo \"\$out\" | grep -q 'nothing to do'"
check "falsy secret values are off" bash -c "
    out=\$(ENABLE_STORAGE_TUNING=false ENABLE_STORAGE_PREWARM=0 storage-tuning) && echo \"\$out\" | grep -q 'nothing to do'"

# Opted in but no host socket: still exit 0 (start-up path), exit 1 only with --strict.
check "opted in, missing socket -> logged, exit 0" bash -c "
    out=\$(STORAGE_TUNING_DOCKER_SOCKET=/nonexistent/docker.sock ENABLE_STORAGE_TUNING=1 storage-tuning) \
    && echo \"\$out\" | grep -q 'is not present'"
check "opted in, missing socket, --strict -> exit 1" bash -c "
    ! STORAGE_TUNING_DOCKER_SOCKET=/nonexistent/docker.sock storage-tuning --prewarm --strict >/dev/null"

# Command assembly against a fake docker CLI: the real host must never be touched by tests.
# shellcheck disable=SC2016
check "host invocation is assembled correctly" bash -c '
    set -e
    tmp=$(mktemp -d)
    trap "rm -rf $tmp" EXIT
    python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])" "$tmp/host.sock"
    cat > "$tmp/docker" <<EOF
#!/bin/sh
while [ "\$1" = "-H" ]; do shift 2; done
{ echo "\$*" | tr "\\n" " "; echo; } >> "$tmp/calls"   # one line per call (the host command has newlines)
case "\$1" in
    version) echo 99.0.0 ;;
    inspect) echo sha256:0123456789abcdef ;;
    run) cat > "$tmp/stdin" ;;
esac
EOF
    chmod +x "$tmp/docker"
    out=$(PATH="$tmp:$PATH" STORAGE_TUNING_DOCKER_SOCKET="$tmp/host.sock" STORAGE_TUNING_TIMEOUT=0 \
          ENABLE_STORAGE_TUNING=1 ENABLE_STORAGE_PREWARM=yes INNER_RA=256 storage-tuning --dry-run)
    echo "$out" | grep -q "running apply-tunables.py"
    run=$(grep "^run " "$tmp/calls")
    for want in "--privileged" "--pid=host" "--userns=host" "--entrypoint nsenter" " 0123456789abcdef " \
                "-t 1 -m -u -i -n -p --" "env -i PATH=" " INNER_RA=256 " "sh -c " " --warm " " --readers 2 " " --dry-run"; do
        case "$run" in *"$want"*) ;; *) echo "missing [$want] in: $run"; exit 1 ;; esac
    done
    case "$run" in *chroot*) echo "must not chroot"; exit 1 ;; esac
    cmp -s "$tmp/stdin" /usr/local/share/storage-tuning/apply-tunables.py
'

reportResults
