#!/usr/bin/env python3
"""Entry point wrapper for Claude bot."""

from shared.lockfile import acquire_or_exit
acquire_or_exit("claudebot")

from claude.bot import main

if __name__ == "__main__":
    main()
