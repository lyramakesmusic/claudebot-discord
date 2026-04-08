#!/usr/bin/env python3
"""Entry point wrapper for Codex bot."""

from shared.lockfile import acquire_or_exit
acquire_or_exit("codexbot")

from codex.bot import main

if __name__ == "__main__":
    main()
