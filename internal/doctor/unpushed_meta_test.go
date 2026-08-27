package doctor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

// registerBox writes a box into the local store and checks its DATA out, which
// is the minimum doctor needs to see it as a real box.
func registerBox(t *testing.T, cfg *config.Config, indexName, metaBody string) *models.BoxMeta {
	t.Helper()
	boxDir := filepath.Join(cfg.LocalStorePath(), "local", indexName)
	if err := os.MkdirAll(boxDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(boxDir, "boxmeta.toml"), []byte(metaBody), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(cfg.UserBoxesPath, indexName), 0o755); err != nil {
		t.Fatal(err)
	}
	bm, err := models.LoadBoxMeta(cfg, "local", indexName)
	if err != nil {
		t.Fatal(err)
	}
	return bm
}

func setMeta(t *testing.T, cfg *config.Config, bm *models.BoxMeta, body string) {
	t.Helper()
	path, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

const metaWork = "creator_hostname = \"test\"\nparents = []\ngroups = [\"work\"]\n"

func TestDoctorReportsAnUnpushedMetaEdit(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_aaaaa__pending", metaWork)

	// Agree with the remote, then edit locally without pushing — which is what
	// `add-to-group` does by default.
	if err := models.RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}
	setMeta(t, cfg, bm, "creator_hostname = \"test\"\nparents = []\ngroups = [\"work\", \"archived\"]\n")

	f := findings(runDoctor(t, cfg), "unpushed-meta-edit")
	if len(f) != 1 {
		t.Fatalf("got %d findings, want 1: %+v", len(f), f)
	}
	if !strings.Contains(f[0].Message, bm.IndexName()) || !strings.Contains(f[0].Message, "groups") {
		t.Errorf("message = %s", f[0].Message)
	}
	// Scoped to META: a whole-box push would move DATA too.
	if !strings.Contains(f[0].Hint, "--sync-choices meta") {
		t.Errorf("hint = %s", f[0].Hint)
	}
}

func TestDoctorSaysNothingWithoutABase(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_bbbbb__never-synced", metaWork)
	setMeta(t, cfg, bm, "creator_hostname = \"test\"\nparents = []\ngroups = [\"work\", \"archived\"]\n")

	// An absence, not a fault: reporting it would flag every box on every
	// machine on the day of the upgrade.
	if f := findings(runDoctor(t, cfg), "unpushed-meta-edit"); len(f) != 0 {
		t.Errorf("got %d findings for a box with no base: %+v", len(f), f)
	}
}

func TestDoctorSaysNothingWhenOnlyTheFileBytesChanged(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_ccccc__settled", metaWork)
	if err := models.RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}

	// Same content, different bytes. Comparing bytes rather than FIELDS would
	// report this and train the reader to ignore the check.
	setMeta(t, cfg, bm, "groups = [\"work\"]\nparents = []\ncreator_hostname = \"test\"\n\n")

	if f := findings(runDoctor(t, cfg), "unpushed-meta-edit"); len(f) != 0 {
		t.Errorf("got %d findings for an unchanged boxmeta: %+v", len(f), f)
	}
}

func TestDoctorNamesEveryChangedField(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_ddddd__several", metaWork)
	if err := models.RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}
	setMeta(t, cfg, bm, "creator_hostname = \"test\"\nparents = [\"20260101_zzzzz\"]\n"+
		"groups = [\"work\", \"archived\"]\nwrite_owner = \"somewhere\"\n")

	f := findings(runDoctor(t, cfg), "unpushed-meta-edit")
	if len(f) != 1 {
		t.Fatalf("got %d findings, want 1: %+v", len(f), f)
	}
	for _, want := range []string{"groups", "parents", "write_owner"} {
		if !strings.Contains(f[0].Message, want) {
			t.Errorf("%q missing from: %s", want, f[0].Message)
		}
	}
}

// `unowned-box`: a box included here that no machine has claimed. Nothing
// surfaced this before — `include` prints a one-line nudge and that was all.
// Scoped to boxes INCLUDED here for the same reason `claim` refuses a box that
// is not: a machine that does not hold a box cannot become its owner, so a
// finding about one would name a command that fails.

func TestDoctorReportsAnUnownedBox(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_eeeee__ownerless", metaWork)

	f := findings(runDoctor(t, cfg), "unowned-box")
	if len(f) != 1 {
		t.Fatalf("got %d findings, want 1: %+v", len(f), f)
	}
	if !strings.Contains(f[0].Hint, "claim -r '"+bm.IndexName()+"'") {
		t.Errorf("the hint does not name the command that fixes it: %s", f[0].Hint)
	}
}

func TestDoctorDoesNotReportAClaimedBox(t *testing.T) {
	cfg := doctorYard(t)
	registerBox(t, cfg, "20260822_fffff__owned",
		"creator_hostname = \"test\"\nparents = []\ngroups = []\nwrite_owner = \"testmachine\"\n")

	if f := findings(runDoctor(t, cfg), "unowned-box"); len(f) != 0 {
		t.Errorf("a claimed box was reported: %+v", f)
	}
}

func TestDoctorDoesNotReportAnUnownedBoxNotHeldHere(t *testing.T) {
	cfg := doctorYard(t)
	bm := registerBox(t, cfg, "20260822_ggggg__elsewhere", metaWork)
	// Registered but not checked out — the shape of the ~470 boxes a machine
	// knows about and does not hold. `claim` refuses these, so a finding would
	// name a command that fails.
	if err := os.RemoveAll(filepath.Join(cfg.UserBoxesPath, bm.IndexName())); err != nil {
		t.Fatal(err)
	}

	if f := findings(runDoctor(t, cfg), "unowned-box"); len(f) != 0 {
		t.Errorf("a box not held here was reported: %+v", f)
	}
}
