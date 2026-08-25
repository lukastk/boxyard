// Package models holds boxyard's core data model: box metadata, the yard-wide
// registry, and sync records.
//
// Ported from src/boxyard/_models.py.
package models

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/pelletier/go-toml/v2"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/naming"
	"github.com/lukastk/boxyard/internal/strict"
)

// BoxMeta describes one box.
//
// Only four of these fields live in boxmeta.toml. The other three —
// creation_timestamp_utc, box_subid and name — are derived from the directory
// name the file sits in, so a box can be renamed by renaming its directory
// without rewriting its metadata. Save and Load encode that split.
type BoxMeta struct {
	CreationTimestampUTC string   `toml:"-" json:"creation_timestamp_utc"`
	BoxSubid             string   `toml:"-" json:"box_subid"`
	Name                 string   `toml:"-" json:"name"`
	StorageLocation      string   `toml:"storage_location" json:"storage_location"`
	CreatorHostname      string   `toml:"creator_hostname" json:"creator_hostname"`
	Groups               []string `toml:"groups" json:"groups"`
	// Parents holds box_id values. It defaults to empty for backwards
	// compatibility with boxmeta.toml files written before parents existed.
	Parents []string `toml:"parents" json:"parents"`

	// WriteOwner names the single machine allowed to PUSH this box's DATA.
	// Empty means unowned, and an unowned box's boxmeta.toml omits the key
	// entirely rather than writing "" or a null — that is what keeps every
	// pre-0.5 file byte-identical across the upgrade.
	//
	// Inert in this release, exactly as in Python v0.5.0: nothing writes it
	// and nothing enforces it. Reading it is what matters here.
	WriteOwner string `toml:"write_owner,omitempty" json:"write_owner,omitempty"`

	// UnknownKeys holds keys a NEWER boxyard wrote that this build does not
	// know. They are preserved rather than rejected.
	//
	// This is not politeness, it is the difference between a box syncing and
	// silently disappearing. Rejecting the file does not merely fail to parse
	// it: the registration is skipped, so the box vanishes from
	// boxyard_meta.json, from `boxyard list`, from ~/g (its symlinks are
	// deleted) and from multi-sync — with no error, and it does not heal
	// after upgrading. Python learned this in v0.5.0; this port must not
	// reintroduce it.
	UnknownKeys map[string]any `toml:"-" json:"-"`
}

// normalizeSlices replaces nil slices with empty ones. Go marshals a nil slice
// as `null` where pydantic emits `[]`, and boxyard_meta.json is read by
// mysystem's TypeScript BoxyardService as well as by both implementations.
func (b *BoxMeta) NormalizeSlices() {
	if b.Groups == nil {
		b.Groups = []string{}
	}
	if b.Parents == nil {
		b.Parents = []string{}
	}
}

// BoxID is "{creation_timestamp_utc}_{box_subid}".
func (b *BoxMeta) BoxID() string {
	return b.CreationTimestampUTC + "_" + b.BoxSubid
}

// IndexName is "{box_id}__{name}" — the unique identifier used as a directory
// name everywhere.
func (b *BoxMeta) IndexName() string {
	return b.BoxID() + "__" + b.Name
}

// CreationTimestamp parses the box's creation timestamp. Boxes exist in both
// formats: date-only ("20260601") and the legacy date-and-time
// ("20250622_000000").
func (b *BoxMeta) CreationTimestamp() (time.Time, error) {
	layout := boxconst.BoxTimestampFormatDateOnly
	if strings.Contains(b.CreationTimestampUTC, "_") {
		layout = boxconst.BoxTimestampFormat
	}
	t, err := time.Parse(layout, b.CreationTimestampUTC)
	if err != nil {
		return time.Time{}, fmt.Errorf("creation timestamp %q is not valid: %w", b.CreationTimestampUTC, err)
	}
	return t, nil
}

// ParseIndexName splits an index name into its box id and name.
func ParseIndexName(indexName string) (boxID, name string, err error) {
	parts := strings.SplitN(indexName, "__", 2)
	if len(parts) != 2 {
		return "", "", fmt.Errorf("invalid index_name format: %s", indexName)
	}
	return parts[0], parts[1], nil
}

// ExtractBoxID returns just the box id from an index name.
func ExtractBoxID(indexName string) (string, error) {
	id, _, err := ParseIndexName(indexName)
	return id, err
}

