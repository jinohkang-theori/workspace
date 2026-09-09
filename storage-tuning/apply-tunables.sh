#!/bin/bash
# Speed up the Codespace docker root (/var/lib/docker) whose backing chain is:
#   Azure blob --(codespaces-fuse-driver, 1MiB blocks)--> disk.img --loop--> ext4 /mnt/cloudenvdata
#   --> dockerlib --loop--> ext4 /var/lib/docker (+ bind /home/ubuntu)
# Idempotent; safe to re-run. Must run as root on the host.
set -u
LOOP4_RA=${LOOP4_RA:-128}      # readahead for files inside /var/lib/docker (KiB); larger measured slower on small files
LOOP3_RA=${LOOP3_RA:-4096}     # readahead when reading dockerlib out of /mnt/cloudenvdata (KiB)
FUSE_RA=${FUSE_RA:-4096}       # readahead on disk.img (FUSE file); FUSE requests are 128KiB each
log(){ echo "[apply-tunables] $*"; }
w(){ [ -e "$1" ] && { echo "$2" > "$1" && log "$1 = $2" || log "FAILED $1 = $2"; }; }

# --- locate devices -------------------------------------------------------
FUSE_MM=$(findmnt -n -t fuse -o MAJ:MIN,SOURCE | awk '$2=="csfs"{print $1; exit}')
LOOP3=$(losetup -l -n -O NAME,BACK-FILE | awk '$2 ~ /csfs-fuse/ {print $1; exit}')
LOOP4=$(losetup -l -n -O NAME,BACK-FILE | awk '$2 ~ /cloudenvdata\/dockerlib$/ {print $1; exit}')
log "fuse bdi=${FUSE_MM:-?} disk.img loop=${LOOP3:-?} dockerlib loop=${LOOP4:-?}"

# --- readahead: the single biggest win (cold sequential 15-38 MB/s -> 140+ MB/s) ---
[ -n "$FUSE_MM" ] && w /sys/class/bdi/$FUSE_MM/read_ahead_kb $FUSE_RA
[ -n "$LOOP3" ]   && w /sys/block/${LOOP3#/dev/}/queue/read_ahead_kb $LOOP3_RA
[ -n "$LOOP4" ]   && w /sys/block/${LOOP4#/dev/}/queue/read_ahead_kb $LOOP4_RA

# --- FUSE: allow more async (readahead) requests in flight to the driver (16 workers) ---
if [ -n "$FUSE_MM" ]; then
  C=/sys/fs/fuse/connections/${FUSE_MM#*:}
  w $C/max_background 64
  w $C/congestion_threshold 48
fi

# --- ext4 mount options: drop atime writes that would otherwise traverse 3 layers ---
for m in /var/lib/docker /mnt/cloudenvdata; do
  if findmnt -n "$m" >/dev/null 2>&1 && ! findmnt -n -o OPTIONS "$m" | grep -q noatime; then
    mount -o remount,noatime "$m" && log "remounted $m noatime" || log "FAILED remount $m"
  fi
done

# --- VM: keep inner-fs dentries/inodes cached; bound dirty data so flushes to the
#     ~50 MB/s FUSE write path do not stall for tens of seconds -----------------
w /proc/sys/vm/vfs_cache_pressure 50
w /proc/sys/vm/dirty_background_bytes $((128*1024*1024))
w /proc/sys/vm/dirty_bytes $((512*1024*1024))
log done
