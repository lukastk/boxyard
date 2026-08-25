package doctor

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
)

func TestIsValidIndexName(t *testing.T) {
	const charset = "abcdefghijklmnopqrstuvwxyz0123456789"
	cases := map[string]bool{
		// Current format: date-only timestamp + 6-char subid.
		"20260620_tbmxs5__pi-rpc-set-model": true,
		// Names may contain spaces, and even "__".
		"20240805_000000_E4Dzy__Notion integration test": true,
		"20260623_9qkepq__sesh-ui__ui-links":             true,
		// Legacy: date+time timestamp + 5-char MIXED-CASE subid. A long-lived
		// yard is full of these, and calling them malformed would bury the real
		// findings.
		"20250427_000000_VOGj7__name": true,
		"20251210_bnesz__my-servers":  true,

		"no-separator":                false,
		"20260620_tbmxs5__":           false,
		"notadate_tbmxs5__name":       false,
		"20261340_tbmxs5__name":       false, // month 13
		"20260620_TOOLONGSUBID__name": false,
		"20260620_a_b_c_d__name":      false,
	}
	for name, want := range cases {
		if got := IsValidIndexName(name, charset, 6); got != want {
			t.Errorf("IsValidIndexName(%q) = %v, want %v", name, got, want)
		}
	}
}

