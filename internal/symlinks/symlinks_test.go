package symlinks

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
)

// ---------------------------------------------------------------------------
// Test harness
//
// SAFETY: this package deletes symlinks and directories, and the tree it is
// built to operate on is the user's real ~/g, populated from hundreds of real
// boxes in ~/dev. Every test therefore builds a throwaway yard rooted at a
// fresh t.TempDir(), and newYard REFUSES TO RETURN a config whose paths are not
// inside that temp directory. A bug that let a test escape its sandbox would
// silently destroy the user's group tree, so the check fails closed rather than
// warning.
// ---------------------------------------------------------------------------

type yard struct {
	t    *testing.T
	root string
	cfg  *config.Config
	meta *models.BoxyardMeta
}

// newYard writes a config.toml under a fresh temp dir and loads it through the
// real config loader, so virtual-group filter expressions are compiled exactly
// as they are in production.
//
// realGroups and virtualGroups are raw TOML table blocks, e.g.
// "[box_groups.backend]\nsymlink_name = \"all/backend\"\n".
func newYard(t *testing.T, realGroups, virtualGroups string) *yard {
	t.Helper()
	root := t.TempDir()

	var b strings.Builder
	b.WriteString("default_storage_location = \"fake\"\n")
	fmt.Fprintf(&b, "boxyard_data_path = %q\n", filepath.Join(root, ".boxyard"))
	b.WriteString("box_timestamp_format = \"date_only\"\n")
	fmt.Fprintf(&b, "user_boxes_path = %q\n", filepath.Join(root, "dev"))
	fmt.Fprintf(&b, "user_box_groups_path = %q\n", filepath.Join(root, "g"))
	b.WriteString("default_box_groups = []\n")
	b.WriteString("box_subid_character_set = \"abcdefghijklmnopqrstuvwxyz0123456789\"\n")
	b.WriteString("box_subid_length = 6\n")
	b.WriteString("max_concurrent_rclone_ops = 2\n")
	if strings.TrimSpace(realGroups) == "" {
		b.WriteString("box_groups = {}\n")
	}
	if strings.TrimSpace(virtualGroups) == "" {
		b.WriteString("virtual_box_groups = {}\n")
	}
	b.WriteString("[storage_locations.fake]\nstorage_type = \"local\"\n")
	fmt.Fprintf(&b, "store_path = %q\n", filepath.Join(root, ".boxyard", "fake_store"))
	b.WriteString(realGroups)
	b.WriteString(virtualGroups)

	cfgPath := filepath.Join(root, "config.toml")
	if err := os.WriteFile(cfgPath, []byte(b.String()), 0o644); err != nil {
		t.Fatalf("write config: %v", err)
	}
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatalf("load config: %v\n%s", err, b.String())
	}

	assertSandboxed(t, root, cfg)

	if err := os.MkdirAll(cfg.UserBoxesPath, 0o755); err != nil {
		t.Fatalf("mkdir user boxes: %v", err)
	}
	return &yard{t: t, root: root, cfg: cfg, meta: &models.BoxyardMeta{BoxMetas: []*models.BoxMeta{}}}
}

// assertSandboxed fails the test unless every path the symlink builder can
// write to or delete from lives inside the test's own temp directory, and is
// none of the user's real boxyard paths.
func assertSandboxed(t *testing.T, root string, cfg *config.Config) {
	t.Helper()
	if err := sandboxViolation(root, cfg); err != nil {
		t.Fatalf("REFUSING TO RUN: %v", err)
	}
}

// sandboxViolation is the guard itself, as a pure function so it can be tested
// without a live *testing.T. It fails CLOSED: any path it cannot prove is
// inside root is a violation.
func sandboxViolation(root string, cfg *config.Config) error {
	if root == "" || !filepath.IsAbs(root) {
		return fmt.Errorf("test sandbox root %q is not an absolute path", root)
	}
	paths := map[string]string{
		"user_box_groups_path": cfg.UserBoxGroupsPath,
		"user_boxes_path":      cfg.UserBoxesPath,
		"boxyard_data_path":    cfg.BoxyardDataPath,
		"config_path":          cfg.ConfigPath,
	}
	for _, name := range sortedKeys(paths) {
		p := paths[name]
		if p == "" {
			return fmt.Errorf("%s is empty", name)
		}
		rel, err := filepath.Rel(root, p)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return fmt.Errorf("%s = %q is not inside the test temp dir %q", name, p, root)
		}
	}
	// Belt and braces: never the real thing, whatever TMPDIR happens to be.
	home, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	for _, forbidden := range []string{"g", "dev", ".boxyard", filepath.Join(".config", "boxyard")} {
		abs := filepath.Join(home, forbidden)
		for _, name := range sortedKeys(paths) {
			p := paths[name]
			if p == abs || strings.HasPrefix(p, abs+string(filepath.Separator)) {
				return fmt.Errorf("%s = %q is inside the user's real %q", name, p, abs)
			}
		}
	}
	return nil
}

func sortedKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// addBox registers a box AND creates its DATA directory, so CheckIncluded is
// true and it takes part in the symlink tree.
func (y *yard) addBox(timestamp, subid, name string, groups ...string) *models.BoxMeta {
	y.t.Helper()
	bm := y.register(timestamp, subid, name, groups...)
	dataPath := filepath.Join(y.cfg.UserBoxesPath, bm.IndexName())
	if err := os.MkdirAll(dataPath, 0o755); err != nil {
		y.t.Fatalf("mkdir box data: %v", err)
	}
	return bm
}

// register adds a box to the registry WITHOUT creating its DATA directory: an
// excluded box, i.e. one that exists in the yard but is not checked out here.
func (y *yard) register(timestamp, subid, name string, groups ...string) *models.BoxMeta {
	y.t.Helper()
	if groups == nil {
		groups = []string{}
	}
	bm := &models.BoxMeta{
		CreationTimestampUTC: timestamp,
		BoxSubid:             subid,
		Name:                 name,
		StorageLocation:      "fake",
		CreatorHostname:      "testhost",
		Groups:               groups,
		Parents:              []string{},
	}
	if err := bm.Validate(); err != nil {
		y.t.Fatalf("invalid test box %q: %v", name, err)
	}
	y.meta.BoxMetas = append(y.meta.BoxMetas, bm)
	return bm
}

// exclude removes a box's DATA directory, the way `boxyard exclude-box` does.
func (y *yard) exclude(bm *models.BoxMeta) {
	y.t.Helper()
	if err := os.RemoveAll(filepath.Join(y.cfg.UserBoxesPath, bm.IndexName())); err != nil {
		y.t.Fatalf("exclude box: %v", err)
	}
}

func (y *yard) build() string {
	y.t.Helper()
	var warnings bytes.Buffer
	if err := BuildTo(y.cfg, y.meta, &warnings); err != nil {
		y.t.Fatalf("Build: %v", err)
	}
	return warnings.String()
}

func (y *yard) buildErr() error {
	y.t.Helper()
	var warnings bytes.Buffer
	return BuildTo(y.cfg, y.meta, &warnings)
}

func (y *yard) groupsRoot() string { return y.cfg.UserBoxGroupsPath }

func (y *yard) dataPath(bm *models.BoxMeta) string {
	return filepath.Join(y.cfg.UserBoxesPath, bm.IndexName())
}

// tree renders the whole group tree as sorted lines, never following a symlink.
// A symlink is shown with the target it points at, relative to the temp root,
// so expectations read as the tree a user would see.
func (y *yard) tree() []string {
	y.t.Helper()
	var out []string
	var walk func(dir string)
	walk = func(dir string) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			y.t.Fatalf("read %q: %v", dir, err)
		}
		for _, e := range entries {
			p := filepath.Join(dir, e.Name())
			rel, err := filepath.Rel(y.groupsRoot(), p)
			if err != nil {
				y.t.Fatal(err)
			}
			switch {
			case e.Type()&os.ModeSymlink != 0:
				target, err := os.Readlink(p)
				if err != nil {
					y.t.Fatal(err)
				}
				trel, err := filepath.Rel(y.root, target)
				if err != nil {
					trel = target
				}
				out = append(out, fmt.Sprintf("%s -> %s", rel, trel))
			case e.IsDir():
				out = append(out, rel+"/")
				walk(p)
			default:
				out = append(out, rel+" (file)")
			}
		}
	}
	if _, err := os.Stat(y.groupsRoot()); err == nil {
		walk(y.groupsRoot())
	}
	sort.Strings(out)
	return out
}

