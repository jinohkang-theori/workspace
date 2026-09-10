#!/usr/bin/env python3
"""Run a command in the namespaces of the docker *host* through the Docker Engine API.

This is the standard-library equivalent of

    docker run --rm -i --privileged --pid=host --userns=host --uts=host --ipc=host \\
        --cgroupns=host --network=host --entrypoint nsenter IMAGE -t 1 -m -u -i -n -p -- CMD...

spoken directly over the host's docker socket, so neither the docker CLI nor the
docker-outside-of-docker Feature is needed. The socket only has to be bind-mounted into
the container (the storage-tuning Feature mounts /var/run/docker.sock at
/var/run/docker-host.sock itself); /var/run/docker.sock is left alone, which keeps this
compatible with docker-in-docker.

A throw-away privileged helper container is created from IMAGE (default: this dev
container's own image, so nothing is pulled), `nsenter -t 1 -m -u -i -n -p` moves into
PID 1's namespaces and CMD runs there. stdout/stderr are streamed back; stdin can be
fed from a file (--stdin FILE) or forwarded (-i). With -t the helper gets a pseudo-TTY
and the local terminal is put into raw mode, so `run-on-host.py -it -- bash` is a
root shell on the host. The exit status is CMD's.

Usage: run-on-host.py [--socket PATH] [--image IMAGE] [--name NAME] [--timeout SECONDS]
                      [--stdin FILE | -i] [-t] [--verbose] -- CMD [ARGS...]

If the socket is not accessible to the current user and passwordless sudo is available,
the script re-executes itself through sudo. Exit status 125 means this script (or the
daemon) failed before CMD ran, like docker's own convention.
"""
import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import termios
import threading
import tty
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

TAG = "[run-on-host]"
DEFAULT_SOCKET = "/var/run/docker-host.sock"
FALLBACK_IMAGE = "docker.io/library/busybox:stable"
NSENTER_ARGS = ["-t", "1", "-m", "-u", "-i", "-n", "-p", "--"]
EXIT_INTERNAL = 125

VERBOSE = False


def log(*parts: Any) -> None:
    print(TAG, *parts, file=sys.stderr, flush=True)


def debug(*parts: Any) -> None:
    if VERBOSE:
        log(*parts)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket (the docker daemon ignores the Host header)."""

    def __init__(self, path: str, timeout: Optional[float] = None):
        super().__init__("docker", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


class Docker:
    def __init__(self, sock_path: str):
        self.sock_path = sock_path

    def request(self, method: str, path: str, body: Any = None, ok=(200, 201, 204)) -> Any:
        """One request/response. JSON bodies are decoded; errors raise ApiError."""
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = UnixHTTPConnection(self.sock_path)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.decode(errors="replace")
        debug(f"{method} {path} -> {resp.status}")
        if resp.status not in ok:
            msg = parsed.get("message") if isinstance(parsed, dict) else parsed
            raise ApiError(resp.status, str(msg or resp.reason or "").strip())
        return parsed

    def attach(self, cid: str, with_stdin: bool) -> Tuple[socket.socket, bytes]:
        """Hijack an attach stream. Returns the raw socket and any stream bytes that
        arrived together with the response headers."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock_path)
        query = "stream=1&stdout=1&stderr=1&stdin=" + ("1" if with_stdin else "0")
        req = (
            f"POST /containers/{cid}/attach?{query} HTTP/1.1\r\n"
            "Host: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n"
        )
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                s.close()
                raise ApiError(0, "daemon closed the attach connection before answering")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError):
            s.close()
            raise ApiError(0, "malformed attach response: " + lines[0])
        debug(f"POST /containers/{cid}/attach -> {status}")
        if status in (101, 200):
            return s, rest
        # Error: collect the body (Content-Length is what the daemon uses for errors).
        length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip() or 0)
        while len(rest) < length:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        s.close()
        try:
            msg = json.loads(rest).get("message", rest.decode(errors="replace"))
        except ValueError:
            msg = rest.decode(errors="replace")
        raise ApiError(status, str(msg).strip())


# --- helpers -------------------------------------------------------------------------

def self_container_id() -> Optional[str]:
    """The 64-hex id of the container we run in, from mountinfo/cgroup; None if unknown."""
    pat = re.compile(r"/(?:docker|containers)/([0-9a-f]{64})")
    for f in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        try:
            with open(f) as fh:
                m = pat.search(fh.read())
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def pick_image(api: Docker, sock_path: str) -> str:
    """This container's image, so the helper needs no pull; falls back to busybox."""
    for cid in (self_container_id(), socket.gethostname()):
        if not cid:
            continue
        try:
            info = api.request("GET", f"/containers/{cid}/json")
            image = info.get("Image") or ""
            if image:
                debug(f"helper image from own container {cid[:12]}: {image}")
                return image
        except ApiError as e:
            debug(f"inspect {cid[:12]}: {e}")
    filters = quote(json.dumps({"volume": [sock_path]}))
    try:
        for c in api.request("GET", f"/containers/json?filters={filters}") or []:
            image = c.get("ImageID") or c.get("Image")
            if image:
                debug(f"helper image from container mounting {sock_path}: {image}")
                return image
    except ApiError as e:
        debug(f"list containers: {e}")
    log(f"could not identify this container's image; falling back to {FALLBACK_IMAGE} (may need a pull)")
    return FALLBACK_IMAGE


