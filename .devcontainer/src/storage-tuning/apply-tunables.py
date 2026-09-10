#!/usr/bin/env python3
"""Tune the storage stack under the Codespace docker root (/var/lib/docker).

Instead of hard-coding "loop3 over csfs, loop4 over dockerlib", this walks the
stack that is actually present, starting from the docker root directory:

    filesystem mount  -> block device -> (loop: backing file -> its filesystem -> ...)
                                      -> (dm/md/stacked: slaves -> ...)
                                      -> (partition: parent disk -> ...)
                                      -> (leaf: local disk / remote block dev / unknown)
    filesystem mount  -> FUSE or network filesystem  (remote leaf)

and tunes each layer it recognises. Anything it does not recognise is logged and
left alone; the walk stops there ("apply as much as possible, guess nothing").

This runs on the devcontainer critical path. It therefore never exits non-zero
unless --strict is given: every failure is logged and skipped. None of the knobs
touched here (readahead, loop direct I/O, FUSE queue depth, noatime, dirty-page
limits) can corrupt data, so there is no situation in which aborting is safer than
continuing.

With --warm it additionally prefetches the target filesystem's *allocated* blocks
(read from the block device directly under it, O_DIRECT, in 1 MiB chunks) so that the
remote layer's local read cache holds the whole working set instead of whatever the
driver's own background prefetch happened to pick. Only ext2/3/4 targets are supported
(dumpe2fs supplies the block bitmaps; its output is streamed, never held in memory);
the warm-up is skipped when no remote layer is present, since there is nothing to pull
into a local cache.

Usage: apply-tunables.py [--target PATH]... [--warm | --warm-only] [--readers N]
                         [--dry-run] [--strict] [--verbose]
Environment overrides (KiB unless noted):
  INNER_RA  (alias LOOP4_RA, default 128)  readahead of the device directly under
                                            the docker root; larger measured slower
                                            for small files. Only applied to virtual
                                            devices (loop/dm), never to real disks.
  MID_RA    (alias LOOP3_RA, default 4096) readahead of intermediate virtual block
                                            devices and remote block devices, applied
                                            only when a remote layer is detected below.
                                            Note: a file's readahead window is copied
                                            from the bdi when the file is opened, so this
                                            (and REMOTE_RA) only reaches files opened
                                            afterwards, not the backing file a loop
                                            device already holds open.
  MID_DIO   (alias LOOP3_DIO, default 1)   direct I/O (LOOP_SET_DIRECT_IO) on intermediate
                                            loop devices when a remote layer is below. The
                                            loop then reads/writes its backing file with
                                            O_DIRECT: one page-cache copy of the data
                                            less, and writes leave as the loop's requests
                                            (128 KiB+) instead of 4 KiB page writeback
                                            (measured 4x-7x faster write+fsync, no read
                                            regression: the inner loop's cache still
                                            serves hits). Set 0 to leave buffered.
  INNER_DIO (alias LOOP4_DIO, default 0)   same for the loop device directly under the
                                            docker root. Off: it would remove the last
                                            kernel page-cache copy below the docker root,
                                            and every inner miss would then pay a FUSE
                                            round trip (measured 6x slower small-file
                                            reads).
  REMOTE_RA (alias FUSE_RA,  default 4096) readahead on FUSE / network filesystems.
  FUSE_MAX_BACKGROUND (64), FUSE_CONGESTION_THRESHOLD (48)
  VM_DIRTY_BACKGROUND_BYTES (128 MiB), VM_DIRTY_BYTES (512 MiB), VM_VFS_CACHE_PRESSURE (50)
  WARM_READERS (2)        parallel O_DIRECT readers for --warm (also --readers).
  WARM_CHUNK_KB (1024)    read granularity for --warm; match the remote layer's block size.
  ASSUME_REMOTE=1  treat the stack as network-backed even if no remote layer was
                   recognised (enables MID_RA, the vm.* limits and --warm).
"""
import argparse
import fcntl
import json
import mmap
import os
import subprocess
import sys
import tempfile
import time
import traceback
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

TAG = "[apply-tunables]"
SYS = "/sys"
PROC = "/proc"

# Filesystems that are served by a userspace daemon or the network. Reads through
# them benefit from large readahead; writes drain slowly, so dirty data is bounded.
REMOTE_FS_PREFIXES = ("fuse",)
REMOTE_FS_TYPES = {
    "nfs", "nfs4", "cifs", "smb3", "9p", "virtiofs", "ceph", "afs", "glusterfs",
    "ncpfs", "coda", "ocfs2", "gfs2", "lustre",
}
# Local filesystems that live directly on a block device and accept remount,noatime.
BLOCK_FS_TYPES = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs", "reiserfs", "vfat", "exfat", "ntfs3",
}
# Block filesystems whose allocation bitmaps dumpe2fs can print (used by --warm).
EXT_FS_TYPES = {"ext2", "ext3", "ext4"}
# Anonymous local filesystems: nothing to tune, not remote.
LOCAL_ANON_FS_TYPES = {"tmpfs", "ramfs", "overlay", "zfs", "bcachefs"}

