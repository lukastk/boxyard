package cmds

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

// newTestYard builds an isolated boxyard whose every path lives under t.TempDir().
//
// The config is written by hand rather than by RenderDefault, because the
// default names the REAL ~/dev and ~/g — a test that let those defaults through
// would create boxes in the user's live yard.
func newTestYard(t *testing.T) *config.Config {
	t.Helper()
	root := t.TempDir()
	cfgPath := filepath.Join(root, "config", "config.toml")
	if err := os.MkdirAll(filepath.Dir(cfgPath), 0o755); err != nil {
		t.Fatal(err)
	}
	body := "" +
		"default_storage_location = \"local\"\n" +
		"boxyard_data_path = \"" + filepath.Join(root, "data") + "\"\n" +
		"box_timestamp_format = \"date_only\"\n" +
		"user_boxes_path = \"" + filepath.Join(root, "boxes") + "\"\n" +
		"user_box_groups_path = \"" + filepath.Join(root, "groups") + "\"\n" +
		"default_box_groups = []\n" +
		"box_subid_character_set = \"abcdefghijklmnopqrstuvwxyz0123456789\"\n" +
		"box_subid_length = 5\n" +
		"max_concurrent_rclone_ops = 4\n" +
		"\n[storage_locations.local]\n" +
		"storage_type = \"local\"\n" +
		"store_path = \"" + filepath.Join(root, "store") + "\"\n" +
		"\n[box_groups]\n" +
		"\n[virtual_box_groups]\n"
	if err := os.WriteFile(cfgPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{"data", "boxes", "groups", "store"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	cfg, err := Init(InitOptions{ConfigPath: cfgPath, DataPath: filepath.Join(root, "data")})
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// yardContents is what must be unchanged after a creation that fails.
func yardContents(t *testing.T, cfg *config.Config) []string {
	t.Helper()
	var out []string
	for _, dir := range []string{filepath.Join(cfg.LocalStorePath(), "local"), cfg.UserBoxesPath} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatal(err)
		}
		for _, e := range entries {
			out = append(out, filepath.Join(dir, e.Name()))
		}
	}
	return out
}

func TestNewBoxCreatesEverything(t *testing.T) {
	cfg := newTestYard(t)

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "a-box"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(indexName, "__a-box") {
		t.Fatalf("index name %q does not end in the box name", indexName)
	}

	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	bm, ok := meta.ByIndexName()[indexName]
	if !ok {
		t.Fatalf("%s is not registered in boxyard_meta.json", indexName)
	}
	if bm.StorageLocation != "local" {
		t.Errorf("storage_location = %q, want the config default", bm.StorageLocation)
	}
	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		t.Fatal(err)
	}
	metaPath, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{bm.LocalPath(cfg), dataPath, confPath, metaPath} {
		if _, err := os.Stat(p); err != nil {
			t.Errorf("new_box did not create %s: %v", p, err)
		}
	}
}

func TestNewBoxHonoursFixedTimestamp(t *testing.T) {
	cfg := newTestYard(t)
	ts := time.Date(2024, 1, 2, 3, 4, 5, 0, time.UTC)

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "dated", CreationTimestampUTC: &ts})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(indexName, "20240102_") {
		t.Fatalf("index name %q does not start with the timestamp that was asked for", indexName)
	}
}

