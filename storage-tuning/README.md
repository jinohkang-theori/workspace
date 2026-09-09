# Codespace docker-root storage tuning

## The stack (host side)

```
Azure Blob (257 GB image, 1 MiB blocks, on-demand)
  └─ codespaces-fuse-driver  → /mnt/csfs-fuse/<id>/disk.img   (FUSE, 128 KiB max request)
       read cache: /mnt/storage-driver-tmp/fuse-read.cache-*  (sparse, local temp disk)
       writes:     /mnt/data/fuse-writes.img                    (sparse, local data disk)
     └─ /dev/loop3 → ext4 /mnt/cloudenvdata
          └─ dockerlib (32 GB file, 23 GB allocated, only ~1.9 GB referenced)
             └─ /dev/loop4 → ext4 /var/lib/docker  (+ bind mount /home/ubuntu)
```

Every first-touch read walks that whole chain and ends in a network fetch. The driver's own
background prefetch has a 10 s budget and spends it on the ~23 GB of stale blocks in `dockerlib`,
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

## What is applied (`apply-tunables.sh`, idempotent, run as root)

- `read_ahead_kb = 4096` on the FUSE bdi (`/sys/class/bdi/0:44`) and `/sys/block/loop3/queue`.
- `/sys/block/loop4/queue/read_ahead_kb` left at 128: 1024/4096 measured 5–10 % slower on the
  small-file (overlay2) workload and on a 32 MB single-file read.
- FUSE connection `max_background 64`, `congestion_threshold 48` (more async readahead in flight).
- `noatime` remount of `/var/lib/docker` and `/mnt/cloudenvdata` (atime writes traversed 3 layers).
- `vm.vfs_cache_pressure = 50`, `vm.dirty_background_bytes = 128 MiB`, `vm.dirty_bytes = 512 MiB`
  (the write path drains at ~50 MB/s; the default 20 % of RAM ≈ 1.6 GB of dirty data would stall
  fsync/sync for 30 s+).

## Warm-up (`warm-docker-root.sh [readers]`)

Parses `dumpe2fs /dev/loop4`, computes the *allocated* block ranges of the inner ext4 (~1.9 GB),
and reads them through `/dev/loop4` with O_DIRECT in 1 MiB chunks. This pulls exactly the working
set into the driver's local cache and skips the ~21 GB of stale blocks. Measured: 12 s on first
run (network), 6–9 s afterwards (local). After it, everything under `/var/lib/docker` and
`/home/ubuntu` is a local-cache hit; the only "remote" blocks left are zero-filled journal and
inode-table space that the driver answers from its zero-block manifest without network.

## Things measured and rejected

- `losetup --direct-io=on /dev/loop4`: +50 % on cold sequential reads, but 8–10 % slower on the
  small-file local-hit workload and worse random-read tail latency. Left off.
- Bigger FUSE bdi `max_ratio` for writes: no change; the ~50 MB/s write ceiling is the driver.
- `fstrim` to shrink the remote data set: the driver reports `fallocate` as not implemented, so
  hole punching never reaches the blob manifest. Only the driver could drop the stale 21 GB.
- Extra reader parallelism beyond one aggressive readahead stream: no gain (network-bound).
- Inner ext4 fragmentation score is 0 (e4defrag): no relayout needed.

## Persistence

- Host: `codespace-storage-tuning.service` (enabled, runs both scripts after docker) and
  `/etc/udev/rules.d/99-codespace-loop-tuning.rules` (re-applies tunables whenever a loop device
  is (re)attached). The host VM is ephemeral across codespace stop/start, so these vanish then.
- This directory lives inside `disk.img` and does persist. To re-apply automatically on every
  codespace start, add a `postStartCommand` to the devcontainer that uses the host docker socket
  (mounted at `/var/run/docker-host.sock`) to run the scripts on the host, e.g.

  ```
  docker -H unix:///var/run/docker-host.sock run --rm --privileged -v /:/host ubuntu \
    chroot /host bash -c '/home/ubuntu/claude/storage-tuning/apply-tunables.sh && /home/ubuntu/claude/storage-tuning/warm-docker-root.sh 2'
  ```

## Analysis helpers

- `datamap.py <file> [out]`: SEEK_DATA/SEEK_HOLE map of a sparse file (which blocks are local).
- `usedmap.py <dumpe2fs-output> <out>`: allocated ranges of an ext4 image.
- `mapping.py`: maps loop4 offsets → disk.img offsets → local/remote using the maps above.
- `rand4k.py <base> <span> <n> <seed>`: O_DIRECT random 4 KiB read latency on `/dev/loop4`.
