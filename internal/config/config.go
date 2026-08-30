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
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
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

// intervalUnits maps an interval suffix to its length in seconds.
var intervalUnits = map[byte]int{
	's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800,
}

// ParseInterval turns a cadence like "6h", "90m" or "7d" into whole seconds.
//
// `where` names the config location, so a bad value says which key is wrong
// rather than only that something is.
//
// Deliberately strict, matching the Python: no bare numbers (is "6" six seconds
// or six hours?), no compound forms ("1h30m"), no floats. A cadence that
// silently means something other than what was written is worse than a
// refusal, and every real cadence in this design is one unit.
func ParseInterval(text, where string) (int, error) {
	raw := strings.TrimSpace(text)
	if raw == "" {
		return 0, fmt.Errorf("%s: interval is empty", where)
	}
	unit := raw[len(raw)-1]
	if 'A' <= unit && unit <= 'Z' {
		unit += 'a' - 'A'
	}
	seconds, ok := intervalUnits[unit]
	if !ok {
		return 0, fmt.Errorf(
			"%s: interval %q must end in one of d/h/m/s/w (e.g. '6h', '90m', '7d')",
			where, text)
	}
	number := raw[:len(raw)-1]
	if number == "" {
		return 0, fmt.Errorf(
			"%s: interval %q must be a whole number followed by a unit (e.g. '6h'); got %q before %q",
			where, text, number, string(unit))
	}
	for i := 0; i < len(number); i++ {
		if number[i] < '0' || number[i] > '9' {
			return 0, fmt.Errorf(
				"%s: interval %q must be a whole number followed by a unit (e.g. '6h'); got %q before %q",
				where, text, number, string(unit))
		}
	}
	n, err := strconv.Atoi(number)
	if err != nil {
		return 0, fmt.Errorf("%s: interval %q: %w", where, text, err)
	}
	total := n * seconds
	if total <= 0 {
		return 0, fmt.Errorf("%s: interval %q must be greater than zero", where, text)
	}
	return total, nil
}

// SyncPolicyConfig is one named sync policy: how often a box's parts are
// checked, and whether its DATA is stored packed.
//
// Every field is OPTIONAL and unset means "not stated at this level" rather
// than "off" — that is what makes resolution work per DIMENSION, so a box can
// take its DATA cadence from conf/sync.toml and its META cadence from the group
// policy.
//
// There is deliberately NO Compress field. Compression is a property of the
// storage BACKEND, not a scheduling policy: a restic-backed box is compressed
// and deduplicated because that is what the backend does, and no per-box knob
// would change it. Measured on jackfruit-hq: 687,876 remote objects -> 56,
// 7.52 GiB -> 0.72 GiB. A choice of BACKEND may well be wanted, but that is
// StorageFormat, a different field answering a different question, and it
// belongs with the code that can honour it.
type SyncPolicyConfig struct {
	DataInterval string   `toml:"data_interval"`
	MetaInterval string   `toml:"meta_interval"`
	Groups       []string `toml:"groups"`
}

// IntervalSeconds returns the parsed cadence for a part, and whether one was
// stated at all.
func (p *SyncPolicyConfig) IntervalSeconds(part, policyName string) (int, bool, error) {
	text := p.DataInterval
	if part == "meta" {
		text = p.MetaInterval
	}
	if text == "" {
		return 0, false, nil
	}
	seconds, err := ParseInterval(text, fmt.Sprintf("sync_policies.%s.%s_interval", policyName, part))
	if err != nil {
		return 0, false, err
	}
	return seconds, true, nil
}

// validate refuses a policy this build cannot honour.
//

// TODO(cleanup): drop this check — when packing is actually implemented, or
// when the decision not to implement it is final and the field is removed.
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
	SyncPolicies           map[string]*SyncPolicyConfig      `toml:"sync_policies"`
	DefaultBoxGroups       []string                          `toml:"default_box_groups"`
	BoxSubidCharacterSet   string                            `toml:"box_subid_character_set"`
	BoxSubidLength         int                               `toml:"box_subid_length"`
	MaxConcurrentRcloneOps int                               `toml:"max_concurrent_rclone_ops"`

	// Optional.

	// MachineName is this machine's canonical short name — the one myrig
	// assigns, never the hostname, which is unusable as an identity (it has
	// produced both "lukas-pocket4" and "pocket4" for one machine, and macOS
	// pretty names like "Tom's Mac Studio"). Box write-ownership identifies a
	// machine by this and nothing else.
	MachineName string `toml:"machine_name"`

	RclonePath       string `toml:"rclone_path"`
	SingleParent     bool   `toml:"single_parent"`
	SyncBeforeNewBox bool   `toml:"sync_before_new_box"`

	// MergeDivergedBoxmetas turns on the three-way merge for a boxmeta BOTH
	// sides have edited, against the copy they last agreed on. `groups` and
	// `parents` merge as sets; a scalar both sides changed differently is
	// still a refusal for a human to settle.
	//
	// OFF by default, and deliberately so. Resolving a merge means
	// force-pushing the result over the remote boxmeta — safe, because the
	// merge CONTAINS what the remote had, but still a write today's code would
	// refuse to make. That is a decision to take per fleet rather than one
	// that arrives with an upgrade.
	MergeDivergedBoxmetas bool `toml:"merge_diverged_boxmetas"`

	// UnknownKeys holds keys a NEWER boxyard wrote, by dotted path (e.g.
	// "storage_locations.hetzner-box.some_key"). Never written back — boxyard
	// only ever creates config.toml, at init — so these exist purely to be
	// reported.
	//
	// Rejecting them instead would break EVERY boxyard command on any machine
	// running an older build, all at once: config.toml is a single myrig
	// artefact shared by every machine, so one added key breaks the fleet
	// rather than one box. Python learned this in v0.5.0.
	UnknownKeys map[string]any `toml:"-"`
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
	// An explicit override, for a one-off or a machine whose config predates
	// the myrig template.
	if env := os.Getenv(boxconst.EnvBoxyardMachineName); env != "" {
		cfg.MachineName = env
	}
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", expanded, err)
	}
	return cfg, nil
}

