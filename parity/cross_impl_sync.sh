#!/bin/sh
# Cross-implementation sync, end to end, in two throwaway yards that share one
# rclone alias remote:
#
#   Go creates a box, writes files, pushes  ->  Python includes it and pulls
#   Python writes a file, pushes            ->  Go pulls
#   both report the same box-status bytes
#
# This is the test that would have caught the registry-JSON gap, the exec-bit
# manifest not round-tripping, and any divergence in the sync-record format —
# none of which a unit test on either side can see.
#
# SAFETY: everything lives under $ROOT, and both implementations are pointed at
# a config inside it (--config / BOXYARD_CONFIG_PATH). Nothing reads or writes
# the real yard.
set -e
ROOT="$1"; GOBIN="$2"; PYBIN="$3"
case "$ROOT" in
  /tmp/*|/var/folders/*|"$TMPDIR"*) ;;
  *) echo "refusing to run outside a temp directory: $ROOT" >&2; exit 1 ;;
esac
rm -rf "$ROOT"; mkdir -p "$ROOT/store"

mkyard () {
  d="$ROOT/$1"
  mkdir -p "$d/config" "$d/data" "$d/boxes" "$d/groups"
  cat > "$d/config/config.toml" <<EOF
default_storage_location = "remote"
boxyard_data_path = "$d/data"
box_timestamp_format = "date_only"
user_boxes_path = "$d/boxes"
user_box_groups_path = "$d/groups"
default_box_groups = []
box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
box_subid_length = 6
max_concurrent_rclone_ops = 2
machine_name = "$1"

[storage_locations.remote]
storage_type = "rclone"
store_path = "boxyard"

[box_groups]

[virtual_box_groups]
EOF
  cat > "$d/config/boxyard_rclone.conf" <<EOF
[remote]
type = alias
remote = $ROOT/store
EOF
}
mkyard A
mkyard B

# `init` is what creates the default exclude file and the local_store links.
# Without it every DATA sync dies inside rclone with `failed to reload "filter"
# options` -- which is the same on both implementations, and is why `doctor`
# has a `default exclude file does not exist` check.
"$GOBIN" --config "$ROOT/A/config/config.toml" init >/dev/null 2>&1 || true
"$GOBIN" --config "$ROOT/B/config/config.toml" init >/dev/null 2>&1 || true

export DEFAULT_BOX_GROUPS=
A="$ROOT/A/config/config.toml"
B="$ROOT/B/config/config.toml"

echo "== Go: create a box in yard A"
idx=$("$GOBIN" --config "$A" new -n shared-box --no-initialise-git)
echo "   $idx"
mkdir -p "$ROOT/A/boxes/$idx/sub"
printf 'hello from go\n' > "$ROOT/A/boxes/$idx/sub/note.txt"
printf '#!/bin/sh\necho hi\n' > "$ROOT/A/boxes/$idx/run.sh"
chmod +x "$ROOT/A/boxes/$idx/run.sh"

echo "== Go: sync (push) from yard A"
"$GOBIN" --config "$A" sync -r "$idx" --no-refresh-user-symlinks

echo "== remote now holds:"
find "$ROOT/store" -maxdepth 4 | sed "s|$ROOT/store|<store>|" | sort | head -20

echo "== Go discovers the box in yard B with sync-missing-meta"
"$GOBIN" --config "$B" sync-missing-meta --no-refresh-user-symlinks 2>&1 | tail -3
"$GOBIN" --config "$B" list | grep -q "$idx" && echo "yard B now knows the box"

echo "== Python: pull into yard B"
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync-missing-meta 2>&1 | tail -3
BOXYARD_CONFIG_PATH="$B" "$PYBIN" include -r "$idx" 2>&1 | tail -3
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync -r "$idx" 2>&1 | tail -5

echo "== yard B contents:"
cat "$ROOT/B/boxes/$idx/sub/note.txt"
test -x "$ROOT/B/boxes/$idx/run.sh" && echo "run.sh is executable in B (perms manifest survived)"

echo "== Reverse: Python writes in B and pushes"
printf 'hello from python\n' > "$ROOT/B/boxes/$idx/from-b.txt"
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync -r "$idx" 2>&1 | tail -3

echo "== Go: pull into yard A"
"$GOBIN" --config "$A" sync -r "$idx" --no-refresh-user-symlinks 2>&1 | tail -3
cat "$ROOT/A/boxes/$idx/from-b.txt"

echo "== Go excludes the box from yard A, then includes it again"
"$GOBIN" --config "$A" exclude -r "$idx" --skip-sync --no-refresh-user-symlinks 2>&1 | tail -2
test ! -e "$ROOT/A/boxes/$idx" && echo "DATA gone from A after exclude"
"$GOBIN" --config "$A" include -r "$idx" --no-refresh-user-symlinks 2>&1 | tail -2
cat "$ROOT/A/boxes/$idx/from-b.txt"
test -x "$ROOT/A/boxes/$idx/run.sh" && echo "run.sh still executable after a Go include"

echo "== Ownership: Go claims in A, Python sees it, Go releases"
"$GOBIN" --config "$A" claim -r "$idx" 2>&1 | tail -1
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync -r "$idx" --sync-choices meta >/dev/null 2>&1
BOXYARD_CONFIG_PATH="$B" "$PYBIN" owner -r "$idx" -o json | python3 -c "
import json,sys; print('   B sees write_owner =', json.load(sys.stdin)['write_owner'])"
"$GOBIN" --config "$A" release -r "$idx" 2>&1 | tail -1
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync -r "$idx" --sync-choices meta >/dev/null 2>&1
BOXYARD_CONFIG_PATH="$B" "$PYBIN" owner -r "$idx" -o json | python3 -c "
import json,sys; print('   B sees write_owner =', json.load(sys.stdin)['write_owner'])"

echo "== multi-sync: the finished line is byte-identical across implementations"
# Both sides take the SAME flag. They did not: the Go had invented a
# friendlier `--print-skipped` for what typer spells `--no-no-print-skipped`,
# and this script quietly passed each implementation its own spelling — so the
# one comparison that would have caught the divergence was the thing hiding it.
# ONE pipeline, applied to both. It used to strip ANSI escapes from the Python
# and not from the Go, because the Go emitted none — so the moment the Go
# started colouring its output the comparison broke, and had it broken the
# other way it would have compared a coloured line against a plain one and
# called them different for the wrong reason. Whenever a differential needs
# different treatment per implementation, that difference IS the finding.
#
# The LAST non-"Syncing" match is the result: rich redraws the line in place
# while the sync runs, so the earlier frames are transient.
finished_line() {
  sed 's/\x1b\[[0-9;]*m//g' | tr '\r' '\n' \
    | grep -oE '\(1/1\) [^ ]+ \.+ [A-Za-z-]+' | grep -v 'Syncing' | tail -1
}
GO_LINE=$(COLUMNS=80 "$GOBIN" --config "$A" multi-sync --no-refresh-user-symlinks --no-no-print-skipped 2>&1 | finished_line)
PY_LINE=$(COLUMNS=80 BOXYARD_CONFIG_PATH="$A" "$PYBIN" multi-sync --no-refresh-user-symlinks --no-no-print-skipped 2>&1 | finished_line)
echo "   go: $GO_LINE"
echo "   py: $PY_LINE"
[ -n "$GO_LINE" ] && [ "$GO_LINE" = "$PY_LINE" ] && echo "multi-sync finished line IDENTICAL"

echo "== multi-sync: the WHOLE block is byte-identical, escapes included"
# FORCE_COLOR= (set but EMPTY) means "not a terminal" to rich, which is the
# supervisor's case: no live redraw, no escapes, so the entire stdout can be
# compared rather than one grepped line. FORCE_COLOR=1 is compared separately
# below, because there rich animates and the frames are not durable output.
touch "$ROOT/A/boxes/$idx/touch-for-a-real-sync.txt"
FORCE_COLOR= COLUMNS=80 "$GOBIN" --config "$A" multi-sync --no-refresh-user-symlinks --no-no-print-skipped > "$ROOT/go-ms.txt" 2>&1
touch "$ROOT/A/boxes/$idx/touch-for-a-real-sync.txt"
FORCE_COLOR= COLUMNS=80 BOXYARD_CONFIG_PATH="$A" "$PYBIN" multi-sync --no-refresh-user-symlinks --no-no-print-skipped > "$ROOT/py-ms.txt" 2>&1
diff "$ROOT/go-ms.txt" "$ROOT/py-ms.txt" && echo "multi-sync plain output IDENTICAL"

echo "== multi-sync: a non-owner reports Read-only, with the same explanation"
# The port printed the "Read-only" status word and then stopped: the two lines
# that name the owner and point at `boxyard doctor` were missing entirely, so
# the status was a dead end. Yard A claims the box; yard B is then a non-owner
# and its multi-sync must say so in the same words.
"$GOBIN" --config "$A" claim -r "$idx" >/dev/null 2>&1
BOXYARD_CONFIG_PATH="$B" "$PYBIN" sync -r "$idx" --sync-choices meta >/dev/null 2>&1
printf 'a local change B may not push\n' > "$ROOT/B/boxes/$idx/from-b.txt"
FORCE_COLOR= COLUMNS=80 "$GOBIN" --config "$B" multi-sync --no-refresh-user-symlinks --no-no-print-skipped > "$ROOT/go-ro.txt" 2>&1
printf 'a local change B may not push\n' > "$ROOT/B/boxes/$idx/from-b.txt"
FORCE_COLOR= COLUMNS=80 BOXYARD_CONFIG_PATH="$B" "$PYBIN" multi-sync --no-refresh-user-symlinks --no-no-print-skipped > "$ROOT/py-ro.txt" 2>&1
grep -q 'Read-only' "$ROOT/py-ro.txt" || { echo "the Python did not report Read-only -- the comparison would be vacuous"; cat "$ROOT/py-ro.txt"; exit 1; }
grep -q 'names both ways out' "$ROOT/py-ro.txt" || { echo "no owner explanation in the Python output -- the comparison would be vacuous"; exit 1; }
diff "$ROOT/go-ro.txt" "$ROOT/py-ro.txt" && echo "multi-sync Read-only output IDENTICAL"
"$GOBIN" --config "$A" release -r "$idx" >/dev/null 2>&1

echo "== Go box-status agrees with Python box-status"
"$GOBIN" --config "$A" box-status -r "$idx" -o json > "$ROOT/go-status.json"
BOXYARD_CONFIG_PATH="$A" "$PYBIN" box-status -r "$idx" -o json > "$ROOT/py-status.json"
diff "$ROOT/go-status.json" "$ROOT/py-status.json" && echo "box-status IDENTICAL across implementations"

echo "OK"
