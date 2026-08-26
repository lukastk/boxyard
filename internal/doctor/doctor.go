// Package doctor is boxyard's read-only health check.
//
// It NEVER mutates or auto-fixes anything — not even the registry cache, which
// is why it scans local_store directly rather than going through
// GetBoxyardMeta (that writes the cache when it is missing) or
// CreateBoxyardMeta (that raises on the first broken registration, when
// reporting them all is the whole point).
//
// Every finding carries a HINT naming an exact command that is safe to run
// verbatim. That is a rule with a scar behind it: the duplicate-box-id hint
// once said "delete or re-create one of them", and `delete` purges the remote
// and writes a tombstone keyed by box id — so following it destroyed BOTH
// boxes.
//
// Ported from pts/mod/cmds/14_doctor.pct.py.
package doctor

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
)

// CheckNames is every check, in report order.
var CheckNames = []string{
	"unregistered-folder",
	"malformed-name",
	"broken-registration",
	"duplicate-box-id",
	"stale-cache",
	"dangling-symlinks",
	"group-tree-debris",
	"orphaned-sync-records",
	"interrupted-sync",
	"unknown-storage-location",
	"rclone-config",
	"stale-meta-mirror",
	"tombstoned-box",
	"diverged-box",
	"tree-orphans",
	"unknown-boxmeta-keys",
	"machine-name-unset",
	"unknown-config-keys",
	"write-denied",
	"stale-owner",
	"unpushed-meta-edit",
}

// Finding is one problem, with a hint that names the fix.
type Finding struct {
	Message string
	Hint    string
	// Extra carries the machine-readable fields, in insertion order.
	Extra []Field
}

// Field is one extra key/value on a finding.
type Field struct {
	Key   string
	Value any
}

// Check is one named check's outcome.
type Check struct {
	Skipped  bool
	Findings []Finding
}

// Report is the whole health check.
type Report struct {
	Healthy     bool
	NumFindings int
	Checks      map[string]*Check
}

// Options mirrors the Python `run_doctor` signature.
type Options struct {
	// CheckRemote false skips every check that touches remote storage, so
	// doctor works offline.
	CheckRemote bool
	// StorageLocations restricts the REMOTE checks. Local checks always cover
	// all storage locations.
	StorageLocations []string
}

// scan is the tolerant walk of local_store that several checks build on.
type scan struct {
	registrationDirsBySL map[string][]string
	registeredIndexNames map[string]bool
	problemIndexNames    map[string]bool
	boxMetas             []*models.BoxMeta
}

func (r *Report) add(check, message, hint string, extra ...Field) {
	c := r.Checks[check]
	c.Findings = append(c.Findings, Finding{Message: message, Hint: hint, Extra: extra})
}

// Run performs the health check.
func Run(ctx context.Context, cfg *config.Config, s RemoteStore, opts Options) (*Report, error) {
	for _, sl := range opts.StorageLocations {
		if _, ok := cfg.StorageLocations[sl]; !ok {
			return nil, fmt.Errorf("Invalid storage location(s): ['%s']", sl)
		}
	}

	report := &Report{Checks: map[string]*Check{}}
	for _, name := range CheckNames {
		report.Checks[name] = &Check{Findings: []Finding{}}
	}

	sc := runScan(cfg, report)
	checkUserBoxes(cfg, report, sc)
	checkDuplicateBoxIDs(cfg, report, sc)
	checkStaleCache(cfg, report, sc)
	checkGroupTree(cfg, report)
	checkSyncRecords(cfg, report, sc)
	checkUnknownStorageLocations(cfg, report)
	checkRcloneConfig(cfg, report)
	checkTreeOrphans(report, sc)
	checkUnknownBoxMetaKeys(cfg, report, sc)
	checkMachineName(cfg, report)
	checkUnknownConfigKeys(cfg, report)

	if err := checkOwnership(ctx, cfg, s, report, sc); err != nil {
		return nil, err
	}
	if err := checkRemote(ctx, cfg, s, report, sc, opts); err != nil {
		return nil, err
	}
	checkWriteDenied(ctx, cfg, s, report, sc, opts.CheckRemote)
	if err := checkUnpushedMetaEdits(cfg, report, sc); err != nil {
		return nil, err
	}

	for _, c := range report.Checks {
		report.NumFindings += len(c.Findings)
	}
	report.Healthy = report.NumFindings == 0
	return report, nil
}

// runScan walks local_store tolerantly, collecting registrations and reporting
// everything in between as a broken-registration.
func runScan(cfg *config.Config, report *Report) *scan {
	sc := &scan{
		registrationDirsBySL: map[string][]string{},
		registeredIndexNames: map[string]bool{},
		problemIndexNames:    map[string]bool{},
	}
	names := sortedKeys(cfg.StorageLocations)
	for _, slName := range names {
		sc.registrationDirsBySL[slName] = nil
		slPath := filepath.Join(cfg.LocalStorePath(), slName)
		entries, err := os.ReadDir(slPath)
		if err != nil {
			continue
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".") {
				continue
			}
			path := filepath.Join(slPath, entry.Name())
			if !entry.IsDir() {
				report.add("broken-registration",
					fmt.Sprintf("Stray file in local store: '%s'", path),
					"Only box registration directories belong in the local store; move or delete the file.",
					Field{"path", path}, Field{"storage_location", slName})
				continue
			}
			sc.registrationDirsBySL[slName] = append(sc.registrationDirsBySL[slName], entry.Name())

			boxmetaPath := filepath.Join(path, boxconst.BoxMetafileRelPath)
			if _, err := os.Stat(boxmetaPath); err != nil {
				report.add("broken-registration",
					fmt.Sprintf("Registration '%s/%s' has no %s", slName, entry.Name(), boxconst.BoxMetafileRelPath),
					"The registration is incomplete; restore its boxmeta.toml (e.g. `boxyard sync-missing-meta`) or delete the directory if the box is gone.",
					Field{"path", path}, Field{"storage_location", slName})
				sc.problemIndexNames[entry.Name()] = true
				continue
			}
			bm, err := models.LoadBoxMeta(cfg, slName, entry.Name())
			if err != nil {
				report.add("broken-registration",
					fmt.Sprintf("Registration '%s/%s' has a boxmeta.toml that fails to load: %v", slName, entry.Name(), err),
					"Fix or restore the boxmeta.toml; every mutating boxyard command will fail while it is invalid.",
					Field{"path", boxmetaPath}, Field{"storage_location", slName})
				sc.problemIndexNames[entry.Name()] = true
				continue
			}
			sc.registeredIndexNames[entry.Name()] = true
			sc.boxMetas = append(sc.boxMetas, bm)
		}
	}
	return sc
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func readLocalRecord(path string) *models.SyncRecord {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	rec, err := models.UnmarshalSyncRecord(raw)
	if err != nil {
		return nil
	}
	return &rec
}

func fileExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && !info.IsDir()
}

// optionalFile returns p when it exists and "" otherwise — the filter files are
// per box and most boxes have none.
func optionalFile(p string) string {
	if fileExists(p) {
		return p
	}
	return ""
}

func readRemoteRecord(ctx context.Context, s RemoteStore, remote, path string) *models.SyncRecord {
	exists, raw, err := s.Cat(ctx, remote, path)
	if err != nil || !exists {
		return nil
	}
	rec, err := models.UnmarshalSyncRecord([]byte(raw))
	if err != nil {
		return nil
	}
	return &rec
}
