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
touched here (readahead, FUSE queue depth, noatime, dirty-page limits) can
corrupt data, so there is no situation in which aborting is safer than
continuing.

Usage: apply-tunables.py [--target PATH]... [--dry-run] [--strict] [--verbose]
Environment overrides (KiB unless noted):
  INNER_RA  (alias LOOP4_RA, default 128)  readahead of the device directly under
                                            the docker root; larger measured slower
                                            for small files. Only applied to virtual
                                            devices (loop/dm), never to real disks.
  MID_RA    (alias LOOP3_RA, default 4096) readahead of intermediate virtual block
                                            devices and remote block devices, applied
                                            only when a remote layer is detected below.
  REMOTE_RA (alias FUSE_RA,  default 4096) readahead on FUSE / network filesystems.
  FUSE_MAX_BACKGROUND (64), FUSE_CONGESTION_THRESHOLD (48)
  VM_DIRTY_BACKGROUND_BYTES (128 MiB), VM_DIRTY_BYTES (512 MiB), VM_VFS_CACHE_PRESSURE (50)
  ASSUME_REMOTE=1  treat the stack as network-backed even if no remote layer was
                   recognised (enables MID_RA and the vm.* limits).
"""
import argparse
import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

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
    mount: Optional[Mount] = None
    blk: Optional[BlockDev] = None
    terminal_reason: Optional[str] = None   # set when the walk stopped here

    def describe(self) -> str:
        pad = "  " * self.depth
        if self.mount is not None:
            m = self.mount
            s = f"{pad}fs {m.fstype} {m.majmin} {m.target} (source={m.source}, opts={','.join(sorted(m.options))})"
        else:
            s = f"{pad}blk {self.blk}"
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
        self.inner_blk: Optional[str] = None  # majmin of the device directly under the target fs

    def walk_path(self, path: str, depth: int = 0) -> None:
        m = mount_for_path(path, self.mounts)
        if m is None:
            n = Node(depth, terminal_reason=f"no mount found for {path}")
            self.nodes.append(n)
            self.unknown_found = True
            log(f"could not find a mount for {path}")
            return
        self.walk_mount(m, depth)

    def walk_mount(self, m: Mount, depth: int) -> None:
        node = Node(depth, mount=m)
        self.nodes.append(node)
        if m.majmin in self.seen_mounts:
            node.terminal_reason = "already visited"
            return
        self.seen_mounts.add(m.majmin)

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
        if depth == 0 and self.inner_blk is None:
            self.inner_blk = blk_mm
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
        node = Node(depth)
        self.nodes.append(node)
        if majmin in self.seen_blk:
            node.blk = block_device(majmin) or BlockDev(majmin, "?", "", "unknown")
            node.terminal_reason = "already visited"
            return
        self.seen_blk.add(majmin)
        b = block_device(majmin)
        if b is None:
            node.blk = BlockDev(majmin, "?", "", "unknown")
            node.terminal_reason = "no sysfs entry for this device"
            self.unknown_found = True
            return
        node.blk = b

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


def apply(walker: Walker, cfg: Dict[str, int], assume_remote: bool) -> None:
    remote = walker.remote_found or assume_remote
    if walker.remote_found:
        log("remote layer detected: enabling intermediate readahead and vm dirty limits")
    elif assume_remote:
        log("ASSUME_REMOTE set: enabling intermediate readahead and vm dirty limits")
    else:
        log("no remote layer recognised" + (" (unknown layers present)" if walker.unknown_found else "")
            + ": leaving intermediate readahead and vm.* at defaults; set ASSUME_REMOTE=1 to override")

    done_mounts: Set[str] = set()
    done_blk: Set[str] = set()
    for n in walker.nodes:
        if n.mount is not None:
            m = n.mount
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
        elif n.blk is not None:
            b = n.blk
            if b.majmin in done_blk or n.terminal_reason == "already visited":
                continue
            done_blk.add(b.majmin)
            if b.majmin == walker.inner_blk:
                if b.is_virtual:
                    set_readahead(b, cfg["INNER_RA"], f"readahead inner /dev/{b.name}")
                else:
                    log(f"leave readahead of inner /dev/{b.name} ({b.kind}) at default")
            elif b.is_virtual or b.kind == "disk-remote":
                if remote:
                    set_readahead(b, cfg["MID_RA"], f"readahead {b.kind} /dev/{b.name}")
                else:
                    log(f"leave readahead of /dev/{b.name} ({b.kind}) at default: stack not known to be remote")
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
    ap.add_argument("--dry-run", action="store_true", help="detect and report only; write nothing")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if anything failed (default: always 0)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    VERBOSE, DRY_RUN = args.verbose, args.dry_run

    MiB = 1 << 20
    cfg = {
        "INNER_RA": env_int("INNER_RA", 128, "LOOP4_RA"),
        "MID_RA": env_int("MID_RA", 4096, "LOOP3_RA"),
        "REMOTE_RA": env_int("REMOTE_RA", 4096, "FUSE_RA"),
        "FUSE_MAX_BACKGROUND": env_int("FUSE_MAX_BACKGROUND", 64),
        "FUSE_CONGESTION_THRESHOLD": env_int("FUSE_CONGESTION_THRESHOLD", 48),
        "VM_DIRTY_BACKGROUND_BYTES": env_int("VM_DIRTY_BACKGROUND_BYTES", 128 * MiB),
        "VM_DIRTY_BYTES": env_int("VM_DIRTY_BYTES", 512 * MiB),
        "VM_VFS_CACHE_PRESSURE": env_int("VM_VFS_CACHE_PRESSURE", 50),
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

    apply(walker, cfg, assume_remote)

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