def check_socket(path: str) -> None:
    """Raise PermissionError/FileNotFoundError/OSError with a usable message."""
    st = os.stat(path)  # FileNotFoundError if absent
    if not stat.S_ISSOCK(st.st_mode):
        raise OSError(f"{path} is not a socket")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)  # PermissionError if we lack rw on the socket
    finally:
        s.close()


def reexec_with_sudo(argv: List[str]) -> None:
    """Replace this process with `sudo -n python3 <this script> argv...`, if possible."""
    if os.geteuid() == 0 or not shutil.which("sudo"):
        return
    probe = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        return
    debug("socket not accessible; re-executing through sudo")
    os.execvp("sudo", ["sudo", "-n", "--", sys.executable, os.path.abspath(__file__)] + argv)


def feed_stdin(sock: socket.socket, fd: int) -> None:
    """Copy file descriptor fd into the hijacked connection, then half-close it so the
    daemon closes the container's stdin (StdinOnce). Runs in a daemon thread, so it must
    use raw os.read: a blocked read on the buffered sys.stdin would hold that object's
    lock at interpreter shutdown and abort the process."""
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            sock.sendall(data)
    except OSError as e:
        debug(f"stdin copy stopped: {e}")
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def pump_output(sock: socket.socket, initial: bytes, raw: bool) -> None:
    """Copy the attach stream to our stdout/stderr until the daemon closes the connection.
    With a TTY the stream is raw; otherwise it is multiplexed in frames (8-byte header:
    type, 3 pad, u32 BE length) that are split onto stdout and stderr."""
    outs = {1: sys.stdout.buffer, 2: sys.stderr.buffer}
    if raw:
        if initial:
            sys.stdout.buffer.write(initial)
            sys.stdout.buffer.flush()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    buf = bytearray(initial)
    while True:
        while len(buf) >= 8:
            size = int.from_bytes(buf[4:8], "big")
            if len(buf) < 8 + size:
                break
            kind = buf[0]
            payload = bytes(buf[8:8 + size])
            del buf[:8 + size]
            out = outs.get(kind, sys.stderr.buffer)
            out.write(payload)
            out.flush()
        chunk = sock.recv(65536)
        if not chunk:
            if buf:  # not frame-aligned: dump what is left rather than lose it
                sys.stdout.buffer.write(bytes(buf))
                sys.stdout.buffer.flush()
            return
        buf += chunk


# --- main ----------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run-on-host",
        description="Run CMD in the docker host's namespaces (nsenter into PID 1) via the docker socket.",
    )
    p.add_argument("--socket", default=DEFAULT_SOCKET,
                   help=f"host docker socket inside this container (default {DEFAULT_SOCKET})")
    p.add_argument("--image", default="",
                   help="helper container image; it only needs nsenter. Empty: this container's own image")
    p.add_argument("--name", default=f"run-on-host-{os.getpid()}", help="helper container name")
    p.add_argument("--timeout", type=float, default=0,
                   help="kill the helper (and with it the host-side processes) after this many seconds; 0 = never")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stdin", metavar="FILE", help="feed FILE to CMD's stdin")
    g.add_argument("-i", "--interactive", action="store_true", help="forward our stdin to CMD")
    p.add_argument("-t", "--tty", action="store_true",
                   help="give CMD a pseudo-TTY (raw mode on our terminal, resize forwarded); combine with -i for a shell")
    p.add_argument("--verbose", action="store_true", help="log every API call")
    p.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- CMD [ARGS...]")
    args = p.parse_args(argv)
    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if not args.cmd:
        p.error("no command given")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", args.name):
        p.error("--name must match [A-Za-z0-9][A-Za-z0-9_.-]+")
    return args


