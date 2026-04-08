#!/usr/bin/env python3
"""Entry point wrapper for Kimi bot."""

from shared.lockfile import acquire_or_exit
acquire_or_exit("kimibot")

from kimi.bot import main

if __name__ == "__main__":
    main()
