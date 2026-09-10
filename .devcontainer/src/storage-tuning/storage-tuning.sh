#!/bin/sh
# storage-tuning: run apply-tunables.py on the VM *host* of this dev container.
#
# Installed by the storage-tuning dev container Feature as
# /usr/local/share/storage-tuning/storage-tuning (also on PATH) and wired to
# postStartCommand. It does nothing unless opted in through the environment:
#
#   ENABLE_STORAGE_TUNING   apply readahead / FUSE / dirty-page tunables on the host
#   ENABLE_STORAGE_PREWARM  prefetch the docker root's allocated blocks into the
#                           host's local read cache
#
# In Codespaces both are meant to be *user secrets*, which arrive as environment
# variables. Any value other than "", 0, false, no, off or disabled enables.
# `--tune` / `--prewarm` do the same from the command line.
#
# How the host is reached: run-on-host.py talks to the host docker daemon over
# the Docker Engine API on the bind-mounted host socket (the Feature mounts
# /var/run/docker.sock at /var/run/docker-host.sock; no docker CLI is involved
# and /var/run/docker.sock is left alone, so docker-in-docker keeps working).
# It starts a throw-away privileged helper container with --pid=host
# --userns=host, and `nsenter -t 1 -m -u -i -n -p` moves into PID 1's
# namespaces. This is deliberately NOT `chroot /host`: a chroot keeps the
# container's mount and user namespace, and several of the knobs this touches
# (remount, /proc/sys/vm, FUSE connection files, mountinfo of loop backing
# files) resolve against the *current* namespaces. After the switch the script
# verifies that its mnt, user and pid namespaces are PID 1's and refuses otherwise.
# apply-tunables.py itself is streamed over stdin and run by the host's python3,
# so nothing is copied onto the host.
#
# Exit status is always 0 unless --strict is passed through: this runs in the
# container start-up path and none of the tunables can corrupt data.
set -u

TAG="[storage-tuning]"
SHARE="${STORAGE_TUNING_SHARE:-/usr/local/share/storage-tuning}"
SCRIPT="$SHARE/apply-tunables.py"
RUNNER="$SHARE/run-on-host.py"

log() { printf '%s %s\n' "$TAG" "$*"; }

# Feature options land here at install time; environment wins over the file.
if [ -r "$SHARE/config.env" ]; then
    # shellcheck disable=SC1091
    . "$SHARE/config.env"
fi
: "${STORAGE_TUNING_DOCKER_SOCKET:=/var/run/docker-host.sock}"
: "${STORAGE_TUNING_HELPER_IMAGE:=}"
: "${STORAGE_TUNING_WARM_READERS:=2}"
: "${STORAGE_TUNING_VERBOSE:=false}"
: "${STORAGE_TUNING_TIMEOUT:=900}"

truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        ""|0|false|no|off|disabled) return 1 ;;
        *) return 0 ;;
    esac
}

usage() {
    cat <<EOF
usage: storage-tuning [--tune] [--prewarm] [apply-tunables.py options...]

  --tune      same as ENABLE_STORAGE_TUNING=1
  --prewarm   same as ENABLE_STORAGE_PREWARM=1
  -h, --help  this text

Everything else (e.g. --dry-run, --verbose, --strict, --readers N, --target PATH)
is passed to apply-tunables.py on the host. With neither --tune/--prewarm nor the
matching environment variables set, nothing is done.
EOF
}

tune=; warm=; strict=
truthy "${ENABLE_STORAGE_TUNING:-}" && tune=1
truthy "${ENABLE_STORAGE_PREWARM:-}" && warm=1
truthy "$STORAGE_TUNING_VERBOSE" && set -- --verbose "$@"

# Peel off our own flags; keep the rest for the script.
n=$#
while [ "$n" -gt 0 ]; do
    a=$1; shift; n=$((n - 1))
    case "$a" in
        --tune) tune=1 ;;
        --prewarm|--warm) warm=1 ;;
        -h|--help) usage; exit 0 ;;
        --strict) strict=1; set -- "$@" "$a" ;;
        *) set -- "$@" "$a" ;;
    esac
done

fail_soft() {
    log "$*"
    [ -n "$strict" ] && exit 1
    exit 0
}

