package parity

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// validSandbox builds a sandbox that the guard should accept, rooted in a
// throwaway temp dir.
func validSandbox(t *testing.T) *Sandbox {
	t.Helper()
	root := t.TempDir()
	return &Sandbox{
		Root:           root,
		ConfigPath:     filepath.Join(root, "config", "config.toml"),
		DataPath:       filepath.Join(root, "data"),
		UserBoxesPath:  filepath.Join(root, "boxes"),
		UserGroupsPath: filepath.Join(root, "groups"),
		RemotePrefix:   RequiredRemotePrefix + "run-test",
		RcloneConfig:   "~/.config/boxyard/boxyard_rclone.conf",
	}
}

func home(t *testing.T) string {
	t.Helper()
	h, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("UserHomeDir: %v", err)
	}
	return h
}

func TestAcceptsWellFormedSandbox(t *testing.T) {
	if err := AssertSandboxed(validSandbox(t)); err != nil {
		t.Fatalf("guard rejected a valid sandbox: %v", err)
	}
}

// The guard must derive its no-go list from the live config. If that config
// cannot be read or parsed, the guard must refuse rather than relax.
func TestRealConfigPathsAreDiscovered(t *testing.T) {
	local, remote, err := realConfigPaths()
	if err != nil {
		t.Fatalf("could not derive forbidden paths: %v", err)
	}
	if len(local) == 0 {
		t.Fatal("no forbidden local paths derived — guard would be toothless")
	}
	foundRealRemote := false
	for _, r := range remote {
		if r == "boxyard" {
			foundRealRemote = true
		}
	}
	if !foundRealRemote {
		t.Errorf("expected the real remote store_path %q among %v", "boxyard", remote)
	}
}

func TestRejectsRootInsideRealPaths(t *testing.T) {
	forbidden, _, err := realConfigPaths()
	if err != nil {
		t.Fatalf("realConfigPaths: %v", err)
	}
	for _, f := range forbidden {
		s := validSandbox(t)
		s.Root = filepath.Join(f, "parity-sandbox")
		s.ConfigPath = filepath.Join(s.Root, "config.toml")
		s.DataPath = filepath.Join(s.Root, "data")
		s.UserBoxesPath = filepath.Join(s.Root, "boxes")
		s.UserGroupsPath = filepath.Join(s.Root, "groups")
		if err := AssertSandboxed(s); err == nil {
			t.Fatalf("guard ACCEPTED a sandbox rooted inside real path %q", f)
		}
	}
}

func TestRejectsRootContainingRealPaths(t *testing.T) {
	s := validSandbox(t)
	s.Root = home(t)
	s.ConfigPath = filepath.Join(s.Root, "config.toml")
	s.DataPath = filepath.Join(s.Root, "data")
	s.UserBoxesPath = filepath.Join(s.Root, "boxes")
	s.UserGroupsPath = filepath.Join(s.Root, "groups")
	if err := AssertSandboxed(s); err == nil {
		t.Fatal("guard ACCEPTED $HOME as the sandbox root")
	}
}

func TestRejectsBroadRoots(t *testing.T) {
	for _, root := range []string{"/", "/tmp", home(t)} {
		s := validSandbox(t)
		s.Root = root
		s.ConfigPath = filepath.Join(root, "config.toml")
		s.DataPath = filepath.Join(root, "data")
		s.UserBoxesPath = filepath.Join(root, "boxes")
		s.UserGroupsPath = filepath.Join(root, "groups")
		if err := AssertSandboxed(s); err == nil {
			t.Errorf("guard ACCEPTED overly broad root %q", root)
		}
	}
}

func TestRejectsLocalPathOutsideRoot(t *testing.T) {
	for _, field := range []string{"ConfigPath", "DataPath", "UserBoxesPath", "UserGroupsPath"} {
		s := validSandbox(t)
		outside := filepath.Join(t.TempDir(), "elsewhere")
		switch field {
		case "ConfigPath":
			s.ConfigPath = outside
		case "DataPath":
			s.DataPath = outside
		case "UserBoxesPath":
			s.UserBoxesPath = outside
		case "UserGroupsPath":
			s.UserGroupsPath = outside
		}
		if err := AssertSandboxed(s); err == nil {
			t.Errorf("guard ACCEPTED %s outside the sandbox root", field)
		}
	}
}

