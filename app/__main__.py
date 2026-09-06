"""Console entry point: ``python -m app``.

Loads and validates configuration (a validation failure exits with a readable
message via :class:`SystemExit` from ``load_config``), then runs the
composition root. Unexpected fatal exceptions are logged with full traceback
and exit non-zero; expected startup failures (bad token, no network) are
already converted to readable log lines + ``SystemExit`` by ``app.main.run``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import load_config
from app.main import run

logger = logging.getLogger("app.main")


def main() -> None:
    config = load_config()
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        # Signal before polling installed its handlers; run()'s finally has
        # already released everything asyncio.run unwound through.
        logger.warning("interrupted")
    except SystemExit:
        raise
    except Exception:
        logger.critical("Fatal error, exiting", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
