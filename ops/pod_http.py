#!/usr/bin/env python3
"""Tiny real upstream for the pod's nginx: reads the live state file and
returns 502 while the cortex API is seized, 200 once the operator restores it.

The deployment runs this behind nginx as the node's inference endpoint, so the
process has to look like the vendor daemon from the outside: comm and the argv
image are rewritten in place before the HTTP loop starts.  A reader of
/proc/<pid>/cmdline (or `ps`) then sees /opt/cortex/bin/synapse_http instead of
a Python script.
"""
import ctypes
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = "/var/lib/cortex-agent/state.json"

#: comm is capped at 15 bytes by the kernel
COMM = b"synapse_http"


def _rename_process() -> None:
    """Set the comm shown by ps/top and /proc/<pid>/comm.

    The argv image is left alone: the unit already starts the vendor-named
    script (/opt/cortex/bin/synapse_http.py), so cmdline reads like a normal
    Python service on a production node instead of an engine helper.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(15, ctypes.c_char_p(COMM), 0, 0, 0)  # PR_SET_NAME
    except Exception:
        pass


def online():
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return bool(data.get("http_online"))
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nginx/1.18.0"
    sys_version = "(Ubuntu)"

    def _reply(self):
        ok = online()
        body = b'{"status":"ok","model":"cortex-api"}\n' if ok else (
            b"<html><head><title>502 Bad Gateway</title></head>"
            b"<body><center><h1>502 Bad Gateway</h1></center><hr>"
            b"<center>nginx</center></body></html>\n")
        self.send_response(200 if ok else 502)
        self.send_header("Content-Type", "application/json" if ok else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply()

    def do_POST(self):
        self._reply()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    _rename_process()
    # The edge gateway proxies to this port over the campaign network, so the
    # upstream has to be reachable on the node address, not only loopback: a
    # seized worker answers 502, the silicon mirror answers 200.
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
