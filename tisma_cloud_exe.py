"""Entry point of the tisma-cloud executable (tisma-cloud.spec)."""

import multiprocessing

from tisma_cloud.cli import main

if __name__ == "__main__":
    # The processes that read large G-code (threats.py) start this same executable.
    multiprocessing.freeze_support()
    raise SystemExit(main())
