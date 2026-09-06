#!/usr/bin/env python3
"""Entry point wrapper for OpenRouter bot."""

from shared.lockfile import acquire_or_exit
acquire_or_exit("openrouterbot")

from openrouter.bot import main

if __name__ == "__main__":
    main()
