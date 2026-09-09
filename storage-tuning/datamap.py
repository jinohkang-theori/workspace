import os, sys
SEEK_DATA, SEEK_HOLE = 3, 4
p = sys.argv[1]
fd = os.open(p, os.O_RDONLY)
size = os.fstat(fd).st_size
off = 0; ranges = []; total = 0
while off < size:
    try: d = os.lseek(fd, off, SEEK_DATA)
    except OSError: break
    h = os.lseek(fd, d, SEEK_HOLE)
    ranges.append((d, h)); total += h - d; off = h
print(f"{p}: size={size/2**30:.1f}G data_ranges={len(ranges)} data_total={total/2**30:.2f}G")
if len(sys.argv) > 2:
    with open(sys.argv[2], 'w') as f:
        for d, h in ranges: f.write(f"{d} {h}\n")
# coarse histogram per 1GiB of how much is local
hist = {}
for d, h in ranges:
    g = d // 2**30
    while d < h:
        e = min(h, (g+1)*2**30); hist[g] = hist.get(g, 0) + (e - d); d = e; g += 1
print(" per-GiB local MiB:", {k: round(v/2**20) for k, v in sorted(hist.items()) if k < 24})
