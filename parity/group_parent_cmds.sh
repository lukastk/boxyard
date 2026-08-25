#!/bin/sh
# Run the boxmeta-editing commands through BOTH implementations in matching
# throwaway yards and print what each ends up with.
set -e
ROOT="$1"; GOBIN="$2"; PYBIN="$3"
case "$ROOT" in /tmp/*|/var/folders/*|"$TMPDIR"*) ;; *) echo "refusing: $ROOT" >&2; exit 1;; esac
rm -rf "$ROOT"; mkdir -p "$ROOT"
export DEFAULT_BOX_GROUPS=
for impl in GO PY; do
  d="$ROOT/$impl"; mkdir -p "$d/config" "$d/data" "$d/boxes" "$d/groups" "$d/store"
  cat > "$d/config/config.toml" <<EOF
default_storage_location = "local"
boxyard_data_path = "$d/data"
box_timestamp_format = "date_only"
user_boxes_path = "$d/boxes"
user_box_groups_path = "$d/groups"
default_box_groups = []
box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
box_subid_length = 6
max_concurrent_rclone_ops = 2

[storage_locations.local]
storage_type = "local"
store_path = "$d/store"

[box_groups.proj]
symlink_name = "all/proj"

[virtual_box_groups]
EOF
  C="$d/config/config.toml"
  if [ "$impl" = GO ]; then RUN="$GOBIN --config $C"; else RUN="$PYBIN --config $C"; fi
  $RUN init >/dev/null
  parent=$($RUN new -n parent-box --no-initialise-git)
  child=$($RUN new -n child-box --no-initialise-git)
  $RUN add-to-group -r "$child" proj extra >/dev/null
  $RUN add-to-group -r "$child" proj >/dev/null   # already-there path
  $RUN remove-from-group -r "$child" extra >/dev/null
  $RUN remove-from-group -r "$child" nope >/dev/null   # not-there path
  $RUN add-parent -r "$child" --parent "$parent" >/dev/null
  $RUN add-parent -r "$child" --parent "$parent" >/dev/null  # already-there path
  $RUN create-user-symlinks >/dev/null
  echo "$impl state:"
  "$PYBIN" --config "$C" list -o json | python3 -c "
import json,sys
for b in sorted(json.load(sys.stdin), key=lambda b: b['name']):
    print('   ', b['name'], 'groups=', sorted(b['groups']), 'nparents=', len(b['parents']))
"
  echo "$impl symlink tree:"
  (cd "$d/groups" && find . | sort | sed 's|^|    |')
  # remove-parent by dangling id must work
  $RUN remove-parent -r "$child" --parent-id 20240102_dangling >/dev/null 2>&1 && echo "  $impl: dangling remove-parent ok" || echo "  $impl: dangling remove-parent exit $?"
  $RUN remove-parent -r "$child" --parent "$parent" >/dev/null
  "$PYBIN" --config "$C" list -o json | python3 -c "
import json,sys
c=[b for b in json.load(sys.stdin) if b['name']=='child-box'][0]
print('   after remove-parent: nparents=', len(c['parents']))
"
done