# Block-device leaf classification by kernel name. Anything else is "unknown".
LOCAL_DISK_PREFIXES = ("sd", "hd", "vd", "xvd", "nvme", "mmcblk", "sr", "fd", "pmem")
REMOTE_BLOCK_PREFIXES = ("nbd", "ublkb", "rbd", "drbd", "iscsi", "nvme-fabrics")

VERBOSE = False
DRY_RUN = False
FAILURES: List[str] = []


def log(msg: str) -> None:
    print(f"{TAG} {msg}", flush=True)


def dbg(msg: str) -> None:
    if VERBOSE:
        log(msg)


def fail(msg: str) -> None:
    FAILURES.append(msg)
    log(f"FAILED {msg}")


def env_int(name: str, default: int, *aliases: str) -> int:
    for key in (name,) + aliases:
        val = os.environ.get(key)
        if val is None or val == "":
            continue
        try:
            return int(val, 0)
        except ValueError:
            log(f"ignoring {key}={val!r}: not an integer")
    return default


# ----------------------------------------------------------------------------
# mountinfo
# ----------------------------------------------------------------------------
@dataclass
class Mount:
    mount_id: int
    majmin: str
    root: str
    target: str
    options: Set[str]
    fstype: str
    source: str
    sb_options: Set[str]

    @property
    def is_remote(self) -> bool:
        return self.fstype.startswith(REMOTE_FS_PREFIXES) or self.fstype in REMOTE_FS_TYPES

    @property
    def is_ro(self) -> bool:
        return "ro" in self.options


def _unescape(s: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \ooo
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 3 < len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def read_mounts() -> List[Mount]:
    mounts = []
    with open(f"{PROC}/self/mountinfo") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(" ")
            try:
                sep = parts.index("-")
            except ValueError:
                continue
            if sep < 6 or len(parts) < sep + 3:
                continue
            mounts.append(Mount(
                mount_id=int(parts[0]),
                majmin=parts[2],
                root=_unescape(parts[3]),
                target=_unescape(parts[4]),
                options=set(parts[5].split(",")),
                fstype=parts[sep + 1],
                source=_unescape(parts[sep + 2]),
                sb_options=set(parts[sep + 3].split(",")) if len(parts) > sep + 3 else set(),
            ))
    return mounts


def _is_prefix(mount_target: str, path: str) -> bool:
    if mount_target == "/":
        return True
    return path == mount_target or path.startswith(mount_target + "/")


def mount_for_path(path: str, mounts: List[Mount]) -> Optional[Mount]:
    """Return the mount that holds `path`.

    Prefer stat(): it is authoritative for the device the path lives on. Then
    pick, among mounts with that device, the one whose target is the longest
    prefix of the path (bind mounts share a device). If the path cannot be
    stat'ed (e.g. a loop backing file that was unlinked and shows up as
    "... (deleted)") fall back to the longest target prefix over all mounts;
    later mounts in mountinfo shadow earlier ones on ties.
    """
    if path.endswith(" (deleted)"):
        path = path[: -len(" (deleted)")]
    path = os.path.normpath(path)
    candidates = mounts
    try:
        st = os.stat(path)
        devstr = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        same_dev = [m for m in mounts if m.majmin == devstr]
        if same_dev:
            candidates = same_dev
    except OSError as e:
        dbg(f"stat({path}) failed ({e.strerror}); resolving by mount prefix only")
    best: Optional[Mount] = None
    for m in candidates:
        if not _is_prefix(m.target, path):
            continue
        if best is None or len(m.target) >= len(best.target):
            best = m
    if best is None and candidates is not mounts:
        # stat gave a device but no mount target prefixes the path (odd namespace);
        # still, the device is right: take any mount of it.
        best = candidates[0]
    return best


# ----------------------------------------------------------------------------
# sysfs block devices
# ----------------------------------------------------------------------------
@dataclass
class BlockDev:
    majmin: str
    name: str
    syspath: str                    # realpath of /sys/dev/block/MAJ:MIN
    kind: str                       # loop | stacked | partition | disk-local | disk-remote | unknown
    backing_file: Optional[str] = None
    slaves: List[str] = field(default_factory=list)   # majmin of lower devices
    parent: Optional[str] = None    # majmin of the whole disk for a partition
    stack_type: Optional[str] = None  # "dm", "md" or "?" for stacked devices
    dm_name: Optional[str] = None

    def __str__(self) -> str:
        s = f"{self.kind} {self.majmin} /dev/{self.name}"
        if self.kind == "loop":
            s += f" backing_file={self.backing_file}"
        elif self.kind == "stacked":
            s += f" {self.stack_type}"
            if self.dm_name:
                s += f" name={self.dm_name}"
            s += f" slaves={self.slaves}"
        elif self.kind == "partition":
            s += f" parent={self.parent}"
        elif self.kind == "unknown":
            s += " (no loop/slaves/partition attributes and unrecognised name)"
        return s

    @property
    def is_virtual(self) -> bool:
        return self.kind in ("loop", "stacked")

    @property
    def queue_dir(self) -> Optional[str]:
        p = os.path.join(self.syspath, "queue")
        return p if os.path.isdir(p) else None

    @property
    def devnode(self) -> Optional[str]:
        """Device node to open for I/O, or None if udev has not created one."""
        for p in (f"/dev/{self.name}", f"/dev/block/{self.majmin}"):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.exists(f"{SYS}/dev/block/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}") \
                    and f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}" == self.majmin:
                return p
        return None


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _majmin_of_sysdir(sysdir: str) -> Optional[str]:
    return _read(os.path.join(sysdir, "dev"))


