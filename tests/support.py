import json
import subprocess
import sys

from swarm_control.project import load_project


def make_project(root, repository="example/project", *, check_marker="validated"):
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", f"https://github.com/{repository}.git"],
                   cwd=root, check=True)
    (root / "README.md").write_text("Fixture project\n")
    (root / "AGENTS.md").write_text("Keep changes inside task ownership.\n")
    tasks = root / "swarm/tasks"
    tasks.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/item.txt").write_text("baseline\n")
    (tasks / "A1.json").write_text(json.dumps({
        "id": "A1", "title": "Useful work", "prompt": "Implement acceptance.",
        "ownership": ["src"], "acceptance": ["Tests pass"], "issue": 1, "status": "ready",
    }))
    config = root / "swarm.toml"
    install = [[sys.executable, "-c", "print('installed')"]]
    checks = [[sys.executable, "-c", f"print({check_marker!r})"]]
    config.write_text(f'''version = 1
[project]
repository = {json.dumps(repository)}
[validation]
install = {json.dumps(install)}
checks = {json.dumps(checks)}
[budget]
live_enabled = false
model_limit_cents = 1800
infra_limit_cents = 700
''')
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return load_project(config)
