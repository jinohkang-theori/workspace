import re, sys
# parse dumpe2fs: block ranges per group and free block ranges -> used ranges (in 4K blocks)
txt = open(sys.argv[1]).read()
bs = int(re.search(r'Block size:\s+(\d+)', txt).group(1))
nblocks = int(re.search(r'Block count:\s+(\d+)', txt).group(1))
free = []
for m in re.finditer(r'Free blocks: ([\d\-, ]*)\n', txt):
    for part in m.group(1).split(','):
        part = part.strip()
        if not part: continue
        if '-' in part:
            a, b = part.split('-'); free.append((int(a), int(b)+1))
        else:
            free.append((int(part), int(part)+1))
# the first "Free blocks:" line is the summary count; filter out tiny bogus if any
free = [r for r in free if r[1] > r[0]]
free.sort()
used = []; cur = 0
for a, b in free:
    if a > cur: used.append((cur, a))
    cur = max(cur, b)
if cur < nblocks: used.append((cur, nblocks))
tot = sum(b-a for a, b in used)
print(f"block_size={bs} blocks={nblocks} used_ranges={len(used)} used={tot*bs/2**30:.2f}G")
with open(sys.argv[2], 'w') as f:
    for a, b in used: f.write(f"{a*bs} {b*bs}\n")
