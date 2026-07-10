#!/usr/bin/env bash
# Experiment 02 — real end-to-end via the boxyard CLI (new code) against the
# actual hetzner-box (SFTP) remote. Proves +x survives a real push/pull round-trip
# through the production transport. Uses a clearly-named throwaway box and deletes
# it at the end.
set -uo pipefail
BOX="$(cd "$(dirname "$0")/../../.." && pwd)/.venv/bin/boxyard"
RCLONE_CONF="$HOME/.config/boxyard/boxyard_rclone.conf"
NAME="zzz-perms-e2e"
SRC="$(mktemp -d /tmp/e2e_src.XXXXXX)"
DST="$(mktemp -d /tmp/e2e_dst.XXXXXX)"

cleanup() {
    echo "--- cleanup ---"
    "$BOX" delete -n "$NAME" --force 2>&1 | tail -2 || true
    rm -rf "$SRC" "$DST"
}
trap cleanup EXIT

echo "### 1. create source content with an executable script"
printf '#!/bin/sh\necho hello\n' > "$SRC/run.sh"; chmod 755 "$SRC/run.sh"
printf 'just data\n' > "$SRC/notes.txt"; chmod 644 "$SRC/notes.txt"
ls -l "$SRC"

echo "### 2. boxyard new (copy source into a new box)"
"$BOX" new -n "$NAME" -f "$SRC" -c 2>&1 | tail -5

echo "### 3. resolve index_name + local path"
IDX="$("$BOX" list 2>/dev/null | grep -o "[0-9]\{8\}_[0-9]\{6\}_[a-z0-9]\{5\}__${NAME}" | head -1)"
echo "index_name = $IDX"
LOCALP="$("$BOX" path -n "$NAME" 2>/dev/null | tail -1)"
echo "local path = $LOCALP"

echo "### 4. push to remote"
"$BOX" sync -n "$NAME" -d push 2>&1 | tail -6

echo "### 5. inspect the manifest ON THE REMOTE (proves it shipped over SFTP)"
rclone --config "$RCLONE_CONF" cat "hetzner-box:boxyard/boxes/$IDX/data/.boxyard-perms.json" 2>&1 || echo "(manifest not found on remote!)"

echo "### 6. copy the box fresh from remote to a clean dir (exercises apply-on-pull)"
"$BOX" copy -n "$NAME" --dest "$DST/box" --overwrite 2>&1 | tail -4

echo "### 7. VERIFY exec bit on the freshly-pulled copy"
ls -l "$DST/box"
if [[ -x "$DST/box/run.sh" ]]; then echo "PASS: run.sh is executable after remote round-trip"; else echo "FAIL: run.sh lost +x"; fi
[[ -x "$DST/box/notes.txt" ]] && echo "FAIL: notes.txt unexpectedly executable" || echo "PASS: notes.txt correctly non-executable"

echo "### done (cleanup runs on exit)"
