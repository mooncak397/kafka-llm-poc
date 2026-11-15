#!/usr/bin/env bash
# L2 to L6 (plan section 9.1). Exit non-zero on any hit. Never prints denylist entries or matched text.
set -u
cd "$(dirname "$0")/.."
DENY="$HOME/.config/kafka-poc-denylist.txt"
fail=0

gitleaks dir . --config .gitleaks.toml --no-banner --redact || fail=1
# "gitleaks git" needs at least one commit
if git rev-parse --verify -q HEAD >/dev/null; then
  gitleaks git . --config .gitleaks.toml --no-banner --redact || fail=1
fi

if [ ! -f "$DENY" ]; then echo "denylist missing"; exit 1; fi
echo "denylist entries: $(rg -c '' "$DENY")"  # count only, never the entries
# -l: list file names only, so a hit never echoes the matched text. Denylist is outside the repo.
if rg -l -i --hidden -g '!.git' -f "$DENY" . ; then echo "DENYLIST HIT in files above"; fail=1; fi
# history: patches and commit metadata
if git rev-parse --verify -q HEAD >/dev/null; then
  if git log -p --all --format='%an %ae %cn %ce %B' | rg -q -i -f "$DENY"; then
    echo "DENYLIST HIT in git history"; fail=1
  fi
fi
# --- L4/L5 structure checks (file names only; no file content is printed)
# 1. forbidden paths on disk (also catches ignored files, because a fresh clone must not need them)
bad=$(find . \( -name .git -o -name .venv -o -name node_modules \) -prune -o \
  \( -name docs-cache -o -name '*confluent-docs*' -o -name .env -o -name '.env.*' -o -name 'kubeconfig*' -o -name '*.kubeconfig' \
     -o -name '*.pem' -o -name '*.key' -o -name '*.db' -o -name '*.sqlite' \) -print)
[ -n "$bad" ] && { echo "FORBIDDEN PATH:"; echo "$bad"; fail=1; }
# 2. raw evidence must never be tracked
git ls-files | rg -q '^evidence/raw/' && { echo "TRACKED raw evidence"; fail=1; }
# 3. size limits on tracked files: 500 KB (PNG in evidence/: 1.5 MB); 200 KB outside evidence/ except lock files
sizes=$(mktemp)
git ls-files -z | while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  sz=$(stat -c %s "$f")
  case "$f" in
    evidence/*.png) [ "$sz" -gt 1572864 ] && echo "TOO BIG (>1.5MB): $f" ;;
    evidence/*) [ "$sz" -gt 512000 ] && echo "TOO BIG (>500KB): $f" ;;
    *uv.lock|*package-lock.json) [ "$sz" -gt 512000 ] && echo "TOO BIG (>500KB): $f" ;;
    *) [ "$sz" -gt 204800 ] && echo "TOO BIG (>200KB): $f" ;;
  esac
done > "$sizes"
[ -s "$sizes" ] && { cat "$sizes"; fail=1; }
rm -f "$sizes"
# 4. Secret YAML with a value
for f in $(git ls-files '*.yaml' '*.yml'); do
  if rg -q '^kind: Secret' "$f" && rg -q '^(data|stringData):' "$f"; then echo "SECRET YAML WITH VALUES: $f"; fail=1; fi
done
# 5. identity: every commit, author and committer, uses the configured noreply address; no remote
me=$(git config user.email)
case "$me" in *@users.noreply.github.com) ;; *) echo "git user.email is not a noreply address"; fail=1 ;; esac
if git rev-parse --verify -q HEAD >/dev/null; then
  git log --all --format='%ae%n%ce' | grep -vxF "$me" | grep -q . && { echo "COMMIT EMAIL differs from configured noreply"; fail=1; }
fi
# LEAKSCAN_ALLOW_REMOTE=1 skips this rule (a fresh clone has origin; S51 adds the real remote). Env passes through make and the hook.
[ -n "$(git remote)" ] && [ "${LEAKSCAN_ALLOW_REMOTE:-}" != 1 ] && { echo "REMOTE configured (the Owner adds it at S51; a clone or S52 runs LEAKSCAN_ALLOW_REMOTE=1 make leakscan)"; fail=1; }

# --- L6 image check: only plain PNG image fields allowed; any other tag (EXIF, XMP, text chunks, ICC, time) fails
for f in $(git ls-files 'evidence/*.png' 'evidence/**/*.png' | sort -u); do
  extra=$(exiftool -s -G1 -a "$f" | rg -v '^\[(System|File|ExifTool|Composite)\]' \
    | rg -v '^\[PNG\]\s+(ImageWidth|ImageHeight|BitDepth|ColorType|Compression|Filter|Interlace|PixelsPerUnit[XY]|PixelUnits|Gamma|WhitePoint|Red[XY]|Green[XY]|Blue[XY]|SRGBRendering|Palette|Transparency|BackgroundColor|SignificantBits)\s')
  [ -n "$extra" ] && { echo "PNG METADATA in $f: $(echo "$extra" | rg -o '^\[[^]]+\]\s+\S+' | tr '\n' ' ')"; fail=1; }
  echo "png ok-check: $f $(stat -c %s "$f") bytes"
done

[ "$fail" = 0 ] && echo "leakscan OK"
exit "$fail"