// splitBoxID splits a box id into its timestamp and subid. A box id has either
// two underscore-separated parts (date-only: "20260601_rh9q4r") or three
// (legacy date-and-time: "20250622_000000_aTrMF").
func splitBoxID(boxID string) (timestamp, subid string, err error) {
	parts := strings.Split(boxID, "_")
	switch len(parts) {
	case 3:
		return parts[0] + "_" + parts[1], parts[2], nil
	case 2:
		return parts[0], parts[1], nil
	default:
		return "", "", fmt.Errorf("invalid box id: %s", boxID)
	}
}

// Validate checks the invariants the Python model_validator enforces.
// writeOwnerRe is the machine-name shape Python validates write_owner against.
var writeOwnerRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

func (b *BoxMeta) Validate() error {
	const t = "BoxMeta"
	if err := strict.RequireNonZero(t, "storage_location", b.StorageLocation); err != nil {
		return err
	}
	if err := strict.RequireNonZero(t, "creator_hostname", b.CreatorHostname); err != nil {
		return err
	}
	// creator_hostname is the only free-text field written to boxmeta.toml —
	// it comes from `scutil --get ComputerName` or the POSIX hostname, so its
	// content is not otherwise constrained.
	//
	// Control characters are rejected. This began as a guard against Python's
	// toml 0.10.2, which silently CORRUPTED them rather than escaping. That
	// library has since been replaced by tomli_w, which handles them
	// correctly, but the rule is kept on its own merits: a control character
	// in a machine name is meaningless and would corrupt the output of
	// `boxyard list` and `doctor`, which print this value. Python enforces the
	// same rule.
	for _, r := range b.CreatorHostname {
		if r < 0x20 || r == 0x7f {
			return strict.Invalid(t, "creator_hostname",
				fmt.Sprintf("must not contain control characters, but contains %q: %q", r, b.CreatorHostname))
		}
	}
	// Same shape Python validates: a machine name, not free text. Empty is
	// legitimate and means unowned.
	if b.WriteOwner != "" && !writeOwnerRe.MatchString(b.WriteOwner) {
		return strict.Invalid(t, "write_owner",
			fmt.Sprintf("must be 1-64 characters of [A-Za-z0-9_-], got %q", b.WriteOwner))
	}
	if b.Groups == nil {
		return strict.Missing(t, "groups")
	}
	if dup := firstDuplicate(b.Groups); dup != "" {
		return strict.Invalid(t, "groups", "Groups must be unique.")
	}
	for _, g := range b.Groups {
		if err := naming.ValidateGroupName(g); err != nil {
			return err
		}
	}
	if dup := firstDuplicate(b.Parents); dup != "" {
		return strict.Invalid(t, "parents", "Parents must be unique.")
	}
	for _, p := range b.Parents {
		if p == b.BoxID() {
			return strict.Invalid(t, "parents", "A box cannot be its own parent.")
		}
	}
	if _, err := b.CreationTimestamp(); err != nil {
		return strict.Invalid(t, "creation_timestamp_utc", "Creation timestamp is not valid.")
	}
	return nil
}

func firstDuplicate(xs []string) string {
	seen := make(map[string]bool, len(xs))
	for _, x := range xs {
		if seen[x] {
			return x
		}
		seen[x] = true
	}
	return ""
}

// --- paths ---
//
// Remote paths use forward slashes unconditionally: they are rclone paths, not
// filesystem paths, and are joined with path rather than filepath so they stay
// correct regardless of the host OS.

func (b *BoxMeta) StorageLocationConfig(cfg *config.Config) (*config.StorageConfig, error) {
	return cfg.StorageLocation(b.StorageLocation)
}

func (b *BoxMeta) RemotePath(cfg *config.Config) (string, error) {
	sl, err := b.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	return path.Join(sl.StorePath, boxconst.RemoteBoxesRelPath, b.IndexName()), nil
}

func (b *BoxMeta) LocalPath(cfg *config.Config) string {
	return filepath.Join(cfg.LocalStorePath(), b.StorageLocation, b.IndexName())
}

func (b *BoxMeta) RemotePartPath(cfg *config.Config, part enums.BoxPart) (string, error) {
	root, err := b.RemotePath(cfg)
	if err != nil {
		return "", err
	}
	switch part {
	case enums.PartData:
		return path.Join(root, boxconst.BoxDataRelPath), nil
	case enums.PartMeta:
		return path.Join(root, boxconst.BoxMetafileRelPath), nil
	case enums.PartConf:
		return path.Join(root, boxconst.BoxConfRelPath), nil
	default:
		return "", fmt.Errorf("invalid box part: %s", part)
	}
}

