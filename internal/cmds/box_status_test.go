package cmds

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// refusingProber fails every call. A local-storage box must never reach it —
// which is the whole point: Python DID reach rclone there, and died with
// "didn't find section in config file".
type refusingProber struct{ t *testing.T }

func (p refusingProber) PathExists(context.Context, string, string) (bool, bool, error) {
	p.t.Fatal("a local storage location must not be probed through rclone")
	return false, false, nil
}
func (p refusingProber) ReadSyncRecord(context.Context, string, string) (*models.SyncRecord, error) {
	p.t.Fatal("a local storage location must not be probed through rclone")
	return nil, nil
}
func (p refusingProber) LocalIsEmptyDir(string) (bool, error) {
	p.t.Fatal("a local storage location must not be probed through rclone")
	return false, nil
}
func (p refusingProber) LocalLastModified(string, map[string]bool) (time.Time, bool, error) {
	p.t.Fatal("a local storage location must not be probed through rclone")
	return time.Time{}, false, nil
}

func TestBoxSyncStatusLocalStorage(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "local-status", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	got, err := BoxSyncStatus(context.Background(), cfg, refusingProber{t}, indexName)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(enums.AllBoxParts) {
		t.Fatalf("got %d parts, want %d", len(got), len(enums.AllBoxParts))
	}
	for _, part := range enums.AllBoxParts {
		status, ok := got[part]
		if !ok {
			t.Fatalf("no status for %s", part)
		}
		if status.Condition != syncengine.LocalStorage {
			t.Errorf("%s: condition = %q, want %q", part, status.Condition, syncengine.LocalStorage)
		}
		if status.ErrorMessage != "" {
			t.Errorf("%s: unexpected error message %q", part, status.ErrorMessage)
		}
	}
}

func TestBoxSyncStatusUnknownBox(t *testing.T) {
	cfg := newTestYard(t)
	if _, err := BoxSyncStatus(context.Background(), cfg, refusingProber{t}, "20240102_aaaaa__nope"); err == nil {
		t.Fatal("expected an error for a box that is not registered")
	}
}

// A box's own conf/.rclone_exclude REPLACES the global default rather than
// adding to it, so resolving the wrong one can prune a directory the box really
// does sync — hiding a genuine change.
func TestEffectiveExcludePath(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "excludes", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	bm := meta.ByIndexName()[indexName]

	got, err := EffectiveExcludePath(cfg, bm)
	if err != nil {
		t.Fatal(err)
	}
	if got != cfg.DefaultRcloneExcludePath() {
		t.Fatalf("without a per-box file the default must be used, got %q", got)
	}

	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		t.Fatal(err)
	}
	boxExclude := filepath.Join(confPath, boxconst.RcloneExcludeFilename)
	if err := os.WriteFile(boxExclude, []byte("node_modules/\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err = EffectiveExcludePath(cfg, bm)
	if err != nil {
		t.Fatal(err)
	}
	if got != boxExclude {
		t.Fatalf("a per-box exclude file must win, got %q", got)
	}
}

// recordingProber captures the paths it is asked about, so the wiring can be
// checked without a real remote. PathExists is the first call GetSyncStatus
// makes for each side, which is enough to pin which path went where.
type recordingProber struct {
	localPaths  []string
	remoteNames []string
	remotePaths []string
}

func (p *recordingProber) PathExists(_ context.Context, remote, path string) (bool, bool, error) {
	if remote == "" {
		p.localPaths = append(p.localPaths, path)
	} else {
		p.remoteNames = append(p.remoteNames, remote)
		p.remotePaths = append(p.remotePaths, path)
	}
	return false, false, nil
}
func (p *recordingProber) ReadSyncRecord(context.Context, string, string) (*models.SyncRecord, error) {
	return nil, nil
}
func (p *recordingProber) LocalIsEmptyDir(string) (bool, error) { return true, nil }
func (p *recordingProber) LocalLastModified(string, map[string]bool) (time.Time, bool, error) {
	return time.Time{}, false, nil
}

func TestBoxSyncStatusProbesEveryPart(t *testing.T) {
	cfg := newTestYard(t)
	// A second, rclone-typed storage location, so the local shortcut does not
	// apply and the real probing path runs.
	cfg.StorageLocations["remote"] = &config.StorageConfig{
		StorageType: config.StorageRclone,
		StorePath:   "boxyard",
	}
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "probed", StorageLocation: "remote", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	p := &recordingProber{}
	got, err := BoxSyncStatus(context.Background(), cfg, p, indexName)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(enums.AllBoxParts) {
		t.Fatalf("got %d parts, want %d", len(got), len(enums.AllBoxParts))
	}
	if len(p.remotePaths) != len(enums.AllBoxParts) {
		t.Fatalf("probed %d remote paths, want one per part", len(p.remotePaths))
	}
	for _, name := range p.remoteNames {
		if name != "remote" {
			t.Fatalf("remote name = %q, want the box's storage location", name)
		}
	}
	// The remote paths must be under the storage location's store, never under
	// the local box tree — passing one for the other is the wiring mistake this
	// test exists to catch.
	for _, path := range p.remotePaths {
		if !strings.HasPrefix(path, filepath.Join("boxyard", boxconst.RemoteBoxesRelPath)) {
			t.Fatalf("remote path %q is not under the store's boxes/ root", path)
		}
	}
	for _, path := range p.localPaths {
		if strings.HasPrefix(path, "boxyard/") {
			t.Fatalf("local path %q looks like a remote path", path)
		}
	}
}
