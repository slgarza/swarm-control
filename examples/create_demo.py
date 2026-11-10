"""Create a separate local teaching repository; never access GitHub or Modal."""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).with_name("demo-target")


def create_demo(destination, repository="YOUR_GITHUB_USER/swarm-demo"):
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("destination already exists; choose a new directory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ValueError("repository must be owner/name, without a URL or credentials")
    shutil.copytree(TEMPLATE, destination, ignore=shutil.ignore_patterns(
        ".git", ".swarm", ".venv", "__pycache__", "*.pyc", "*.log"
    ))
    config = destination / "swarm.toml"
    config.write_text(config.read_text().replace('"YOUR_GITHUB_USER/swarm-demo"', json.dumps(repository)))

    def git(*args):
        subprocess.run(["git", *args], cwd=destination, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Swarm Demo")
    git("config", "user.email", "demo@example.invalid")
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-m", "Create tutorial baseline and two tasks")
    git("remote", "add", "origin", f"https://github.com/{repository}.git")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--repository", default="YOUR_GITHUB_USER/swarm-demo")
    args = parser.parse_args()
    try:
        destination = create_demo(args.destination, args.repository)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Created local demo: {destination}")
    print("No remote repository was created. No agents or paid services were started.")
    print("Tasks D01 and D02 are ready for local planning; live execution is disabled.")


if __name__ == "__main__":
    main()
