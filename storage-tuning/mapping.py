import bisect, re, sys, random
S = "/home/ubuntu/claude/storage-tuning"
BS = 4096
# dockerlib extents: logical block -> physical block (loop3/disk.img block)
ext = []
for line in open(f"{S}/dockerlib.frag"):
    m = re.match(r'\s*\d+:\s+(\d+)\.\.\s*(\d+):\s+(\d+)\.\.\s*(\d+):\s+(\d+):', line)
    if m:
        lo, lhi, po, phi, ln = map(int, m.groups()); ext.append((lo*BS, po*BS, ln*BS))
ext.sort(); ext_starts = [e[0] for e in ext]
def l4_to_img(off):
    i = bisect.bisect_right(ext_starts, off) - 1
    if i < 0: return None
    lo, po, ln = ext[i]
    if off >= lo + ln: return None  # hole
    return po + (off - lo), lo + ln - off  # img offset, bytes remaining in extent
def load(path):
    r = [tuple(map(int, l.split())) for l in open(path)]; r.sort(); return r, [a for a, b in r]
rc, rcs = load(f"{S}/readcache.ranges"); wr, wrs = load(f"{S}/writes.ranges")
def inr(r, rs, off):
    i = bisect.bisect_right(rs, off) - 1
    return i >= 0 and off < r[i][1]
def is_local(img_off):
    return inr(rc, rcs, img_off) or inr(wr, wrs, img_off)
MB = 2**20
def split_local(l4_a, l4_b, step=MB):
    """classify [a,b) of loop4 in 1MiB units: returns (local_bytes, remote_bytes, hole_bytes)"""
    loc = rem = hole = 0; off = l4_a
    while off < l4_b:
        n = min(step - off % step, l4_b - off)
        m = l4_to_img(off)
        if m is None: hole += n
        elif is_local(m[0]): loc += n
        else: rem += n
        off += n
    return loc, rem, hole
if __name__ == "__main__":
    used = [tuple(map(int, l.split())) for l in open(f"{S}/loop4.used")]
    L = R = H = 0
    for a, b in used:
        l, r, h = split_local(a, b); L += l; R += r; H += h
    print(f"inner-ext4 used set: local={L/MB:.0f}MiB remote={R/MB:.0f}MiB hole={H/MB:.0f}MiB")
    # find fully-cold 64MiB windows on loop4 in 8..22GiB
    cold = []
    for g in range(8*1024, 22*1024, 64):
        a = g*MB; l, r, h = split_local(a, a+64*MB)
        if r == 64*MB: cold.append(g)
    print(f"fully cold 64MiB windows: {len(cold)}; sample MiB offsets: {cold[:12]}")
    open(f"{S}/cold64.txt", "w").write("\n".join(map(str, cold)))
