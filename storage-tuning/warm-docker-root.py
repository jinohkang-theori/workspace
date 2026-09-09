#!/usr/bin/env python3
"""Prefetch every block the inner ext4 (/var/lib/docker) actually uses, so the FUSE
driver's local read cache holds the whole working set. Reads only *allocated*
inner-fs blocks (~2 GB), not the ~23 GB of stale data still present in dockerlib.
Idempotent. Run as root on the host. Usage: warm-docker-root.py [parallel-readers]
"""
import mmap
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MB = 1 << 20


def log(msg):
    print(f"[warm] {msg}", flush=True)


def find_loop(pattern):
    """First loop device whose backing file matches the regex, or None."""
    out = subprocess.run(
        ["losetup", "-l", "-n", "-O", "NAME,BACK-FILE"],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and re.search(pattern, fields[1]):
            return fields[0]
    return None


def used_ranges(dumpe2fs_txt):
    """Byte ranges [a, b) of allocated blocks, derived from the free-block lists."""
    bs = int(re.search(r"Block size:\s+(\d+)", dumpe2fs_txt).group(1))
    nblocks = int(re.search(r"Block count:\s+(\d+)", dumpe2fs_txt).group(1))
    free = []
    for m in re.finditer(r"Free blocks: ([\d\-, ]*)\n", dumpe2fs_txt):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            a, _, b = part.partition("-")
            free.append((int(a), int(b or a) + 1))
    free.sort()
    used = []
    cur = 0
    for a, b in free:
        if a > cur:
            used.append((cur * bs, a * bs))
        cur = max(cur, b)
    if cur < nblocks:
        used.append((cur * bs, nblocks * bs))
    return used


def chunk_offsets(used):
    """Expand ranges to 1 MiB aligned chunk offsets (driver block size), deduped."""
    starts = {(o // MB) * MB for a, b in used for o in range(a, b, MB)}
    ends = {((b - 1) // MB) * MB for a, b in used}
    return sorted(starts | ends)


def read_chunks(dev, offsets):
    fd = os.open(dev, os.O_RDONLY | os.O_DIRECT)
    buf = mmap.mmap(-1, MB)
    n = 0
    try:
        for off in offsets:
            try:
                n += os.preadv(fd, [buf], off)
            except OSError as e:
                log(f"read error @ {off}: {e}")
    finally:
        os.close(fd)
    return n


def main():
    par = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    inner = find_loop(r"cloudenvdata/dockerlib$")
    outer = find_loop(r"csfs-fuse")
    if not inner:
        print("dockerlib loop device not found", file=sys.stderr)
        return 1
    log(f"devices: inner={inner} outer={outer or '?'}")

    start = time.monotonic()
    # outer ext4 metadata (group descriptors, bitmaps) is tiny; touch it too
    if outer:
        subprocess.run(["dumpe2fs", outer], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    txt = subprocess.run(["dumpe2fs", inner], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True).stdout
    used = used_ranges(txt)
    chunks = chunk_offsets(used)
    log(f"used={sum(b - a for a, b in used) / MB:.0f} MiB -> "
        f"{len(chunks)} x 1MiB chunks, {par} readers")

    # contiguous slices per worker keep each reader sequential (readahead-friendly)
    per = (len(chunks) + par - 1) // par
    slices = [chunks[i:i + per] for i in range(0, len(chunks), per)]
    with ThreadPoolExecutor(par) as ex:
        done = sum(ex.map(lambda s: read_chunks(inner, s), slices))
    log(f"read {done / MB:.0f} MiB")
    log(f"finished in {time.monotonic() - start:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