def main(argv: List[str]) -> int:
    global VERBOSE
    args = parse_args(argv)
    VERBOSE = args.verbose

    try:
        check_socket(args.socket)
    except PermissionError:
        reexec_with_sudo(argv)  # only returns if sudo is not an option
        log(f"permission denied on {args.socket} and passwordless sudo is unavailable; "
            "run as root or add the user to the socket's group")
        return EXIT_INTERNAL
    except FileNotFoundError:
        log(f"host docker socket {args.socket} is not present; is the bind mount of /var/run/docker.sock in place?")
        return EXIT_INTERNAL
    except OSError as e:
        log(f"cannot use {args.socket}: {e}")
        return EXIT_INTERNAL

    stdin_file = None
    stdin_fd = None
    if args.stdin:
        try:
            stdin_file = open(args.stdin, "rb")
        except OSError as e:
            log(f"cannot read {args.stdin}: {e}")
            return EXIT_INTERNAL
        stdin_fd = stdin_file.fileno()
    elif args.interactive:
        stdin_fd = sys.stdin.fileno()
    with_stdin = stdin_fd is not None
    use_tty = args.tty
    local_tty = use_tty and sys.stdin.isatty()
    if use_tty and with_stdin and not (args.interactive and local_tty):
        # In TTY mode the daemon closes stdout/stderr as soon as stdin hits EOF, so a
        # pipe or file on stdin would truncate the output. Same rule as `docker run -t`.
        log("the input device is not a TTY; use -t only with -i on a terminal (or without stdin)")
        return EXIT_INTERNAL

    api = Docker(args.socket)
    try:
        version = api.request("GET", "/version")
    except (ApiError, OSError) as e:
        log(f"cannot talk to the docker daemon through {args.socket}: {e}")
        return EXIT_INTERNAL
    debug(f"daemon {version.get('Version')} (API {version.get('ApiVersion')})")

    image = args.image or pick_image(api, args.socket)

    spec = {
        "Image": image,
        "Entrypoint": ["nsenter"],
        "Cmd": NSENTER_ARGS + args.cmd,
        "Tty": use_tty,
        "OpenStdin": with_stdin,
        "StdinOnce": with_stdin,
        "AttachStdin": with_stdin,
        "AttachStdout": True,
        "AttachStderr": True,
        "Labels": {"run-on-host": "1"},
        "HostConfig": {
            "Privileged": True,
            "PidMode": "host",
            "UsernsMode": "host",
            "UTSMode": "host",
            "IpcMode": "host",
            "CgroupnsMode": "host",
            "NetworkMode": "host",
        },
    }

    cid = None
    stream = None
    timer = None
    saved_termios = None
    timed_out = threading.Event()
    rc = EXIT_INTERNAL

    def resize() -> None:
        try:
            size = os.get_terminal_size(sys.stdin.fileno())
            if size.lines <= 0 or size.columns <= 0:
                return  # no real size to forward (e.g. a fresh pty); the daemon rejects 0x0 anyway
            api.request("POST", f"/containers/{cid}/resize?h={size.lines}&w={size.columns}", ok=(200, 204))
        except (OSError, ApiError) as e:
            debug(f"resize: {e}")

    def kill(reason: str) -> None:
        if cid is None:
            return
        try:
            api.request("POST", f"/containers/{cid}/kill?signal=SIGKILL", ok=(204, 409, 404))
            log(f"{reason}; helper container killed")
        except (ApiError, OSError) as e:
            debug(f"kill: {e}")

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        try:
            created = api.request("POST", f"/containers/create?name={quote(args.name)}", spec)
        except ApiError as e:
            log(f"cannot create the helper container from {image}: {e.message}")
            return EXIT_INTERNAL
        cid = created["Id"]
        debug(f"created helper {cid[:12]} ({args.name}) from {image}")

        # Attach before starting so no early output is lost (as the docker CLI does).
        stream, initial = api.attach(cid, with_stdin)
        api.request("POST", f"/containers/{cid}/start")
        if local_tty:
            # Raw mode: keystrokes (including ^C) go to the host command, not to us.
            saved_termios = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            resize()
            signal.signal(signal.SIGWINCH, lambda *_: resize())

        if args.timeout and args.timeout > 0:
            def fire():
                timed_out.set()
                kill(f"timed out after {args.timeout:g}s")
            timer = threading.Timer(args.timeout, fire)
            timer.daemon = True
            timer.start()

        if with_stdin:
            # Daemon thread: when the container exits it may still be blocked reading a
            # terminal, and there is nothing left to feed anyway.
            threading.Thread(target=feed_stdin, args=(stream, stdin_fd), daemon=True).start()

        pump_output(stream, initial, raw=use_tty)

        result = api.request("POST", f"/containers/{cid}/wait")
        rc = int(result.get("StatusCode", EXIT_INTERNAL))
        err = (result.get("Error") or {}).get("Message")
        if err:
            log(f"helper reported: {err}")
        debug(f"helper exited with {rc}")
        return rc
    except ApiError as e:
        log(f"docker API error: {e}")
        return EXIT_INTERNAL
    except OSError as e:
        log(f"I/O error talking to the daemon: {e}")
        return EXIT_INTERNAL
    except SystemExit as e:
        kill(f"interrupted ({e.code})")
        raise
    finally:
        if saved_termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_termios)
        if timer is not None:
            timer.cancel()
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        if stdin_file is not None:
            stdin_file.close()
        if cid is not None:
            try:
                api.request("DELETE", f"/containers/{cid}?v=1&force=1", ok=(204, 404))
            except (ApiError, OSError) as e:
                log(f"could not remove helper container {cid[:12]}: {e}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
