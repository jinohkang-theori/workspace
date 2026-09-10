#!/usr/bin/env python3
"""A tiny stand-in for the Docker Engine API, for testing run-on-host.py without a host.

    fake-dockerd.py SOCKET RECORD_DIR

Listens on the unix SOCKET and answers just enough of the API for run-on-host.py to
complete one helper run. Everything it learns is written under RECORD_DIR:

    create.json   the body of POST /containers/create
    stdin         what the client streamed into the attach connection
    deleted       exists once DELETE /containers/<id> was called
    killed        exists once POST /containers/<id>/kill was called

The "container" prints one line on stdout and one on stderr and exits with
$FAKE_EXIT (default 0). The real host is never touched.
"""
import json
import os
import socketserver
import sys

CID = "c" * 64
IMAGE = "sha256:0123456789abcdef"


def frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


class Handler(socketserver.StreamRequestHandler):
    rbufsize = 0  # unbuffered: bytes after the headers must stay on the socket for attach

    def respond(self, status: int, body=None, reason="OK") -> None:
        data = b"" if body is None else json.dumps(body).encode()
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
        self.connection.sendall(head + data)

    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        method, target, _ = line.decode().split(" ", 2)
        headers = {}
        while True:
            h = self.rfile.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, v = h.decode().split(":", 1)
            headers[k.strip().lower()] = v.strip()
        body = b""
        n = int(headers.get("content-length", "0") or 0)
        while len(body) < n:
            chunk = self.connection.recv(n - len(body))
            if not chunk:
                break
            body += chunk
        path, _, _query = target.partition("?")
        rec = self.server.record_dir  # type: ignore[attr-defined]

        if method == "GET" and path == "/version":
            self.respond(200, {"Version": "99.0.0", "ApiVersion": "1.43"})
        elif method == "GET" and path == "/containers/json":
            self.respond(200, [])
        elif method == "GET" and path.startswith("/containers/") and path.endswith("/json"):
            self.respond(200, {"Id": path.split("/")[2], "Image": IMAGE})
        elif method == "POST" and path == "/containers/create":
            with open(os.path.join(rec, "create.json"), "wb") as f:
                f.write(body)
            self.respond(201, {"Id": CID, "Warnings": []}, "Created")
        elif method == "POST" and path == f"/containers/{CID}/attach":
            self.connection.sendall(
                b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
                b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            data = b""
            if "stdin=1" in _query:
                while True:
                    chunk = self.connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            with open(os.path.join(rec, "stdin"), "wb") as f:
                f.write(data)
            self.connection.sendall(frame(1, b"fake-host: ran\n") + frame(2, b"fake-host: stderr\n"))
        elif method == "POST" and path == f"/containers/{CID}/start":
            self.respond(204, None, "No Content")
        elif method == "POST" and path == f"/containers/{CID}/kill":
            open(os.path.join(rec, "killed"), "w").close()
            self.respond(204, None, "No Content")
        elif method == "POST" and path == f"/containers/{CID}/wait":
            self.respond(200, {"StatusCode": int(os.environ.get("FAKE_EXIT", "0")), "Error": None})
        elif method == "DELETE" and path == f"/containers/{CID}":
            open(os.path.join(rec, "deleted"), "w").close()
            self.respond(204, None, "No Content")
        else:
            self.respond(404, {"message": f"fake-dockerd: no handler for {method} {path}"}, "Not Found")


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, record_dir: str):
        self.record_dir = record_dir
        super().__init__(path, Handler)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    sock, rec = sys.argv[1], sys.argv[2]
    os.makedirs(rec, exist_ok=True)
    if os.path.exists(sock):
        os.unlink(sock)
    srv = Server(sock, rec)
    print("ready", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
