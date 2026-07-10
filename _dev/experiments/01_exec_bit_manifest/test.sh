#!/usr/bin/env bash
# Experiment 01 — drive the B1 manifest through a real SFTP round-trip.
# Proves: exec bit survives sync; the pure-chmod case (which --metadata cannot
# handle) works; clearing +x propagates; symlinks don't break it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RCLONE="$(command -v rclone)"
PY="python3"

WORK="$(mktemp -d /tmp/exp01.XXXXXX)"
SFTP_ROOT="$WORK/sftp_root"; mkdir -p "$SFTP_ROOT"
CONF="$WORK/rclone.conf"; PORT=2223; U=t; P=t
cat > "$CONF" <<EOF
[sftp]
type = sftp
host = localhost
port = $PORT
user = $U
pass = $("$RCLONE" obscure "$P")
shell_type = unix
EOF
"$RCLONE" serve sftp --addr "localhost:$PORT" --user "$U" --pass "$P" --config "$CONF" "$SFTP_ROOT" >"$WORK/serve.log" 2>&1 &
trap 'kill %1 2>/dev/null || true; rm -rf "$WORK"' EXIT
sleep 2

FLAGS=(--config "$CONF" --links --fast-list)   # boxyard's exact base flags (no --metadata)
push() { "$RCLONE" sync "${FLAGS[@]}" "$1" "sftp:box" >/dev/null 2>&1; }
pull() { "$RCLONE" sync "${FLAGS[@]}" "sftp:box" "$1" >/dev/null 2>&1; }
xbit() { [[ -x "$1" ]] && echo x || echo -; }
PASS=0; FAIL=0
chk() { # chk <label> <expected x|-> <path>
    local got; got="$(xbit "$3")"
    if [[ "$got" == "$2" ]]; then echo "  PASS $1 ($3 = $got)"; PASS=$((PASS+1))
    else echo "  FAIL $1 ($3 = $got, wanted $2)"; FAIL=$((FAIL+1)); fi
}

A="$WORK/boxA"; B="$WORK/boxB"
rm -rf "$A" "$B"; mkdir -p "$A/sub"
printf '#!/bin/sh\necho hi\n' > "$A/script.sh"; chmod 755 "$A/script.sh"
printf 'plain\n'             > "$A/data.txt";  chmod 644 "$A/data.txt"
printf '#!/usr/bin/env python\n' > "$A/sub/tool"; chmod 755 "$A/sub/tool"
ln -s script.sh "$A/link.sh"   # symlink -> exercised via --links

echo "=== T1: baseline round-trip WITHOUT manifest (should lose +x) ==="
push "$A"; pull "$B"
chk "no-manifest script.sh loses x" "-" "$B/script.sh"
chk "no-manifest sub/tool loses x"  "-" "$B/sub/tool"

echo "=== T2: WITH manifest — generate before push, apply after pull ==="
$PY "$HERE/perms_manifest.py" generate "$A" >/dev/null
push "$A"; rm -rf "$B"; pull "$B"
chk "pre-apply script.sh still lost" "-" "$B/script.sh"
$PY "$HERE/perms_manifest.py" apply "$B" >/dev/null
chk "restored script.sh"  "x" "$B/script.sh"
chk "restored sub/tool"   "x" "$B/sub/tool"
chk "data.txt stays non-exec" "-" "$B/data.txt"
[[ -L "$B/link.sh" ]] && { echo "  PASS symlink survived as symlink"; PASS=$((PASS+1)); } || { echo "  FAIL symlink"; FAIL=$((FAIL+1)); }

echo "=== T3: THE CRUX — pure 'chmod +x data.txt', no content change ==="
chmod +x "$A/data.txt"
before_sum="$(md5sum "$A/data.txt" | cut -d' ' -f1)"
$PY "$HERE/perms_manifest.py" generate "$A" >/dev/null
# Show rclone does NOT re-transfer data.txt but DOES re-sync the manifest:
echo "  rclone push verbose (grep):"
"$RCLONE" sync "${FLAGS[@]}" "$A" "sftp:box" -v 2>&1 | grep -iE 'data.txt|boxyard-perms' | sed 's/^/    /' || true
pull "$B"
$PY "$HERE/perms_manifest.py" apply "$B" >/dev/null
chk "pure-chmod +x propagated to B" "x" "$B/data.txt"
after_sum="$(md5sum "$B/data.txt" | cut -d' ' -f1)"
[[ "$before_sum" == "$after_sum" ]] && { echo "  PASS content unchanged (md5 match)"; PASS=$((PASS+1)); } || { echo "  FAIL content changed"; FAIL=$((FAIL+1)); }

echo "=== T4: clearing the bit ('chmod -x script.sh') propagates ==="
chmod -x "$A/script.sh"
$PY "$HERE/perms_manifest.py" generate "$A" >/dev/null
push "$A"; pull "$B"; $PY "$HERE/perms_manifest.py" apply "$B" >/dev/null
chk "cleared script.sh now non-exec on B" "-" "$B/script.sh"

echo "=== T5: apply is idempotent ==="
out="$($PY "$HERE/perms_manifest.py" apply "$B")"
[[ -z "$out" ]] && { echo "  PASS second apply is a no-op"; PASS=$((PASS+1)); } || { echo "  FAIL idempotency: $out"; FAIL=$((FAIL+1)); }

echo; echo "RESULT: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
