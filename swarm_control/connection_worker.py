"""Isolated, silent Modal account operations; private values arrive through stdin."""

import contextlib
import json
import os
import sys

MAX_INPUT = 16384


def perform(request):
    """Use only APIs present in the pinned Modal 1.6.3 SDK."""
    from modal_version import __version__

    if __version__ != "1.6.3":
        return {"ok": False, "error_code": "sdk_version_mismatch"}

    import modal
    from modal.exception import AlreadyExistsError, AuthError, NotFoundError
    from modal.workspace import Workspace

    operation = request["operation"]
    writing = operation == "openai_configure"
    mutation_started = False
    try:
        workspace = Workspace.from_context().hydrate().name
        if operation == "modal_verify":
            return {"ok": True, "identity": workspace}
        if operation not in {"openai_configure", "openai_verify"}:
            return {"ok": False, "error_code": "invalid_operation"}
        if not workspace or workspace != request["expected_workspace"]:
            return {"ok": False, "error_code": "workspace_changed"}
        name, environment = request["secret_name"], request["environment"]
        if writing:
            values = {"CODEX_API_KEY": request.pop("api_key")}
            if request["replace"]:
                # Update preserves unrelated keys. A missing target is never created
                # implicitly by an operation labelled as replacement.
                secret = modal.Secret.from_name(name, environment_name=environment).hydrate()
                mutation_started = True
                secret.update(values)
            else:
                mutation_started = True
                modal.Secret.objects.create(name, values, environment_name=environment)
            values.clear()
        modal.Secret.from_name(name, environment_name=environment,
                               required_keys=["CODEX_API_KEY"]).hydrate()
        return {"ok": True, "identity": workspace, "secret_present": True}
    except AlreadyExistsError:
        return {"ok": False, "error_code": "secret_exists", "uncertain": False}
    except NotFoundError:
        return {"ok": False, "error_code": "secret_missing", "uncertain": mutation_started}
    except AuthError:
        return {"ok": False, "error_code": "modal_auth_failed", "uncertain": mutation_started}
    except Exception:  # noqa: BLE001 - SDK exception text can contain private values.
        return {"ok": False, "error_code": "provider_unavailable", "uncertain": mutation_started}


def main():
    # Never allow SDK logs or exception strings to cross the subprocess protocol.
    # This also discards writes made directly to stdout's OS file descriptor.
    output = os.dup(sys.stdout.fileno())
    result = {"ok": False, "error_code": "invalid_provider_response"}
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                raw = sys.stdin.buffer.read(MAX_INPUT + 1)
                if len(raw) <= MAX_INPUT:
                    request = json.loads(raw)
                    result = perform(request)
    except Exception:  # noqa: BLE001 - Never print private request/SDK exception text.
        result = {"ok": False, "error_code": "invalid_provider_response"}
    with os.fdopen(output, "w") as stream:
        stream.write(json.dumps(result, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
