#!/bin/sh
# `boxyard new`'s --parent and --group, run through BOTH implementations in two
# throwaway yards, and printed so a test can compare them.
#
# --parent is documented as taking an "index name, id, or name" and honoured
# only the name until Python v0.5.8; all three are exercised here. --group has
# to go through modify_boxmeta, which is what enforces the group rules — hence
# the virtual-group refusal at the end.
#
# SAFETY: everything lives under $ROOT, and both implementations are pointed at
# a config inside it. Nothing touches the real yard.
set -e
ROOT="$1"; GOBIN="$2"; PYBIN="$3"
case "$ROOT" in
  /tmp/*|/var/folders/*|"$TMPDIR"*) ;;
  *) echo "refusing to run outside a temp directory: $ROOT" >&2; exit 1 ;;
esac
rm -rf "$ROOT"; mkdir -p "$ROOT"
mk () {
  d="$ROOT/$1"; mkdir -p "$d/config" "$d/data" "$d/boxes" "$d/groups" "$d/store"
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

[box_groups.solo]
symlink_name = "solo"

[virtual_box_groups.everything]
symlink_name = "everything"
filter_expr = "NOT null"
EOF
}
export DEFAULT_BOX_GROUPS=
for impl in GO PY; do
  mk "$impl"
  C="$ROOT/$impl/config/config.toml"
  if [ "$impl" = GO ]; then RUN="$GOBIN --config $C"; else RUN="$PYBIN --config $C"; fi
  $RUN init >/dev/null
  parent=$($RUN new -n the-parent --no-initialise-git)
  echo "$impl parent: $parent"
  for form in index id name; do
    case $form in
      index) sel="$parent" ;;
      id)    sel="${parent%%__*}" ;;
      name)  sel="the-parent" ;;
    esac
    child=$($RUN new -n "child-$form" --parent "$sel" -g solo --no-initialise-git)
    echo "  $impl --parent $form -> $child"
  done
  echo "  $impl groups+parents:"
  "$PYBIN" --config "$C" list -o json 2>/dev/null | python3 -c "
import json,sys
for b in json.load(sys.stdin):
    print('   ', b['name'], 'groups=', b['groups'], 'parents=', b['parents'])
" || true
  echo "  $impl refuses a virtual group:"
  $RUN new -n bad -g everything >/dev/null 2>&1 && echo "    NOT REFUSED" || echo "    refused (exit $?)"
done
