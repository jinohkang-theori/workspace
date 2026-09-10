# Codespace docker-root storage tuning

## The stack (host side)

```
Azure Blob (257 GB image, 1 MiB blocks, on-demand)
  └─ codespaces-fuse-driver  → /mnt/csfs-fuse/<id>/disk.img   (FUSE, 128 KiB max request)
       read cache: /mnt/storage-driver-tmp/fuse-read.cache-*  (sparse, local temp disk /dev/sdb1)
       writes:     /mnt/data/fuse-writes.img                    (sparse, local data disk /dev/sdc1)
     │  [kernel page cache of disk.img — only while loop3 is buffered]
     └─ /dev/loop3 → ext4 /mnt/cloudenvdata
          └─ dockerlib (32 GB file, ~26 GB allocated, only ~1.9 GB referenced)
             │  [kernel page cache of dockerlib — only while loop4 is buffered]
             └─ /dev/loop4 → ext4 /var/lib/docker  (+ bind mount /home/ubuntu)
                   [page cache of the files inside]
```

A buffered loop device reads its backing file through that file's page cache, so with both
loops buffered a hot block lives in memory **three** times below the driver (inner file,
`dockerlib`, `disk.img`) plus a fourth time in the page cache of the driver's own cache file.
Reading 64 MiB of local data once through `/dev/loop4` grows `Cached` by 199 MiB; by 131 MiB with
either loop in direct I/O; by 65 MiB with both. With loop3 in direct I/O (what is applied) the
`disk.img` copy is gone and a hit below the inner filesystem is served from `dockerlib`'s page
cache.

Driver behaviour that matters for the numbers below (from `/proc/<pid>/io` and `strace`):

- For every `FUSE_READ`, whatever its size, the driver `pread`s the whole 1 MiB block from its
  cache file. 300 random 4 KiB reads that reach it cost it 307 MiB of reads. A request that
  reaches the driver costs ~0.3 ms when that block is in the host page cache and ~1.2 ms (p99
  ~45 ms) when it has to come from the temp disk.
- `disk.img` *is* page-cached by the kernel (the driver does not ask for direct I/O), but it does
  not set `FOPEN_KEEP_CACHE`, so **every `open()` of `disk.img` by any process drops that cache**.
  `fincore`, `datamap.py` and any `dd` on `disk.img` do exactly that, which is why `fincore` always
  reports 0 for it and why measuring the FUSE layer by opening the file yourself gives numbers the
  loop stack never sees.
- The driver never calls `fsync`/`fdatasync` on `fuse-writes.img`; durability of writes is the
  host page cache in every configuration.

Every first-touch read walks that whole chain and ends in a network fetch. The driver's own
background prefetch has a 10 s budget and spends it on the ~24 GB of stale blocks in `dockerlib`,
so it only ever fetched ~1.7 GB of mostly useless data.

## Measurements (2026-09-10, raw numbers in [`dio-results.md`](dio-results.md))

| Path | Measured |
|---|---|
| Cold sequential read through `/var/lib/docker` | 15–18 MB/s; 26–37 MB/s with direct I/O on both loops (rejected, see below) |
| Cold sequential read of `disk.img` through a freshly opened fd | 104–120 MB/s (FUSE readahead, 4 MiB window) |
| Cold 1 MiB block miss latency | 16–127 ms, p50 19 ms; 16 parallel fetches reach ~76 MB/s |
| Hit served by the driver from its cache file on the temp disk | 200–370 MB/s sequential; 1.2 ms p50 / ~45 ms p99 per 4 KiB |
| Hit served by the driver from the host page cache of its cache file | 0.3 ms per 4 KiB |
| Hit served by a kernel page-cache copy (`dockerlib` or `disk.img`) | 2–4 GB/s; 0.03 ms per 4 KiB |
| Write path, 128 MiB + fsync | 47 MB/s with loop3 buffered; 176–196 MB/s with loop3 direct I/O |

**Cold sequential reads are stuck at ~16 MB/s and no readahead knob reaches them.** A file's
readahead window (`file->f_ra.ra_pages`) is copied from its bdi when the file is **opened**, and
a loop device opens its backing file once, at `losetup` time, when the bdi still had the 128 KiB
default. `REMOTE_RA`/`MID_RA` therefore only reach files opened afterwards (an fd on `disk.img`
opened before the change reads cold data at 34 MB/s, one opened after at 120 MB/s), never the
`disk.img` and `dockerlib` files that loop3 and loop4 hold. Through the loops every cold 128 KiB
is fetched serially and the block-miss latency sets the throughput (~1 MiB per 60 ms). Nothing
userspace can do reaches the loops' file structs (`LOOP_CHANGE_FD` is for read-only devices only);
the two readahead knobs stay because they are free and correct for files opened later.

