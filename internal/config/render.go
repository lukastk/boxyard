package config

import (
	"fmt"
	"path"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
)

// RenderDefault returns the config.toml body that `boxyard init` writes,
// byte-for-byte as Python's `tomli_w.dumps(_get_default_config_dict(...))`.
//
// Byte-compatibility is not cosmetic here. This file is the one artefact both
// implementations must agree on before anything else can run, and a config
// written by one and read by the other is the normal case during a migration.
//
// The layout reproduces tomli_w's rules rather than TOML's freedom:
//
//   - every non-table value first, in the dict's insertion order — note that
//     puts `default_box_groups` (a list) up with the scalars, even though it is
//     declared after the table fields;
//   - then each table, in insertion order, preceded by a blank line;
//   - an EMPTY table still emits its header (`[box_groups]`) and nothing else.
//
// `config_path` is deliberately absent: Python deletes it before writing,
// because the file's own location is not something the file should record.
func RenderDefault(configPath, dataPath string) string {
	if configPath == "" {
		configPath = boxconst.DefaultConfigPath
	}
	if dataPath == "" {
		dataPath = boxconst.DefaultDataPath
	}

	var b strings.Builder
	kv := func(k, v string) { fmt.Fprintf(&b, "%s = %s\n", k, v) }

	kv("default_storage_location", tomlString("fake"))
	kv("boxyard_data_path", tomlString(dataPath))
	kv("box_timestamp_format", tomlString(string(TimestampDateOnly)))
	kv("user_boxes_path", tomlString(boxconst.DefaultUserBoxesPath))
	kv("user_box_groups_path", tomlString(boxconst.DefaultUserBoxGroupsPath))
	kv("default_box_groups", "[]")
	kv("box_subid_character_set", tomlString(boxconst.DefaultBoxSubidCharacterSet))
	kv("box_subid_length", fmt.Sprintf("%d", boxconst.DefaultBoxSubidLength))
	kv("max_concurrent_rclone_ops", fmt.Sprintf("%d", boxconst.DefaultMaxConcurrentRclone))
	kv("single_parent", "false")
	kv("sync_before_new_box", "false")
	kv("merge_diverged_boxmetas", "false")

	// The one storage location a fresh boxyard gets: a LOCAL one, so the tool
	// is usable before any remote is configured.
	fmt.Fprintf(&b, "\n[storage_locations.fake]\n")
	kv("storage_type", tomlString(string(StorageLocal)))
	kv("store_path", tomlString(path.Join(dataPath, boxconst.DefaultFakeStoreRelPath)))

	// Empty, but emitted: the headers are how a user discovers the keys exist.
	fmt.Fprintf(&b, "\n[box_groups]\n")
	fmt.Fprintf(&b, "\n[virtual_box_groups]\n")

	return b.String()
}

// tomlString quotes a value the way tomli_w does — a basic string with
// backslash and double-quote escaped. The paths here are generated, not user
// input, but the escaping is applied anyway so a data path containing a quote
// cannot produce a file that does not parse.
func tomlString(s string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`)
	return `"` + r.Replace(s) + `"`
}
