// Package config loads and validates boxyard's config.toml.
//
// Ported from the Python src/boxyard/config.py. The Python models are pydantic
// StrictModels whose @model_validator(mode="after") both NORMALISES (expanding
// ~ in paths, applying enum defaults) and VALIDATES. Validate here does the
// same two jobs in the same order, so the behaviour matches; see the note on
// Config.Validate.
//
// Required-vs-optional was established by probing the live Python
// implementation rather than read off the type annotations. Required:
// default_storage_location, boxyard_data_path, box_timestamp_format,
// user_boxes_path, user_box_groups_path, storage_locations, box_groups,
// virtual_box_groups, default_box_groups, box_subid_character_set,
// box_subid_length, max_concurrent_rclone_ops. Optional: rclone_path,
// single_parent, sync_before_new_box.
package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/groupexpr"
	"github.com/lukastk/boxyard/internal/naming"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/pelletier/go-toml/v2"
)

// StorageType selects how a storage location is reached.
type StorageType string

const (
	StorageRclone StorageType = "rclone"
	StorageLocal  StorageType = "local"
)

func (s StorageType) valid() bool {
	return s == StorageRclone || s == StorageLocal
}

// BoxGroupTitleMode selects how a box is named inside a group's symlink tree.
type BoxGroupTitleMode string

const (
	TitleIndexName       BoxGroupTitleMode = "index_name"
	TitleDatetimeAndName BoxGroupTitleMode = "datetime_and_name"
	TitleName            BoxGroupTitleMode = "name"
)

func (m BoxGroupTitleMode) valid() bool {
	return m == TitleIndexName || m == TitleDatetimeAndName || m == TitleName
}

// BoxTimestampFormat selects the granularity of the timestamp in a box id.
type BoxTimestampFormat string

const (
	TimestampDateAndTime BoxTimestampFormat = "date_and_time"
	TimestampDateOnly    BoxTimestampFormat = "date_only"
)

func (f BoxTimestampFormat) valid() bool {
	return f == TimestampDateAndTime || f == TimestampDateOnly
}

// Layout returns the Go time layout for this format.
func (f BoxTimestampFormat) Layout() (string, error) {
	switch f {
	case TimestampDateAndTime:
		return boxconst.BoxTimestampFormat, nil
	case TimestampDateOnly:
		return boxconst.BoxTimestampFormatDateOnly, nil
	default:
		return "", fmt.Errorf("invalid box timestamp format: %s", f)
	}
}

// storageNameRe mirrors the Python's r"[A-Za-z0-9_-]+" fullmatch. Storage names
// become path components and rclone remote names, so the character set is
// deliberately narrow.
var storageNameRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// StorageConfig describes one storage location.
type StorageConfig struct {
	StorageType StorageType `toml:"storage_type"`
	StorePath   string      `toml:"store_path"`
}

func (s *StorageConfig) normalizeAndValidate(name string) error {
	if s.StorageType == "" {
		return strict.Missing("StorageConfig["+name+"]", "storage_type")
	}
	if !s.StorageType.valid() {
		return strict.Invalid("StorageConfig["+name+"]", "storage_type",
			fmt.Sprintf("should be %q or %q, got %q", StorageRclone, StorageLocal, s.StorageType))
	}
	if s.StorePath == "" {
		return strict.Missing("StorageConfig["+name+"]", "store_path")
	}
	// A local store path is a filesystem path and gets ~ expanded; an rclone
	// store path is a remote-relative path and must be left alone.
	if s.StorageType == StorageLocal {
		expanded, err := ExpandUser(s.StorePath)
		if err != nil {
			return err
		}
		s.StorePath = expanded
	}
	return nil
}

// BoxGroupConfig configures a real (membership-based) box group.
type BoxGroupConfig struct {
	SymlinkName    string            `toml:"symlink_name"`
	BoxTitleMode   BoxGroupTitleMode `toml:"box_title_mode"`
	UniqueBoxNames bool              `toml:"unique_box_names"`
}

