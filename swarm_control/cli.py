"""Console entry point; every target operation requires its project config."""

import sys


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
            from .dashboard import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "ops":
            from .operations import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "preflight":
            from .preflight import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "evidence":
            from .evidence import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "budget":
            from .budget_policy import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "deploy-gateway":
            from .deployment import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "integrate":
            from .pilot_integrate import main as run
            del sys.argv[1]
        elif len(sys.argv) > 1 and sys.argv[1] == "probe":
            from .probe_gateway import main as run
            del sys.argv[1]
        else:
            from .modal_runner import main as run
        return run()
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