if [ -z "$tune" ] && [ -z "$warm" ]; then
    log "neither ENABLE_STORAGE_TUNING nor ENABLE_STORAGE_PREWARM is set; nothing to do." \
        "Set them as Codespaces user secrets (or pass --tune/--prewarm) to opt in."
    exit 0
fi
if [ -n "$tune" ] && [ -n "$warm" ]; then
    set -- --warm "$@"
elif [ -n "$warm" ]; then
    set -- --warm-only "$@"
fi
case " $* " in
    *" --readers "*) ;;
    *) set -- --readers "$STORAGE_TUNING_WARM_READERS" "$@" ;;
esac

[ -r "$SCRIPT" ] || fail_soft "$SCRIPT is missing; the Feature was not installed correctly"
[ -r "$RUNNER" ] || fail_soft "$RUNNER is missing; the Feature was not installed correctly"
command -v python3 >/dev/null 2>&1 || fail_soft "python3 not found in the container; it is needed to talk to the docker socket"

sock=$STORAGE_TUNING_DOCKER_SOCKET
[ -S "$sock" ] || fail_soft "host docker socket $sock is not present; is the bind mount of /var/run/docker.sock in place?"

# The helper only needs `nsenter`; everything else runs from the host's own root
# filesystem. run-on-host.py reuses this dev container's image unless
# STORAGE_TUNING_HELPER_IMAGE names another, so no pull is needed.
image=$STORAGE_TUNING_HELPER_IMAGE

# Forward the tuning knobs apply-tunables.py understands, if set here.
envs=""
for v in INNER_RA LOOP4_RA MID_RA LOOP3_RA REMOTE_RA FUSE_RA FUSE_MAX_BACKGROUND FUSE_CONGESTION_THRESHOLD \
         VM_DIRTY_BACKGROUND_BYTES VM_DIRTY_BYTES VM_VFS_CACHE_PRESSURE WARM_READERS WARM_CHUNK_KB ASSUME_REMOTE; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || continue
    case "$val" in
        *[!A-Za-z0-9_.x-]*) log "ignoring $v: unsafe characters"; continue ;;
    esac
    envs="$envs $v=$val"
done

# Runs on the host after nsenter (host /bin/sh). $0 is a label, $@ the script args.
# shellcheck disable=SC2016
host_cmd='
tag="[storage-tuning]"
for ns in mnt user pid; do
    me=$(readlink /proc/self/ns/$ns 2>/dev/null); init=$(readlink /proc/1/ns/$ns 2>/dev/null)
    if [ -z "$me" ] || [ -z "$init" ]; then
        echo "$tag refusing to run: cannot read /proc/1/ns/$ns (or our own); not verifiably on the host" >&2
        exit 0
    fi
    if [ "$me" != "$init" ]; then
        echo "$tag refusing to run: $ns namespace differs from PID 1 ($me vs $init); nsenter did not reach the host" >&2
        exit 0
    fi
done
comm=$(cat /proc/1/comm 2>/dev/null)
case "$comm" in
    systemd|init) ;;
    *) echo "$tag warning: PID 1 on the docker host is \"$comm\", not systemd; is this really the VM host?" >&2 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
    echo "$tag host has no python3; apply-tunables.py cannot run there" >&2
    exit 0
fi
exec python3 - "$@"
'

log "running apply-tunables.py $* on the docker host (helper image ${image:-<own image>}, via $sock)"

# run-on-host.py reads the daemon's answers itself, kills the helper container
# after STORAGE_TUNING_TIMEOUT seconds (killing it also kills the nsenter'ed
# host processes, which stay in the helper's cgroup) and falls back to sudo if
# the socket is not writable by this user.
# shellcheck disable=SC2086
python3 "$RUNNER" --socket "$sock" --image "$image" --name "storage-tuning-$$" \
    --timeout "$STORAGE_TUNING_TIMEOUT" --stdin "$SCRIPT" -- \
    env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin $envs \
    sh -c "$host_cmd" storage-tuning "$@"
rc=$?

if [ "$rc" -ne 0 ]; then
    log "helper exited with status $rc"
    [ -n "$strict" ] && exit "$rc"
fi
exit 0
