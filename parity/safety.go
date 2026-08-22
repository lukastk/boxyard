// Package parity holds the cross-implementation parity harness that proves the
// Go boxyard behaves identically to the Python one.
//
// This package is TEMPORARY. It exists only for the duration of the Go rewrite
// and is deleted at cutover, once the Go implementation is trusted. It is
// deliberately kept out of internal/ so that deleting it is a single `rm -rf`
// with no untangling.
//
// SAFETY IS THIS FILE'S ONLY JOB.
//
// The parity suite runs real boxyard commands against a real SFTP remote. The
// user's actual boxyard — 583 boxes in ~/dev, backed by hetzner-box:boxyard —
// must never be touched. Every guard here FAILS CLOSED: if it cannot prove the
// target is a sandbox, it refuses to run.
//
// The forbidden paths are read from the user's live config at check time rather
// than hardcoded, so the guard keeps working if they move ~/dev or add a
// storage location.
package parity

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

// RequiredRemotePrefix is the only remote path prefix the parity suite may
// write to. The real yard lives at "boxyard/" on the same remote; this prefix
// is deliberately a sibling, not a child, so no path-joining slip can land
// inside real data.
const RequiredRemotePrefix = "boxyard-gotest/"

// RealConfigPath is the user's live boxyard config, used only to derive the
// set of paths the sandbox must not overlap. Never modified.
const RealConfigPath = "~/.config/boxyard/config.toml"

// Sandbox is an isolated boxyard installation created for one parity run.
type Sandbox struct {
	Root           string // local sandbox root; every local path lives under this
	ConfigPath     string // path to the sandbox's config.toml
	DataPath       string // boxyard_data_path
	UserBoxesPath  string // user_boxes_path
	UserGroupsPath string // user_box_groups_path
	RemotePrefix   string // EFFECTIVE full remote path, e.g. "boxyard-gotest/run-a1b2c3"
	RcloneConfig   string // path to the sandbox's own generated rclone config

	// storePath is the store_path written into the sandbox config. It is
	// relative to the alias remote's root, so RemotePrefix is always
	// RequiredRemotePrefix + storePath. See sandbox.go.
	storePath string

	// isolationVerified records that a binary was OBSERVED reading this
	// sandbox rather than the real config. Run refuses until it is set. See
	// VerifyIsolation in harness.go for why config validation alone is not
	// sufficient.
	isolationVerified bool
}

// expand resolves a leading ~ and makes the path absolute and clean.
func expand(p string) (string, error) {
	if p == "" {
		return "", fmt.Errorf("empty path")
	}
	if p == "~" || strings.HasPrefix(p, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		p = filepath.Join(home, strings.TrimPrefix(strings.TrimPrefix(p, "~"), "/"))
	}
	abs, err := filepath.Abs(p)
	if err != nil {
		return "", err
	}
	return filepath.Clean(abs), nil
}

// isUnder reports whether child is parent or lies beneath it. Both are assumed
// already expanded. Uses a separator-terminated prefix so that "/a/bc" is not
// treated as being under "/a/b".
func isUnder(child, parent string) bool {
	if child == parent {
		return true
	}
	return strings.HasPrefix(child, parent+string(filepath.Separator))
}

// realConfigPaths reads the user's live config and returns every local path it
// owns, plus every remote store_path it uses. These are the no-go zones.
func realConfigPaths() (local []string, remote []string, err error) {
	cfgPath, err := expand(RealConfigPath)
	if err != nil {
		return nil, nil, err
	}
	raw, err := os.ReadFile(cfgPath)
	if err != nil {
		// No live config is not a reason to relax the guard — it is a reason to
		// stop, because we can no longer prove what we must avoid.
		return nil, nil, fmt.Errorf("cannot read real config at %s to derive forbidden paths: %w", cfgPath, err)
	}
	var cfg struct {
		BoxyardDataPath   string `toml:"boxyard_data_path"`
		UserBoxesPath     string `toml:"user_boxes_path"`
		UserBoxGroupsPath string `toml:"user_box_groups_path"`
		StorageLocations  map[string]struct {
			StorageType string `toml:"storage_type"`
			StorePath   string `toml:"store_path"`
		} `toml:"storage_locations"`
	}
	if err := toml.Unmarshal(raw, &cfg); err != nil {
		return nil, nil, fmt.Errorf("cannot parse real config at %s: %w", cfgPath, err)
	}

	for _, p := range []string{cfg.BoxyardDataPath, cfg.UserBoxesPath, cfg.UserBoxGroupsPath} {
		if p == "" {
			continue
		}
		e, err := expand(p)
		if err != nil {
			return nil, nil, err
		}
		local = append(local, e)
	}
	// The live config itself, and its directory, are off limits for writes.
	local = append(local, filepath.Dir(cfgPath))

	for _, sl := range cfg.StorageLocations {
		if sl.StorePath == "" {
			continue
		}
		if sl.StorageType == "local" {
			e, err := expand(sl.StorePath)
			if err != nil {
				return nil, nil, err
			}
			local = append(local, e)
		} else {
			remote = append(remote, strings.Trim(sl.StorePath, "/"))
		}
	}
	return local, remote, nil
}