// LocalPartPath returns where a part lives locally. Note DATA is the odd one
// out: it lives in the user's boxes directory (~/dev), not in the local store.
func (b *BoxMeta) LocalPartPath(cfg *config.Config, part enums.BoxPart) (string, error) {
	switch part {
	case enums.PartData:
		return filepath.Join(cfg.UserBoxesPath, b.IndexName()), nil
	case enums.PartMeta:
		return filepath.Join(b.LocalPath(cfg), boxconst.BoxMetafileRelPath), nil
	case enums.PartConf:
		return filepath.Join(b.LocalPath(cfg), boxconst.BoxConfRelPath), nil
	default:
		return "", fmt.Errorf("invalid box part: %s", part)
	}
}

func (b *BoxMeta) RemoteSyncRecordPath(cfg *config.Config, part enums.BoxPart) (string, error) {
	sl, err := b.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	return path.Join(sl.StorePath, boxconst.SyncRecordsRelPath, b.IndexName(), string(part)+".rec"), nil
}

func (b *BoxMeta) LocalSyncRecordPath(cfg *config.Config, part enums.BoxPart) string {
	return filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, b.IndexName(), string(part)+".rec")
}

// CheckIncluded reports whether the box's data is checked out on this machine.
func (b *BoxMeta) CheckIncluded(cfg *config.Config) bool {
	p, err := b.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		return false
	}
	fi, err := os.Stat(p)
	return err == nil && fi.IsDir()
}

// --- persistence ---

// tomlEscape escapes a string for a TOML basic string, matching the output of
// Python's tomli_w.
//
// Note tomli_w leaves a literal TAB in place (TOML permits it in basic
// strings) and uses LOWERCASE hex in \uXXXX escapes. Control characters cannot
// actually reach here — Validate rejects them — but the escaping is
// implemented faithfully so the two implementations cannot drift if that ever
// changes.
func tomlEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '"':
			b.WriteString(`\"`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			// Literal, as tomli_w writes it.
			b.WriteRune(r)
		default:
			if r < 0x20 || r == 0x7f {
				b.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else {
				// Everything else, including non-ASCII, is written literally.
				b.WriteRune(r)
			}
		}
	}
	return b.String()
}

// tomlList renders a string list the way Python's tomli_w does: `[]` when
// empty, otherwise one element per line at four-space indent, each with a
// trailing comma.
//
// boxmeta.toml is the META part of every box and is synced to a shared remote,
// so this is a cross-implementation contract, verified differentially against
// tomli_w rather than by inspection.
func tomlList(xs []string) string {
	if len(xs) == 0 {
		return "[]"
	}
	var b strings.Builder
	b.WriteString("[\n")
	for _, x := range xs {
		b.WriteString(`    "`)
		b.WriteString(tomlEscape(x))
		b.WriteString("\",\n")
	}
	b.WriteString("]")
	return b.String()
}

// Render returns the boxmeta.toml body for this box.
//
// Field order matches the Python model's declaration order with the three
// directory-derived fields removed.
func (b *BoxMeta) Render() string {
	var s strings.Builder
	fmt.Fprintf(&s, "storage_location = \"%s\"\n", tomlEscape(b.StorageLocation))
	fmt.Fprintf(&s, "creator_hostname = \"%s\"\n", tomlEscape(b.CreatorHostname))
	fmt.Fprintf(&s, "groups = %s\n", tomlList(b.Groups))
	fmt.Fprintf(&s, "parents = %s\n", tomlList(b.Parents))
	// An UNOWNED box omits the key entirely — not `write_owner = ""`, not a
	// null. That is what keeps every pre-0.5 boxmeta.toml byte-identical, and
	// what lets an older boxyard still read a released box.
	if b.WriteOwner != "" {
		fmt.Fprintf(&s, "write_owner = \"%s\"\n", tomlEscape(b.WriteOwner))
	}
	return s.String()
}