// decodeInto decodes without validating, so Load can apply the environment
// merge between decode and validation — the order the Python uses.
func decodeInto(data []byte, cfg *Config) error {
	// NOT DisallowUnknownFields. See Config.UnknownKeys: config.toml is one
	// myrig artefact shared by every machine, so rejecting an unknown key
	// breaks every boxyard command on every machine still running an older
	// build — all at once, rather than one box at a time.
	if err := toml.Unmarshal(data, cfg); err != nil {
		return fmt.Errorf("invalid TOML: %w", err)
	}
	// Decoded a second time as a plain map to see which keys were actually
	// present. A key that is not ours is carried for reporting.
	var raw map[string]any
	if err := toml.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("invalid TOML: %w", err)
	}
	unknown := collectUnknownKeys(raw, reflect.TypeOf(Config{}), "")
	if len(unknown) > 0 {
		cfg.UnknownKeys = unknown
	}

	// An EMPTY table decodes to a nil map, which is indistinguishable from an
	// absent one — but Validate must tell them apart, because Python does: a
	// missing `box_groups` is a validation error while `[box_groups]` with no
	// entries is the normal state of a fresh boxyard. `boxyard init` writes
	// exactly that, so without this the Go loader REJECTS the very config
	// Python's init produces.
	//
	// The raw decode still knows which keys were present, so normalise
	// present-but-empty to an allocated empty map here and leave Validate's
	// nil check meaning purely "absent".
	if _, ok := raw["storage_locations"]; ok && cfg.StorageLocations == nil {
		cfg.StorageLocations = map[string]*StorageConfig{}
	}
	if _, ok := raw["box_groups"]; ok && cfg.BoxGroups == nil {
		cfg.BoxGroups = map[string]*BoxGroupConfig{}
	}
	if _, ok := raw["virtual_box_groups"]; ok && cfg.VirtualBoxGroups == nil {
		cfg.VirtualBoxGroups = map[string]*VirtualBoxGroupConfig{}
	}
	if _, ok := raw["default_box_groups"]; ok && cfg.DefaultBoxGroups == nil {
		cfg.DefaultBoxGroups = []string{}
	}
	return nil
}

// tomlKeys returns the TOML key names a struct type owns.
func tomlKeys(t reflect.Type) map[string]reflect.Type {
	keys := map[string]reflect.Type{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		tag := f.Tag.Get("toml")
		if tag == "" || tag == "-" {
			continue
		}
		if c := strings.Index(tag, ","); c >= 0 {
			tag = tag[:c]
		}
		keys[tag] = f.Type
	}
	return keys
}

// collectUnknownKeys returns every key in `raw` that `t` does not declare, by
// dotted path, descending into the entries of every map[string]*Struct table.
//
// The tables are derived from the struct's own fields rather than a hardcoded
// list. That is the point: a config model added later is covered without
// anyone remembering this function exists, and forgetting to extend a list
// would silently reintroduce the gap somewhere no test obviously covers.
func collectUnknownKeys(raw map[string]any, t reflect.Type, prefix string) map[string]any {
	unknown := map[string]any{}
	known := tomlKeys(t)
	for k, v := range raw {
		ft, isKnown := known[k]
		if !isKnown {
			unknown[prefix+k] = v
			continue
		}
		// A table of named entries — descend into each entry, but only when
		// the value really is a table. An entry that is NOT a table is left
		// alone so the typed decode reports it: that is not a newer boxyard
		// adding an option, it is a key someone believed they had put at top
		// level, and swallowing it would silently discard their edit.
		if ft.Kind() != reflect.Map || ft.Key().Kind() != reflect.String {
			continue
		}
		elem := ft.Elem()
		for elem.Kind() == reflect.Ptr {
			elem = elem.Elem()
		}
		if elem.Kind() != reflect.Struct {
			continue
		}
		entries, ok := v.(map[string]any)
		if !ok {
			continue
		}
		for name, entry := range entries {
			sub, ok := entry.(map[string]any)
			if !ok {
				continue
			}
			for kk, vv := range collectUnknownKeys(sub, elem, "") {
				unknown[prefix+k+"."+name+"."+kk] = vv
			}
		}
	}
	return unknown
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