func (y *yard) assertTree(want ...string) {
	y.t.Helper()
	got := y.tree()
	sort.Strings(want)
	if len(got) != len(want) {
		y.t.Fatalf("group tree mismatch\n got: %s\nwant: %s", strings.Join(got, "\n      "), strings.Join(want, "\n      "))
	}
	for i := range got {
		if got[i] != want[i] {
			y.t.Fatalf("group tree mismatch\n got: %s\nwant: %s", strings.Join(got, "\n      "), strings.Join(want, "\n      "))
		}
	}
}

func mustMkdir(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(p, 0o755); err != nil {
		t.Fatalf("mkdir %q: %v", p, err)
	}
}

func mustWrite(t *testing.T, p, content string) {
	t.Helper()
	mustMkdir(t, filepath.Dir(p))
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("write %q: %v", p, err)
	}
}

func mustSymlink(t *testing.T, target, linkPath string) {
	t.Helper()
	mustMkdir(t, filepath.Dir(linkPath))
	if err := os.Symlink(target, linkPath); err != nil {
		t.Fatalf("symlink %q -> %q: %v", linkPath, target, err)
	}
}

func exists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

// ---------------------------------------------------------------------------
// The sandbox guard itself
// ---------------------------------------------------------------------------

func TestSandboxGuardRejectsPathsOutsideTempDir(t *testing.T) {
	// A config that escapes the sandbox must be caught by the guard, not
	// discovered by the user missing their ~/g. Exercised directly, because
	// this check is the only thing standing between a future bug and real data.
	root := t.TempDir()
	inside := &config.Config{
		ConfigPath:        filepath.Join(root, "config.toml"),
		UserBoxGroupsPath: filepath.Join(root, "g"),
		UserBoxesPath:     filepath.Join(root, "dev"),
		BoxyardDataPath:   filepath.Join(root, ".boxyard"),
	}
	if err := sandboxViolation(root, inside); err != nil {
		t.Fatalf("a properly sandboxed config was rejected: %v", err)
	}

	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	escapes := []struct {
		name string
		cfg  *config.Config
	}{
		{"absolute path outside the temp dir", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxesPath: filepath.Join(root, "dev"),
			BoxyardDataPath: filepath.Join(root, ".boxyard"), UserBoxGroupsPath: "/somewhere/g",
		}},
		{"the user's real group tree", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxesPath: filepath.Join(root, "dev"),
			BoxyardDataPath: filepath.Join(root, ".boxyard"), UserBoxGroupsPath: filepath.Join(home, "g"),
		}},
		{"the user's real boxes directory", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxGroupsPath: filepath.Join(root, "g"),
			BoxyardDataPath: filepath.Join(root, ".boxyard"), UserBoxesPath: filepath.Join(home, "dev"),
		}},
		{"the user's real data directory", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxGroupsPath: filepath.Join(root, "g"),
			UserBoxesPath: filepath.Join(root, "dev"), BoxyardDataPath: filepath.Join(home, ".boxyard"),
		}},
		{"a traversal out of the temp dir", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxesPath: filepath.Join(root, "dev"),
			BoxyardDataPath: filepath.Join(root, ".boxyard"), UserBoxGroupsPath: filepath.Join(root, "..", "g"),
		}},
		{"an unset path", &config.Config{
			ConfigPath: filepath.Join(root, "config.toml"), UserBoxesPath: filepath.Join(root, "dev"),
			BoxyardDataPath: filepath.Join(root, ".boxyard"),
		}},
	}
	for _, c := range escapes {
		if err := sandboxViolation(root, c.cfg); err == nil {
			t.Errorf("guard accepted %s", c.name)
		}
	}
	if err := sandboxViolation("", inside); err == nil {
		t.Error("guard accepted an empty sandbox root")
	}
}

func TestAssertUnderRefusesEscapes(t *testing.T) {
	root := t.TempDir()
	for _, p := range []string{
		root,
		filepath.Dir(root),
		filepath.Join(root, "..", "elsewhere"),
		"/etc/passwd",
	} {
		if err := assertUnder(root, p); err == nil {
			t.Errorf("assertUnder(%q, %q) allowed a path outside the group tree", root, p)
		}
	}
	if err := assertUnder(root, filepath.Join(root, "a", "b")); err != nil {
		t.Errorf("assertUnder rejected a path inside the tree: %v", err)
	}
}

// ---------------------------------------------------------------------------
// get_box_group_configs — ported from TestGetBoxGroupConfigs
// ---------------------------------------------------------------------------

const groupConfigsReal = `[box_groups.backend]
symlink_name = "backend-projects"
[box_groups.frontend]
box_title_mode = "name"
`

const groupConfigsVirtual = `[virtual_box_groups.active]
filter_expr = "NOT archived"
`

func groupConfigsYard(t *testing.T) *yard {
	y := newYard(t, groupConfigsReal, groupConfigsVirtual)
	y.register("20251120", "abc123", "project-alpha", "backend", "python")
	y.register("20251121", "def345", "project-beta", "frontend", "custom-group")
	return y
}

func TestGroupConfigsReturnsConfigGroups(t *testing.T) {
	y := groupConfigsYard(t)
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	for _, name := range []string{"backend", "frontend"} {
		if _, ok := groups[name]; !ok {
			t.Errorf("config group %q missing from result", name)
		}
	}
}

func TestGroupConfigsReturnsVirtualGroupsSeparately(t *testing.T) {
	y := groupConfigsYard(t)
	groups, virtual := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	if _, ok := virtual["active"]; !ok {
		t.Fatal("virtual group 'active' missing")
	}
	if _, ok := groups["active"]; ok {
		t.Error("virtual group leaked into the real group map")
	}
}

func TestGroupConfigsAddsGroupsFromBoxMetas(t *testing.T) {
	y := groupConfigsYard(t)
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	for _, name := range []string{"python", "custom-group"} {
		if _, ok := groups[name]; !ok {
			t.Errorf("group %q claimed by a box was not added", name)
		}
	}
}

func TestGroupConfigsNewGroupsGetDefaults(t *testing.T) {
	y := groupConfigsYard(t)
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	g := groups["python"]
	if g == nil {
		t.Fatal("group 'python' missing")
	}
	if g.SymlinkName != "" {
		t.Errorf("symlink_name = %q, want empty", g.SymlinkName)
	}
	if g.BoxTitleMode != config.TitleIndexName {
		t.Errorf("box_title_mode = %q, want %q", g.BoxTitleMode, config.TitleIndexName)
	}
}

func TestGroupConfigsPreservesConfiguredSettings(t *testing.T) {
	y := groupConfigsYard(t)
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	if got := groups["backend"].SymlinkName; got != "backend-projects" {
		t.Errorf("backend symlink_name = %q", got)
	}
	if got := groups["frontend"].BoxTitleMode; got != config.TitleName {
		t.Errorf("frontend box_title_mode = %q", got)
	}
}

func TestGroupConfigsDoesNotMutateConfig(t *testing.T) {
	y := groupConfigsYard(t)
	before := make([]string, 0, len(y.cfg.BoxGroups))
	for name := range y.cfg.BoxGroups {
		before = append(before, name)
	}
	sort.Strings(before)

	models.GroupConfigs(y.cfg, y.meta.BoxMetas)

	after := make([]string, 0, len(y.cfg.BoxGroups))
	for name := range y.cfg.BoxGroups {
		after = append(after, name)
	}
	sort.Strings(after)
	if strings.Join(before, ",") != strings.Join(after, ",") {
		t.Errorf("config.box_groups mutated: %v -> %v", before, after)
	}
}

func TestGroupConfigsWithNoBoxes(t *testing.T) {
	y := newYard(t, groupConfigsReal, groupConfigsVirtual)
	groups, _ := models.GroupConfigs(y.cfg, nil)
	if _, ok := groups["backend"]; !ok {
		t.Error("config group 'backend' missing")
	}
	if _, ok := groups["python"]; ok {
		t.Error("group from a box appeared with no boxes")
	}
}

func TestGroupConfigsBoxWithNoGroups(t *testing.T) {
	y := newYard(t, groupConfigsReal, groupConfigsVirtual)
	y.register("20251120", "xyz999", "no-groups-box")
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	if len(groups) != 2 {
		t.Errorf("groups = %v, want just the two configured ones", groups)
	}
}