def block_device(majmin: str) -> Optional[BlockDev]:
    link = f"{SYS}/dev/block/{majmin}"
    if not os.path.exists(link):
        return None
    syspath = os.path.realpath(link)
    name = os.path.basename(syspath)
    dev = BlockDev(majmin=majmin, name=name, syspath=syspath, kind="unknown")

    loop_dir = os.path.join(syspath, "loop")
    slaves_dir = os.path.join(syspath, "slaves")
    slaves = sorted(os.listdir(slaves_dir)) if os.path.isdir(slaves_dir) else []

    if os.path.isdir(loop_dir):
        dev.kind = "loop"
        dev.backing_file = _read(os.path.join(loop_dir, "backing_file"))
    elif slaves:
        dev.kind = "stacked"
        for s in slaves:
            mm = _majmin_of_sysdir(os.path.join(slaves_dir, s))
            if mm:
                dev.slaves.append(mm)
        dev.stack_type = "dm" if os.path.isdir(os.path.join(syspath, "dm")) else \
                         "md" if os.path.isdir(os.path.join(syspath, "md")) else "?"
        dev.dm_name = _read(os.path.join(syspath, "dm", "name"))
    elif os.path.exists(os.path.join(syspath, "partition")):
        dev.kind = "partition"
        dev.parent = _majmin_of_sysdir(os.path.dirname(syspath))
    elif name.startswith(REMOTE_BLOCK_PREFIXES):
        dev.kind = "disk-remote"
    elif name.startswith(LOCAL_DISK_PREFIXES):
        dev.kind = "disk-local"
    else:
        dev.kind = "unknown"
    return dev


# ----------------------------------------------------------------------------
# topology walk
# ----------------------------------------------------------------------------
@dataclass
class Node:
    depth: int
    # A filesystem mount, a block device, or a description of something that
    # could not be resolved to either (e.g. "path /x", "block device 7:9").
    layer: Union[Mount, BlockDev, str]
    terminal_reason: Optional[str] = None   # set when the walk stopped here

    def describe(self) -> str:
        pad = "  " * self.depth
        if isinstance(self.layer, Mount):
            m = self.layer
            s = f"{pad}fs {m.fstype} {m.majmin} {m.target} (source={m.source}, opts={','.join(sorted(m.options))})"
        elif isinstance(self.layer, BlockDev):
            s = f"{pad}blk {self.layer}"
        else:
            s = f"{pad}?? {self.layer}"
        if self.terminal_reason:
            s += f"  <- STOP: {self.terminal_reason}"
        return s


