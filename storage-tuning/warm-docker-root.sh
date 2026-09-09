#!/bin/bash
# Prefetch every block the inner ext4 (/var/lib/docker) actually uses, so the FUSE
# driver's local read cache holds the whole working set. Reads only *allocated*
# inner-fs blocks (~2 GB), not the ~23 GB of stale data still present in dockerlib.
# Idempotent. Run as root on the host. Usage: warm-docker-root.sh [parallel-readers]
set -u
PAR=${1:-2}
LOOP4=$(losetup -l -n -O NAME,BACK-FILE | awk '$2 ~ /cloudenvdata\/dockerlib$/ {print $1; exit}')
LOOP3=$(losetup -l -n -O NAME,BACK-FILE | awk '$2 ~ /csfs-fuse/ {print $1; exit}')
[ -z "$LOOP4" ] && { echo "dockerlib loop device not found"; exit 1; }
echo "[warm] devices: inner=$LOOP4 outer=${LOOP3:-?}"
# outer ext4 metadata (group descriptors, bitmaps) is tiny; touch it too
[ -n "$LOOP3" ] && dumpe2fs "$LOOP3" >/dev/null 2>&1
start=$(date +%s.%N)
PY=$(mktemp); cat > "$PY" <<'PYEOF'
import os, re, sys, mmap, time
from concurrent.futures import ThreadPoolExecutor
dev, par = sys.argv[1], int(sys.argv[2])
txt = sys.stdin.read()
bs = int(re.search(r'Block size:\s+(\d+)', txt).group(1))
nblocks = int(re.search(r'Block count:\s+(\d+)', txt).group(1))
free = []
for m in re.finditer(r'Free blocks: ([\d\-, ]*)\n', txt):
    for part in m.group(1).split(','):
        part = part.strip()
        if not part: continue
        a, _, b = part.partition('-'); free.append((int(a), int(b or a) + 1))
free.sort(); used = []; cur = 0
for a, b in free:
    if a > cur: used.append((cur * bs, a * bs))
    cur = max(cur, b)
if cur < nblocks: used.append((cur * bs, nblocks * bs))
MB = 1 << 20
# expand to 1 MiB aligned chunks (driver block size), dedupe
chunks = sorted({(o // MB) * MB for a, b in used for o in range(a, b, MB)} | {((b - 1) // MB) * MB for a, b in used})
total = len(chunks) * MB
print(f"[warm] used={sum(b-a for a,b in used)/MB:.0f} MiB -> {len(chunks)} x 1MiB chunks, {par} readers", flush=True)
def worker(sub):
    fd = os.open(dev, os.O_RDONLY | os.O_DIRECT); buf = mmap.mmap(-1, MB); n = 0
    for off in sub:
        try: n += os.preadv(fd, [buf], off)
        except OSError as e: print(f"[warm] read error @ {off}: {e}", flush=True)
    os.close(fd); return n
# contiguous slices per worker keep each reader sequential (readahead-friendly)
per = (len(chunks) + par - 1) // par
with ThreadPoolExecutor(par) as ex:
    done = sum(ex.map(worker, [chunks[i:i+per] for i in range(0, len(chunks), per)]))
print(f"[warm] read {done/MB:.0f} MiB", flush=True)
PYEOF
dumpe2fs "$LOOP4" 2>/dev/null | python3 "$PY" "$LOOP4" "$PAR"; rm -f "$PY"
end=$(date +%s.%N)
echo "[warm] finished in $(echo "$end - $start" | bc) s"