// The single most dangerous mistake: pointing the sandbox at the real yard.
func TestRejectsUserBoxesPathEqualToRealDev(t *testing.T) {
	s := validSandbox(t)
	s.UserBoxesPath = filepath.Join(home(t), "dev")
	if err := AssertSandboxed(s); err == nil {
		t.Fatal("guard ACCEPTED ~/dev as user_boxes_path")
	}
}

func TestRejectsDangerousRemotePrefixes(t *testing.T) {
	bad := []string{
		"boxyard",                   // the real yard
		"boxyard/",                  // the real yard, trailing slash
		"boxyard/nested",            // inside the real yard
		"",                          // empty
		"backups",                   // an unrelated real directory
		"boxyard-gotest/../boxyard", // traversal
		"/boxyard",                  // absolute-looking
	}
	for _, prefix := range bad {
		s := validSandbox(t)
		s.RemotePrefix = prefix
		if err := AssertSandboxed(s); err == nil {
			t.Errorf("guard ACCEPTED dangerous remote prefix %q", prefix)
		}
	}
}

func TestAcceptsOnlyTestRemotePrefix(t *testing.T) {
	good := []string{
		RequiredRemotePrefix + "run-1",
		RequiredRemotePrefix + "run-abc/nested",
		strings.TrimSuffix(RequiredRemotePrefix, "/") + "/x",
	}
	for _, prefix := range good {
		s := validSandbox(t)
		s.RemotePrefix = prefix
		if err := AssertSandboxed(s); err != nil {
			t.Errorf("guard rejected valid test prefix %q: %v", prefix, err)
		}
	}
}

func TestRejectsEmptyFields(t *testing.T) {
	s := validSandbox(t)
	s.DataPath = ""
	if err := AssertSandboxed(s); err == nil {
		t.Fatal("guard ACCEPTED an empty DataPath")
	}
	if err := AssertSandboxed(nil); err == nil {
		t.Fatal("guard ACCEPTED a nil sandbox")
	}
}

func TestIsUnder(t *testing.T) {
	cases := []struct {
		child, parent string
		want          bool
	}{
		{"/a/b", "/a", true},
		{"/a", "/a", true},
		{"/a/bc", "/a/b", false}, // prefix-without-separator must not match
		{"/ab", "/a", false},
		{"/a/b/c", "/a", true},
		{"/x", "/a", false},
	}
	for _, c := range cases {
		if got := isUnder(c.child, c.parent); got != c.want {
			t.Errorf("isUnder(%q, %q) = %v, want %v", c.child, c.parent, got, c.want)
		}
	}
}

func TestCanaryDetectsDrift(t *testing.T) {
	// Build a canary over a file we control, then mutate it.
	dir := t.TempDir()
	f := filepath.Join(dir, "watched")
	if err := os.WriteFile(f, []byte("before"), 0o644); err != nil {
		t.Fatal(err)
	}
	c := &Canary{entries: map[string]string{f: stamp(f)}}
	if err := c.Verify(); err != nil {
		t.Fatalf("canary reported drift with no change: %v", err)
	}
	if err := os.WriteFile(f, []byte("after-and-longer"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := c.Verify(); err == nil {
		t.Fatal("canary MISSED a modification")
	}
}

func TestCanaryDetectsDeletion(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "watched")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	c := &Canary{entries: map[string]string{f: stamp(f)}}
	if err := os.Remove(f); err != nil {
		t.Fatal(err)
	}
	if err := c.Verify(); err == nil {
		t.Fatal("canary MISSED a deletion")
	}
}

// The canary must actually be watching the real yard's metadata file.
func TestCanaryWatchesRealMetadata(t *testing.T) {
	c, err := TakeCanary()
	if err != nil {
		t.Fatalf("TakeCanary: %v", err)
	}
	var watchesMeta bool
	for p := range c.entries {
		if strings.HasSuffix(p, "boxyard_meta.json") {
			watchesMeta = true
		}
	}
	if !watchesMeta {
		t.Errorf("canary does not watch boxyard_meta.json; watched: %v", c.entries)
	}
	if err := c.Verify(); err != nil {
		t.Fatalf("canary reported drift on a no-op run: %v", err)
	}
}