func (g *BoxGroupConfig) normalizeAndValidate(name string) error {
	if g.BoxTitleMode == "" {
		g.BoxTitleMode = TitleIndexName
	}
	if !g.BoxTitleMode.valid() {
		return strict.Invalid("BoxGroupConfig["+name+"]", "box_title_mode",
			fmt.Sprintf("unknown title mode %q", g.BoxTitleMode))
	}
	return nil
}

// VirtualBoxGroupConfig configures a group whose membership is computed from a
// boolean expression over a box's real groups.
type VirtualBoxGroupConfig struct {
	SymlinkName  string            `toml:"symlink_name"`
	BoxTitleMode BoxGroupTitleMode `toml:"box_title_mode"`
	FilterExpr   string            `toml:"filter_expr"`

	// filter is compiled once during validation, so a malformed expression is
	// a config-load error rather than a surprise at symlink-build time.
	filter func([]string) bool
}

func (g *VirtualBoxGroupConfig) normalizeAndValidate(name string) error {
	if g.BoxTitleMode == "" {
		g.BoxTitleMode = TitleIndexName
	}
	if !g.BoxTitleMode.valid() {
		return strict.Invalid("VirtualBoxGroupConfig["+name+"]", "box_title_mode",
			fmt.Sprintf("unknown title mode %q", g.BoxTitleMode))
	}
	if g.FilterExpr == "" {
		return strict.Missing("VirtualBoxGroupConfig["+name+"]", "filter_expr")
	}
	// A malformed filter_expr is a config-load error in both implementations.
	//
	// Python originally deferred this: get_group_filter_func only tokenizes
	// eagerly and re-parses on each call, so "(a AND b" compiled fine and
	// raised only when the predicate was first invoked, during symlink
	// building. That was fixed in Python (VirtualBoxGroupConfig now validates
	// in a model_validator) rather than reproduced here.
	f, err := groupexpr.Parse(g.FilterExpr)
	if err != nil {
		return strict.Invalid("VirtualBoxGroupConfig["+name+"]", "filter_expr", err.Error())
	}
	g.filter = f
	return nil
}

// IsInGroup reports whether a box with these groups belongs to this virtual
// group.
//
// It cannot fail: Load rejects a config whose filter_expr does not compile, so
// by the time a caller holds one of these the predicate exists.
func (g *VirtualBoxGroupConfig) IsInGroup(groups []string) bool {
	if g.filter == nil {
		// Only reachable if the value was built by hand rather than loaded.
		panic("VirtualBoxGroupConfig used before its filter_expr was compiled")
	}
	return g.filter(groups)
}

// Config is boxyard's whole configuration.
type Config struct {
	// ConfigPath is the file this config was loaded from. Load seeds it before
	// decoding, matching the Python's {"config_path": path, **toml.load(path)}.
	ConfigPath string `toml:"config_path"`

	DefaultStorageLocation string                            `toml:"default_storage_location"`
	BoxyardDataPath        string                            `toml:"boxyard_data_path"`
	BoxTimestampFormat     BoxTimestampFormat                `toml:"box_timestamp_format"`
	UserBoxesPath          string                            `toml:"user_boxes_path"`
	UserBoxGroupsPath      string                            `toml:"user_box_groups_path"`
	StorageLocations       map[string]*StorageConfig         `toml:"storage_locations"`
	BoxGroups              map[string]*BoxGroupConfig        `toml:"box_groups"`
	VirtualBoxGroups       map[string]*VirtualBoxGroupConfig `toml:"virtual_box_groups"`
	DefaultBoxGroups       []string                          `toml:"default_box_groups"`
	BoxSubidCharacterSet   string                            `toml:"box_subid_character_set"`
	BoxSubidLength         int                               `toml:"box_subid_length"`
	MaxConcurrentRcloneOps int                               `toml:"max_concurrent_rclone_ops"`

	// Optional.
	RclonePath       string `toml:"rclone_path"`
	SingleParent     bool   `toml:"single_parent"`
	SyncBeforeNewBox bool   `toml:"sync_before_new_box"`
}

// Derived paths. These mirror the Python's @property accessors.

func (c *Config) LocalStorePath() string {
	return filepath.Join(c.BoxyardDataPath, boxconst.LocalStoreRelPath)
}

func (c *Config) LocalSyncBackupsPath() string {
	return filepath.Join(c.BoxyardDataPath, boxconst.SyncBackupsRelPath)
}