class Walker:
    def __init__(self, mounts: List[Mount]):
        self.mounts = mounts
        self.nodes: List[Node] = []
        self.seen_mounts: Set[str] = set()   # majmin
        self.seen_blk: Set[str] = set()
        self.remote_found = False
        self.unknown_found = False
        self.targets: List[Mount] = []        # the filesystems the walk started from (depth 0)
        self.fs_dev: Dict[str, str] = {}      # mount majmin -> majmin of the block device it sits on

    @property
    def inner_blks(self) -> Set[str]:
        """Block devices directly under a target filesystem."""
        return {self.fs_dev[m.majmin] for m in self.targets if m.majmin in self.fs_dev}

    def walked_mounts(self) -> List[Mount]:
        """Every filesystem the walk passed through, outermost target first, each once."""
        return [n.layer for n in self.nodes
                if isinstance(n.layer, Mount) and n.terminal_reason != "already visited"]

    def walk_path(self, path: str, depth: int = 0) -> None:
        m = mount_for_path(path, self.mounts)
        if m is None:
            self.nodes.append(Node(depth, f"path {path}", terminal_reason="no mount found"))
            self.unknown_found = True
            log(f"could not find a mount for {path}")
            return
        self.walk_mount(m, depth)

    def walk_mount(self, m: Mount, depth: int) -> None:
        node = Node(depth, m)
        self.nodes.append(node)
        if m.majmin in self.seen_mounts:
            node.terminal_reason = "already visited"
            return
        self.seen_mounts.add(m.majmin)
        if depth == 0:
            self.targets.append(m)

        if m.is_remote:
            self.remote_found = True
            node.terminal_reason = "remote filesystem (leaf)"
            return
        if m.fstype in LOCAL_ANON_FS_TYPES:
            node.terminal_reason = "local non-block filesystem (leaf)"
            return

        blk_mm = self._block_dev_of_mount(m)
        if blk_mm is None:
            if m.fstype in BLOCK_FS_TYPES:
                node.terminal_reason = "block filesystem but its device could not be resolved"
            else:
                node.terminal_reason = f"unrecognised filesystem type {m.fstype!r}"
            self.unknown_found = True
            return
        self.fs_dev[m.majmin] = blk_mm
        self.walk_block(blk_mm, depth + 1)

    def _block_dev_of_mount(self, m: Mount) -> Optional[str]:
        major = m.majmin.split(":")[0]
        if major != "0" and os.path.exists(f"{SYS}/dev/block/{m.majmin}"):
            return m.majmin
        # btrfs & co. use an anonymous dev; the source is the real device.
        if m.source.startswith("/dev/"):
            try:
                st = os.stat(m.source)
                if os.path.exists(f"{SYS}/dev/block/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"):
                    return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
            except OSError:
                pass
        return None

    def walk_block(self, majmin: str, depth: int) -> None:
        b = block_device(majmin)
        if b is None:
            self.nodes.append(Node(depth, f"block device {majmin}", terminal_reason="no sysfs entry"))
            self.unknown_found = True
            return
        node = Node(depth, b)
        self.nodes.append(node)
        if majmin in self.seen_blk:
            node.terminal_reason = "already visited"
            return
        self.seen_blk.add(majmin)

        if b.kind == "loop":
            if not b.backing_file:
                node.terminal_reason = "loop device without backing file"
                self.unknown_found = True
                return
            self.walk_path(b.backing_file, depth + 1)
        elif b.kind == "stacked":
            if not b.slaves:
                node.terminal_reason = "stacked device with unreadable slaves"
                self.unknown_found = True
                return
            for s in b.slaves:
                self.walk_block(s, depth + 1)
        elif b.kind == "partition":
            if not b.parent:
                node.terminal_reason = "partition without parent"
                self.unknown_found = True
                return
            self.walk_block(b.parent, depth + 1)
        elif b.kind == "disk-remote":
            self.remote_found = True
            node.terminal_reason = "remote block device (leaf)"
        elif b.kind == "disk-local":
            node.terminal_reason = "local disk (leaf)"
        else:
            self.unknown_found = True
            node.terminal_reason = "unknown block device type"


# ----------------------------------------------------------------------------
# knobs
# ----------------------------------------------------------------------------
def write_knob(path: str, value, what: str) -> bool:
    value = str(value)
    if not os.path.exists(path):
        log(f"skip {what}: {path} does not exist")
        return False
    cur = _read(path)
    if cur == value:
        dbg(f"{what}: {path} already {value}")
        return True
    if DRY_RUN:
        log(f"would set {what}: {path} {cur} -> {value}")
        return True
    try:
        with open(path, "w") as fh:
            fh.write(value)
    except OSError as e:
        fail(f"{what}: {path} = {value} ({e.strerror})")
        return False
    log(f"{what}: {path} {cur} -> {value}")
    return True


def set_readahead(b: BlockDev, kb: int, what: str) -> None:
    q = b.queue_dir
    if q is None:
        # partitions have no queue; the bdi symlink works for whole devices too
        bdi = os.path.join(b.syspath, "bdi", "read_ahead_kb")
        if os.path.exists(bdi):
            write_knob(bdi, kb, what)
        else:
            log(f"skip {what}: /dev/{b.name} has neither queue/ nor bdi/")
        return
    write_knob(os.path.join(q, "read_ahead_kb"), kb, what)


LOOP_SET_DIRECT_IO = 0x4C08