// Save writes boxmeta.toml atomically (temp file + rename), as the Python does.
func (b *BoxMeta) Save(cfg *config.Config) error {
	// Validate before writing: Render cannot represent every string faithfully
	// (see the control-character note in Validate), so an invalid BoxMeta must
	// never reach disk.
	if err := b.Validate(); err != nil {
		return err
	}
	// Python v0.5.0 writes carried keys back verbatim. This port does not
	// implement that yet, and the failure mode of getting it wrong is silent
	// data loss — the newer machine's key would be stripped by an ordinary
	// `add-to-group`, which is the exact loss the passthrough exists to
	// prevent. So refuse loudly instead of writing a lossy file.
	//
	// Reading such a box already works, which is the half that stops a box
	// disappearing. Writing one is blocked until Render can reproduce
	// tomli_w's output for arbitrary values.
	if len(b.UnknownKeys) > 0 {
		keys := make([]string, 0, len(b.UnknownKeys))
		for k := range b.UnknownKeys {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		return fmt.Errorf("refusing to write %s: it carries key(s) written by a newer boxyard (%s) that this build cannot reproduce faithfully; writing would silently discard them. Use the Python boxyard for this box, or upgrade",
			b.IndexName(), strings.Join(keys, ", "))
	}
	savePath, err := b.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(savePath), 0o755); err != nil {
		return err
	}
	// Python uses Path.with_suffix(".tmp"), which turns boxmeta.toml into
	// boxmeta.tmp — same directory, so the rename stays on one filesystem.
	tmpPath := strings.TrimSuffix(savePath, filepath.Ext(savePath)) + ".tmp"
	if err := os.WriteFile(tmpPath, []byte(b.Render()), 0o644); err != nil {
		return err
	}
	return os.Rename(tmpPath, savePath)
}

// boxMetaFile is the on-disk half of a BoxMeta — the fields that actually live
// in boxmeta.toml.
type boxMetaFile struct {
	StorageLocation string   `toml:"storage_location"`
	CreatorHostname string   `toml:"creator_hostname"`
	Groups          []string `toml:"groups"`
	Parents         []string `toml:"parents"`
	WriteOwner      string   `toml:"write_owner"`
}

// boxMetaKnownKeys is every key this build owns. A key outside this set is
// carried through untouched; see BoxMeta.UnknownKeys.
var boxMetaKnownKeys = map[string]bool{
	"storage_location": true,
	"creator_hostname": true,
	"groups":           true,
	"parents":          true,
	"write_owner":      true,
}

// readBoxMetaFile decodes boxmeta.toml into `file` and returns the keys this
// build does not know. Unlike strict.ReadTOMLFile it does not reject them.
func readBoxMetaFile(path string, file *boxMetaFile) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	// Decoded twice on purpose: once into the typed struct so field types are
	// still enforced, once into a map to see which keys were actually present.
	if err := toml.Unmarshal(data, file); err != nil {
		return nil, fmt.Errorf("invalid TOML in %s: %w", path, err)
	}
	var all map[string]any
	if err := toml.Unmarshal(data, &all); err != nil {
		return nil, fmt.Errorf("invalid TOML in %s: %w", path, err)
	}
	unknown := map[string]any{}
	for k, v := range all {
		if !boxMetaKnownKeys[k] {
			unknown[k] = v
		}
	}
	if len(unknown) == 0 {
		return nil, nil
	}
	return unknown, nil
}

// LoadBoxMeta reads a box's metadata, deriving the timestamp, subid and name
// from its directory name and the storage location from its parent directory.
func LoadBoxMeta(cfg *config.Config, storageLocationName, boxIndexName string) (*BoxMeta, error) {
	boxID, name, err := ParseIndexName(boxIndexName)
	if err != nil {
		return nil, err
	}
	timestamp, subid, err := splitBoxID(boxID)
	if err != nil {
		return nil, err
	}

	metaPath := filepath.Join(cfg.LocalStorePath(), storageLocationName, boxIndexName, boxconst.BoxMetafileRelPath)
	if _, err := os.Stat(metaPath); err != nil {
		return nil, fmt.Errorf("box meta file %s does not exist", metaPath)
	}

	// NOT a strict decode. `strict.ReadTOMLFile` rejects unknown keys, which
	// for THIS file is the failure described on BoxMeta.UnknownKeys: the box
	// disappears instead of syncing. Decode permissively, then split the keys
	// into the ones this build knows and the ones it must carry through.
	var file boxMetaFile
	raw, err := readBoxMetaFile(metaPath, &file)
	if err != nil {
		return nil, err
	}
	if file.Parents == nil {
		// Absent `parents` is a legitimate expected state: boxmeta.toml files
		// written before parents existed have no such key.
		file.Parents = []string{}
	}

	bm := &BoxMeta{
		CreationTimestampUTC: timestamp,
		BoxSubid:             subid,
		Name:                 name,
		// The directory the file sits in is authoritative for the storage
		// location, overriding whatever the file says.
		StorageLocation: storageLocationName,
		CreatorHostname: file.CreatorHostname,
		Groups:          file.Groups,
		Parents:         file.Parents,
		WriteOwner:      file.WriteOwner,
		UnknownKeys:     raw,
	}
	if bm.Groups == nil {
		bm.Groups = []string{}
	}
	if err := bm.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", metaPath, err)
	}
	return bm, nil
}

