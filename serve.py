"""Start the web app on http://127.0.0.1:8000 (or 8001 if 8000 is busy).

Equivalent to:  uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
plus the automatic port fallback. Used by run.bat / run.sh.

Usage:
    python serve.py              # with auto-reload (restarts when you edit code)
    python serve.py --no-reload
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

import uvicorn

HOST = "127.0.0.1"
PORTS = (8000, 8001)


def port_is_free(host: str, port: int) -> bool:
    """True if nothing is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Resume Parser web app.")
    parser.add_argument("--no-reload", action="store_true", help="disable auto-reload on code changes")
    args = parser.parse_args()

    port = next((p for p in PORTS if port_is_free(HOST, p)), None)
    if port is None:
        print(f"Ports {' and '.join(map(str, PORTS))} are both in use. Close the other program "
              f"(see README 'Common errors') and try again.", file=sys.stderr)
        return 1
    if port != PORTS[0]:
        print(f"Port {PORTS[0]} is busy - using {port} instead.")

    # The app prints this URL in its startup banner (the reload worker inherits the variable).
    os.environ["RESUME_PARSER_URL"] = f"http://{HOST}:{port}"
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=port,
        reload=not args.no_reload,
        reload_dirs=["app"] if not args.no_reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