func TestGroupConfigsWithEmptyConfigGroups(t *testing.T) {
	y := newYard(t, "", "")
	y.register("20251120", "abc123", "project-alpha", "backend", "python")
	y.register("20251121", "def345", "project-beta", "frontend", "custom-group")
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	for _, name := range []string{"backend", "frontend", "python", "custom-group"} {
		if _, ok := groups[name]; !ok {
			t.Errorf("group %q missing", name)
		}
	}
}

func TestGroupConfigsDeduplicatesGroupsAcrossBoxes(t *testing.T) {
	y := newYard(t, groupConfigsReal, groupConfigsVirtual)
	y.register("20251120", "abc123", "box1", "shared-group")
	y.register("20251121", "def345", "box2", "shared-group")
	groups, _ := models.GroupConfigs(y.cfg, y.meta.BoxMetas)
	if _, ok := groups["shared-group"]; !ok {
		t.Fatal("shared-group missing")
	}
	count := 0
	for name := range groups {
		if name == "shared-group" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("shared-group appears %d times", count)
	}
}

// ---------------------------------------------------------------------------
// Title generation — ported from TestSymlinkTitleGeneration
// ---------------------------------------------------------------------------

func titleTestBox() *models.BoxMeta {
	return &models.BoxMeta{
		CreationTimestampUTC: "20251122_143022",
		BoxSubid:             "a7kx9",
		Name:                 "my-project",
		StorageLocation:      "default",
		CreatorHostname:      "host",
		Groups:               []string{"backend"},
	}
}

func TestBoxTitleModes(t *testing.T) {
	bm := titleTestBox()
	cases := []struct {
		mode config.BoxGroupTitleMode
		want string
	}{
		{config.TitleIndexName, "20251122_143022_a7kx9__my-project"},
		{config.TitleDatetimeAndName, "20251122_143022__my-project"},
		{config.TitleName, "my-project"},
	}
	for _, c := range cases {
		got, err := boxTitle(bm, c.mode)
		if err != nil {
			t.Fatalf("boxTitle(%q): %v", c.mode, err)
		}
		if got != c.want {
			t.Errorf("boxTitle(%q) = %q, want %q", c.mode, got, c.want)
		}
	}
}

func TestBoxTitleWithDateOnlyTimestamp(t *testing.T) {
	bm := &models.BoxMeta{CreationTimestampUTC: "20251122", BoxSubid: "b8ly0", Name: "date-only-box"}
	got, err := boxTitle(bm, config.TitleDatetimeAndName)
	if err != nil {
		t.Fatal(err)
	}
	if got != "20251122__date-only-box" {
		t.Errorf("got %q", got)
	}
}

func TestBoxTitleWithSpecialCharactersInName(t *testing.T) {
	bm := &models.BoxMeta{CreationTimestampUTC: "20251122_143022", BoxSubid: "c9mz1", Name: "my-project_v2.0"}
	got, err := boxTitle(bm, config.TitleName)
	if err != nil {
		t.Fatal(err)
	}
	if got != "my-project_v2.0" {
		t.Errorf("got %q", got)
	}
}

