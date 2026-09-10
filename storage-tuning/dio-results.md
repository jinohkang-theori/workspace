# Loop direct I/O and page-cache measurements (2026-09-10)

Host: Codespaces VM, kernel 6.8.0-1064-azure, 2 vCPU, 7.9 GB RAM, driver
`storage-driver-rs-abi-7-19/codespaces-fuse-driver`, `--block-size 1048576`. Stack as in
[README.md](README.md). All tests run on the host through `run-on-host.py`; DIO toggled live with
`losetup --direct-io` (later the `LOOP_SET_DIRECT_IO` ioctl) and restored afterwards. Configs are
written `loop4dio=X loop3dio=Y`.

Cache states used below (what was dropped before the run):

- **disk**: inner file pages (`fadvise DONTNEED`), loop4 bdev (`blockdev --flushbufs`), `dockerlib`
  (`fadvise`), `disk.img` (an `open()` drops it) and the driver's cache files (`fadvise`) — the
  driver reads its cache file from the temp disk.
- **mem**: as above but the driver's cache-file pages left resident.
- **pc / all-hit**: only the inner ext4 caches dropped; every kernel page-cache copy below is warm.

## 1. Where the caches are

`/proc/<driver pid>/io` and `strace` during 300 random 4 KiB O_DIRECT reads on `/dev/loop4`
(loop4dio=1 loop3dio=0):

| step | p50 / p99 | driver rchar | driver read_bytes | driver syscalls |
|---|---|---|---|---|
| fresh offsets, cache files dropped | 1.29 / 47 ms | +307 MiB | +264 MiB | +616 |
| same offsets again | 0.04 / 0.9 ms | 0 | 0 | 0 |
| cache files dropped, same offsets | 0.04 / 0.09 ms | 0 | 0 | 0 |
| fresh offsets again | 1.24 / 48 ms | +297 MiB | +263 MiB | +597 |

Repeat reads never reach the driver: they are served by the kernel page cache of `disk.img`, held by
loop3's open (loop4 was in DIO, so it cannot be `dockerlib`'s). The driver `pread`s 1 MiB per
`FUSE_READ` (`pread64(fd=13, …, 1048576, …)` in strace) even for 4 KiB requests.

`disk.img` page cache and `open()`: a single long-lived fd reading 16 MiB three times: 373, 4309,
4871 MB/s (driver syscalls 153, 33, 3). After another process did `open(); close()` on the file:
576 MB/s (180 syscalls), then 3036 MB/s. `fincore` reports 0 resident pages because its own `open()`
drops them first.

`Cached` growth for one 64 MiB read through `/dev/loop4` (buffered) with everything dropped first:

| loop4dio | loop3dio | Cached delta | copies |
|---|---|---|---|
| 0 | 0 | +199 MiB | 3.1 |
| 1 | 0 | +131 MiB | 2.0 |
| 0 | 1 | +134 MiB | 2.1 |
| 1 | 1 | +65 MiB | 1.0 |

The copy that always remains is the driver's cache file in the host page cache.

## 2. Read hits (all-hit state), 3 interleaved rounds

7,545 files / 196 MiB overlay2 layer read sequentially with 1 MiB reads (`smallfiles`, two passes per
round), and 400 random 4 KiB O_DIRECT reads on `/dev/loop4` over a 1,152 MiB fully-local span
(`rand4k`).

| loop4dio | loop3dio | smallfiles (s) | rand4k p50 / p90 / p99 (ms) |
|---|---|---|---|
| 0 | 0 | 0.55 0.54 0.56 0.60 0.57 (and one 2.28 first-touch) | 0.029 / 0.034–0.036 / 0.14–0.18 |
| 1 | 0 | 0.68 0.67 0.75 0.67 0.63 0.64 | 0.040–0.041 / 0.044–0.052 / 0.10–0.42 |
| 0 | 1 | 0.57 0.53 0.57 0.51 0.64 0.60 | 0.028 / 0.030 / 0.10–0.29 |
| 1 | 1 | 3.42 3.41 3.56 3.71 3.24 3.50 | 0.287–0.301 / 0.41–0.51 / 2.5–3.2 |

A separate interleaved run (`bench2`), pc state: smallfiles 0.64/0.50 (0,0), 0.77/0.68 (1,0),
0.51/0.49 (0,1), 3.13/3.46 (1,1) s.

## 3. Read hits under memory pressure

