#!/usr/bin/env bash
# `make leakscan-test`: proves rule 5 skips only the allowed hash. (a) this repo passes, the accepted merge commit included;
# (b) commits with a non-noreply author or committer in a throwaway repo still fail.
set -eu
D=$(cd "$(dirname "$0")" && pwd)
"$D/check-commit-emails.sh" "$D/.." >/dev/null && echo "ok: this repo passes rule 5 (accepted merge commit 9d07d87 included)"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
git -C "$T" init -q
git -C "$T" config user.name tester
git -C "$T" config user.email tester@users.noreply.github.com
git -C "$T" commit -q --allow-empty -m good
"$D/check-commit-emails.sh" "$T" >/dev/null && echo "ok: a noreply-only throwaway repo passes"
GIT_AUTHOR_EMAIL=someone@example.com git -C "$T" commit -q --allow-empty -m bad-author
if "$D/check-commit-emails.sh" "$T" >/dev/null; then echo "FAIL: non-noreply author passed"; exit 1; fi
echo "ok: a commit with a non-noreply author fails"
git -C "$T" reset -q --hard HEAD~1
GIT_COMMITTER_EMAIL=someone@example.com git -C "$T" commit -q --allow-empty -m bad-committer
if "$D/check-commit-emails.sh" "$T" >/dev/null; then echo "FAIL: non-noreply committer passed"; exit 1; fi
echo "ok: a commit with a non-noreply committer fails"
