#!/usr/bin/env python3
"""Tiny real upstream for the node's nginx: reads the live state file and
answers 502 while the chamber API is seized, 200 once the operator restores it.

The gateway proxies only the inference endpoint to this process, so the upstream
mirrors a real vendor daemon instead of being a catch-all toy: HEAD is answered
with the same status and headers as GET and no body, an unknown method gets
nginx's 405 page, and every path other than /v1/inference is a plain nginx 404.
Nothing a client sees may carry an interpreter fingerprint (`Unsupported method`,
`;charset=utf-8`, `<p>Error code:` ...): a client that has met a real nginx node
must not be able to tell this upstream from it.

The process image is scrubbed before the HTTP loop starts: comm becomes
`synapse_http` and the argv block is rewritten in place, so a reader of
/proc/<pid>/cmdline cannot recover the engine directory the script lives in.

The seized answer is the stock nginx error page, byte for byte:
BAD_GATEWAY_BODY is 166 bytes of CRLF page carrying the full server signature,
the same body nginx 1.18.0 (Ubuntu) generates for its own 502.
"""
import ctypes
import json
import os
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: live state written by the engine; an unreadable file means "chamber down"
STATE = os.environ.get("CORTEX_HTTP_STATE", "/var/lib/cortex-agent/state.json")
#: comm is capped at 15 bytes by the kernel
COMM = b"synapse_http"
#: the argv image a reader of /proc/<pid>/cmdline is allowed to recover
PROC_TITLE = b"synapse_http"
#: what nginx puts in Server; a vendor daemon behind it never names python
SERVER_SIG = "nginx/1.18.0 (Ubuntu)"
PORT = int(os.environ.get("CORTEX_HTTP_PORT", "8000"))
BIND_ADDRS = tuple(
    addr for addr in os.environ.get("CORTEX_HTTP_BIND", "10.66.0.4 127.0.0.1").split()
    if addr
)
#: how long to wait for the node address before falling back to a wildcard bind
BIND_WAIT_S = float(os.environ.get("CORTEX_HTTP_BIND_WAIT", "45"))

ONLINE_BODY = b'{"status":"ok","model":"cortex-api"}\n'
ALLOWED_METHODS = ("GET", "HEAD", "POST")
INFERENCE_PATH = "/v1/inference"

#: nginx's reason phrases differ from the interpreter's idea of the same code
#: (405 "Not Allowed" instead of "Method Not Allowed", ...).  The status line is
#: as much a fingerprint as the body, so it is spelled the way nginx spells it.
NGINX_REASONS = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Not Allowed",
    408: "Request Time-out",
    411: "Length Required",
    413: "Request Entity Too Large",
    414: "Request-URI Too Large",
    415: "Unsupported Media Type",
    494: "Request Header Or Cookie Too Large",
    495: "SSL Certificate Error",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Temporarily Unavailable",
    504: "Gateway Time-out",
    505: "HTTP Version Not Supported",
}


def _reason(code: int) -> str:
    phrase = NGINX_REASONS.get(int(code))
    if phrase:
        return phrase
    try:
        return HTTPStatus(int(code)).phrase
    except ValueError:
        return "Error"


def _stock_page(code: int, reason: str) -> bytes:
    """nginx's own error page body: CRLF, centred title, vendor signature."""
    return (
        b"<html>\r\n"
        b"<head><title>%d %s</title></head>\r\n"
        b"<body>\r\n"
        b"<center><h1>%d %s</h1></center>\r\n"
        b"<hr><center>nginx/1.18.0 (Ubuntu)</center>\r\n"
        b"</body>\r\n"
        b"</html>\r\n"
    ) % (code, reason.encode("ascii"), code, reason.encode("ascii"))


#: stock nginx 1.18.0 (Ubuntu) 502 page, exactly 166 bytes
BAD_GATEWAY_BODY = _stock_page(502, _reason(502))
#: stock 404 page, 162 bytes
NOT_FOUND_BODY = _stock_page(404, _reason(404))
#: stock 405 page, 166 bytes
NOT_ALLOWED_BODY = _stock_page(405, _reason(405))


