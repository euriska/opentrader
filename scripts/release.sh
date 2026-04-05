#!/usr/bin/env bash
# OpenTrader release script
# Usage:
#   ./scripts/release.sh patch      # 3.5.1 → 3.5.2
#   ./scripts/release.sh minor      # 3.5.1 → 3.6.0
#   ./scripts/release.sh major      # 3.5.1 → 4.0.0
#   ./scripts/release.sh 3.7.0      # explicit version

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$REPO_ROOT/VERSION"
CHANGELOG="$REPO_ROOT/CHANGELOG.md"

current=$(cat "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r major minor patch <<< "$current"

bump="${1:-patch}"

case "$bump" in
  major)
    major=$((major + 1)); minor=0; patch=0 ;;
  minor)
    minor=$((minor + 1)); patch=0 ;;
  patch)
    patch=$((patch + 1)) ;;
  [0-9]*.[0-9]*.[0-9]*)
    IFS='.' read -r major minor patch <<< "$bump" ;;
  *)
    echo "Usage: $0 [major|minor|patch|X.Y.Z]" >&2; exit 1 ;;
esac

new_version="${major}.${minor}.${patch}"
tag="v${new_version}"
today=$(date +%Y-%m-%d)

echo "Releasing: $current → $new_version ($tag)"

# Confirm
read -rp "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# Check for clean working tree
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo ""
  echo "Warning: uncommitted changes present."
  read -rp "Commit everything now with 'chore: release $tag'? [y/N] " do_commit
  if [[ "$do_commit" =~ ^[Yy]$ ]]; then
    git -C "$REPO_ROOT" add -A
    git -C "$REPO_ROOT" commit -m "chore: release $tag"
  else
    echo "Please commit or stash changes before releasing." >&2
    exit 1
  fi
fi

# Bump VERSION file
echo "$new_version" > "$VERSION_FILE"

# Prepend new section to CHANGELOG
tmpfile=$(mktemp)
cat > "$tmpfile" <<EOF
## [$new_version] - $today

### Added
-

### Fixed
-

### Changed
-

---

EOF

# Insert after the header block (first blank line after the intro paragraph)
awk -v new="$(cat "$tmpfile")" '
  /^---$/ && !inserted { print new; inserted=1 }
  { print }
' "$CHANGELOG" > "${CHANGELOG}.tmp" && mv "${CHANGELOG}.tmp" "$CHANGELOG"
rm "$tmpfile"

# Open editor for changelog entry
EDITOR="${EDITOR:-vi}"
echo ""
echo "Opening CHANGELOG.md for release notes..."
"$EDITOR" "$CHANGELOG"

# Commit version bump + changelog
git -C "$REPO_ROOT" add "$VERSION_FILE" "$CHANGELOG"
git -C "$REPO_ROOT" commit -m "chore: release $tag"

# Tag
git -C "$REPO_ROOT" tag -a "$tag" -m "Release $tag"

echo ""
echo "Tagged $tag locally. Push with:"
echo "  git push origin main && git push origin $tag"
echo ""
read -rp "Push now? [y/N] " do_push
if [[ "$do_push" =~ ^[Yy]$ ]]; then
  git -C "$REPO_ROOT" push origin main
  git -C "$REPO_ROOT" push origin "$tag"
  echo "Pushed. GitHub Actions will create the release."
fi
