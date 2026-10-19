"""`python -m symbion`, as the `symbion` script runs it. Guarded: uvicorn's
`--reload` worker imports the parent's __main__ again."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