func (c *Config) BoxyardMetaPath() string {
	return filepath.Join(c.BoxyardDataPath, "boxyard_meta.json")
}

func (c *Config) RcloneConfigPath() string {
	return filepath.Join(filepath.Dir(c.ConfigPath), "boxyard_rclone.conf")
}

func (c *Config) DefaultRcloneExcludePath() string {
	return filepath.Join(filepath.Dir(c.ConfigPath), "default.rclone_exclude")
}

func (c *Config) RemoteIndexesPath() string {
	return filepath.Join(c.BoxyardDataPath, boxconst.RemoteIndexesRel)
}

func (c *Config) SyncRecordsPath() string {
	return filepath.Join(c.BoxyardDataPath, boxconst.SyncRecordsRelPath)
}

func (c *Config) LocksPath() string {
	return filepath.Join(c.BoxyardDataPath, boxconst.LocksRelPath)
}

// Validate normalises then checks, in that order — the same two jobs the
// Python's @model_validator(mode="after") does, and in the same order, so a
// config that loads in one implementation loads in the other.
//
// Normalising here (rather than in a separate pass) is deliberate: it is what
// makes strict.UnmarshalTOML a complete load, with no way to obtain a
// half-initialised Config.
func (c *Config) Validate() error {
	const t = "Config"

	// --- expand paths ---
	for _, p := range []struct {
		name string
		dst  *string
	}{
		{"config_path", &c.ConfigPath},
		{"boxyard_data_path", &c.BoxyardDataPath},
		{"user_boxes_path", &c.UserBoxesPath},
		{"user_box_groups_path", &c.UserBoxGroupsPath},
	} {
		if *p.dst == "" {
			return strict.Missing(t, p.name)
		}
		expanded, err := ExpandUser(*p.dst)
		if err != nil {
			return strict.Invalid(t, p.name, err.Error())
		}
		*p.dst = expanded
	}
	if c.RclonePath != "" {
		expanded, err := ExpandUser(c.RclonePath)
		if err != nil {
			return strict.Invalid(t, "rclone_path", err.Error())
		}
		c.RclonePath = expanded
	}

	// --- required scalars ---
	if err := strict.RequireNonZero(t, "default_storage_location", c.DefaultStorageLocation); err != nil {
		return err
	}
	if err := strict.RequireNonZero(t, "box_subid_character_set", c.BoxSubidCharacterSet); err != nil {
		return err
	}
	if err := strict.RequireNonZero(t, "box_subid_length", c.BoxSubidLength); err != nil {
		return err
	}
	if err := strict.RequireNonZero(t, "max_concurrent_rclone_ops", c.MaxConcurrentRcloneOps); err != nil {
		return err
	}
	if c.BoxTimestampFormat == "" {
		return strict.Missing(t, "box_timestamp_format")
	}
	if !c.BoxTimestampFormat.valid() {
		return strict.Invalid(t, "box_timestamp_format",
			fmt.Sprintf("should be %q or %q, got %q", TimestampDateAndTime, TimestampDateOnly, c.BoxTimestampFormat))
	}

	// --- required collections ---
	// Python declares these without defaults, so an absent table is a
	// validation error, not an empty map. Verified against the live
	// implementation: omitting box_groups raises "Field required".
	if c.StorageLocations == nil {
		return strict.Missing(t, "storage_locations")
	}
	if c.BoxGroups == nil {
		return strict.Missing(t, "box_groups")
	}
	if c.VirtualBoxGroups == nil {
		return strict.Missing(t, "virtual_box_groups")
	}
	if c.DefaultBoxGroups == nil {
		return strict.Missing(t, "default_box_groups")
	}

	// --- nested models ---
	for name, sl := range c.StorageLocations {
		if !storageNameRe.MatchString(name) {
			return strict.Invalid(t, "storage_locations",
				fmt.Sprintf("StorageConfig name %s is invalid. StorageConfig names can only contain alphanumeric characters, underscore(_), or dash(-).", name))
		}
		if err := sl.normalizeAndValidate(name); err != nil {
			return err
		}
	}
	if len(c.StorageLocations) == 0 {
		return strict.Invalid(t, "storage_locations", "No storage locations defined.")
	}
	if _, ok := c.StorageLocations[c.DefaultStorageLocation]; !ok {
		return strict.Invalid(t, "default_storage_location",
			fmt.Sprintf("default_storage_location '%s' not found in storage_locations", c.DefaultStorageLocation))
	}
	for name, g := range c.BoxGroups {
		if err := naming.ValidateGroupName(name); err != nil {
			return err
		}
		if err := g.normalizeAndValidate(name); err != nil {
			return err
		}
	}
	for name, g := range c.VirtualBoxGroups {
		if err := naming.ValidateGroupName(name); err != nil {
			return err
		}
		if err := g.normalizeAndValidate(name); err != nil {
			return err
		}
	}
	return nil
}