// AssertSandboxed refuses to proceed unless s is provably isolated from the
// user's real boxyard. Every failure is fatal and descriptive; there is no
// "warn and continue" path by design.
func AssertSandboxed(s *Sandbox) error {
	if s == nil {
		return fmt.Errorf("parity guard: nil sandbox")
	}

	root, err := expand(s.Root)
	if err != nil {
		return fmt.Errorf("parity guard: bad sandbox root %q: %w", s.Root, err)
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("parity guard: cannot resolve home: %w", err)
	}
	if root == home || root == "/" || root == filepath.Clean("/tmp") {
		return fmt.Errorf("parity guard: sandbox root %q is too broad", root)
	}

	forbiddenLocal, forbiddenRemote, err := realConfigPaths()
	if err != nil {
		return fmt.Errorf("parity guard: %w", err)
	}

	// 1. The sandbox root must not sit inside — or contain — any real path.
	for _, f := range forbiddenLocal {
		if isUnder(root, f) {
			return fmt.Errorf("parity guard: sandbox root %q is inside real boxyard path %q", root, f)
		}
		if isUnder(f, root) {
			return fmt.Errorf("parity guard: sandbox root %q contains real boxyard path %q", root, f)
		}
	}

	// 2. Every local path the sandbox uses must live under the sandbox root.
	named := map[string]string{
		"ConfigPath":     s.ConfigPath,
		"DataPath":       s.DataPath,
		"UserBoxesPath":  s.UserBoxesPath,
		"UserGroupsPath": s.UserGroupsPath,
	}
	for name, p := range named {
		if p == "" {
			return fmt.Errorf("parity guard: %s is empty", name)
		}
		e, err := expand(p)
		if err != nil {
			return fmt.Errorf("parity guard: bad %s %q: %w", name, p, err)
		}
		if !isUnder(e, root) {
			return fmt.Errorf("parity guard: %s %q is outside the sandbox root %q", name, e, root)
		}
		for _, f := range forbiddenLocal {
			if isUnder(e, f) || isUnder(f, e) {
				return fmt.Errorf("parity guard: %s %q collides with real boxyard path %q", name, e, f)
			}
		}
	}

	// 3. The remote prefix must be inside the dedicated test prefix.
	rp := strings.TrimPrefix(strings.Trim(s.RemotePrefix, "/")+"/", "/")
	if !strings.HasPrefix(rp, RequiredRemotePrefix) {
		return fmt.Errorf("parity guard: remote prefix %q must start with %q", s.RemotePrefix, RequiredRemotePrefix)
	}
	if strings.Contains(rp, "..") {
		return fmt.Errorf("parity guard: remote prefix %q contains %q", s.RemotePrefix, "..")
	}
	// 4. And must not collide with any real remote store_path.
	for _, f := range forbiddenRemote {
		fp := strings.Trim(f, "/") + "/"
		if strings.HasPrefix(rp, fp) || strings.HasPrefix(fp, rp) {
			return fmt.Errorf("parity guard: remote prefix %q collides with real remote store_path %q", s.RemotePrefix, f)
		}
	}

	return nil
}

// Canary captures the observable state of the user's real boxyard so a parity
// run can prove afterwards that it changed nothing.
//
// It compares CONTENT, not timestamps. The user's supervisor runs
// `boxyard multi-sync` continuously on this machine and rewrites
// boxyard_meta.json with byte-identical content on a 20-minute cycle, so an
// mtime-based canary false-positives constantly — and a canary that cries wolf
// is worse than none, because it trains you to ignore it.
//
// Directories are compared by their ENTRY SET rather than a count, so the
// report can name exactly what appeared or vanished. That specificity is the
// point: "a box named parity-probe appeared in ~/dev" is actionable, where
// "count went 117 -> 118" is a puzzle.
type Canary struct {
	files map[string]string   // path -> content hash, or a sentinel
	dirs  map[string][]string // path -> sorted entry names
	boxes map[string][]string // meta path -> sorted box index names
}

func canaryTargets() (files []string, dirs []string, err error) {
	local, _, err := realConfigPaths()
	if err != nil {
		return nil, nil, err
	}
	cfgPath, err := expand(RealConfigPath)
	if err != nil {
		return nil, nil, err
	}
	files = append(files, cfgPath)
	for _, p := range local {
		fi, err := os.Lstat(p)
		if err != nil {
			// A configured path that does not exist is still worth watching:
			// its sudden appearance would be an escape.
			files = append(files, p)
			continue
		}
		if fi.IsDir() {
			dirs = append(dirs, p)
			// The metadata file is the single most damage-revealing artifact.
			meta := filepath.Join(p, "boxyard_meta.json")
			if _, err := os.Stat(meta); err == nil {
				files = append(files, meta)
			}
		} else {
			files = append(files, p)
		}
	}
	return files, dirs, nil
}

