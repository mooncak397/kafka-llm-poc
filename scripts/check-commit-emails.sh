#!/usr/bin/env bash
# Leak scan rule 5 (commit part): every commit, author and committer, uses the configured noreply address.
# Usage: check-commit-emails.sh [repo dir]. Exit 1 if a commit outside ALLOWED differs. Prints short hashes, never an address.
set -u
cd "${1:-.}" || exit 1
# Accepted exceptions, FULL hashes only, each with its reason. Nothing else is skipped.
# 9d07d87...: merge of PR #1, made on GitHub on 2026-11-19 with the account's primary email. Accepted by the owner;
# history is not rewritten (DECISIONS.md, entry 14).
ALLOWED="9d07d87016882fee2693f401725a4b0a6859cd82"
git rev-parse --verify -q HEAD >/dev/null || exit 0
me=$(git config user.email)
bad=$(git log --all --format='%H %ae %ce' | awk -v me="$me" -v ok=" $ALLOWED " 'index(ok, " " $1 " ") == 0 && ($2 != me || $3 != me) {print substr($1, 1, 7)}')
[ -z "$bad" ] || { echo "COMMIT EMAIL differs from configured noreply in: $(echo $bad)"; exit 1; }