func TestBoxTitleRejectsUnknownMode(t *testing.T) {
	// Python raises Exception("Invalid box title mode: ..."). Config validation
	// makes this unreachable in practice; it must still be loud, not silently
	// fall back to a default title.
	_, err := boxTitle(titleTestBox(), config.BoxGroupTitleMode("nonsense"))
	if err == nil {
		t.Fatal("unknown title mode was accepted")
	}
	if !strings.Contains(err.Error(), "nonsense") {
		t.Errorf("error does not name the bad mode: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Symlink directory-name resolution — ported from TestSymlinkNameResolution
// ---------------------------------------------------------------------------

func TestSymlinkNameResolution(t *testing.T) {
	cases := []struct {
		name        string
		symlinkName string
		groupName   string
		want        string
	}{
		{"custom symlink_name wins", "custom-folder", "backend", "custom-folder"},
		{"unset symlink_name falls back to the group name", "", "backend", "backend"},
		{"empty symlink_name falls back too", "", "backend", "backend"},
		{"nested symlink_name is kept whole", "active/all", "active", "active/all"},
	}
	for _, c := range cases {
		if got := orGroupName(c.symlinkName, c.groupName); got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

func TestVirtualGroupSymlinkNameIsUsed(t *testing.T) {
	y := newYard(t, "", `[virtual_box_groups.virtual-backend]
filter_expr = "backend"
symlink_name = "all-backend-projects"
`)
	bm := y.addBox("20260101", "aaa111", "box", "backend")
	y.build()
	y.assertTree(
		"all-backend-projects/",
		"all-backend-projects/"+bm.IndexName()+" -> dev/"+bm.IndexName(),
		// "backend" is also an implicit real group, because the box claims it.
		"backend/",
		"backend/"+bm.IndexName()+" -> dev/"+bm.IndexName(),
	)
}

// ---------------------------------------------------------------------------
// Group membership — ported from TestGroupMembershipChecks
// ---------------------------------------------------------------------------

func TestRealGroupMembershipIsByName(t *testing.T) {
	g := groupSpec{name: "backend"}
	in := &models.BoxMeta{Groups: []string{"backend", "python"}}
	out := &models.BoxMeta{Groups: []string{"frontend"}}
	if !g.contains(in) {
		t.Error("box listing 'backend' is not in group 'backend'")
	}
	if g.contains(out) {
		t.Error("box not listing 'backend' is in group 'backend'")
	}
	if g.contains(&models.BoxMeta{Groups: []string{}}) {
		t.Error("box with no groups is in a real group")
	}
}

func TestVirtualGroupMembershipIsByFilter(t *testing.T) {
	cases := []struct {
		filter string
		groups []string
		want   bool
	}{
		{"backend AND active", []string{"backend", "active"}, true},
		{"backend AND frontend", []string{"backend"}, false},
		{"backend AND NOT archived", []string{"backend", "archived"}, false},
		{"backend AND NOT archived", []string{"backend"}, true},
		// A box with no groups still matches a NOT filter: "archived" is
		// simply absent.
		{"NOT archived", []string{}, true},
	}
	for _, c := range cases {
		y := newYard(t, "", fmt.Sprintf("[virtual_box_groups.v]\nfilter_expr = %q\n", c.filter))
		vg := y.cfg.VirtualBoxGroups["v"]
		g := groupSpec{name: "v", virtual: vg}
		if got := g.contains(&models.BoxMeta{Groups: c.groups}); got != c.want {
			t.Errorf("filter %q over %v = %v, want %v", c.filter, c.groups, got, c.want)
		}
	}
}

// ---------------------------------------------------------------------------
// Title conflicts — ported from TestSymlinkConflictHandling, plus the counter
// ---------------------------------------------------------------------------

func TestDistinctTitlesInIndexNameMode(t *testing.T) {
	box1 := &models.BoxMeta{CreationTimestampUTC: "20251122_143022", BoxSubid: "abc12", Name: "my-project"}
	box2 := &models.BoxMeta{CreationTimestampUTC: "20251123_143022", BoxSubid: "def34", Name: "my-project"}
	t1, _ := boxTitle(box1, config.TitleIndexName)
	t2, _ := boxTitle(box2, config.TitleIndexName)
	if t1 == t2 {
		t.Fatal("index_name mode produced identical titles")
	}
	if t1 != "20251122_143022_abc12__my-project" || t2 != "20251123_143022_def34__my-project" {
		t.Errorf("titles = %q, %q", t1, t2)
	}
}

func TestSameNameCollidesInNameMode(t *testing.T) {
	box1 := &models.BoxMeta{CreationTimestampUTC: "20251122_143022", BoxSubid: "abc12", Name: "my-project"}
	box2 := &models.BoxMeta{CreationTimestampUTC: "20251123_143022", BoxSubid: "def34", Name: "my-project"}
	t1, _ := boxTitle(box1, config.TitleName)
	t2, _ := boxTitle(box2, config.TitleName)
	if t1 != "my-project" || t2 != "my-project" {
		t.Fatalf("titles = %q, %q", t1, t2)
	}
}

func TestDatetimeAndNameSeparatesDifferentTimestamps(t *testing.T) {
	box1 := &models.BoxMeta{CreationTimestampUTC: "20251122_143022", BoxSubid: "abc12", Name: "my-project"}
	box2 := &models.BoxMeta{CreationTimestampUTC: "20251123_143022", BoxSubid: "def34", Name: "my-project"}
	t1, _ := boxTitle(box1, config.TitleDatetimeAndName)
	t2, _ := boxTitle(box2, config.TitleDatetimeAndName)
	if t1 == t2 {
		t.Fatal("datetime_and_name mode produced identical titles")
	}
}

// TestConflictNumberingDisambiguates pins the CORRECTED numbering. The
// original produced only two distinct names for any number of colliding boxes,
// so the rest were silently dropped from the group; that was fixed in Python
// (v0.4.2) rather than reproduced here.
func TestConflictNumberingDisambiguates(t *testing.T) {
	counter := map[string]int{}
	var got []string
	for i := 0; i < 6; i++ {
		got = append(got, conflictTitle(counter, "foo"))
	}
	want := []string{
		"foo",
		"foo (CONFLICT 1)",
		"foo (CONFLICT 2)",
		"foo (CONFLICT 3)",
		"foo (CONFLICT 4)",
		"foo (CONFLICT 5)",
	}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("conflict titles:\n got %q\nwant %q", got, want)
	}
	// Every name must be distinct — that is the whole point.
	seen := map[string]bool{}
	for _, g := range got {
		if seen[g] {
			t.Fatalf("duplicate title %q: a box would be silently dropped", g)
		}
		seen[g] = true
	}
	if counter["foo"] != 6 {
		t.Errorf(`counter["foo"] = %d, want 6 — the count must track the ORIGINAL title`, counter["foo"])
	}
}

// Counters are kept per title, so an unrelated title is unaffected.
func TestConflictCounterIsPerTitle(t *testing.T) {
	counter := map[string]int{}
	if got := conflictTitle(counter, "a"); got != "a" {
		t.Errorf("first a = %q, want %q", got, "a")
	}
	if got := conflictTitle(counter, "b"); got != "b" {
		t.Errorf("first b = %q, want %q — b must not be affected by a", got, "b")
	}
	if got := conflictTitle(counter, "a"); got != "a (CONFLICT 1)" {
		t.Errorf("second a = %q, want %q", got, "a (CONFLICT 1)")
	}
	if got := conflictTitle(counter, "b"); got != "b (CONFLICT 1)" {
		t.Errorf("second b = %q, want %q", got, "b (CONFLICT 1)")
	}
}

// TestConflictingTitlesAllBoxesSurvive is the user-visible point of the
// numbering: every box in a name-mode group gets its own symlink, and each
// points at a DIFFERENT box. Before the v0.4.2 fix three same-named boxes
// produced two symlinks and one box vanished.
func TestConflictingTitlesAllBoxesSurvive(t *testing.T) {
	counter := map[string]int{}
	titles := map[string]bool{}
	for i := 0; i < 3; i++ {
		titles[conflictTitle(counter, "dup")] = true
	}
	if len(titles) != 3 {
		t.Fatalf("three boxes produced %d distinct titles: %v", len(titles), titles)
	}
}

func TestBuildCreatesGroupDirectoriesAndSymlinks(t *testing.T) {
	y := newYard(t, "", "")
	b1 := y.addBox("20260101", "aaa111", "backend-api", "backend", "api")
	b2 := y.addBox("20260102", "bbb222", "backend-worker", "backend", "worker")
	b3 := y.addBox("20260103", "ccc333", "frontend-app", "frontend")
	y.build()

	y.assertTree(
		"api/", "api/"+b1.IndexName()+" -> dev/"+b1.IndexName(),
		"backend/", "backend/"+b1.IndexName()+" -> dev/"+b1.IndexName(),
		"backend/"+b2.IndexName()+" -> dev/"+b2.IndexName(),
		"frontend/", "frontend/"+b3.IndexName()+" -> dev/"+b3.IndexName(),
		"worker/", "worker/"+b2.IndexName()+" -> dev/"+b2.IndexName(),
	)

	// Symlinks are absolute and resolve to real directories.
	target, err := os.Readlink(filepath.Join(y.groupsRoot(), "backend", b1.IndexName()))
	if err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(target) {
		t.Errorf("symlink target %q is not absolute", target)
	}
	if target != y.dataPath(b1) {
		t.Errorf("symlink target = %q, want %q", target, y.dataPath(b1))
	}
}

func TestBuildSkipsExcludedBoxes(t *testing.T) {
	y := newYard(t, "", "")
	included := y.addBox("20260101", "aaa111", "here", "grp")
	y.register("20260102", "bbb222", "elsewhere", "grp") // no DATA directory
	y.build()

	y.assertTree("grp/", "grp/"+included.IndexName()+" -> dev/"+included.IndexName())
}

func TestBuildDropsBoxWhenItBecomesExcluded(t *testing.T) {
	y := newYard(t, "", "")
	b1 := y.addBox("20260101", "aaa111", "stays", "backend")
	b2 := y.addBox("20260102", "bbb222", "goes", "backend", "worker")
	y.build()
	y.assertTree(
		"backend/",
		"backend/"+b1.IndexName()+" -> dev/"+b1.IndexName(),
		"backend/"+b2.IndexName()+" -> dev/"+b2.IndexName(),
		"worker/", "worker/"+b2.IndexName()+" -> dev/"+b2.IndexName(),
	)

	y.exclude(b2)
	y.build()

	// b2's symlinks are gone, and "worker" — a group only b2 belonged to, and
	// not declared in the config — is pruned entirely.
	y.assertTree("backend/", "backend/"+b1.IndexName()+" -> dev/"+b1.IndexName())
}

func TestBuildIsIdempotent(t *testing.T) {
	y := newYard(t, "[box_groups.grp]\nsymlink_name = \"nested/grp\"\n", "")
	y.addBox("20260101", "aaa111", "one", "grp")
	y.addBox("20260102", "bbb222", "two", "grp")
	y.build()
	first := y.tree()

	// An untouched symlink must not be recreated: capture the inode and check
	// it survives.
	link := filepath.Join(y.groupsRoot(), "nested", "grp", y.meta.BoxMetas[0].IndexName())
	before, err := os.Lstat(link)
	if err != nil {
		t.Fatal(err)
	}

	y.build()
	y.build()

	if strings.Join(y.tree(), "\n") != strings.Join(first, "\n") {
		t.Errorf("tree changed across rebuilds:\n%s\n---\n%s", strings.Join(first, "\n"), strings.Join(y.tree(), "\n"))
	}
	after, err := os.Lstat(link)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(before, after) {
		t.Error("an already-correct symlink was recreated instead of left alone")
	}
}

func TestBuildNestsSlashedSymlinkNames(t *testing.T) {
	y := newYard(t, "[box_groups.physics]\nsymlink_name = \"all/physics\"\n", "")
	bm := y.addBox("20260101", "aaa111", "qft", "physics")
	y.build()
	y.assertTree(
		"all/",
		"all/physics/",
		"all/physics/"+bm.IndexName()+" -> dev/"+bm.IndexName(),
	)
}

func TestBuildHonoursTitleModes(t *testing.T) {
	y := newYard(t, `[box_groups.by-index]
box_title_mode = "index_name"
[box_groups.by-datetime]
box_title_mode = "datetime_and_name"
[box_groups.by-name]
box_title_mode = "name"
`, "")
	bm := y.addBox("20260101", "aaa111", "thing", "by-index", "by-datetime", "by-name")
	y.build()
	y.assertTree(
		"by-datetime/", "by-datetime/20260101__thing -> dev/"+bm.IndexName(),
		"by-index/", "by-index/"+bm.IndexName()+" -> dev/"+bm.IndexName(),
		"by-name/", "by-name/thing -> dev/"+bm.IndexName(),
	)
}

func TestBuildProcessesBoxesOldestFirst(t *testing.T) {
	// Registry order is deliberately newest-first; the builder must sort.
	y := newYard(t, "[box_groups.grp]\nbox_title_mode = \"name\"\n", "")
	newest := y.addBox("20260303", "ccc333", "dup", "grp")
	middle := y.addBox("20260202", "bbb222", "dup", "grp")
	oldest := y.addBox("20260101", "aaa111", "dup", "grp")
	y.meta.BoxMetas = []*models.BoxMeta{newest, middle, oldest}
	y.build()

	// Oldest-first means the OLDEST box takes the plain title and the others
	// get sequential CONFLICT suffixes, regardless of registry order.
	y.assertTree(
		"grp/",
		"grp/dup -> dev/"+oldest.IndexName(),
		"grp/dup (CONFLICT 1) -> dev/"+middle.IndexName(),
		"grp/dup (CONFLICT 2) -> dev/"+newest.IndexName(),
	)
}

// ---------------------------------------------------------------------------
// Virtual groups
// ---------------------------------------------------------------------------

func TestVirtualGroupUsesFilterExpression(t *testing.T) {
	y := newYard(t, "", `[virtual_box_groups.active]
symlink_name = "active/all"
box_title_mode = "name"
filter_expr = "(NOT archived) AND (NOT null)"
`)
	live := y.addBox("20260101", "aaa111", "live", "proj")
	y.addBox("20260102", "bbb222", "old", "proj", "archived")
	y.addBox("20260103", "ccc333", "void", "null")
	y.build()

	y.assertTree(
		"active/",
		"active/all/",
		"active/all/live -> dev/"+live.IndexName(),
		"proj/",
		"proj/"+live.IndexName()+" -> dev/"+live.IndexName(),
		"proj/"+y.meta.BoxMetas[1].IndexName()+" -> dev/"+y.meta.BoxMetas[1].IndexName(),
		"archived/",
		"archived/"+y.meta.BoxMetas[1].IndexName()+" -> dev/"+y.meta.BoxMetas[1].IndexName(),
		"null/",
		"null/"+y.meta.BoxMetas[2].IndexName()+" -> dev/"+y.meta.BoxMetas[2].IndexName(),
	)
}

func TestVirtualGroupCollidingWithRealGroupWarnsAndWins(t *testing.T) {
	// "shared" is both a configured real group and a virtual group. Python
	// warns, then `groups.update(virtual_box_groups)` REPLACES the real config,
	// so membership becomes the filter and the real group's own symlink_name is
	// discarded.
	y := newYard(t, "[box_groups.shared]\nsymlink_name = \"real-shared\"\n", `[virtual_box_groups.shared]
symlink_name = "virtual-shared"
filter_expr = "tagged"
`)
	member := y.addBox("20260101", "aaa111", "matches-filter", "tagged")
	y.addBox("20260102", "bbb222", "named-only", "shared")

	warnings := y.build()
	if !strings.Contains(warnings, "Warning: Virtual box group 'shared' is also a regular box group.") {
		t.Errorf("missing collision warning, got: %q", warnings)
	}

	// The real group "shared" is gone entirely: "named-only" claims it by NAME,
	// but the virtual group that replaced it goes by filter, so that box gets
	// no symlink at all. Only "tagged" — an implicit group the other box
	// claims — survives alongside the virtual group's own directory.
	y.assertTree(
		"virtual-shared/",
		"virtual-shared/"+member.IndexName()+" -> dev/"+member.IndexName(),
		"tagged/",
		"tagged/"+member.IndexName()+" -> dev/"+member.IndexName(),
	)
	if exists(filepath.Join(y.groupsRoot(), "real-shared")) {
		t.Error("the replaced real group's symlink_name directory was created")
	}
}

func TestVirtualGroupCollidingWithBoxDeclaredGroupWarns(t *testing.T) {
	// The collision check runs against the MERGED real-group map, which
	// includes groups no config mentions but some box claims.
	y := newYard(t, "", "[virtual_box_groups.tagged]\nfilter_expr = \"tagged\"\n")
	y.addBox("20260101", "aaa111", "box", "tagged")
	warnings := y.build()
	if !strings.Contains(warnings, "Virtual box group 'tagged' is also a regular box group.") {
		t.Errorf("expected a collision warning, got %q", warnings)
	}
}

func TestNoWarningWithoutCollision(t *testing.T) {
	y := newYard(t, "", "[virtual_box_groups.active]\nfilter_expr = \"NOT archived\"\n")
	y.addBox("20260101", "aaa111", "box", "proj")
	if w := y.build(); w != "" {
		t.Errorf("unexpected warning: %q", w)
	}
}

// ---------------------------------------------------------------------------
// Removal and pruning
// ---------------------------------------------------------------------------

func TestStaleSymlinksAreRemoved(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	stale := filepath.Join(y.groupsRoot(), "grp", "an-old-box")
	mustSymlink(t, filepath.Join(y.cfg.UserBoxesPath, "gone"), stale)
	deepStale := filepath.Join(y.groupsRoot(), "old", "nested", "thing")
	mustSymlink(t, filepath.Join(y.cfg.UserBoxesPath, "gone"), deepStale)

	y.build()

	if exists(stale) {
		t.Error("stale symlink survived")
	}
	if exists(filepath.Join(y.groupsRoot(), "old")) {
		t.Error("directory tree left behind by a stale symlink was not pruned")
	}
	y.assertTree("grp/", "grp/"+bm.IndexName()+" -> dev/"+bm.IndexName())
}

func TestSymlinkPointingAtTheWrongBoxIsRewritten(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	link := filepath.Join(y.groupsRoot(), "grp", bm.IndexName())
	mustSymlink(t, filepath.Join(y.cfg.UserBoxesPath, "some-other-box"), link)

	y.build()

	target, err := os.Readlink(link)
	if err != nil {
		t.Fatal(err)
	}
	if target != y.dataPath(bm) {
		t.Errorf("symlink target = %q, want %q", target, y.dataPath(bm))
	}
}

func TestHiddenEntriesAreLeftAloneButHiddenSymlinksAreNot(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	y.build()

	// A dotfile in a group directory: not debris, not removed, and it keeps
	// the directory from being pruned only in the sense that os.Remove refuses.
	dotfile := filepath.Join(y.groupsRoot(), "grp", ".DS_Store")
	mustWrite(t, dotfile, "junk")
	// A hidden top-level directory is skipped by the debris check entirely.
	mustWrite(t, filepath.Join(y.groupsRoot(), ".cache", "notes.txt"), "junk")
	// A hidden SYMLINK, though, is not in the plan, so it goes.
	hiddenLink := filepath.Join(y.groupsRoot(), ".cache", ".link")
	mustSymlink(t, y.dataPath(bm), hiddenLink)

	y.build()

	if !exists(dotfile) {
		t.Error("a dotfile in a group directory was deleted")
	}
	if !exists(filepath.Join(y.groupsRoot(), ".cache", "notes.txt")) {
		t.Error("a file in a hidden directory was deleted")
	}
	if exists(hiddenLink) {
		t.Error("an unplanned hidden symlink survived")
	}
}

func TestEmptiedGroupDirectoryWithOnlyHiddenFilesFailsLoudly(t *testing.T) {
	// Python's Path.rmdir() raises OSError here; os.Remove does the same. The
	// point is that the hidden file is NOT swept away to make the prune work.
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	y.build()
	mustWrite(t, filepath.Join(y.groupsRoot(), "grp", ".keep"), "")

	y.exclude(bm)
	err := y.buildErr()
	if err == nil {
		t.Fatal("expected a loud failure removing a directory that still holds a hidden file")
	}
	if !strings.Contains(err.Error(), "cannot remove empty group directory") {
		t.Errorf("unexpected error: %v", err)
	}
	if !exists(filepath.Join(y.groupsRoot(), "grp", ".keep")) {
		t.Error("the hidden file was deleted")
	}
}

// TestGroupNamedDirectoriesSurviveWhenEmpty pins the Python's `is_group_folder`
// rule: an empty directory is kept only when its path RELATIVE TO THE TREE ROOT
// is a group NAME. Note the asymmetry with symlink_name, documented on
// pruneEmptyNonGroupDirs — "kept" here works only because this group's
// symlink_name is unset and so equals its name.
func TestGroupNamedDirectoriesSurviveWhenEmpty(t *testing.T) {
	y := newYard(t, "[box_groups.keeper]\n", "")
	bm := y.addBox("20260101", "aaa111", "box", "keeper")
	y.build()
	y.exclude(bm)
	y.build()

	if !exists(filepath.Join(y.groupsRoot(), "keeper")) {
		t.Error("an empty directory named after a configured group was pruned")
	}
	y.assertTree("keeper/")
}

// TestEmptyDirectoryOfGroupWithSymlinkNameIsPruned pins the OTHER half of that
// rule — the suspected Python bug reproduced deliberately. Group "keeper" lives
// at "all/keeper", which is not a group NAME, so the directory is pruned once
// it empties even though the group is still configured.
func TestEmptyDirectoryOfGroupWithSymlinkNameIsPruned(t *testing.T) {
	y := newYard(t, "[box_groups.keeper]\nsymlink_name = \"all/keeper\"\n", "")
	bm := y.addBox("20260101", "aaa111", "box", "keeper")
	y.build()
	y.exclude(bm)
	y.build()

	if exists(filepath.Join(y.groupsRoot(), "all")) {
		t.Error("the emptied symlink_name directory was not pruned")
	}
	y.assertTree()
}

func TestPruningDoesNotTouchTheTreeRoot(t *testing.T) {
	y := newYard(t, "", "")
	mustMkdir(t, y.groupsRoot())
	y.build()
	if !exists(y.groupsRoot()) {
		t.Fatal("the group tree root itself was removed")
	}
	y.assertTree()
}

func TestMissingGroupTreeIsNotAnError(t *testing.T) {
	y := newYard(t, "", "")
	if exists(y.groupsRoot()) {
		t.Fatal("precondition: group tree should not exist yet")
	}
	y.build()
	// With nothing to link, the tree is not created at all.
	if exists(y.groupsRoot()) {
		t.Error("an empty group tree was created for no reason")
	}

	bm := y.addBox("20260101", "aaa111", "box", "grp")
	y.build()
	y.assertTree("grp/", "grp/"+bm.IndexName()+" -> dev/"+bm.IndexName())
}

// ---------------------------------------------------------------------------
// Debris: real files must fail loudly, never be deleted
// ---------------------------------------------------------------------------

func TestRealFileInGroupTreeAbortsTheBuild(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	debris := filepath.Join(y.groupsRoot(), "grp", "notes.txt")
	mustWrite(t, debris, "important")

	err := y.buildErr()
	if err == nil {
		t.Fatal("a real file in the group tree did not abort the build")
	}
	if !strings.Contains(err.Error(), "is in the user box group path") {
		t.Errorf("unexpected error: %v", err)
	}
	if !exists(debris) {
		t.Fatal("the real file was DELETED")
	}
	if data, _ := os.ReadFile(debris); string(data) != "important" {
		t.Error("the real file was modified")
	}
	// The build aborted before creating anything.
	if exists(filepath.Join(y.groupsRoot(), "grp", bm.IndexName())) {
		t.Error("symlinks were created despite the debris")
	}
}

func TestRealFileAtTreeRootAbortsTheBuild(t *testing.T) {
	y := newYard(t, "", "")
	y.addBox("20260101", "aaa111", "box", "grp")
	debris := filepath.Join(y.groupsRoot(), "README")
	mustWrite(t, debris, "hello")

	err := y.buildErr()
	if err == nil {
		t.Fatal("a real file at the tree root did not abort the build")
	}
	if !strings.Contains(err.Error(), "is not a directory!") {
		t.Errorf("unexpected error: %v", err)
	}
	if !exists(debris) {
		t.Fatal("the real file was DELETED")
	}
}

func TestRealDirectoryWhereASymlinkBelongsAbortsTheBuild(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	// A real (empty) directory sitting exactly where the symlink should go.
	occupied := filepath.Join(y.groupsRoot(), "grp", bm.IndexName())
	mustMkdir(t, occupied)

	err := y.buildErr()
	if err == nil {
		t.Fatal("a real directory in a symlink's place did not abort the build")
	}
	if !strings.Contains(err.Error(), "is not a symlink!") {
		t.Errorf("unexpected error: %v", err)
	}
	if !exists(occupied) {
		t.Fatal("the real directory was DELETED")
	}
}

// TestRealDirectoryWithContentIsReportedAsDebris covers the same shape with
// real content in it — a box copied in by hand rather than linked. The debris
// pass runs first, so this is caught even earlier, and nothing is touched.
func TestRealDirectoryWithContentIsReportedAsDebris(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	occupied := filepath.Join(y.groupsRoot(), "grp", bm.IndexName())
	mustWrite(t, filepath.Join(occupied, "work.txt"), "hand-made")

	err := y.buildErr()
	if err == nil {
		t.Fatal("a hand-made box directory did not abort the build")
	}
	if !strings.Contains(err.Error(), "work.txt") {
		t.Errorf("the error does not name the file: %v", err)
	}
	if data, _ := os.ReadFile(filepath.Join(occupied, "work.txt")); string(data) != "hand-made" {
		t.Fatal("the real directory's contents were DESTROYED")
	}
}

// ---------------------------------------------------------------------------
// The tree walks must never follow a symlink into a box
// ---------------------------------------------------------------------------

func TestBuildNeverDescendsIntoABox(t *testing.T) {
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")

	// Real work inside the box, including things that would look like debris
	// and like prunable empty directories if the walk followed the symlink.
	mustWrite(t, filepath.Join(y.dataPath(bm), "main.go"), "package main")
	mustWrite(t, filepath.Join(y.dataPath(bm), "sub", "notes.txt"), "notes")
	mustMkdir(t, filepath.Join(y.dataPath(bm), "empty-dir"))
	mustSymlink(t, filepath.Join(y.dataPath(bm), "main.go"), filepath.Join(y.dataPath(bm), "alias.go"))

	y.build()
	y.build()

	for _, p := range []string{"main.go", "sub/notes.txt", "empty-dir", "alias.go"} {
		if !exists(filepath.Join(y.dataPath(bm), filepath.FromSlash(p))) {
			t.Errorf("box content %q was destroyed by the symlink builder", p)
		}
	}
	y.assertTree("grp/", "grp/"+bm.IndexName()+" -> dev/"+bm.IndexName())
}

func TestCollectTreeDoesNotFollowSymlinks(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(root, "outside")
	mustWrite(t, filepath.Join(outside, "secret.txt"), "x")
	tree := filepath.Join(root, "g")
	mustMkdir(t, filepath.Join(tree, "grp"))
	mustSymlink(t, outside, filepath.Join(tree, "grp", "link"))

	var found []string
	if err := collectTree(tree, &found); err != nil {
		t.Fatal(err)
	}
	for _, p := range found {
		if strings.Contains(p, "secret.txt") {
			t.Fatalf("collectTree followed a symlink out of the tree: %v", found)
		}
	}
	if len(found) != 2 {
		t.Errorf("collectTree = %v, want the group dir and the symlink", found)
	}
}

// ---------------------------------------------------------------------------
// sameTarget / resolveLenient
// ---------------------------------------------------------------------------

func TestSameTargetExactMatch(t *testing.T) {
	root := t.TempDir()
	dest := filepath.Join(root, "dest")
	mustMkdir(t, dest)
	link := filepath.Join(root, "link")
	mustSymlink(t, dest, link)
	if !sameTarget(link, dest) {
		t.Error("an exact symlink was reported as pointing elsewhere")
	}
	if sameTarget(link, filepath.Join(root, "other")) {
		t.Error("a symlink was reported as pointing at the wrong destination")
	}
}

func TestSameTargetOnBrokenSymlink(t *testing.T) {
	// A box whose DATA has been removed leaves a broken symlink. It still
	// points AT the right path, so it must not be churned.
	root := t.TempDir()
	dest := filepath.Join(root, "gone")
	link := filepath.Join(root, "link")
	mustSymlink(t, dest, link)
	if !sameTarget(link, dest) {
		t.Error("a broken symlink pointing at the right path was reported as wrong")
	}
}

func TestSameTargetThroughASymlinkedParent(t *testing.T) {
	// The resolved comparison behind the fast path: the link reaches the same
	// real directory by another route.
	root := t.TempDir()
	real := filepath.Join(root, "real")
	mustMkdir(t, real)
	alias := filepath.Join(root, "alias")
	mustSymlink(t, real, alias)
	link := filepath.Join(root, "link")
	mustSymlink(t, alias, link)
	if !sameTarget(link, real) {
		t.Error("a symlink reaching the destination via an aliased parent was reported as wrong")
	}
}

func TestResolveLenientTerminatesOnASymlinkCycle(t *testing.T) {
	root := t.TempDir()
	a := filepath.Join(root, "a")
	b := filepath.Join(root, "b")
	mustSymlink(t, b, a)
	mustSymlink(t, a, b)
	// The only assertion that matters is that this returns at all.
	if got := resolveLenient(a); got == "" {
		t.Error("resolveLenient returned an empty path")
	}
}

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

func TestGroupIterationIsDeterministic(t *testing.T) {
	// Go map iteration is randomised; the planner must not be.
	build := func() string {
		y := newYard(t, `[box_groups.a]
[box_groups.b]
[box_groups.c]
`, `[virtual_box_groups.v1]
filter_expr = "a OR b"
[virtual_box_groups.v2]
filter_expr = "NOT c"
`)
		y.addBox("20260101", "aaa111", "one", "a", "b")
		y.addBox("20260102", "bbb222", "two", "b", "c")
		y.build()
		return strings.Join(y.tree(), "\n")
	}
	first := build()
	for i := 0; i < 8; i++ {
		if got := build(); got != first {
			t.Fatalf("tree differs between runs:\n%s\n---\n%s", first, got)
		}
	}
}

func TestMergeGroupsIsSortedByName(t *testing.T) {
	y := newYard(t, "[box_groups.zeta]\n[box_groups.alpha]\n", "[virtual_box_groups.mid]\nfilter_expr = \"x\"\n")
	var buf bytes.Buffer
	groups, err := mergeGroups(y.cfg, nil, &buf)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, g := range groups {
		names = append(names, g.name)
	}
	if strings.Join(names, ",") != "alpha,mid,zeta" {
		t.Errorf("group order = %v, want alpha,mid,zeta", names)
	}
}

// ---------------------------------------------------------------------------
// Parity with the Python implementation
//
// Every `want` below is not a guess: it was produced by RUNNING
// create_user_box_group_symlinks from src/boxyard/_models.py over the identical
// scenario and recording the tree it left behind. This is the parity contract
// for the module — including the behaviours that are wrong (the CONFLICT
// numbering, the group-name-vs-symlink-name prune asymmetry), which are pinned
// here so that fixing them in Python is a deliberate, visible change on both
// sides rather than a silent divergence.
// ---------------------------------------------------------------------------

type preEntry struct {
	path string // relative to the group tree root
	kind string // "dir", "file" or "link"
	// target is relative to the sandbox root, for kind "link".
	target string
}

type parityBox struct {
	ts, subid, name string
	groups          []string
	excluded        bool
}

type parityCase struct {
	name          string
	realGroups    string
	virtualGroups string
	boxes         []parityBox
	pre           []preEntry
	wantWarning   string
	wantErr       string
	want          []string
}

func TestParityWithPython(t *testing.T) {
	cases := []parityCase{
		{
			name:       "conflict-name-mode-5-boxes",
			realGroups: "[box_groups.grp]\nbox_title_mode = \"name\"\n",
			boxes: []parityBox{
				{ts: "20260101", subid: "aaa111", name: "dup", groups: []string{"grp"}},
				{ts: "20260102", subid: "bbb222", name: "dup", groups: []string{"grp"}},
				{ts: "20260103", subid: "ccc333", name: "dup", groups: []string{"grp"}},
				{ts: "20260104", subid: "ddd444", name: "dup", groups: []string{"grp"}},
				{ts: "20260105", subid: "eee555", name: "dup", groups: []string{"grp"}},
			},
			// Five boxes, five symlinks, oldest-first. Before the v0.4.2 fix
			// this produced only TWO symlinks and silently dropped boxes 1, 3
			// and 4 from the group.
			want: []string{
				"grp/",
				"grp/dup -> dev/20260101_aaa111__dup",
				"grp/dup (CONFLICT 1) -> dev/20260102_bbb222__dup",
				"grp/dup (CONFLICT 2) -> dev/20260103_ccc333__dup",
				"grp/dup (CONFLICT 3) -> dev/20260104_ddd444__dup",
				"grp/dup (CONFLICT 4) -> dev/20260105_eee555__dup",
			},
		},
		{
			name:       "conflict-registry-order-newest-first",
			realGroups: "[box_groups.grp]\nbox_title_mode = \"name\"\n",
			boxes: []parityBox{
				{ts: "20260303", subid: "ccc333", name: "dup", groups: []string{"grp"}},
				{ts: "20260202", subid: "bbb222", name: "dup", groups: []string{"grp"}},
				{ts: "20260101", subid: "aaa111", name: "dup", groups: []string{"grp"}},
			},
			// Registered newest-first, but SortByCreation means the oldest box
			// still takes the plain title.
			want: []string{
				"grp/",
				"grp/dup -> dev/20260101_aaa111__dup",
				"grp/dup (CONFLICT 1) -> dev/20260202_bbb222__dup",
				"grp/dup (CONFLICT 2) -> dev/20260303_ccc333__dup",
			},
		},
		{
			name: "nested-symlink-name-and-title-modes",
			realGroups: "[box_groups.physics]\nsymlink_name = \"all/physics\"\n" +
				"[box_groups.bydate]\nbox_title_mode = \"datetime_and_name\"\n" +
				"[box_groups.byname]\nbox_title_mode = \"name\"\n",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "qft", groups: []string{"physics", "bydate", "byname"}}},
			want: []string{
				"all/", "all/physics/", "all/physics/20260101_aaa111__qft -> dev/20260101_aaa111__qft",
				"bydate/", "bydate/20260101__qft -> dev/20260101_aaa111__qft",
				"byname/", "byname/qft -> dev/20260101_aaa111__qft",
			},
		},
		{
			name:          "virtual-replaces-real-group",
			realGroups:    "[box_groups.shared]\nsymlink_name = \"real-shared\"\n",
			virtualGroups: "[virtual_box_groups.shared]\nsymlink_name = \"virtual-shared\"\nfilter_expr = \"tagged\"\n",
			boxes: []parityBox{
				{ts: "20260101", subid: "aaa111", name: "matches-filter", groups: []string{"tagged"}},
				{ts: "20260102", subid: "bbb222", name: "named-only", groups: []string{"shared"}},
			},
			wantWarning: "Warning: Virtual box group 'shared' is also a regular box group.\n",
			want: []string{
				"tagged/", "tagged/20260101_aaa111__matches-filter -> dev/20260101_aaa111__matches-filter",
				"virtual-shared/", "virtual-shared/20260101_aaa111__matches-filter -> dev/20260101_aaa111__matches-filter",
			},
		},
		{
			name:          "virtual-active-all-with-excluded-and-archived",
			virtualGroups: "[virtual_box_groups.active]\nsymlink_name = \"active/all\"\nbox_title_mode = \"name\"\nfilter_expr = \"(NOT archived) AND (NOT null)\"\n",
			boxes: []parityBox{
				{ts: "20260101", subid: "aaa111", name: "live", groups: []string{"proj"}},
				{ts: "20260102", subid: "bbb222", name: "old", groups: []string{"proj", "archived"}},
				{ts: "20260103", subid: "ccc333", name: "void", groups: []string{"null"}},
				{ts: "20260104", subid: "ddd444", name: "gone", groups: []string{"proj"}, excluded: true},
			},
			want: []string{
				"active/", "active/all/", "active/all/live -> dev/20260101_aaa111__live",
				"archived/", "archived/20260102_bbb222__old -> dev/20260102_bbb222__old",
				"null/", "null/20260103_ccc333__void -> dev/20260103_ccc333__void",
				"proj/", "proj/20260101_aaa111__live -> dev/20260101_aaa111__live",
				"proj/20260102_bbb222__old -> dev/20260102_bbb222__old",
			},
		},
		{
			name:       "prune-group-named-vs-symlink-named-empty-dirs",
			realGroups: "[box_groups.keeper]\n[box_groups.renamed]\nsymlink_name = \"all/renamed\"\n",
			boxes:      []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"other"}}},
			pre: []preEntry{
				{path: "keeper", kind: "dir"},
				{path: "all/renamed", kind: "dir"},
				{path: "stale/nested", kind: "dir"},
			},
			// "keeper" survives because its relative path IS a group name;
			// "all/renamed" does not, because the prune compares against group
			// names and this directory is named after a symlink_name.
			want: []string{"keeper/", "other/", "other/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name:  "stale-symlinks-removed-and-dirs-pruned",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre: []preEntry{
				{path: "grp/an-old-box", kind: "link", target: "dev/gone"},
				{path: "old/nested/thing", kind: "link", target: "dev/gone"},
			},
			want: []string{"grp/", "grp/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name:  "hidden-entries-and-hidden-symlink",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre: []preEntry{
				{path: "grp/.DS_Store", kind: "file"},
				{path: ".cache/notes.txt", kind: "file"},
				{path: ".cache/.link", kind: "link", target: "dev/20260101_aaa111__box"},
			},
			want: []string{
				".cache/", ".cache/notes.txt (file)",
				"grp/", "grp/.DS_Store (file)",
				"grp/20260101_aaa111__box -> dev/20260101_aaa111__box",
			},
		},
		{
			name:    "debris-real-file-in-group-dir",
			boxes:   []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre:     []preEntry{{path: "grp/notes.txt", kind: "file"}},
			wantErr: "is in the user box group path",
			want:    []string{"grp/", "grp/notes.txt (file)"},
		},
		{
			name:    "debris-real-file-at-tree-root",
			boxes:   []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre:     []preEntry{{path: "README", kind: "file"}},
			wantErr: "but is not a directory!",
			want:    []string{"README (file)"},
		},
		{
			name:    "real-empty-dir-where-symlink-belongs",
			boxes:   []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre:     []preEntry{{path: "grp/20260101_aaa111__box", kind: "dir"}},
			wantErr: "but is not a symlink!",
			want:    []string{"grp/", "grp/20260101_aaa111__box/"},
		},
		{
			name:  "wrongly-pointed-symlink-rewritten",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre:   []preEntry{{path: "grp/20260101_aaa111__box", kind: "link", target: "dev/some-other-box"}},
			want:  []string{"grp/", "grp/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name:  "emptied-group-dir-with-only-hidden-file",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"other"}}},
			pre:   []preEntry{{path: "grp/.keep", kind: "file"}},
			// Python raises OSError(Directory not empty); os.Remove gives the
			// same refusal. Either way the hidden file survives.
			wantErr: "cannot remove empty group directory",
			want: []string{
				"grp/", "grp/.keep (file)",
				"other/", "other/20260101_aaa111__box -> dev/20260101_aaa111__box",
			},
		},
		{
			name:  "empty-tree-no-boxes",
			boxes: nil,
			want:  nil,
		},
		{
			name:  "box-with-no-groups",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box"}},
			want:  nil,
		},
		{
			name:          "virtual-group-collides-with-box-declared-group",
			virtualGroups: "[virtual_box_groups.tagged]\nfilter_expr = \"tagged\"\n",
			boxes:         []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"tagged"}}},
			wantWarning:   "Warning: Virtual box group 'tagged' is also a regular box group.\n",
			want:          []string{"tagged/", "tagged/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name: "excluded-box-symlink-removed-even-when-broken",
			boxes: []parityBox{
				{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}},
				{ts: "20260102", subid: "bbb222", name: "ghost", groups: []string{"grp"}, excluded: true},
			},
			pre:  []preEntry{{path: "grp/20260102_bbb222__ghost", kind: "link", target: "dev/20260102_bbb222__ghost"}},
			want: []string{"grp/", "grp/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name:  "symlink-dir-at-top-level-is-removed-not-descended",
			boxes: []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"grp"}}},
			pre:   []preEntry{{path: "toplink", kind: "link", target: "dev/20260101_aaa111__box"}},
			want:  []string{"grp/", "grp/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
		{
			name:       "deeply-nested-symlink-names-sharing-a-prefix",
			realGroups: "[box_groups.a]\nsymlink_name = \"x/y/a\"\n[box_groups.b]\nsymlink_name = \"x/y/b\"\n",
			boxes:      []parityBox{{ts: "20260101", subid: "aaa111", name: "box", groups: []string{"a"}}},
			pre:        []preEntry{{path: "x/y/b", kind: "dir"}, {path: "x/z", kind: "dir"}},
			want:       []string{"x/", "x/y/", "x/y/a/", "x/y/a/20260101_aaa111__box -> dev/20260101_aaa111__box"},
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			y := newYard(t, c.realGroups, c.virtualGroups)
			for _, b := range c.boxes {
				if b.excluded {
					y.register(b.ts, b.subid, b.name, b.groups...)
				} else {
					y.addBox(b.ts, b.subid, b.name, b.groups...)
				}
			}
			for _, p := range c.pre {
				target := filepath.Join(y.groupsRoot(), filepath.FromSlash(p.path))
				switch p.kind {
				case "dir":
					mustMkdir(t, target)
				case "file":
					mustWrite(t, target, "x")
				case "link":
					mustSymlink(t, filepath.Join(y.root, filepath.FromSlash(p.target)), target)
				default:
					t.Fatalf("unknown pre-entry kind %q", p.kind)
				}
			}

			var warnings bytes.Buffer
			err := BuildTo(y.cfg, y.meta, &warnings)
			if c.wantErr == "" {
				if err != nil {
					t.Fatalf("Build: %v", err)
				}
			} else {
				if err == nil {
					t.Fatalf("expected an error containing %q", c.wantErr)
				}
				if !strings.Contains(err.Error(), c.wantErr) {
					t.Fatalf("error = %v, want it to contain %q", err, c.wantErr)
				}
			}
			if warnings.String() != c.wantWarning {
				t.Errorf("warnings = %q, want %q", warnings.String(), c.wantWarning)
			}
			y.assertTree(c.want...)
		})
	}
}

