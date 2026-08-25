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

echo "== Go box-status agrees with Python box-status"
"$GOBIN" --config "$A" box-status -r "$idx" -o json > "$ROOT/go-status.json"
BOXYARD_CONFIG_PATH="$A" "$PYBIN" box-status -r "$idx" -o json > "$ROOT/py-status.json"
diff "$ROOT/go-status.json" "$ROOT/py-status.json" && echo "box-status IDENTICAL across implementations"

echo "OK"