// IndexNameFromSubPath returns the index name of the box containing subPath,
// or "" if the path is not inside a box.
//
// The path is resolved first, so a symlink into a box — which is how the whole
// group tree under ~/g works — is recognised.
func IndexNameFromSubPath(cfg *config.Config, subPath string) (string, error) {
	p, err := config.ExpandUser(subPath)
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(p)
	if err != nil {
		// A path that does not exist cannot be inside a box.
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	root, err := filepath.EvalSymlinks(cfg.UserBoxesPath)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	if resolved == root {
		// In the boxes root, but not inside any box.
		return "", nil
	}
	rel, err := filepath.Rel(root, resolved)
	if err != nil || rel == "." || strings.HasPrefix(rel, "..") {
		return "", nil
	}
	return strings.SplitN(rel, string(filepath.Separator), 2)[0], nil
}

// SortByCreation orders boxes oldest-first, the order the symlink builder uses.
func SortByCreation(metas []*BoxMeta) {
	sort.SliceStable(metas, func(i, j int) bool {
		ti, erri := metas[i].CreationTimestamp()
		tj, errj := metas[j].CreationTimestamp()
		if erri != nil || errj != nil {
			return metas[i].CreationTimestampUTC < metas[j].CreationTimestampUTC
		}
		return ti.Before(tj)
	})
}

// boxMetaJSON is the boxyard_meta.json representation, which differs from
// boxmeta.toml's in two ways that matter:
//
//   - write_owner is ALWAYS present, and null when the box is unowned. The TOML
//     file omits the key entirely instead, which is what keeps every pre-0.5
//     boxmeta.toml byte-identical — but pydantic dumps every model field, so
//     the JSON carries an explicit null. 318 of the 586 boxes on this fleet are
//     unowned, so getting this wrong rewrites more than half the registry.
//   - unknown_keys is ALWAYS present, and {} when there are none. Python has
//     emitted it since v0.5.0.
//
// Both were missing from the Go struct's JSON, which made EVERY Go command that
// reads the registry fail on the real yard with `unknown field "unknown_keys"`.
// The round-trip test did not catch it because its golden fixture predates
// v0.5.0 — exactly the frozen-fixture trap the status doc warns about.
type boxMetaJSON struct {
	CreationTimestampUTC string         `json:"creation_timestamp_utc"`
	BoxSubid             string         `json:"box_subid"`
	Name                 string         `json:"name"`
	StorageLocation      string         `json:"storage_location"`
	CreatorHostname      string         `json:"creator_hostname"`
	Groups               []string       `json:"groups"`
	Parents              []string       `json:"parents"`
	WriteOwner           *string        `json:"write_owner"`
	UnknownKeys          map[string]any `json:"unknown_keys"`
}

// MarshalJSON renders the box the way pydantic's model_dump_json does.
func (b *BoxMeta) MarshalJSON() ([]byte, error) {
	out := boxMetaJSON{
		CreationTimestampUTC: b.CreationTimestampUTC,
		BoxSubid:             b.BoxSubid,
		Name:                 b.Name,
		StorageLocation:      b.StorageLocation,
		CreatorHostname:      b.CreatorHostname,
		Groups:               b.Groups,
		Parents:              b.Parents,
		UnknownKeys:          b.UnknownKeys,
	}
	if out.Groups == nil {
		out.Groups = []string{}
	}
	if out.Parents == nil {
		out.Parents = []string{}
	}
	if out.UnknownKeys == nil {
		// A nil map marshals as null; pydantic emits {}.
		out.UnknownKeys = map[string]any{}
	}
	if b.WriteOwner != "" {
		owner := b.WriteOwner
		out.WriteOwner = &owner
	}
	return json.Marshal(out)
}

// UnmarshalJSON parses a registry entry written by either implementation.
//
// Unknown fields are still rejected: a key nobody knows in boxyard_meta.json is
// a REGENERATABLE cache, not shared state, so a loud failure is the right
// answer — unlike boxmeta.toml, where rejecting would make a box disappear.
func (b *BoxMeta) UnmarshalJSON(data []byte) error {
	var in boxMetaJSON
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&in); err != nil {
		return err
	}
	b.CreationTimestampUTC = in.CreationTimestampUTC
	b.BoxSubid = in.BoxSubid
	b.Name = in.Name
	b.StorageLocation = in.StorageLocation
	b.CreatorHostname = in.CreatorHostname
	b.Groups = in.Groups
	b.Parents = in.Parents
	b.UnknownKeys = in.UnknownKeys
	b.WriteOwner = ""
	if in.WriteOwner != nil {
		b.WriteOwner = *in.WriteOwner
	}
	return nil
}
