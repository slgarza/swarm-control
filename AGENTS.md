# Swarm Control agent contract

Read README.md and the relevant docs before changing code.
Keep target repository settings outside the controller implementation.
Never commit credentials, raw worker logs, local runtime state or customer code.
Preserve durable budget accounting; extraction is not authorization for more spend.
Keep remote execution disabled in migration examples; paid runs need an authorized budget.
Run unit tests, Ruff and Python compilation before publishing changes.
Keep worker execution separate from credentialed GitHub review/integration.
