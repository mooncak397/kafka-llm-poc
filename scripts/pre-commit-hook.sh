#!/usr/bin/env bash
# Tracked source of .git/hooks/pre-commit. Install with: make hooks
# Scans the working tree and all history; staged files are part of the working tree.
cd "$(git rev-parse --show-toplevel)" && exec make leakscan