Same file set re-read four times (first pass is the fill) by a child process in a cgroup with
`memory.max`; loop workers charge their backing-file page cache to the issuing cgroup, so all kernel
copies count against the limit (the driver's cache file does not).

| memory.max | loop4dio loop3dio | pass 1–4 (s) | memory.current | reclaim (`max` events) |
|---|---|---|---|---|
| max | 0 0 | 1.83 0.17 0.11 0.12 | 659 MiB | 0 |
| max | 0 1 | 2.38 0.13 0.11 0.11 | 432 MiB | 0 |
| max | 1 0 | 2.19 0.11 0.11 0.12 | 433 MiB | 0 |
| max | 1 1 | 3.17 0.12 0.15 0.12 | 211 MiB | 0 |
| 480M | 0 0 | 1.88 1.98 1.89 2.19 | 476 MiB | 8564 |
| 480M | 0 1 | 2.38 0.11 0.13 0.17 | 432 MiB | 0 |
| 480M | 1 0 | 1.98 0.11 0.12 0.14 | 434 MiB | 0 |
| 480M | 1 1 | 2.95 0.11 0.13 0.13 | 212 MiB | 0 |
| 320M | 0 0 | 1.94 2.06 1.91 2.01 | 317 MiB | 9335 |
| 320M | 0 1 | 2.36 2.40 2.42 2.33 | 316 MiB | 5605 |
| 320M | 1 0 | 2.14 2.19 2.13 2.26 | 317 MiB | 5612 |
| 320M | 1 1 | 2.98 0.14 0.13 0.12 | 212 MiB | 0 |

(800M behaved like `max`.)

## 4. Writes

128 MiB written in 1 MiB `pwritev`s to a file on `/var/lib/docker`, then `fsync`; two rounds.

| loop4dio | loop3dio | MB/s | write phase | fsync | driver `syscw` | driver `write_bytes` |
|---|---|---|---|---|---|---|
| 0 | 0 | 47, 47 | 0.11–0.19 s | 2.55–2.59 s | 65.6 k | 128 MiB |
| 0 | 1 | 196, 176 | 0.17–0.19 s | 0.48–0.54 s | 2.1 k | 128 MiB |
| 1 | 0 | 52, 51 | 0.15–0.19 s | 2.29–2.35 s | 65.6 k | 128 MiB |
| 1 | 1 | 337, 347 | 0.14–0.18 s | 0.18–0.24 s | 2.1 k | 128 MiB |

`strace` of the driver during a 32 MiB write + fsync: loop3 buffered → 8,212 × `pwrite(…, 4096)` to
`fuse-writes.img`, 9 MB/s; loop3 DIO → 255 × 131072 + a few smaller, 166 MB/s. No `fsync`,
`fdatasync`, `sync_file_range` or `msync` from the driver in either mode. After applying
`MID_DIO=1` with `apply-tunables.py`: 192 MB/s.

## 5. Cold reads

Block-miss latency via `disk.img` O_DIRECT 4 KiB reads at fresh blocks (n=16): p50 18.8 ms, min 15.9,
max 74.5. After the first 4 KiB of a block, the rest of the block is 0.2–0.4 ms (the driver fetches
whole 1 MiB blocks). 8 threads on one block: all wait ~65 ms. 16 threads × 1 MiB on 16 blocks:
210 ms wall = 76 MB/s.

64 MiB cold sequential, 1 MiB reads, layer by layer (loop4dio=0):

| read from | loop3dio=0 | loop3dio=1 |
|---|---|---|
| `disk.img`, fresh fd, FUSE bdi RA 4096 | 104.0 MB/s | 80.1 |
| `disk.img`, fresh fd, FUSE bdi RA 128 | 18.6 | – |
| `/dev/loop3` buffered (bdev RA 4096) | 15.5 | 31.1 |
| `/dev/loop3` O_DIRECT 4 MiB | 17.9 | 28.6 |
| `dockerlib`, fresh fd (RA 4096) | 17.1 | 20.8 |
| `/dev/loop4` buffered (RA 128) | 17.5 | 15.1 |
| `/dev/loop4` O_DIRECT 1 MiB | 15.4 | 16.4 |
| `/dev/loop4` buffered, loop4 RA 4096 | 14.6 | 15.8 |

Readahead window is fixed at `open()`: an fd opened while the FUSE bdi had RA 128, read after the bdi
was raised to 4096: 34.2 MB/s; an fd opened after: 120.0 MB/s; `POSIX_FADV_NORMAL` on the old fd:
37.3 MB/s.

Cold sequential through `/dev/loop4` (buffered) with inner readahead 128 / 1024 / 4096 KiB:

| loop4dio loop3dio | RA128 | RA1024 | RA4096 |
|---|---|---|---|
| 0 0 | 16.3 | 16.7 | 17.0 |
| 0 1 | 17.7 | 14.1 | 15.8 |
| 1 0 | 18.0 | 17.5 | 12.4 |
| 1 1 | 25.6 | 37.4 | 30.2 |

Inner readahead cost on the all-hit small-file set with loop3 DIO (files reopened each pass):
RA128 0.71/0.53 s, RA1024 0.59/0.51 s, RA4096 0.55/0.55 s.