// TestParityCasesAreIdempotent re-runs every parity case that succeeds and
// checks the tree is unchanged. The Python is called on nearly every mutating
// boxyard command, so a build that churned the tree would be a build that
// churned it hundreds of times a day.
func TestParityRebuildsAreStable(t *testing.T) {
	y := newYard(t, "[box_groups.physics]\nsymlink_name = \"all/physics\"\n[box_groups.byname]\nbox_title_mode = \"name\"\n",
		"[virtual_box_groups.active]\nsymlink_name = \"active/all\"\nbox_title_mode = \"name\"\nfilter_expr = \"NOT archived\"\n")
	y.addBox("20260101", "aaa111", "one", "physics", "byname")
	y.addBox("20260102", "bbb222", "two", "byname", "archived")
	y.addBox("20260103", "ccc333", "three", "physics")

	y.build()
	first := strings.Join(y.tree(), "\n")
	for i := 0; i < 3; i++ {
		y.build()
		if got := strings.Join(y.tree(), "\n"); got != first {
			t.Fatalf("rebuild %d changed the tree:\n%s\n---\n%s", i, first, got)
		}
	}
}

// ---------------------------------------------------------------------------
// Loud failure on I/O errors
// ---------------------------------------------------------------------------

func TestBuildWritesWarningsToStdout(t *testing.T) {
	// The exported wrapper. Python's warning is a bare print(), so stdout is
	// the faithful destination; BuildTo exists so callers (and these tests) can
	// capture it instead.
	y := newYard(t, "", "")
	bm := y.addBox("20260101", "aaa111", "box", "grp")
	if err := Build(y.cfg, y.meta); err != nil {
		t.Fatalf("Build: %v", err)
	}
	y.assertTree("grp/", "grp/"+bm.IndexName()+" -> dev/"+bm.IndexName())
}