def set_loop_dio(b: BlockDev, on: int, what: str) -> None:
    """Switch a loop device between buffered and direct I/O on its backing file.

    Safe at run time: the kernel fsyncs the backing file and freezes the queue while
    switching. The kernel refuses (EINVAL) when the backing file does not support
    O_DIRECT or the loop offset is not aligned to the backing device's block size; that
    is logged and skipped.
    """
    on = 1 if on else 0
    sysfs = os.path.join(b.syspath, "loop", "dio")
    cur = _read(sysfs)
    if cur is None:
        log(f"skip {what}: {sysfs} not readable")
        return
    if cur == str(on):
        dbg(f"{what}: already {'on' if on else 'off'}")
        return
    if DRY_RUN:
        log(f"would set {what}: {sysfs} {cur} -> {on}")
        return
    node = b.devnode
    if node is None:
        fail(f"{what}: no device node for /dev/{b.name}")
        return
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    except OSError as e:
        fail(f"{what}: open {node} ({e.strerror})")
        return
    try:
        fcntl.ioctl(fd, LOOP_SET_DIRECT_IO, on)
    except OSError as e:
        hint = " (backing file does not support O_DIRECT or offset misaligned)" if e.errno == 22 else ""
        fail(f"{what}: LOOP_SET_DIRECT_IO {on} on {node} ({e.strerror}){hint}")
        return
    finally:
        os.close(fd)
    now = _read(sysfs)
    if now == str(on):
        log(f"{what}: {sysfs} {cur} -> {now}")
    else:
        fail(f"{what}: ioctl succeeded but {sysfs} reads {now!r}, expected {on}")


def tune_remote_fs(m: Mount, cfg: Dict[str, int]) -> None:
    bdi = f"{SYS}/class/bdi/{m.majmin}/read_ahead_kb"
    write_knob(bdi, cfg["REMOTE_RA"], f"readahead {m.fstype} {m.target}")
    if m.fstype.startswith("fuse"):
        minor = m.majmin.split(":")[1]
        conn = f"{SYS}/fs/fuse/connections/{minor}"
        if os.path.isdir(conn):
            write_knob(f"{conn}/max_background", cfg["FUSE_MAX_BACKGROUND"], "fuse max_background")
            write_knob(f"{conn}/congestion_threshold", cfg["FUSE_CONGESTION_THRESHOLD"], "fuse congestion_threshold")
        else:
            log(f"skip fuse connection tuning: {conn} not present (fusectl not mounted or no permission)")


