# ---------------------------------------------------------------------------
# Enables `python -m ctmdeploy ...` (the invocation the pipelines use).
# Delegates straight to ctmdeploy.cli:main.
# ---------------------------------------------------------------------------
"""Module runner: ``python -m ctmdeploy deploy --env prod --phase all``."""

from ctmdeploy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
