"""`python -m gtd` 入口（一期不建软链，先用这个跑通）。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
