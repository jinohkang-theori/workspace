import os, sys, time, random, mmap
base = int(sys.argv[1]); span = int(sys.argv[2]); n = int(sys.argv[3]); seed = int(sys.argv[4])
fd = os.open('/dev/loop4', os.O_RDONLY | os.O_DIRECT); buf = mmap.mmap(-1, 4096)
random.seed(seed); lat = []
for i in range(n):
    off = base + random.randrange(0, span // 4096) * 4096
    t = time.perf_counter(); os.preadv(fd, [buf], off); lat.append(time.perf_counter() - t)
lat.sort(); print(f"n={n} p50={lat[n//2]*1e3:.2f}ms p90={lat[int(n*.9)]*1e3:.2f}ms p99={lat[int(n*.99)]*1e3:.2f}ms mean={sum(lat)/n*1e3:.2f}ms")