// hashFile returns a content hash, or a sentinel for absent/unreadable paths.
func hashFile(path string) string {
	fi, err := os.Lstat(path)
	if err != nil {
		return "ABSENT"
	}
	if fi.IsDir() {
		return "IS-DIR"
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		target, err := os.Readlink(path)
		if err != nil {
			return "SYMLINK-UNREADABLE"
		}
		return "SYMLINK->" + target
	}
	f, err := os.Open(path)
	if err != nil {
		return "UNREADABLE"
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "UNREADABLE"
	}
	return hex.EncodeToString(h.Sum(nil))
}

func dirEntries(path string) []string {
	ents, err := os.ReadDir(path)
	if err != nil {
		return []string{"<unreadable>"}
	}
	names := make([]string, 0, len(ents))
	for _, e := range ents {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names
}

// boxNames reads the index names out of a boxyard_meta.json, so a change to it
// can be reported as "these boxes appeared/vanished".
func boxNames(metaPath string) []string {
	raw, err := os.ReadFile(metaPath)
	if err != nil {
		return nil
	}
	var meta struct {
		BoxMetas []struct {
			Timestamp string `json:"creation_timestamp_utc"`
			Subid     string `json:"box_subid"`
			Name      string `json:"name"`
		} `json:"box_metas"`
	}
	if json.Unmarshal(raw, &meta) != nil {
		return nil
	}
	names := make([]string, 0, len(meta.BoxMetas))
	for _, b := range meta.BoxMetas {
		names = append(names, b.Timestamp+"_"+b.Subid+"__"+b.Name)
	}
	sort.Strings(names)
	return names
}

// TakeCanary snapshots the real boxyard's observable state.
func TakeCanary() (*Canary, error) {
	files, dirs, err := canaryTargets()
	if err != nil {
		return nil, err
	}
	c := &Canary{
		files: map[string]string{},
		dirs:  map[string][]string{},
		boxes: map[string][]string{},
	}
	for _, f := range files {
		c.files[f] = hashFile(f)
		if strings.HasSuffix(f, "boxyard_meta.json") {
			c.boxes[f] = boxNames(f)
		}
	}
	for _, d := range dirs {
		c.dirs[d] = dirEntries(d)
	}
	return c, nil
}

// diffSets reports entries added to and removed from a sorted set.
func diffSets(before, after []string) (added, removed []string) {
	b := make(map[string]bool, len(before))
	for _, x := range before {
		b[x] = true
	}
	a := make(map[string]bool, len(after))
	for _, x := range after {
		a[x] = true
	}
	for x := range a {
		if !b[x] {
			added = append(added, x)
		}
	}
	for x := range b {
		if !a[x] {
			removed = append(removed, x)
		}
	}
	sort.Strings(added)
	sort.Strings(removed)
	return added, removed
}

// Verify re-reads every canary target and reports any drift. A non-nil error
// here means the parity run touched real data and must be treated as an
// incident, not a test failure.
func (c *Canary) Verify() error {
	if c == nil {
		return fmt.Errorf("parity guard: nil canary")
	}
	var drift []string

	for path, before := range c.dirs {
		added, removed := diffSets(before, dirEntries(path))
		if len(added) == 0 && len(removed) == 0 {
			continue
		}
		msg := "  " + path
		if len(added) > 0 {
			msg += "\n    APPEARED: " + strings.Join(added, ", ")
		}
		if len(removed) > 0 {
			msg += "\n    VANISHED: " + strings.Join(removed, ", ")
		}
		drift = append(drift, msg)
	}

	for path, before := range c.files {
		after := hashFile(path)
		if after == before {
			continue
		}
		msg := "  " + path + "\n    content changed"
		// For the metadata file, say WHICH boxes changed. The user's
		// supervisor legitimately rewrites this file, so an unexplained
		// content change with no box-set change is far less alarming than one
		// that names a box the parity run created.
		if boxesBefore, ok := c.boxes[path]; ok {
			added, removed := diffSets(boxesBefore, boxNames(path))
			switch {
			case len(added) == 0 && len(removed) == 0:
				msg += " but the box set is IDENTICAL (most likely the user's supervisor rewriting it)"
			default:
				if len(added) > 0 {
					msg += "\n    BOXES ADDED: " + strings.Join(added, ", ")
				}
				if len(removed) > 0 {
					msg += "\n    BOXES REMOVED: " + strings.Join(removed, ", ")
				}
			}
		}
		drift = append(drift, msg)
	}

	if len(drift) > 0 {
		sort.Strings(drift)
		return fmt.Errorf("PARITY RUN MAY HAVE MODIFIED THE REAL BOXYARD — investigate:\n%s", strings.Join(drift, "\n"))
	}
	return nil
}
