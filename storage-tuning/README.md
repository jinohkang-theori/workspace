# Codespace docker-root storage tuning

## The stack (host side)

```
Azure Blob (257 GB image, 1 MiB blocks, on-demand)
  └─ codespaces-fuse-driver  → /mnt/csfs-fuse/<id>/disk.img   (FUSE, 128 KiB max request)
       read cache: /mnt/storage-driver-tmp/fuse-read.cache-*  (sparse, local temp disk)
       writes:     /mnt/data/fuse-writes.img                    (sparse, local data disk)
     └─ /dev/loop3 → ext4 /mnt/cloudenvdata
          └─ dockerlib (32 GB file, ~26 GB allocated, only ~1.9 GB referenced)
             └─ /dev/loop4 → ext4 /var/lib/docker  (+ bind mount /home/ubuntu)
```

Every first-touch read walks that whole chain and ends in a network fetch. The driver's own
background prefetch has a 10 s budget and spends it on the ~24 GB of stale blocks in `dockerlib`,
so it only ever fetched ~1.7 GB of mostly useless data.

## What was slow, and why

| Path | Before | After |
|---|---|---|
| Cold sequential read through loop4 | 15–38 MB/s | ~140 MB/s |
| Driver local-cache hit, sequential | ~440 MB/s | unchanged |
| Cold 1 MiB block miss latency | ~5–7 ms | unchanged (network) |
| Write path (64 MiB + fsync) | ~45–55 MB/s | unchanged (limit is inside the driver) |

Root cause of the sequential number: readahead was 128 KiB at every layer, so each 128 KiB FUSE
request was issued and waited on serially. Raising readahead on the FUSE file and on loop3 to
4 MiB lets the kernel keep ~32 requests in flight; the driver's 16 workers then saturate the link.

## What is applied (`apply-tunables.py`, idempotent, run as root)

The script lives in the dev container Feature at
[`.devcontainer/src/storage-tuning/apply-tunables.py`](../.devcontainer/src/storage-tuning/apply-tunables.py);
this directory keeps the analysis notes, data and helpers.

The script does not assume loop3/loop4/csfs. It walks the stack from the docker
data-root (`/etc/docker/daemon.json` `data-root`, else `/var/lib/docker`):
mount -> block device -> loop backing file -> its mount -> ... following loop devices,
device-mapper/md slaves and partitions, until it reaches a FUSE/network filesystem
(remote leaf), a local disk, or something it does not recognise. It then tunes each
layer by role rather than by name. `--dry-run` prints the detected stack and the planned
writes.

Roles, using the current stack as the example:

- Remote filesystem (FUSE `csfs`, 0:44): `read_ahead_kb = 4096` on its bdi (`REMOTE_RA`); for FUSE
  also connection `max_background 64`, `congestion_threshold 48` (more async readahead in flight).
- Intermediate virtual block devices (loop3) and remote block devices (nbd/ublk/rbd if they ever
  appear): `read_ahead_kb = 4096` (`MID_RA`). Only applied when a remote layer was found below;
  otherwise left at default (override with `ASSUME_REMOTE=1`).
- Innermost virtual block device (loop4): `read_ahead_kb = 128` (`INNER_RA`); 1024/4096 measured
  5–10 % slower on the small-file (overlay2) workload and on a 32 MB single-file read. Real
  disks/partitions in that position are not touched.
- Every block-backed filesystem walked through (`/var/lib/docker`, `/mnt/cloudenvdata`):
  `noatime` remount (atime writes traversed 3 layers). The root filesystem is never remounted.
- `vm.vfs_cache_pressure = 50`, `vm.dirty_background_bytes = 128 MiB`, `vm.dirty_bytes = 512 MiB`,
  only when a remote layer exists (the write path drains at ~50 MB/s; the default 20 % of RAM ≈
  1.6 GB of dirty data would stall fsync/sync for 30 s+).

