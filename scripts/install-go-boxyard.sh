#!/bin/sh
# Install the boxyard Go binary from a `go/vX.Y.Z` GitHub release.
#
# This is the MECHANISM for the cutover, not the cutover itself. myrig installs
# boxyard today with
#
#     uv tool install --upgrade git+https://github.com/lukastk/boxyard.git
#
# which puts the PYTHON boxyard at ~/.local/bin/boxyard. Both implementations
# provide a command by that name, so switching a machine over is a one-line
# change in myrig's setup/installs/all/python_tools.py pointing at this script
# instead — deliberately left for Lukas to make, per machine, when he is ready.
#
# Usage:
#   scripts/install-go-boxyard.sh              # latest go/v* release
#   scripts/install-go-boxyard.sh go/v0.6.0    # a specific one
#
# It REFUSES to replace an existing boxyard unless BOXYARD_REPLACE=1 is set.
# Silently shadowing the tool the supervisor runs every 20 minutes is not a
# thing an install script should do on its own.
set -eu

REPO=lukastk/boxyard
TAG=${1:-}
DEST=${BOXYARD_BIN_DIR:-$HOME/.local/bin}

command -v gh >/dev/null 2>&1 || { echo "gh is required (it authenticates the release download)" >&2; exit 1; }

if [ -z "$TAG" ]; then
  # Newest tag in the go/ namespace. A bare vX.Y.Z is the PYTHON release and
  # has no binary attached, so filtering here is load-bearing, not tidiness.
  TAG=$(gh release list --repo "$REPO" --limit 100 --json tagName --jq '[.[].tagName | select(startswith("go/v"))] | .[0]')
  [ -n "$TAG" ] && [ "$TAG" != "null" ] || { echo "no go/v* release found" >&2; exit 1; }
fi

case $(uname -s) in
  Linux)  OS=linux ;;
  Darwin) OS=darwin ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case $(uname -m) in
  x86_64|amd64) ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

VERSION=${TAG#go/v}
ASSET="boxyard_${VERSION}_${OS}_${ARCH}.tar.gz"

if [ -e "$DEST/boxyard" ] && [ "${BOXYARD_REPLACE:-}" != "1" ]; then
  echo "$DEST/boxyard already exists (currently: $("$DEST/boxyard" --version 2>/dev/null || echo unknown))." >&2
  echo "Re-run with BOXYARD_REPLACE=1 to replace it." >&2
  exit 1
fi

WORK=$(mktemp -d)
# Cleaned up on every exit path, including the failure ones.
trap 'rm -rf "$WORK"' EXIT INT TERM

echo "Downloading $ASSET from $TAG"
gh release download "$TAG" --repo "$REPO" --pattern "$ASSET" --pattern checksums.txt --dir "$WORK"

# Verify before installing. A truncated download would otherwise be discovered
# by the supervisor, mid-pass, on every machine at once.
( cd "$WORK" && grep " $ASSET\$" checksums.txt | sha256sum -c - ) \
  || { echo "checksum mismatch for $ASSET" >&2; exit 1; }

tar -xzf "$WORK/$ASSET" -C "$WORK"
mkdir -p "$DEST"
install -m 0755 "$WORK/boxyard" "$DEST/boxyard"

got=$("$DEST/boxyard" --version)
echo "installed boxyard $got to $DEST/boxyard"
# `boxyard --version` is the rollout gate: `ssh-target <machine> boxyard
# --version` is how a fleet-wide change is checked. A binary that reports "dev"
# would pass that check while meaning nothing.
[ "$got" = "$VERSION" ] || { echo "expected $VERSION, got $got" >&2; exit 1; }