// StorageLocation returns the named storage location, or an error naming what
// was asked for — never a zero-valued StorageConfig.
func (c *Config) StorageLocation(name string) (*StorageConfig, error) {
	sl, ok := c.StorageLocations[name]
	if !ok {
		return nil, fmt.Errorf("unknown storage location %q", name)
	}
	return sl, nil
}

// Load reads a config.toml. If path is empty the default location is used.
func Load(path string) (*Config, error) {
	if path == "" {
		path = os.Getenv(boxconst.EnvBoxyardConfigPath)
	}
	if path == "" {
		path = boxconst.DefaultConfigPath
	}
	expanded, err := ExpandUser(path)
	if err != nil {
		return nil, err
	}

	data, err := os.ReadFile(expanded)
	if err != nil {
		return nil, err
	}

	// Seed ConfigPath before decoding so that a config_path key in the file
	// wins, exactly as the Python's {"config_path": path, **toml.load(path)}
	// does.
	cfg := &Config{ConfigPath: expanded}
	if err := decodeInto(data, cfg); err != nil {
		return nil, fmt.Errorf("%s: %w", expanded, err)
	}

	if err := mergeEnvDefaultBoxGroups(cfg); err != nil {
		return nil, err
	}
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", expanded, err)
	}
	return cfg, nil
}

// decodeInto decodes without validating, so Load can apply the environment
// merge between decode and validation — the order the Python uses.
func decodeInto(data []byte, cfg *Config) error {
	dec := toml.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(cfg); err != nil {
		var se *toml.StrictMissingError
		if errors.As(err, &se) {
			return fmt.Errorf("unknown key in TOML: %s", se.String())
		}
		return fmt.Errorf("invalid TOML: %w", err)
	}
	return nil
}

// mergeEnvDefaultBoxGroups additively merges DEFAULT_BOX_GROUPS into
// default_box_groups, preserving order and de-duplicating — matching the
// Python's list(dict.fromkeys(existing + extra)). The env var holds a TOML
// list literal, e.g. '["ctx/mac", "ctx/linux"]'.
func mergeEnvDefaultBoxGroups(cfg *Config) error {
	raw := os.Getenv(boxconst.EnvDefaultBoxGroups)
	if raw == "" {
		return nil
	}
	var wrapper struct {
		V []string `toml:"v"`
	}
	if err := toml.Unmarshal([]byte("v = "+raw), &wrapper); err != nil {
		return fmt.Errorf("%s is not a TOML list: %w", boxconst.EnvDefaultBoxGroups, err)
	}
	seen := make(map[string]bool, len(cfg.DefaultBoxGroups)+len(wrapper.V))
	merged := make([]string, 0, len(cfg.DefaultBoxGroups)+len(wrapper.V))
	for _, g := range append(append([]string{}, cfg.DefaultBoxGroups...), wrapper.V...) {
		if seen[g] {
			continue
		}
		seen[g] = true
		merged = append(merged, g)
	}
	cfg.DefaultBoxGroups = merged
	return nil
}

// ExpandUser resolves a leading ~ and cleans the path, matching
// pathlib.Path.expanduser.
func ExpandUser(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("empty path")
	}
	if p == "~" || strings.HasPrefix(p, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", fmt.Errorf("cannot resolve home directory: %w", err)
		}
		p = filepath.Join(home, strings.TrimPrefix(strings.TrimPrefix(p, "~"), "/"))
	}
	return filepath.Clean(p), nil
}