func TestNewBoxRejectsBadArguments(t *testing.T) {
	cfg := newTestYard(t)
	before := yardContents(t, cfg)

	cases := []struct {
		name string
		opts NewBoxOptions
		want string
	}{
		{"no name at all", NewBoxOptions{}, "must be provided"},
		{"unknown storage location", NewBoxOptions{BoxName: "x", StorageLocation: "nope"}, "Invalid storage location"},
		{"clone and from together", NewBoxOptions{BoxName: "x", GitCloneURL: "u", FromPath: "/tmp"}, "mutually exclusive"},
		// A name spanning several path components used to spread the box over
		// a nested directory tree, leaving a top-level directory with no
		// boxmeta.toml — which then broke every later meta refresh.
		{"path separator in name", NewBoxOptions{BoxName: "/github.com/user/repo"}, "single path component"},
		{"copy without from", NewBoxOptions{BoxName: "x", CopyFromPath: true}, "`from_path` must be provided"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewBox(context.Background(), cfg, nil, tc.opts)
			if err == nil {
				t.Fatal("expected an error")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err, tc.want)
			}
		})
	}

	if got := yardContents(t, cfg); len(got) != len(before) {
		t.Fatalf("a rejected creation left something behind: %v", got)
	}
}

// A failed `git clone` must leave nothing behind: a box is registered by the
// directories it occupies, so a half-created one is a broken registration, and
// every retry would add another.
func TestNewBoxRollsBackFailedClone(t *testing.T) {
	cfg := newTestYard(t)
	before := yardContents(t, cfg)

	missing := filepath.Join(t.TempDir(), "definitely-not-a-repo.git")
	if _, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{GitCloneURL: missing, BoxName: "doomed"}); err == nil {
		t.Fatal("expected the clone to fail")
	}

	if got := yardContents(t, cfg); len(got) != len(before) {
		t.Fatalf("the half-created box survived: %v", got)
	}
}

// The rollback must UNDO the move, not delete the moved directory: `--from`
// without `--copy` is the user's only copy of that data.
func TestNewBoxRollbackRestoresMovedDirectory(t *testing.T) {
	cfg := newTestYard(t)

	src := filepath.Join(t.TempDir(), "payload")
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "keepme.txt"), []byte("precious"), 0o644); err != nil {
		t.Fatal(err)
	}

	// rollbackNewBox is exercised directly: the move-back branch is otherwise
	// reachable only by injecting a failure between the rename and the end of
	// creation, and what matters here is that the branch restores the data
	// rather than deleting it.
	dst := filepath.Join(cfg.UserBoxesPath, "20240102_aaaaa__payload")
	if err := os.Rename(src, dst); err != nil {
		t.Fatal(err)
	}
	boxPath := filepath.Join(cfg.LocalStorePath(), "local", "20240102_aaaaa__payload")
	if err := os.MkdirAll(boxPath, 0o755); err != nil {
		t.Fatal(err)
	}

	if err := rollbackNewBox(boxPath, dst, src); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(src, "keepme.txt")); err != nil {
		t.Fatalf("rollback did not put the moved directory back: %v", err)
	}
	if _, err := os.Lstat(dst); !os.IsNotExist(err) {
		t.Fatalf("the box's DATA directory survived the rollback")
	}
	if _, err := os.Lstat(boxPath); !os.IsNotExist(err) {
		t.Fatalf("the box's registration directory survived the rollback")
	}
}

