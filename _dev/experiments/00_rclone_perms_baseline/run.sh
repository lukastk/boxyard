#!/usr/bin/env bash
# Experiment 00 — does rclone (as boxyard invokes it) preserve the exec bit?
# Self-contained: stands up a local `rclone serve sftp` server, so nothing
# touches the real hetzner-box. Prints a truth table of exec-bit outcomes.
set -euo pipefail

RCLONE="$(command -v rclone)"
echo "rclone: $RCLONE ($($RCLONE version | head -1))"

WORK="$(mktemp -d /tmp/exp00.XXXXXX)"
SFTP_ROOT="$WORK/sftp_root"; mkdir -p "$SFTP_ROOT"
CONF="$WORK/rclone.conf"
PORT=2222
USER=t; PASS=t
OBSCURED="$("$RCLONE" obscure "$PASS")"

cat > "$CONF" <<EOF
[sftp]
type = sftp
host = localhost
port = $PORT
user = $USER
pass = $OBSCURED
shell_type = unix
EOF

# Start a local SFTP server backed by $SFTP_ROOT
"$RCLONE" serve sftp --addr "localhost:$PORT" --user "$USER" --pass "$PASS" \
    --config "$CONF" "$SFTP_ROOT" >"$WORK/serve.log" 2>&1 &
SERVE_PID=$!
trap 'kill $SERVE_PID 2>/dev/null || true; rm -rf "$WORK"' EXIT
sleep 2  # let the server bind

# boxyard's exact base flags (minus command): --config --links + --fast-list
FLAGS=(--config "$CONF" --links --fast-list)

mode() { stat -c '%a' "$1"; }        # octal perms
isx()  { [[ -x "$1" ]] && echo x || echo -; }   # exec-for-owner shorthand

mk_src() {
    local d="$1"; rm -rf "$d"; mkdir -p "$d"
    printf '#!/bin/sh\necho hi\n' > "$d/script.sh"; chmod 755 "$d/script.sh"
    printf 'plain\n' > "$d/data.txt";              chmod 644 "$d/data.txt"
}

line() { printf '%-42s src=%s  dst=%s\n' "$1" "$2" "$3"; }

echo; echo "================ CASE 1: local -> local ================"
for META in "" "--metadata"; do
    SRC="$WORK/c1_src_${META:-none}"; DST="$WORK/c1_dst_${META:-none}"
    mk_src "$SRC"; rm -rf "$DST"
    "$RCLONE" copy "${FLAGS[@]}" ${META:+$META} "$SRC" "$DST"
    line "meta='${META:-none}' script.sh" "$(isx "$SRC/script.sh")" "$(isx "$DST/script.sh")"
done

echo; echo "================ CASE 2: local -> SFTP -> local ================"
for META in "" "--metadata"; do
    SRC="$WORK/c2_src_${META:-none}"; DST="$WORK/c2_dst_${META:-none}"
    REMOTE_SUB="c2_${META:-none}"
    mk_src "$SRC"; rm -rf "$DST"
    "$RCLONE" copy "${FLAGS[@]}" ${META:+$META} "$SRC" "sftp:$REMOTE_SUB"
    echo "  on-sftp script.sh mode: $(mode "$SFTP_ROOT/$REMOTE_SUB/script.sh")"
    "$RCLONE" copy "${FLAGS[@]}" ${META:+$META} "sftp:$REMOTE_SUB" "$DST"
    line "meta='${META:-none}' script.sh roundtrip" "$(isx "$SRC/script.sh")" "$(isx "$DST/script.sh")"
done

echo; echo "================ CASE 3: pure chmod +x, no content change (SFTP) ================"
# Does --metadata propagate a chmod when file bytes are unchanged?
SRC="$WORK/c3_src"; DST="$WORK/c3_dst"; REMOTE_SUB="c3"
rm -rf "$SRC" "$DST"; mkdir -p "$SRC"
printf 'plain\n' > "$SRC/tool"; chmod 644 "$SRC/tool"
"$RCLONE" copy "${FLAGS[@]}" --metadata "$SRC" "sftp:$REMOTE_SUB"
"$RCLONE" copy "${FLAGS[@]}" --metadata "sftp:$REMOTE_SUB" "$DST"
echo "  after first sync: dst tool exec = $(isx "$DST/tool")"
# now flip the bit WITHOUT changing bytes, and re-sync
chmod +x "$SRC/tool"
"$RCLONE" sync "${FLAGS[@]}" --metadata "$SRC" "sftp:$REMOTE_SUB" -v 2>&1 | grep -iE 'transferr|unchanged|copied|checks' || true
"$RCLONE" sync "${FLAGS[@]}" --metadata "sftp:$REMOTE_SUB" "$DST" -v 2>&1 | grep -iE 'transferr|unchanged|copied|checks' || true
echo "  src tool exec after chmod = $(isx "$SRC/tool"); dst tool exec after resync = $(isx "$DST/tool")"

echo; echo "DONE."
