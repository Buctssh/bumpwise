"""python -m bumpwise 的入口. 命令行的实现在 cli/ 里"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