func TestNewBoxFromPathMoves(t *testing.T) {
	cfg := newTestYard(t)

	src := filepath.Join(t.TempDir(), "moved-in")
	if err := os.MkdirAll(filepath.Join(src, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "sub", "nested.txt"), []byte("nested"), 0o644); err != nil {
		t.Fatal(err)
	}

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{FromPath: src, InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(indexName, "__moved-in") {
		t.Fatalf("index name %q did not take its name from the source directory", indexName)
	}
	if _, err := os.Lstat(src); !os.IsNotExist(err) {
		t.Error("the source directory survived a move")
	}
	body, err := os.ReadFile(filepath.Join(cfg.UserBoxesPath, indexName, "sub", "nested.txt"))
	if err != nil || string(body) != "nested" {
		t.Fatalf("the moved contents are not in the box: %v", err)
	}
}

func TestNewBoxFromPathCopies(t *testing.T) {
	cfg := newTestYard(t)

	src := filepath.Join(t.TempDir(), "copied")
	if err := os.MkdirAll(filepath.Join(src, "sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "sub", "nested.txt"), []byte("nested"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("sub/nested.txt", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "run.sh"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{FromPath: src, CopyFromPath: true, InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(src, "sub", "nested.txt")); err != nil {
		t.Errorf("a copy must leave the source intact: %v", err)
	}
	dataPath := filepath.Join(cfg.UserBoxesPath, indexName)
	if body, err := os.ReadFile(filepath.Join(dataPath, "sub", "nested.txt")); err != nil || string(body) != "nested" {
		t.Fatalf("nested file not copied: %v", err)
	}
	// copytree recreates a symlink as a symlink rather than following it.
	if fi, err := os.Lstat(filepath.Join(dataPath, "link")); err != nil || fi.Mode()&os.ModeSymlink == 0 {
		t.Errorf("the symlink was not copied as a symlink: %v", err)
	}
	// The exec bit is part of the file, and boxyard goes to some length
	// elsewhere (the perms manifest) to keep it — a copy must not drop it.
	if fi, err := os.Stat(filepath.Join(dataPath, "run.sh")); err != nil || fi.Mode().Perm()&0o111 == 0 {
		t.Errorf("the executable bit was lost in the copy: %v", err)
	}
}

func TestNewBoxRefusesToSwallowAnExistingBox(t *testing.T) {
	cfg := newTestYard(t)

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "original", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	existing := filepath.Join(cfg.UserBoxesPath, indexName)

	// Moving a box's own DATA into a new box would deregister the old box by
	// taking its directory away.
	if _, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{FromPath: existing, InitialiseGit: false}); err == nil {
		t.Fatal("expected a refusal")
	} else if !strings.Contains(err.Error(), "already a boxyard box") {
		t.Fatalf("unexpected error: %v", err)
	}

	// ...but copying it is explicitly allowed.
	if _, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{FromPath: existing, BoxName: "clone-of", CopyFromPath: true, InitialiseGit: false}); err != nil {
		t.Fatalf("copying an existing box should be allowed: %v", err)
	}
}

// `sync_before_new_box` guards against a box id already taken on the remote.
// Skipping it silently would remove that guarantee without anyone noticing.
// A failing `git init` warns; the box survives. Python's `check=True` raised
// before its own warning branch could run, so a machine without git got a
// rolled-back box instead of a working one.
func TestNewBoxSurvivesFailingGitInit(t *testing.T) {
	cfg := newTestYard(t)

	shimDir := t.TempDir()
	shim := filepath.Join(shimDir, "git")
	if err := os.WriteFile(shim, []byte("#!/bin/sh\necho 'git: simulated failure' >&2\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", shimDir+string(os.PathListSeparator)+os.Getenv("PATH"))

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "no-git-here", InitialiseGit: true})
	if err != nil {
		t.Fatalf("a failing `git init` must not fail the creation: %v", err)
	}
	dataPath := filepath.Join(cfg.UserBoxesPath, indexName)
	if _, err := os.Stat(dataPath); err != nil {
		t.Fatalf("the box was rolled back: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataPath, ".git")); !os.IsNotExist(err) {
		t.Error("a .git exists even though git failed")
	}
}

func TestExtractBoxNameFromGitURL(t *testing.T) {
	cases := map[string]string{
		"git@github.com:lukastk/boxyard.git":       "boxyard",
		"git@github.com:lukastk/boxyard":           "boxyard",
		"https://github.com/lukastk/boxyard.git":   "boxyard",
		"http://github.com/lukastk/boxyard/":       "boxyard",
		"https://gitlab.com/group/sub/project.git": "project",
		"/srv/git/bare-repo.git":                   "bare-repo",
		"bare-repo":                                "bare-repo",
	}
	for url, want := range cases {
		if got := ExtractBoxNameFromGitURL(url); got != want {
			t.Errorf("ExtractBoxNameFromGitURL(%q) = %q, want %q", url, got, want)
		}
	}
}
