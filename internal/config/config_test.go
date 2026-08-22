package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
)

// validBaseline is the smallest config the live Python implementation accepts.
// Every accept/reject expectation in this file was established by probing that
// implementation directly, not read off the type annotations — see the package
// doc comment.
const validBaseline = `default_storage_location = "fake"
boxyard_data_path = "~/.bytest"
box_timestamp_format = "date_only"
user_boxes_path = "~/bxtest"
user_box_groups_path = "~/bgtest"
default_box_groups = ["a"]
box_subid_character_set = "abc"
box_subid_length = 5
max_concurrent_rclone_ops = 2
box_groups = {}
virtual_box_groups = {}
[storage_locations.fake]
storage_type = "local"
store_path = "~/.bytest/fake_store"
`

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func loadWithEnv(t *testing.T, body, envGroups string) (*Config, error) {
	t.Helper()
	if envGroups == "" {
		t.Setenv(boxconst.EnvDefaultBoxGroups, "")
		os.Unsetenv(boxconst.EnvDefaultBoxGroups)
	} else {
		t.Setenv(boxconst.EnvDefaultBoxGroups, envGroups)
	}
	return Load(writeConfig(t, body))
}

func TestValidBaselineLoads(t *testing.T) {
	cfg, err := loadWithEnv(t, validBaseline, "")
	if err != nil {
		t.Fatalf("valid baseline rejected: %v", err)
	}
	if !reflect.DeepEqual(cfg.DefaultBoxGroups, []string{"a"}) {
		t.Errorf("default_box_groups = %v, want [a]", cfg.DefaultBoxGroups)
	}
	if cfg.BoxTimestampFormat != TimestampDateOnly {
		t.Errorf("box_timestamp_format = %q", cfg.BoxTimestampFormat)
	}
	// Optional fields keep their Python defaults.
	if cfg.SingleParent || cfg.SyncBeforeNewBox || cfg.RclonePath != "" {
		t.Errorf("optional fields should default to zero: %+v", cfg)
	}
}

// Each case below mirrors one probe of the Python implementation.
func TestRejectionsMatchPython(t *testing.T) {
	cases := []struct {
		name      string
		body      string
		wantInErr string
	}{
		{
			name: "missing default_box_groups",
			body: strings.Replace(validBaseline, "default_box_groups = [\"a\"]\n", "", 1),
			// Python: Field required
			wantInErr: "default_box_groups",
		},
		{
			name:      "missing max_concurrent_rclone_ops",
			body:      strings.Replace(validBaseline, "max_concurrent_rclone_ops = 2\n", "", 1),
			wantInErr: "max_concurrent_rclone_ops",
		},
		{
			name:      "missing box_groups table",
			body:      strings.Replace(validBaseline, "box_groups = {}\n", "", 1),
			wantInErr: "box_groups",
		},
		{
			name:      "missing virtual_box_groups table",
			body:      strings.Replace(validBaseline, "virtual_box_groups = {}\n", "", 1),
			wantInErr: "virtual_box_groups",
		},
		{
			// Python: Extra inputs are not permitted
			name:      "unknown key",
			body:      validBaseline + "surprise = 1\n",
			wantInErr: "unknown key",
		},
		{
			// Python: Input should be 'date_and_time' or 'date_only'
			name:      "bad timestamp format enum",
			body:      strings.Replace(validBaseline, `"date_only"`, `"weekly"`, 1),
			wantInErr: "box_timestamp_format",
		},
		{
			// Python: Value error, No storage locations defined.
			name: "empty storage_locations",
			body: strings.Replace(validBaseline,
				"[storage_locations.fake]\nstorage_type = \"local\"\nstore_path = \"~/.bytest/fake_store\"\n",
				"storage_locations = {}\n", 1),
			wantInErr: "No storage locations defined",
		},
		{
			// Python: default_storage_location 'nope' not found in storage_locations
			name:      "default_storage_location absent from storage_locations",
			body:      strings.Replace(validBaseline, `default_storage_location = "fake"`, `default_storage_location = "nope"`, 1),
			wantInErr: "not found in storage_locations",
		},
		{
			// Python: StorageConfig names can only contain alphanumeric, _ or -
			name: "storage location name containing a dot",
			body: strings.NewReplacer(
				"[storage_locations.fake]", `[storage_locations."fa.ke"]`,
				`default_storage_location = "fake"`, `default_storage_location = "fa.ke"`,
			).Replace(validBaseline),
			wantInErr: "can only contain alphanumeric",
		},
		{
			name:      "bad storage_type enum",
			body:      strings.Replace(validBaseline, `storage_type = "local"`, `storage_type = "ftp"`, 1),
			wantInErr: "storage_type",
		},
		{
			name:      "virtual group without filter_expr",
			body:      strings.Replace(validBaseline, "virtual_box_groups = {}", `virtual_box_groups = {active = {symlink_name = "a"}}`, 1),
			wantInErr: "filter_expr",
		},
		{
			name:      "virtual group with unparseable filter_expr",
			body:      strings.Replace(validBaseline, "virtual_box_groups = {}", `virtual_box_groups = {active = {filter_expr = "AND AND"}}`, 1),
			wantInErr: "filter_expr",
		},
		{
			name:      "invalid group name",
			body:      strings.Replace(validBaseline, "box_groups = {}", `box_groups = {"bad name" = {}}`, 1),
			wantInErr: "group name",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := loadWithEnv(t, c.body, "")
			if err == nil {
				t.Fatalf("config was ACCEPTED but Python rejects it")
			}
			if !strings.Contains(err.Error(), c.wantInErr) {
				t.Errorf("error should mention %q, got: %v", c.wantInErr, err)
			}
		})
	}
}