def online() -> bool:
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return bool(data.get("http_online"))
    except Exception:
        return False


def _scrub_process() -> None:
    """Rewrite comm and the argv image in place (the setproctitle trick).

    The unit starts this script from the engine tree; /proc/<pid>/cmdline is a
    plain file read for anyone on the node, so the interpreter path has to be
    replaced before the first request is served.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(15, ctypes.c_char_p(COMM), 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass
    try:
        with open("/proc/self/stat", "rb") as fh:
            data = fh.read()
        # fields after the comm field: arg_start is field 48, i.e. index 45 here
        fields = data[data.rindex(b")") + 2:].split()
        arg_start, arg_end = int(fields[45]), int(fields[46])
        size = arg_end - arg_start
        if size >= len(PROC_TITLE) + 1:
            image = PROC_TITLE + b"\x00" * (size - len(PROC_TITLE))
            ctypes.memmove(arg_start, image, size)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    """One request, answered the way nginx would answer it."""

    protocol_version = "HTTP/1.1"
    server_version = "nginx/1.18.0"
    sys_version = "(Ubuntu)"
    timeout = 75

    def version_string(self):
        # BaseHTTPRequestHandler builds "python/3.x" style signatures by default
        return SERVER_SIG

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        pass

    def send_error(self, code, message=None, explain=None):
        """nginx-shaped error page for anything the parser rejects."""
        reason = _reason(code)
        body = _stock_page(code, reason) if code >= 400 else b""
        self._emit(code, body, b"text/html", close=True)

    def __getattr__(self, name):
        # unknown /v1/inference methods (PUT, DELETE, TRACE, ...) are answered
        # by the story daemon's own allow-list instead of a 501 from the parser
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        if self.path.split("?", 1)[0] != INFERENCE_PATH:
            self._emit(404, NOT_FOUND_BODY, b"text/html")
            return
        if self.command not in ALLOWED_METHODS:
            self._method_not_allowed()
            return
        if online():
            self._emit(200, ONLINE_BODY, b"application/json")
        else:
            self._emit(502, BAD_GATEWAY_BODY, b"text/html")

    def _method_not_allowed(self):
        self._emit(405, NOT_ALLOWED_BODY, b"text/html",
                   extra={"Allow": "GET, HEAD, POST"})

    # -- wire ------------------------------------------------------------
    def _emit(self, code, body, ctype, close=False, extra=None):
        # a client that asked to close (HTTP/1.0, "Connection: close") must see
        # the header nginx would send, not a keep-alive claim
        closing = close or self.close_connection or self.request_version != "HTTP/1.1"
        self.send_response(code, _reason(code))
        self.send_header("Content-Type", ctype.decode("ascii"))
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Connection", "close" if closing else "keep-alive")
        self.end_headers()
        if closing:
            self.close_connection = True
        if self.command == "HEAD" or code == 204:
            return
        try:
            self.wfile.write(body)
        except OSError:
            self.close_connection = True


class Upstream(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _can_bind(addr: str) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((addr, PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def bind_addresses() -> list:
    """The node address plus loopback; a wildcard only if the node has none yet.

    The gateway reaches this process over the campaign network and the node's
    own nginx reaches it over loopback, so both are served.  The node address
    appears a moment after boot, hence the bounded wait: binding loopback alone
    would silently cut the gateway off for the lifetime of the process.
    """
    addrs = []
    waited = 0.0
    while True:
        addrs = [addr for addr in BIND_ADDRS if _can_bind(addr)]
        if len(addrs) == len(BIND_ADDRS) or waited >= BIND_WAIT_S:
            break
        threading.Event().wait(1.0)
        waited += 1.0
    return addrs or ["0.0.0.0"]


def main(argv=None) -> int:
    _scrub_process()
    addrs = bind_addresses()
    servers = []
    for addr in addrs:
        server = Upstream((addr, PORT), Handler)
        servers.append(server)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            try:
                server.server_close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
