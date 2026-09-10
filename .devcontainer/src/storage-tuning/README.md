# Codespaces host storage tuning (storage-tuning)

Tunes the storage stack that sits under the docker root of a GitHub Codespaces VM host
(Azure Blob → FUSE driver → loop → ext4 → loop → ext4 `/var/lib/docker`) and optionally
prefetches the docker root's *allocated* blocks into the host's local read cache. Measured on the
default Codespaces stack: cold sequential reads through the docker root go from 15–38 MB/s to
~140 MB/s; a full prewarm of the ~2 GB working set takes 12–19 s. The reasoning and the
measurements are in [`storage-tuning/README.md`](../../../storage-tuning/README.md).

The Feature bundles [`apply-tunables.py`](apply-tunables.py) and a wrapper that runs it **on the
host** at every container start (`postStartCommand`), because nothing on the host survives a
codespace stop/start.

## This touches the VM host: it is off by default

The host is reached through its docker socket. The Feature bind-mounts the host's
`/var/run/docker.sock` at `/var/run/docker-host.sock` itself (declared in
`devcontainer-feature.json`, the same mount
[docker-outside-of-docker](https://github.com/devcontainers/features/tree/main/src/docker-outside-of-docker)
makes) and talks to the daemon over the Docker Engine API with the bundled
[`run-on-host.py`](run-on-host.py), Python standard library only. No docker CLI is installed and
`/var/run/docker.sock` is not touched, so the Feature coexists with
[docker-in-docker](https://github.com/devcontainers/features/tree/main/src/docker-in-docker), which
owns that path (docker-outside-of-docker, by contrast, symlinks it to the host socket).

`run-on-host.py` creates a throw-away `--privileged --pid=host --userns=host` helper container from
this dev container's own image and runs `nsenter -t 1 -m -u -i -n -p` in it to move into PID 1's
namespaces; the script is streamed over stdin to the host's `python3`, so nothing is written onto the
host. It is deliberately **not** `chroot /host`: a chroot keeps the container's mount and user
namespaces, and the remounts, `/proc/sys/vm` writes, FUSE connection files and loop backing-file
lookups all resolve against the *current* namespaces. After the switch the wrapper checks that its
`mnt`, `user` and `pid` namespaces are PID 1's and refuses to continue otherwise. If the socket is not
writable by the container user (it is `root:docker` on the host), `run-on-host.py` re-executes itself
through passwordless `sudo`, which common-utils sets up for the remote user.

Because of that reach, the Feature does nothing unless you opt in with two
[Codespaces user secrets](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces)
(they arrive in the container as environment variables):

| Secret | Effect |
|---|---|
| `ENABLE_STORAGE_TUNING` | apply the tunables (readahead, FUSE queue depth, `noatime`, `vm.dirty_*`, `vm.vfs_cache_pressure`) |
| `ENABLE_STORAGE_PREWARM` | prefetch the docker root's allocated blocks into the host's local cache |

Any value other than empty, `0`, `false`, `no`, `off` or `disabled` enables. Set either or both;
with both, one `apply-tunables.py --warm` run does everything. Grant the secrets to the repositories
whose codespaces should get the tuning. Without the secrets the postStart hook logs one line and
exits 0.

The tunables are idempotent and none can corrupt data; the wrapper always exits 0 (pass `--strict`
to change that) so it never blocks container start-up. Unknown storage layers are logged and left
alone. On a host that is not a Codespaces VM (no remote layer detected) the script leaves the
intermediate readahead and `vm.*` limits at their defaults and skips the prewarm.

## Usage

```jsonc
// .devcontainer/devcontainer.json
{
    "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
    "features": {
        "ghcr.io/jinohkang-theori/workspace/storage-tuning:1": {}
    },
    // Optional: Codespaces prompts for these when creating a codespace.
    "secrets": {
        "ENABLE_STORAGE_TUNING": {
            "description": "Set to 1 to tune the Codespaces VM host storage stack at every start."
        },
        "ENABLE_STORAGE_PREWARM": {
            "description": "Set to 1 to prefetch the docker root's working set into the host's local cache at every start."
        }
    }
}
```

No other Feature is required. Adding `docker-outside-of-docker` for its docker CLI is fine (both
declare the same mount target and the devcontainer CLI deduplicates mounts), and so is
`docker-in-docker`.

From a terminal inside the container:

```sh
storage-tuning --tune --prewarm --dry-run   # show the detected stack and planned writes, change nothing
storage-tuning --tune                       # same as ENABLE_STORAGE_TUNING=1
storage-tuning --prewarm                    # same as ENABLE_STORAGE_PREWARM=1
storage-tuning                              # honours the environment, as postStart does
```

Anything else on the command line (`--dry-run`, `--verbose`, `--strict`, `--readers N`,
`--target PATH`) is passed to `apply-tunables.py`. Its tuning knobs (`INNER_RA`, `MID_RA`,
`REMOTE_RA`, `FUSE_MAX_BACKGROUND`, `FUSE_CONGESTION_THRESHOLD`, `VM_DIRTY_BACKGROUND_BYTES`,
`VM_DIRTY_BYTES`, `VM_VFS_CACHE_PRESSURE`, `WARM_READERS`, `WARM_CHUNK_KB`, `ASSUME_REMOTE`) are
forwarded from the container environment when set, so they can be secrets or `remoteEnv` too.

## Options

| Option | Type | Default | Description |
|---|---|---|---|
| `dockerSocket` | string | `/var/run/docker-host.sock` | Where the host's docker socket is inside the container. The Feature always mounts it at the default; change this only if your configuration already mounts the host socket elsewhere. |
| `helperImage` | string | `""` | Image for the privileged helper container (needs only `nsenter`). Empty reuses this dev container's own image, so no pull happens. |
| `warmReaders` | string | `"2"` | Parallel O_DIRECT readers for the prewarm. |
| `timeoutSeconds` | string | `"900"` | Kill the host-side run after this long; `0` disables. Interrupting it is harmless. |
| `verbose` | boolean | `false` | Pass `--verbose` to `apply-tunables.py`. |

Options are baked into `/usr/local/share/storage-tuning/config.env` at build time; the
environment variables `STORAGE_TUNING_DOCKER_SOCKET`, `STORAGE_TUNING_HELPER_IMAGE`,
`STORAGE_TUNING_WARM_READERS`, `STORAGE_TUNING_TIMEOUT` and `STORAGE_TUNING_VERBOSE` override them
at run time.

## Requirements

- A host whose `/var/run/docker.sock` can be bind-mounted into the dev container (Codespaces, or a
  local Docker host). Pointing `dockerSocket` at a docker-in-docker daemon is not the same thing:
  there PID 1 is the dev container's init, which the wrapper warns about.
- `python3` in the **container** for `run-on-host.py` (installed with `apt-get` if missing on
  Debian/Ubuntu images). The remote user must be able to open the socket, or have passwordless `sudo`.
- `python3` and `dumpe2fs` (e2fsprogs) on the **host**; both are present on Codespaces hosts. The
  prewarm is skipped without `dumpe2fs`, the whole run without `python3`.
- The helper image must have `nsenter` (util-linux or busybox). Every `mcr.microsoft.com/devcontainers`
  image does.

## Files installed

```
/usr/local/share/storage-tuning/storage-tuning     wrapper (postStartCommand), also /usr/local/bin/storage-tuning
/usr/local/share/storage-tuning/apply-tunables.py  streamed to the host's python3
/usr/local/share/storage-tuning/run-on-host.py     Docker Engine API client: privileged helper + nsenter into PID 1
/usr/local/share/storage-tuning/config.env         defaults generated from the Feature options
```
