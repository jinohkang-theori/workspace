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
check "no docker CLI needed" bash -c "! grep -q 'docker run\|docker inspect\|command -v docker' '$WRAPPER'"
check "python3 present" command -v python3
check "apply-tunables.py compiles" env PYTHONPYCACHEPREFIX=/tmp/storage-tuning-pyc python3 -m py_compile "$SHARE/apply-tunables.py"
check "run-on-host.py installed and compiles" bash -c "test -x '$SHARE/run-on-host.py' && PYTHONPYCACHEPREFIX=/tmp/storage-tuning-pyc python3 -m py_compile '$SHARE/run-on-host.py'"
check "run-on-host.py --help" bash -c "'$SHARE/run-on-host.py' --help | grep -q -- '--stdin'"
check "run-on-host.py refuses without a command" bash -c "! '$SHARE/run-on-host.py' --socket /nonexistent.sock 2>/dev/null"
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

# Docker API traffic against a fake daemon (fake-dockerd.py, copied next to this script):
# the real host must never be touched by tests.
# shellcheck disable=SC2016
check "host invocation is assembled correctly" bash -c '
    set -e
    tmp=$(mktemp -d)
    python3 ./fake-dockerd.py "$tmp/host.sock" "$tmp/rec" > "$tmp/fake.log" &
    fake=$!
    trap "kill $fake 2>/dev/null; rm -rf $tmp" EXIT
    for _ in $(seq 50); do [ -S "$tmp/host.sock" ] && break; sleep 0.1; done
    [ -S "$tmp/host.sock" ] || { echo "fake daemon did not start"; cat "$tmp/fake.log"; exit 1; }

    out=$(STORAGE_TUNING_DOCKER_SOCKET="$tmp/host.sock" STORAGE_TUNING_TIMEOUT=0 \
          ENABLE_STORAGE_TUNING=1 ENABLE_STORAGE_PREWARM=yes INNER_RA=256 storage-tuning --dry-run 2>&1)
    echo "$out" | grep -q "running apply-tunables.py"
    echo "$out" | grep -q "fake-host: ran"        # stdout frame demultiplexed
    echo "$out" | grep -q "fake-host: stderr"     # stderr frame demultiplexed
    cmp -s "$tmp/rec/stdin" /usr/local/share/storage-tuning/apply-tunables.py   # script streamed over stdin
    test -e "$tmp/rec/deleted"                    # helper container removed afterwards
    test ! -e "$tmp/rec/killed"
    python3 - "$tmp/rec/create.json" <<EOF
import json, sys
c = json.load(open(sys.argv[1])); hc = c["HostConfig"]
assert hc["Privileged"] is True, hc
for k in ("PidMode", "UsernsMode", "UTSMode", "IpcMode", "CgroupnsMode", "NetworkMode"):
    assert hc[k] == "host", (k, hc)
assert c["Image"] == "sha256:0123456789abcdef", c["Image"]       # this container image, via inspect
assert c["Entrypoint"] == ["nsenter"], c["Entrypoint"]
assert c["OpenStdin"] and c["StdinOnce"] and c["AttachStdin"] and not c["Tty"], c
cmd = c["Cmd"]
assert cmd[:8] == ["-t", "1", "-m", "-u", "-i", "-n", "-p", "--"], cmd
joined = " " + " ".join(cmd) + " "
for want in (" env -i PATH=", " INNER_RA=256 ", " sh -c ", " storage-tuning ", " --warm ", " --readers 2 ", " --dry-run "):
    assert want in joined, (want, joined)
assert "chroot" not in joined, joined                              # must nsenter, never chroot
EOF
'

# Exit status of the host-side run is surfaced, and only --strict makes it fatal.
# shellcheck disable=SC2016
check "helper exit status: logged, fatal only with --strict" bash -c '
    set -e
    tmp=$(mktemp -d)
    FAKE_EXIT=3 python3 ./fake-dockerd.py "$tmp/host.sock" "$tmp/rec" > /dev/null &
    fake=$!
    trap "kill $fake 2>/dev/null; rm -rf $tmp" EXIT
    for _ in $(seq 50); do [ -S "$tmp/host.sock" ] && break; sleep 0.1; done
    export STORAGE_TUNING_DOCKER_SOCKET="$tmp/host.sock" STORAGE_TUNING_TIMEOUT=0
    out=$(storage-tuning --tune --dry-run 2>&1)
    echo "$out" | grep -q "helper exited with status 3"
    rc=0; storage-tuning --tune --dry-run --strict >/dev/null 2>&1 || rc=$?
    [ "$rc" = 3 ] || { echo "expected exit 3 with --strict, got $rc"; exit 1; }
'

# A socket nobody listens on: run-on-host.py fails cleanly (125), the wrapper still exits 0.
# shellcheck disable=SC2016
check "dead socket -> logged, exit 0" bash -c '
    set -e
    tmp=$(mktemp -d)
    trap "rm -rf $tmp" EXIT
    python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])" "$tmp/dead.sock"
    out=$(STORAGE_TUNING_DOCKER_SOCKET="$tmp/dead.sock" storage-tuning --tune 2>&1)
    echo "$out" | grep -q "cannot use"
    echo "$out" | grep -q "helper exited with status 125"
'

reportResults
