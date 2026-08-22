// Package models holds boxyard's core data model: box metadata, the yard-wide
// registry, and sync records.
//
// Ported from src/boxyard/_models.py.
package models

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

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
	CreationTimestampUTC string   `toml:"-"`
	BoxSubid             string   `toml:"-"`
	Name                 string   `toml:"-"`
	StorageLocation      string   `toml:"storage_location"`
	CreatorHostname      string   `toml:"creator_hostname"`
	Groups               []string `toml:"groups"`
	// Parents holds box_id values. It defaults to empty for backwards
	// compatibility with boxmeta.toml files written before parents existed.
	Parents []string `toml:"parents"`
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
	// Control characters are rejected because Python's toml 0.10.2 SILENTLY
	// CORRUPTS them rather than escaping: dumping "del\x7fchar" produces
	// `"delx7fchar"`, which round-trips back as the wrong string with no error.
	// Since boxmeta.toml is synced to a shared remote, a quietly-wrong value is
	// the worst outcome. Both implementations now refuse it.
	for _, r := range b.CreatorHostname {
		if r < 0x20 || r == 0x7f {
			return strict.Invalid(t, "creator_hostname",
				fmt.Sprintf("must not contain control characters, but contains %q: %q", r, b.CreatorHostname))
		}
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

// tomlEscape escapes a string for a TOML basic string, matching Python's toml
// library.
func tomlEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '\\':
			b.WriteString(`\\`)
		case '"':
			b.WriteString(`\"`)
		case '\b':
			b.WriteString(`\b`)
		case '\t':
			b.WriteString(`\t`)
		case '\n':
			b.WriteString(`\n`)
		case '\f':
			b.WriteString(`\f`)
		case '\r':
			b.WriteString(`\r`)
		default:
			if r < 0x20 || r == 0x7f {
				b.WriteString(fmt.Sprintf(`\u%04X`, r))
			} else {
				// Everything else, including non-ASCII, is written literally —
				// Python's toml library does not \u-escape it.
				b.WriteRune(r)
			}
		}
	}
	return b.String()
}

// tomlList renders a string list the way Python's toml library does:
// `[]` when empty, otherwise `[ "a", "b",]` — leading space, and a trailing
// comma before the bracket.
//
// Matching this exactly is not cosmetic. boxmeta.toml is the META part of every
// box and is synced to a shared remote. If Go rewrote these files in a
// different style, all 583 boxes would show as modified the first time a Go
// boxyard touched them, and each would push a spurious META sync.
func tomlList(xs []string) string {
	if len(xs) == 0 {
		return "[]"
	}
	var b strings.Builder
	b.WriteString("[")
	for _, x := range xs {
		b.WriteString(` "`)
		b.WriteString(tomlEscape(x))
		b.WriteString(`",`)
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

	var file boxMetaFile
	if err := strict.ReadTOMLFile(metaPath, &file); err != nil {
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
	}
	if bm.Groups == nil {
		bm.Groups = []string{}
	}
	if err := bm.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", metaPath, err)
	}
	return bm, nil
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