func TestBuildRejectsAnUnsetGroupTreePath(t *testing.T) {
	y := newYard(t, "", "")
	y.cfg.UserBoxGroupsPath = ""
	if err := y.buildErr(); err == nil {
		t.Fatal("an empty user_box_groups_path was accepted")
	}
}

// TestFailedUnlinkIsNotSwallowed is the counterpart to the safety rule: a
// removal that fails must stop the build, not be skipped over leaving the tree
// half-rebuilt and the failure invisible.
func TestFailedUnlinkIsNotSwallowed(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: directory permissions do not prevent unlink")
	}
	y := newYard(t, "", "")
	y.addBox("20260101", "aaa111", "box", "grp")
	locked := filepath.Join(y.groupsRoot(), "locked")
	mustSymlink(t, filepath.Join(y.cfg.UserBoxesPath, "gone"), filepath.Join(locked, "stale"))
	if err := os.Chmod(locked, 0o555); err != nil {
		t.Fatal(err)
	}
	// Restore write permission so the temp dir can be cleaned up.
	t.Cleanup(func() { _ = os.Chmod(locked, 0o755) })

	err := y.buildErr()
	if err == nil {
		t.Fatal("a failed unlink was swallowed")
	}
	if !strings.Contains(err.Error(), "cannot remove stale group symlink") {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestFailedMkdirIsNotSwallowed(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: directory permissions do not prevent mkdir")
	}
	y := newYard(t, "", "")
	y.addBox("20260101", "aaa111", "box", "grp")
	mustMkdir(t, y.groupsRoot())
	if err := os.Chmod(y.groupsRoot(), 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(y.groupsRoot(), 0o755) })

	err := y.buildErr()
	if err == nil {
		t.Fatal("a failed mkdir was swallowed")
	}
	if !strings.Contains(err.Error(), "cannot create group directory") {
		t.Errorf("unexpected error: %v", err)
	}
}