**Writes were limited by 4 KiB writeback, not by the driver.** With loop3 buffered, an inner
`fsync` makes the kernel write `disk.img`'s dirty pages back through FUSE **one 4 KiB page per
`FUSE_WRITE`** (32 MiB = 8,212 `pwrite(…, 4096)` in the driver). With loop3 in direct I/O the
loop's requests go out as they are (128 KiB `FUSE_WRITE`s, 275 `pwrite`s for the same 32 MiB) and
the same driver sustains 190–350 MB/s.

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
  otherwise left at default (override with `ASSUME_REMOTE=1`). Only reaches files opened after
  it is set, see above.
- Intermediate **loop** devices (loop3): direct I/O on the backing file, `LOOP_SET_DIRECT_IO`
  (`MID_DIO`, default 1; `MID_DIO=0` to keep buffered). Same gate as `MID_RA`. Effects:
  - write + fsync 47 → 176–196 MB/s (fsync of 128 MiB 2.6 s → 0.5 s): the 4 KiB page writeback
    described above disappears;
  - one page-cache copy less: the same 196 MiB small-file set re-read three times inside a cgroup
    with `memory.max=480M` thrashes at 1.9–2.2 s per pass with both loops buffered and runs at
    0.11 s with loop3 direct (it needs 432 MiB instead of 659 MiB);
  - no read-hit regression: small-file all-hit 0.51–0.64 s vs 0.54–0.60 s, random 4 KiB p50
    0.028 ms vs 0.029 ms, because the `dockerlib` page cache under loop4 still answers every hit;
  - cold sequential unchanged (15–18 MB/s either way);
  - the driver sees the same write stream semantics (it fsyncs nothing in either mode); the kernel
    fsyncs the backing file before switching, so toggling on a live stack is safe.
- Innermost virtual block device (loop4): `read_ahead_kb = 128` (`INNER_RA`), buffered
  (`INNER_DIO`, default 0). Readahead of 1024/4096 is within noise on the small-file workload
  (0.51–0.59 s vs 0.53–0.71 s) and brings no cold gain, so 128 stays. Real disks/partitions in
  that position are not touched.
- Every block-backed filesystem walked through (`/var/lib/docker`, `/mnt/cloudenvdata`):
  `noatime` remount (atime writes traversed 3 layers). The root filesystem is never remounted.
- `vm.vfs_cache_pressure = 50`, `vm.dirty_background_bytes = 128 MiB`, `vm.dirty_bytes = 512 MiB`,
  only when a remote layer exists (the write path drains at ~200 MB/s; the default 20 % of RAM ≈
  1.6 GB of dirty data would stall fsync/sync for 8 s+, 30 s+ if loop3 falls back to buffered).

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

- Direct I/O on loop4 (`INNER_DIO=1`), 3–4 interleaved rounds per configuration: small-file
  all-hit workload 0.63–0.75 s vs 0.54–0.60 s buffered (15–30 % slower); random 4 KiB p50
  0.041 ms vs 0.029 ms, p99 equal within noise; cold sequential unchanged at 14–16 MB/s. The hit
  moves from `dockerlib`'s page cache, a memcpy inside loop4's worker, to `disk.img`'s page cache
  one layer down, which adds an ext4 direct-I/O submission, a loop3 request and a worker wake-up
  per request (~12 µs). The memory saving is taken on loop3 instead, where it is free.
- Direct I/O on both loops: cold sequential ~2x (26–37 MB/s), but every inner miss then pays a
  FUSE round trip and the driver's 1 MiB `pread`: small files 3.2–3.7 s (6x), random 4 KiB p50
  0.30 ms (10x).
- Bigger FUSE bdi `max_ratio` for writes: no change.
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
  Do not point it (or `fincore`, `dd`, …) at `disk.img` on a live system unless you mean to: any
  `open()` of that file drops loop3's page cache of it (while loop3 is buffered).
- `dio-results.md`: raw numbers behind the measurements section.
- `usedmap.py <dumpe2fs-output> <out>`: allocated ranges of an ext4 image.
- `mapping.py`: maps loop4 offsets → disk.img offsets → local/remote using the maps above.
- `rand4k.py <base> <span> <n> <seed>`: O_DIRECT random 4 KiB read latency on `/dev/loop4`.