Failure policy: this runs in the devcontainer critical path, so it always exits 0 (use
`--strict` to get a non-zero exit on failures). Unknown layers stop the walk and are logged with a
`STOP:` marker; everything recognised above them is still tuned. None of the knobs can corrupt
data, so nothing is worth aborting the pipeline for.

## Warm-up (`apply-tunables.py --warm [--readers N]`, or `--warm-only` to skip the tuning)

Uses the same stack walk to find the block device directly under the target filesystem
(`/dev/loop4` today), streams `dumpe2fs` of that device to compute the *allocated* block ranges
of the inner ext4 (~2 GB), and reads them through the device with O_DIRECT in `WARM_CHUNK_KB`
(default 1 MiB, the driver's block size) chunks over `WARM_READERS` (default 2) parallel readers.
This pulls exactly the working set into the driver's local cache and skips the ~21 GB of stale
blocks. Outer ext filesystems on the way down (`/mnt/cloudenvdata`) only get their metadata
touched (a discarded `dumpe2fs` run), since their allocated data *is* the stale bulk. Measured:
12–19 s on first run (network), 6–9 s afterwards (local). After it, everything under
`/var/lib/docker` and `/home/ubuntu` is a local-cache hit; the only "remote" blocks left are
zero-filled journal and inode-table space that the driver answers from its zero-block manifest
without network.

Limits: only ext2/3/4 targets are warmed (other filesystems are logged and skipped); the warm-up
is skipped entirely when no remote layer was detected (`ASSUME_REMOTE=1` overrides), because
there is no local cache to fill. `dumpe2fs` output is consumed line by line and folded into a
1-bit-per-chunk bitmap (4 KiB per 32 GiB), so a large, fragmented filesystem on a small-RAM
codespace does not blow up memory. `--dry-run` reports the chunk count without reading anything.

## Things measured and rejected

- `losetup --direct-io=on /dev/loop4`: +50 % on cold sequential reads, but 8–10 % slower on the
  small-file local-hit workload and worse random-read tail latency. Left off.
- Bigger FUSE bdi `max_ratio` for writes: no change; the ~50 MB/s write ceiling is the driver.
- `fstrim` to shrink the remote data set: the driver reports `fallocate` as not implemented, so
  hole punching never reaches the blob manifest. Only the driver could drop the stale 21 GB.
- Extra reader parallelism beyond one aggressive readahead stream: no gain (network-bound).
- Inner ext4 fragmentation score is 0 (e4defrag): no relayout needed.

## Persistence

- Nothing on the host survives a codespace stop/start (the host VM is ephemeral), so the
  tunables are back at defaults after every restart. Re-run `apply-tunables.py --warm` as
  root on the host.
- The repository lives inside `disk.img` and does persist, so the re-apply is done from the dev
  container: the [`storage-tuning` Feature](../.devcontainer/src/storage-tuning/README.md) installs
  the script and a wrapper wired to `postStartCommand`. Because it reaches the VM host, it is inert
  until the Codespaces user secrets `ENABLE_STORAGE_TUNING` and/or `ENABLE_STORAGE_PREWARM` are set.
  The wrapper does not `chroot /host` (that keeps the container's mount and user namespaces, which
  the remounts, `/proc/sys/vm` writes and FUSE connection files depend on); it starts a privileged
  `--pid=host --userns=host` helper through the host docker socket and `nsenter`s PID 1's
  namespaces, then streams the script to the host's `python3`. `shell.sh` in the repository root
  does the same for an interactive host shell.

## Analysis helpers

- `datamap.py <file> [out]`: SEEK_DATA/SEEK_HOLE map of a sparse file (which blocks are local).
- `usedmap.py <dumpe2fs-output> <out>`: allocated ranges of an ext4 image.
- `mapping.py`: maps loop4 offsets → disk.img offsets → local/remote using the maps above.
- `rand4k.py <base> <span> <n> <seed>`: O_DIRECT random 4 KiB read latency on `/dev/loop4`.
