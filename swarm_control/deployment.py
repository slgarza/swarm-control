"""Deploy only the trusted gateway; this does not launch coding workers."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .project import load_project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    project = load_project(args.project)
    project.require_live()
    project.verify_origin()
    env = dict(os.environ, SWARM_GATEWAY_APP=project.gateway_app,
               SWARM_GATEWAY_LEDGER=project.gateway_ledger,
               SWARM_GATEWAY_CONCURRENCY=str(project.gateway_concurrency),
               SWARM_OPENAI_SECRET=project.openai_secret)
    subprocess.run([sys.executable, "-m", "modal", "deploy", "--env", project.environment,
                    "-m", "swarm_control.pilot_gateway"], env=env, check=True)
    return 0
