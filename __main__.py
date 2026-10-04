"""`python -m standup` 入口。"""
import os
import sys

if __package__:
    from .standup import main
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from standup import main

if __name__ == "__main__":
    sys.exit(main())