// Verified against Python: list(dict.fromkeys(existing + extra)) — additive,
// order-preserving, de-duplicated.
func TestDefaultBoxGroupsEnvMerge(t *testing.T) {
	cases := []struct {
		name string
		body string
		env  string
		want []string
	}{
		{"merge preserves order and dedupes", validBaseline, `["b","a","c"]`, []string{"a", "b", "c"}},
		{"merge onto empty base", strings.Replace(validBaseline, `default_box_groups = ["a"]`, `default_box_groups = []`, 1), `["z"]`, []string{"z"}},
		{"no env leaves config value", validBaseline, "", []string{"a"}},
		{"env duplicate of itself dedupes", validBaseline, `["b","b"]`, []string{"a", "b"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg, err := loadWithEnv(t, c.body, c.env)
			if err != nil {
				t.Fatalf("load: %v", err)
			}
			if !reflect.DeepEqual(cfg.DefaultBoxGroups, c.want) {
				t.Errorf("default_box_groups = %v, want %v", cfg.DefaultBoxGroups, c.want)
			}
		})
	}
}

func TestMalformedEnvVarIsLoud(t *testing.T) {
	_, err := loadWithEnv(t, validBaseline, `not a toml list`)
	if err == nil {
		t.Fatal("a malformed DEFAULT_BOX_GROUPS was silently ignored")
	}
	if !strings.Contains(err.Error(), boxconst.EnvDefaultBoxGroups) {
		t.Errorf("error should name the env var, got: %v", err)
	}
}

func TestPathsAreExpanded(t *testing.T) {
	cfg, err := loadWithEnv(t, validBaseline, "")
	if err != nil {
		t.Fatal(err)
	}
	home, _ := os.UserHomeDir()
	for name, got := range map[string]string{
		"boxyard_data_path":    cfg.BoxyardDataPath,
		"user_boxes_path":      cfg.UserBoxesPath,
		"user_box_groups_path": cfg.UserBoxGroupsPath,
	} {
		if strings.Contains(got, "~") {
			t.Errorf("%s was not expanded: %q", name, got)
		}
		if !strings.HasPrefix(got, home) {
			t.Errorf("%s should be under home, got %q", name, got)
		}
	}
	// A local store_path is a filesystem path and gets expanded...
	if strings.Contains(cfg.StorageLocations["fake"].StorePath, "~") {
		t.Error("local store_path was not expanded")
	}
}

