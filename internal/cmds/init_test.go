package cmds

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
)

func TestInitCreatesEverything(t *testing.T) {
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")

	cfg, err := Init(InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")})
	if err != nil {
		t.Fatal(err)
	}

	for _, p := range []string{cfgPath, cfg.DefaultRcloneExcludePath(), cfg.RcloneConfigPath()} {
		if _, err := os.Stat(p); err != nil {
			t.Errorf("init did not create %s: %v", p, err)
		}
	}
	for _, p := range []string{cfg.BoxyardDataPath, cfg.LocalStorePath()} {
		if fi, err := os.Stat(p); err != nil || !fi.IsDir() {
			t.Errorf("init did not create the directory %s: %v", p, err)
		}
	}
}

// TestInitLinksLocalStorageLocations is the Go half of Python v0.5.4.
//
// Python compared `storage_type != StorageType.LOCAL.value` against a plain
// Enum, so the comparison was ALWAYS true, the loop `continue`d every time, and
// the link was never created — while init still printed "Done!". A `local`
// storage location was configured and unusable, silently.
func TestInitLinksLocalStorageLocations(t *testing.T) {
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")

	cfg, err := Init(InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")})
	if err != nil {
		t.Fatal(err)
	}

	// The default config ships exactly one storage location, a local one.
	link := filepath.Join(cfg.LocalStorePath(), "fake")
	fi, err := os.Lstat(link)
	if err != nil {
		t.Fatalf("no local_store entry for the `fake` local storage location: %v", err)
	}
	if fi.Mode()&os.ModeSymlink == 0 {
		t.Fatalf("%s exists but is not a symlink", link)
	}
	target, err := filepath.EvalSymlinks(link)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := filepath.EvalSymlinks(cfg.StorageLocations["fake"].StorePath)
	if target != want {
		t.Errorf("link points at %s, want %s", target, want)
	}
}

// TestInitIsIdempotent — init doubles as a repair, so re-running must not fail
// on what already exists.
func TestInitIsIdempotent(t *testing.T) {
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")
	opts := InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")}

	if _, err := Init(opts); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Init(opts); err != nil {
		t.Fatalf("second init failed: %v", err)
	}
	after, err := os.ReadFile(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) {
		t.Error("init rewrote an existing config file; it must only create what is missing")
	}
}

// TestInitReplacesADanglingLink — the store may have moved. A link to a missing
// target must be replaced, not left. os.Stat would report it as absent, which
// is why the implementation uses Lstat.
func TestInitReplacesADanglingLink(t *testing.T) {
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")
	opts := InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")}

	cfg, err := Init(opts)
	if err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(cfg.LocalStorePath(), "fake")
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "gone"), link); err != nil {
		t.Fatal(err)
	}

	if _, err := Init(opts); err != nil {
		t.Fatalf("init failed on a dangling link: %v", err)
	}
	target, err := filepath.EvalSymlinks(link)
	if err != nil {
		t.Fatalf("the dangling link was not repaired: %v", err)
	}
	want, _ := filepath.EvalSymlinks(cfg.StorageLocations["fake"].StorePath)
	if target != want {
		t.Errorf("link points at %s, want %s", target, want)
	}
}

// TestInitDoesNotLinkRcloneLocations — an rclone location's store is remote, so
// a local directory there would shadow it.
func TestInitDoesNotLinkRcloneLocations(t *testing.T) {
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")
	if err := os.MkdirAll(filepath.Dir(cfgPath), 0o755); err != nil {
		t.Fatal(err)
	}
	body := config.RenderDefault(cfgPath, filepath.Join(root, "data")) +
		"\n[storage_locations.myremote]\nstorage_type = \"rclone\"\nstore_path = \"boxyard\"\n"
	if err := os.WriteFile(cfgPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Init(InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(filepath.Join(cfg.LocalStorePath(), "myremote")); err == nil {
		t.Error("init linked an rclone storage location, which has no local store")
	}
}
