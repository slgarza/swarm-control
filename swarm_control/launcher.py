"""One-command local workspace startup, with an optional prepared repository."""

import argparse
import errno
import threading
import webbrowser
from pathlib import Path

from .dashboard import make_server
from .workspace import Workspace, default_workspace


def print_help():
    print("""Swarm Control — coding-agent runs from a local browser

Start here:
  swarm-control start                         Open your workspace in a browser
  swarm-control start --project /path/to/repo  Connect a prepared local checkout
  swarm-control start --help                   Startup options

Projects and runs:
  swarm-control workspace --help  Register repositories and preserve history
  swarm-control runs --help       Plan, save, inspect and launch runs
  swarm-control preflight --help  Inspect source and budget readiness
  swarm-control analyze --help    Detect dependencies and inspect a 100-agent frontier
  swarm-control budget --help     Inspect cumulative allowances and spend

Advanced commands (each accepts --help):
  plan, smoke, run, dashboard, ops, deploy-gateway, integrate, probe, evidence

First time from a GitHub checkout? Run ./easy-run.
Guide: docs/QUICKSTART.md (English), docs/GETTING_STARTED.es.md (Español)
""")
    return 0


def _open_browser(url):
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"Open this address in your browser: {url}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path,
                        help="Prepared local repository folder or its swarm.toml (optional)")
    parser.add_argument("--workspace", type=Path, default=default_workspace(),
                        help="Private workspace registry; reuse the same file on later starts")
    parser.add_argument("--port", type=int, default=8787,
                        help="Local port (default: 8787; 0 chooses a free port)")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    workspace = Workspace(args.workspace)
    if not workspace.path.exists():
        try:
            workspace.initialize()
        except ValueError:
            # A simultaneous startup can have initialized the same workspace.
            if not workspace.path.exists():
                raise
            workspace.public_catalog()
    else:
        workspace.public_catalog()
    project_id = None
    if args.project is not None:
        project_id = workspace.connect_project(str(args.project))["project_id"]

    try:
        server = make_server(workspace_path=workspace.path, port=args.port)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            parser.exit(1, f"Port {args.port} is already in use. Stop the previous dashboard with Ctrl+C, "
                        "or choose another port: swarm-control start --port 8788\n")
        parser.exit(1, "Could not start the local dashboard. Check the selected port and try again.\n")
    url = f"http://127.0.0.1:{server.server_port}"
    if project_id:
        url += f"/?project={project_id}"
    print(f"\nSwarm Control is ready: {url}\n"
          "1. Connect a local repository with a swarm.toml configuration.\n"
          "2. Select its tasks, check the plan and save a draft.\n"
          "3. Review the setup and limits before launching a paid run.\n"
          "Keep this terminal open. Press Ctrl+C to stop the dashboard.\n", flush=True)
    if not args.no_open:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosing the local dashboard.", flush=True)
    finally:
        server.server_close()
    return 0