// An rclone store_path is remote-relative and must NOT be touched — the real
// config uses store_path = "boxyard", which is a path on the SFTP remote.
func TestRcloneStorePathIsNotExpanded(t *testing.T) {
	body := strings.Replace(validBaseline,
		"[storage_locations.fake]\nstorage_type = \"local\"\nstore_path = \"~/.bytest/fake_store\"\n",
		"[storage_locations.fake]\nstorage_type = \"local\"\nstore_path = \"~/.bytest/fake_store\"\n[storage_locations.remote]\nstorage_type = \"rclone\"\nstore_path = \"boxyard-gotest\"\n", 1)
	cfg, err := loadWithEnv(t, body, "")
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.StorageLocations["remote"].StorePath; got != "boxyard-gotest" {
		t.Errorf("rclone store_path was rewritten to %q", got)
	}
}

func TestDerivedPaths(t *testing.T) {
	cfg, err := loadWithEnv(t, validBaseline, "")
	if err != nil {
		t.Fatal(err)
	}
	cfgDir := filepath.Dir(cfg.ConfigPath)
	checks := map[string]string{
		cfg.BoxyardMetaPath():          filepath.Join(cfg.BoxyardDataPath, "boxyard_meta.json"),
		cfg.LocalStorePath():           filepath.Join(cfg.BoxyardDataPath, "local_store"),
		cfg.LocalSyncBackupsPath():     filepath.Join(cfg.BoxyardDataPath, "sync_backups"),
		cfg.RemoteIndexesPath():        filepath.Join(cfg.BoxyardDataPath, "remote_indexes"),
		cfg.SyncRecordsPath():          filepath.Join(cfg.BoxyardDataPath, "sync_records"),
		cfg.LocksPath():                filepath.Join(cfg.BoxyardDataPath, "locks"),
		cfg.RcloneConfigPath():         filepath.Join(cfgDir, "boxyard_rclone.conf"),
		cfg.DefaultRcloneExcludePath(): filepath.Join(cfgDir, "default.rclone_exclude"),
	}
	for got, want := range checks {
		if got != want {
			t.Errorf("derived path = %q, want %q", got, want)
		}
	}
}

func TestBoxGroupTitleModeDefaultsToIndexName(t *testing.T) {
	body := strings.Replace(validBaseline, "box_groups = {}", `box_groups = {proj = {symlink_name = "all/proj"}}`, 1)
	cfg, err := loadWithEnv(t, body, "")
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.BoxGroups["proj"].BoxTitleMode; got != TitleIndexName {
		t.Errorf("box_title_mode defaulted to %q, want %q", got, TitleIndexName)
	}
}

func TestVirtualGroupFilterIsCompiledAtLoad(t *testing.T) {
	body := strings.Replace(validBaseline, "virtual_box_groups = {}",
		`virtual_box_groups = {active = {filter_expr = "(NOT archived) AND (NOT null)"}}`, 1)
	cfg, err := loadWithEnv(t, body, "")
	if err != nil {
		t.Fatal(err)
	}
	g := cfg.VirtualBoxGroups["active"]
	if !g.IsInGroup([]string{"proj"}) {
		t.Error("box with no archived/null groups should be in 'active'")
	}
	if g.IsInGroup([]string{"archived"}) {
		t.Error("archived box should not be in 'active'")
	}
}

func TestMissingFileSurfacesAsError(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "nope.toml")); err == nil {
		t.Fatal("a missing config file loaded successfully")
	}
}

func TestTimestampLayout(t *testing.T) {
	if l, err := TimestampDateOnly.Layout(); err != nil || l != "20060102" {
		t.Errorf("date_only layout = %q, %v", l, err)
	}
	if l, err := TimestampDateAndTime.Layout(); err != nil || l != "20060102_150405" {
		t.Errorf("date_and_time layout = %q, %v", l, err)
	}
	if _, err := BoxTimestampFormat("weekly").Layout(); err == nil {
		t.Error("invalid format returned a layout instead of an error")
	}
}

func TestStorageLocationLookupIsLoud(t *testing.T) {
	cfg, err := loadWithEnv(t, validBaseline, "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := cfg.StorageLocation("nope"); err == nil {
		t.Fatal("unknown storage location returned no error")
	}
	if _, err := cfg.StorageLocation("fake"); err != nil {
		t.Fatalf("known storage location errored: %v", err)
	}
}