def remount_noatime(m: Mount) -> None:
    what = f"noatime {m.target}"
    if "noatime" in m.options:
        dbg(f"{what}: already set")
        return
    if m.is_ro:
        log(f"skip {what}: mounted read-only")
        return
    if m.target == "/":
        log(f"skip {what}: not touching the root filesystem")
        return
    if DRY_RUN:
        log(f"would remount {m.target} noatime")
        return
    try:
        r = subprocess.run(["mount", "-o", "remount,noatime", m.target],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        fail(f"{what}: {e}")
        return
    if r.returncode != 0:
        fail(f"{what}: mount exited {r.returncode}: {r.stderr.strip()}")
    else:
        log(f"remounted {m.target} noatime")


def decide_remote(walker: Walker, assume_remote: bool) -> bool:
    """Is the stack network-backed? Gates MID_RA, the vm.* limits and the warm-up."""
    if walker.remote_found:
        log("remote layer detected: enabling intermediate readahead, vm dirty limits and warm-up")
    elif assume_remote:
        log("ASSUME_REMOTE set: enabling intermediate readahead, vm dirty limits and warm-up")
    else:
        log("no remote layer recognised" + (" (unknown layers present)" if walker.unknown_found else "")
            + ": leaving intermediate readahead and vm.* at defaults, not warming; set ASSUME_REMOTE=1 to override")
    return walker.remote_found or assume_remote


def apply(walker: Walker, cfg: Dict[str, int], remote: bool) -> None:
    inner_blks = walker.inner_blks
    done_mounts: Set[str] = set()
    done_blk: Set[str] = set()
    for n in walker.nodes:
        if isinstance(n.layer, Mount):
            m = n.layer
            if m.majmin in done_mounts:
                continue
            done_mounts.add(m.majmin)
            if m.is_remote:
                tune_remote_fs(m, cfg)
            elif m.fstype in BLOCK_FS_TYPES:
                # atime flags are per mount point; only the mount that was walked
                # through is remounted, other bind mounts of the device are left alone.
                remount_noatime(m)
            # anonymous/unknown filesystems: nothing to do
        elif isinstance(n.layer, BlockDev):
            b = n.layer
            if b.majmin in done_blk or n.terminal_reason == "already visited":
                continue
            done_blk.add(b.majmin)
            if b.majmin in inner_blks:
                if b.is_virtual:
                    set_readahead(b, cfg["INNER_RA"], f"readahead inner /dev/{b.name}")
                    if b.kind == "loop":
                        set_loop_dio(b, cfg["INNER_DIO"], f"direct-io inner /dev/{b.name}")
                else:
                    log(f"leave readahead of inner /dev/{b.name} ({b.kind}) at default")
            elif b.is_virtual or b.kind == "disk-remote":
                if remote:
                    set_readahead(b, cfg["MID_RA"], f"readahead {b.kind} /dev/{b.name}")
                    if b.kind == "loop":
                        set_loop_dio(b, cfg["MID_DIO"], f"direct-io {b.kind} /dev/{b.name}")
                else:
                    log(f"leave readahead and direct-io of /dev/{b.name} ({b.kind}) at default: stack not known to be remote")
            # partitions, local disks, unknown: untouched

    if remote:
        write_knob(f"{PROC}/sys/vm/vfs_cache_pressure", cfg["VM_VFS_CACHE_PRESSURE"], "vm.vfs_cache_pressure")
        # dirty_bytes must exceed dirty_background_bytes; set background first when lowering,
        # foreground first when raising, so the pair is valid in between.
        cur_dirty = int(_read(f"{PROC}/sys/vm/dirty_bytes") or 0)
        order = [("VM_DIRTY_BACKGROUND_BYTES", "dirty_background_bytes"), ("VM_DIRTY_BYTES", "dirty_bytes")]
        if cur_dirty and cfg["VM_DIRTY_BYTES"] > cur_dirty:
            order.reverse()
        for key, knob in order:
            write_knob(f"{PROC}/sys/vm/{knob}", cfg[key], f"vm.{knob}")


# ----------------------------------------------------------------------------
# warm-up: pull the target filesystem's allocated blocks through the stack
# ----------------------------------------------------------------------------
class ExtAllocation:
    """Streams dumpe2fs output into a bitmap of chunks that hold allocated blocks.

    dumpe2fs lists the *free* ranges of every block group, in ascending order;
    everything between them is allocated (data, inode tables, bitmaps, journal).
    The output can be huge on a fragmented filesystem, so it is consumed line by
    line and only a chunk-granularity bitmap is kept (1 bit per WARM_CHUNK_KB, i.e.
    4 KiB per 32 GiB at the default 1 MiB chunk). A free range that arrives out of
    order is not trusted; the space stays marked allocated, which only over-reads.
    """

    def __init__(self, chunk: int):
        self.chunk = chunk
        self.block_size = 0
        self.block_count = 0
        self.free_blocks = 0
        self.out_of_order = 0
        self._cur = 0                   # first block not yet classified
        self._bits = bytearray()
        self._in_groups = False

    @property
    def used_blocks(self) -> int:
        return self.block_count - self.free_blocks

    def feed(self, line: str) -> None:
        if not self._in_groups:
            # superblock header: "Block count:" / "Block size:" (in that order), then "Group 0:"
            if line.startswith("Block count:"):
                self.block_count = int(line.split(":", 1)[1])
            elif line.startswith("Block size:"):
                self.block_size = int(line.split(":", 1)[1])
            elif line.startswith("Group "):
                if not (self.block_size and self.block_count):
                    raise ValueError("dumpe2fs output lacks 'Block size:' / 'Block count:' before the group list")
                nchunks = -(-(self.block_count * self.block_size) // self.chunk)
                self._bits = bytearray(-(-nchunks // 8))
                self._in_groups = True
            return
        if not line.startswith((" ", "\t")):
            return                      # "Group N:" header line
        stripped = line.lstrip()
        if not stripped.startswith("Free blocks:"):
            return                      # other per-group attribute
        for part in stripped[len("Free blocks:"):].split(","):
            part = part.strip()
            if not part:
                continue
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b or a) + 1
            self.free_blocks += hi - lo
            if lo < self._cur:
                self.out_of_order += 1
                lo = self._cur
                if hi <= lo:
                    continue
            self._mark_used(self._cur, lo)
            self._cur = hi

    def finish(self) -> None:
        if not self._in_groups:
            raise ValueError("dumpe2fs output has no block group list")
        self._mark_used(self._cur, self.block_count)
        self._cur = self.block_count

    def _mark_used(self, lo_block: int, hi_block: int) -> None:
        if hi_block <= lo_block:
            return
        lo = (lo_block * self.block_size) // self.chunk
        hi = (hi_block * self.block_size - 1) // self.chunk + 1   # exclusive
        bits = self._bits
        while lo < hi and lo % 8:
            bits[lo >> 3] |= 1 << (lo & 7)
            lo += 1
        if hi - lo >= 8:
            full = (hi - lo) // 8
            bits[lo >> 3:(lo >> 3) + full] = b"\xff" * full
            lo += full * 8
        while lo < hi:
            bits[lo >> 3] |= 1 << (lo & 7)
            lo += 1

    def offsets(self) -> array:
        """Byte offsets of every chunk holding allocated blocks, ascending."""
        out = array("q")
        chunk = self.chunk
        for i, byte in enumerate(self._bits):
            if byte:
                base = i * 8
                out.extend((base + j) * chunk for j in range(8) if byte >> j & 1)
        return out


def run_dumpe2fs(devnode: str, on_line: Optional[Callable[[str], None]] = None) -> bool:
    """Run dumpe2fs on devnode, streaming stdout to on_line (or discarding it when
    only the metadata read itself is wanted). Returns True on success."""
    with tempfile.TemporaryFile("w+") as err:
        try:
            proc = subprocess.Popen(["dumpe2fs", devnode],
                                    stdout=subprocess.PIPE if on_line else subprocess.DEVNULL,
                                    stderr=err, text=True, errors="replace")
        except FileNotFoundError:
            log("skip warm-up: dumpe2fs not installed (e2fsprogs)")
            return False
        except OSError as e:
            fail(f"warm-up: dumpe2fs {devnode}: {e.strerror}")
            return False
        try:
            if on_line and proc.stdout:
                for line in proc.stdout:
                    on_line(line.rstrip("\n"))
        except ValueError as e:
            proc.kill()
            fail(f"warm-up: parsing dumpe2fs {devnode}: {e}")
            return False
        finally:
            if proc.stdout:
                proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            err.seek(0)
            lines = err.read().strip().splitlines()
            fail(f"warm-up: dumpe2fs {devnode} exited {rc}: {lines[-1] if lines else ''}")
            return False
    return True


def read_chunks(devnode: str, offsets: array, chunk: int, readers: int) -> int:
    """O_DIRECT-read every chunk; returns bytes read. Each reader gets a contiguous
    slice so it stays sequential (readahead-friendly) on the layers below."""
    def worker(sub: array) -> Tuple[int, List[str]]:
        n, errors = 0, []
        try:
            fd = os.open(devnode, os.O_RDONLY | os.O_DIRECT)
        except OSError as e:
            return 0, [f"open {devnode}: {e.strerror}"]
        try:
            buf = mmap.mmap(-1, chunk)      # page aligned, as O_DIRECT requires
            try:
                for off in sub:
                    try:
                        n += os.preadv(fd, [buf], off)
                    except OSError as e:
                        errors.append(f"read {devnode} @ {off}: {e.strerror}")
            finally:
                buf.close()
        finally:
            os.close(fd)
        return n, errors

    readers = max(1, min(readers, len(offsets)))
    per = -(-len(offsets) // readers)
    with ThreadPoolExecutor(readers) as ex:
        results = list(ex.map(worker, [offsets[i:i + per] for i in range(0, len(offsets), per)]))
    total = sum(n for n, _ in results)
    errors = [e for _, errs in results for e in errs]
    for e in errors[:5]:
        fail(f"warm-up: {e}")
    if len(errors) > 5:
        fail(f"warm-up: {len(errors) - 5} more read errors not shown")
    return total


def warm(walker: Walker, cfg: Dict[str, int], remote: bool, readers: int) -> None:
    if not remote:
        log("skip warm-up: stack not known to be remote, nothing to pull into a local cache")
        return
    chunk = cfg["WARM_CHUNK_KB"] * 1024
    MiB = 1 << 20
    for m in walker.walked_mounts():
        is_target = m in walker.targets
        dev_mm = walker.fs_dev.get(m.majmin)
        if m.fstype not in EXT_FS_TYPES or dev_mm is None:
            if is_target:
                log(f"skip warm-up of {m.target}: only ext2/3/4 on a block device is supported (found {m.fstype})")
            continue
        b = block_device(dev_mm)
        node = b.devnode if b else None
        if node is None:
            log(f"skip warm-up of {m.target}: no device node for {dev_mm}")
            continue
        if not os.access(node, os.R_OK):
            log(f"skip warm-up of {m.target}: {node} not readable")
            continue

        # dumpe2fs reads the superblock, group descriptors and bitmaps, i.e. exactly the
        # metadata worth having local on every layer; for non-target layers that is all
        # we want (their allocated *data* is the stale bulk we are trying to avoid).
        if not is_target:
            if run_dumpe2fs(node):
                dbg(f"warm-up: touched metadata of {m.fstype} {m.target} on {node}")
            continue
        alloc = ExtAllocation(chunk)
        if not run_dumpe2fs(node, alloc.feed):
            continue
        try:
            alloc.finish()
        except ValueError as e:
            fail(f"warm-up: parsing dumpe2fs {node}: {e}")
            continue
        if alloc.out_of_order:
            log(f"warm-up {m.target}: {alloc.out_of_order} free range(s) out of order; treating them as allocated")
        offsets = alloc.offsets()
        bs = alloc.block_size
        log(f"warm-up {m.target} via {node}: {alloc.used_blocks * bs / MiB:.0f} MiB allocated of"
            f" {alloc.block_count * bs / MiB:.0f} MiB -> {len(offsets)} x {chunk // 1024} KiB chunks, {readers} readers")
        if DRY_RUN:
            log(f"would read {len(offsets) * chunk / MiB:.0f} MiB from {node}")
            continue
        t0 = time.monotonic()
        n = read_chunks(node, offsets, chunk, readers)
        dt = time.monotonic() - t0
        log(f"warm-up {m.target}: read {n / MiB:.0f} MiB in {dt:.1f} s ({n / MiB / dt if dt else 0:.0f} MiB/s)")


# ----------------------------------------------------------------------------
def default_targets() -> List[str]:
    root = "/var/lib/docker"
    try:
        with open("/etc/docker/daemon.json") as fh:
            conf = json.load(fh)
        if isinstance(conf, dict) and isinstance(conf.get("data-root"), str):
            root = conf["data-root"]
    except (OSError, ValueError):
        pass
    return [root]


def main(argv: List[str]) -> int:
    global VERBOSE, DRY_RUN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", action="append", metavar="PATH",
                    help="directory whose storage stack to tune (default: docker data-root)")
    ap.add_argument("--warm", action="store_true",
                    help="after tuning, prefetch the allocated blocks of each target filesystem "
                         "so the remote layer's local cache holds the working set")
    ap.add_argument("--warm-only", action="store_true", help="warm up without applying tunables")
    ap.add_argument("--readers", type=int, metavar="N", default=env_int("WARM_READERS", 2),
                    help="parallel readers for the warm-up (default: $WARM_READERS or 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and report only; write nothing, read no data blocks")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if anything failed (default: always 0)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    VERBOSE, DRY_RUN = args.verbose, args.dry_run

    MiB = 1 << 20
    cfg = {
        "INNER_RA": env_int("INNER_RA", 128, "LOOP4_RA"),
        "MID_RA": env_int("MID_RA", 4096, "LOOP3_RA"),
        "INNER_DIO": env_int("INNER_DIO", 0, "LOOP4_DIO"),
        "MID_DIO": env_int("MID_DIO", 1, "LOOP3_DIO"),
        "REMOTE_RA": env_int("REMOTE_RA", 4096, "FUSE_RA"),
        "FUSE_MAX_BACKGROUND": env_int("FUSE_MAX_BACKGROUND", 64),
        "FUSE_CONGESTION_THRESHOLD": env_int("FUSE_CONGESTION_THRESHOLD", 48),
        "VM_DIRTY_BACKGROUND_BYTES": env_int("VM_DIRTY_BACKGROUND_BYTES", 128 * MiB),
        "VM_DIRTY_BYTES": env_int("VM_DIRTY_BYTES", 512 * MiB),
        "VM_VFS_CACHE_PRESSURE": env_int("VM_VFS_CACHE_PRESSURE", 50),
        "WARM_CHUNK_KB": env_int("WARM_CHUNK_KB", 1024),
    }
    assume_remote = os.environ.get("ASSUME_REMOTE", "") not in ("", "0", "false", "no")

    if os.geteuid() != 0 and not DRY_RUN:
        log("not running as root: switching to --dry-run (nothing will be applied)")
        DRY_RUN = True

    targets = args.target or default_targets()
    walker = Walker(read_mounts())
    for t in targets:
        if not os.path.isdir(t):
            log(f"target {t} is not a directory; skipping")
            continue
        walker.walk_path(t, 0)

    log("detected stack:")
    for n in walker.nodes:
        log("  " + n.describe())
    if walker.unknown_found:
        log("some layers were not recognised; they are left untouched (see STOP markers above)")

    remote = decide_remote(walker, assume_remote)
    if not args.warm_only:
        apply(walker, cfg, remote)
    if args.warm or args.warm_only:
        warm(walker, cfg, remote, args.readers)

    if FAILURES:
        log(f"done with {len(FAILURES)} failure(s)")
    else:
        log("done" + (" (dry run)" if DRY_RUN else ""))
    return 1 if (args.strict and FAILURES) else 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - critical path: never take the pipeline down
        log("internal error; nothing more applied:")
        for line in traceback.format_exc().rstrip().splitlines():
            log("  " + line)
        rc = 1 if "--strict" in sys.argv[1:] else 0
    sys.exit(rc)