// doctorYard builds an isolated yard. As everywhere in this suite, the paths
// are all under t.TempDir(): the default config names the real ~/dev.
func doctorYard(t *testing.T) *config.Config {
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
		"machine_name = \"testmachine\"\n" +
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
	cfg, err := config.Load(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(cfg.LocalStorePath(), "local"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg.DefaultRcloneExcludePath(), []byte(".DS_Store\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return cfg
}

func runDoctor(t *testing.T, cfg *config.Config) *Report {
	t.Helper()
	// nil store: the remote checks are skipped, which is what --no-remote does.
	report, err := Run(context.Background(), cfg, nil, Options{})
	if err != nil {
		t.Fatal(err)
	}
	return report
}

func findings(r *Report, check string) []Finding { return r.Checks[check].Findings }

// Doctor is READ-ONLY. It must not create the registry cache, which is exactly
// what GetBoxyardMeta would do — hence the separate tolerant scan.
func TestDoctorNeverWritesTheCache(t *testing.T) {
	cfg := doctorYard(t)
	_ = runDoctor(t, cfg)
	if _, err := os.Stat(cfg.BoxyardMetaPath()); !os.IsNotExist(err) {
		t.Fatal("doctor created the registry cache; it must never mutate anything")
	}
}

func TestDoctorReportsAnUnregisteredFolder(t *testing.T) {
	cfg := doctorYard(t)
	if err := os.MkdirAll(filepath.Join(cfg.UserBoxesPath, "20240102_aaaaa__stray"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	f := findings(r, "unregistered-folder")
	if len(f) != 1 || !strings.Contains(f[0].Message, "is not a registered box") {
		t.Fatalf("got %+v", f)
	}
	// The hint must name a command that is safe to run verbatim.
	if !strings.Contains(f[0].Hint, "boxyard new --from") {
		t.Errorf("the hint does not name the fix: %s", f[0].Hint)
	}
	// A well-formed name must NOT also be reported as malformed.
	if len(findings(r, "malformed-name")) != 0 {
		t.Errorf("a valid index name was called malformed: %+v", findings(r, "malformed-name"))
	}
}

func TestDoctorReportsAMalformedName(t *testing.T) {
	cfg := doctorYard(t)
	if err := os.MkdirAll(filepath.Join(cfg.UserBoxesPath, "just-a-folder"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	if len(findings(r, "malformed-name")) != 1 {
		t.Fatalf("got %+v", findings(r, "malformed-name"))
	}
	// It is BOTH unregistered and malformed: the two checks report
	// independently, so a badly named directory shows up under each.
	if len(findings(r, "unregistered-folder")) != 1 {
		t.Fatalf("got %+v", findings(r, "unregistered-folder"))
	}
}

func TestDoctorReportsABrokenRegistration(t *testing.T) {
	cfg := doctorYard(t)
	reg := filepath.Join(cfg.LocalStorePath(), "local", "20240102_aaaaa__broken")
	if err := os.MkdirAll(reg, 0o755); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	f := findings(r, "broken-registration")
	if len(f) != 1 || !strings.Contains(f[0].Message, "has no "+boxconst.BoxMetafileRelPath) {
		t.Fatalf("got %+v", f)
	}
}

func TestDoctorReportsAnInterruptedSync(t *testing.T) {
	cfg := doctorYard(t)
	recDir := filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, "20240102_aaaaa__box")
	if err := os.MkdirAll(recDir, 0o755); err != nil {
		t.Fatal(err)
	}
	rec := `{"ulid":"01M0WJMW3Y86RB5V9ECGABVFMM","timestamp":"2026-08-25T13:44:37.758000Z","sync_complete":false,"syncer_hostname":"somewhere"}`
	if err := os.WriteFile(filepath.Join(recDir, "data.rec"), []byte(rec), 0o644); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	// With no registration for it, the records are ORPHANED — that check wins
	// and the record is not opened at all.
	if len(findings(r, "orphaned-sync-records")) != 1 {
		t.Fatalf("orphaned: %+v", findings(r, "orphaned-sync-records"))
	}
	if len(findings(r, "interrupted-sync")) != 0 {
		t.Fatalf("interrupted: %+v", findings(r, "interrupted-sync"))
	}
}

func TestDoctorReportsGroupTreeDebris(t *testing.T) {
	cfg := doctorYard(t)
	if err := os.WriteFile(filepath.Join(cfg.UserBoxGroupsPath, "notes.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(cfg.UserBoxesPath, "nowhere"),
		filepath.Join(cfg.UserBoxGroupsPath, "dangling")); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	if len(findings(r, "group-tree-debris")) != 1 {
		t.Fatalf("debris: %+v", findings(r, "group-tree-debris"))
	}
	if len(findings(r, "dangling-symlinks")) != 1 {
		t.Fatalf("dangling: %+v", findings(r, "dangling-symlinks"))
	}
}

func TestDoctorReportsAMissingMachineName(t *testing.T) {
	cfg := doctorYard(t)
	cfg.MachineName = ""
	r := runDoctor(t, cfg)
	f := findings(r, "machine-name-unset")
	if len(f) != 1 {
		t.Fatalf("got %+v", f)
	}
	if !strings.Contains(f[0].Hint, boxconst.EnvBoxyardMachineName) {
		t.Errorf("the hint does not name the env var: %s", f[0].Hint)
	}
}

func TestDoctorReportsAnUnknownStorageLocation(t *testing.T) {
	cfg := doctorYard(t)
	if err := os.MkdirAll(filepath.Join(cfg.LocalStorePath(), "retired"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := runDoctor(t, cfg)
	if len(findings(r, "unknown-storage-location")) != 1 {
		t.Fatalf("got %+v", findings(r, "unknown-storage-location"))
	}
}

func TestDoctorRejectsAnUnknownStorageLocationArgument(t *testing.T) {
	cfg := doctorYard(t)
	if _, err := Run(context.Background(), cfg, nil, Options{StorageLocations: []string{"nope"}}); err == nil {
		t.Fatal("expected an error")
	}
}

// The duplicate-box-id hint must never suggest `delete`: it purges the remote
// and writes a tombstone keyed by box id, so following it would destroy BOTH
// boxes. That is a real scar, not a hypothetical.
func TestDuplicateBoxIDHintIsNotDestructive(t *testing.T) {
	cfg := doctorYard(t)
	for _, name := range []string{"20240102_aaaaa__one", "20240102_aaaaa__two"} {
		reg := filepath.Join(cfg.LocalStorePath(), "local", name)
		if err := os.MkdirAll(reg, 0o755); err != nil {
			t.Fatal(err)
		}
		body := "storage_location = \"local\"\ncreator_hostname = \"h\"\ngroups = []\nparents = []\n"
		if err := os.WriteFile(filepath.Join(reg, boxconst.BoxMetafileRelPath), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	r := runDoctor(t, cfg)
	f := findings(r, "duplicate-box-id")
	if len(f) != 1 {
		t.Fatalf("got %+v", f)
	}
	if strings.Contains(f[0].Hint, "boxyard delete") {
		t.Fatalf("the hint suggests a destructive command: %s", f[0].Hint)
	}
	if !strings.Contains(f[0].Hint, "Do NOT re-create the box") {
		t.Errorf("the hint lost its warning: %s", f[0].Hint)
	}
}
