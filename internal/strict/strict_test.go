package strict

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// syncRecordShape mirrors the Python SyncRecord's field order exactly. Field
// order determines JSON key order in both pydantic and encoding/json, and the
// bytes are a cross-implementation contract.
type syncRecordShape struct {
	ULID           string `json:"ulid"`
	Timestamp      string `json:"timestamp"`
	SyncComplete   bool   `json:"sync_complete"`
	SyncerHostname string `json:"syncer_hostname"`
}

// These golden strings were produced by the live Python implementation:
//
//	uv run python -c "from boxyard._models import SyncRecord; ..."
//
// They are the format the other five machines will be reading during the
// migration window.
const (
	goldenUnicode = `{"ulid":"01KT1DYTKZZWNDP4EYPA338HJX","timestamp":"2026-06-01T11:09:00.415000Z","sync_complete":true,"syncer_hostname":"Lukas’s MacBook Pro"}`
	goldenHTML    = `{"ulid":"01KT1DYTKZZWNDP4EYPA338HJX","timestamp":"2026-06-01T11:09:00.415000Z","sync_complete":false,"syncer_hostname":"a<b>c&d"}`
)

func TestMarshalMatchesPydanticBytes(t *testing.T) {
	cases := []struct {
		name string
		rec  syncRecordShape
		want string
	}{
		{
			// Raw UTF-8, not \u-escaped: the real hostname on the user's Mac
			// contains U+2019, and pydantic emits it literally.
			name: "raw unicode hostname",
			rec: syncRecordShape{
				ULID:           "01KT1DYTKZZWNDP4EYPA338HJX",
				Timestamp:      "2026-06-01T11:09:00.415000Z",
				SyncComplete:   true,
				SyncerHostname: "Lukas’s MacBook Pro",
			},
			want: goldenUnicode,
		},
		{
			// The case Go gets wrong by default: encoding/json escapes < > &
			// to < > & unless SetEscapeHTML(false).
			name: "html characters must not be escaped",
			rec: syncRecordShape{
				ULID:           "01KT1DYTKZZWNDP4EYPA338HJX",
				Timestamp:      "2026-06-01T11:09:00.415000Z",
				SyncComplete:   false,
				SyncerHostname: "a<b>c&d",
			},
			want: goldenHTML,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := MarshalJSONCompact(c.rec)
			if err != nil {
				t.Fatalf("MarshalJSONCompact: %v", err)
			}
			if string(got) != c.want {
				t.Errorf("byte mismatch with pydantic\n got: %s\nwant: %s", got, c.want)
			}
		})
	}
}

func TestMarshalHasNoTrailingNewline(t *testing.T) {
	got, err := MarshalJSONCompact(syncRecordShape{ULID: "x"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.HasSuffix(string(got), "\n") {
		t.Error("MarshalJSONCompact left a trailing newline; model_dump_json does not")
	}
}

func TestRoundTripPythonWrittenRecord(t *testing.T) {
	var rec syncRecordShape
	if err := UnmarshalJSON([]byte(goldenUnicode), &rec); err != nil {
		t.Fatalf("could not parse a Python-written record: %v", err)
	}
	if rec.SyncerHostname != "Lukas’s MacBook Pro" {
		t.Errorf("hostname mangled: %q", rec.SyncerHostname)
	}
	if !rec.SyncComplete {
		t.Error("sync_complete lost")
	}
	back, err := MarshalJSONCompact(rec)
	if err != nil {
		t.Fatal(err)
	}
	if string(back) != goldenUnicode {
		t.Errorf("round trip not byte-stable\n got: %s\nwant: %s", back, goldenUnicode)
	}
}

// The whole point of the package: unknown keys must be a loud error, matching
// pydantic's extra="forbid".
func TestJSONRejectsUnknownFields(t *testing.T) {
	bad := `{"ulid":"x","timestamp":"t","sync_complete":true,"syncer_hostname":"h","surprise":1}`
	var rec syncRecordShape
	if err := UnmarshalJSON([]byte(bad), &rec); err == nil {
		t.Fatal("unknown JSON key was silently accepted")
	}
}

func TestJSONRejectsTrailingContent(t *testing.T) {
	var rec syncRecordShape
	if err := UnmarshalJSON([]byte(`{"ulid":"x"} garbage`), &rec); err == nil {
		t.Fatal("trailing content was silently accepted")
	}
}

type tomlShape struct {
	StorageLocation string   `toml:"storage_location"`
	Groups          []string `toml:"groups"`
}

func (s *tomlShape) Validate() error {
	if err := RequireNonZero("tomlShape", "storage_location", s.StorageLocation); err != nil {
		return err
	}
	return nil
}

func TestTOMLRejectsUnknownKeys(t *testing.T) {
	var v tomlShape
	err := UnmarshalTOML([]byte("storage_location = \"a\"\nsurprise = 1\n"), &v)
	if err == nil {
		t.Fatal("unknown TOML key was silently accepted")
	}
	if !strings.Contains(err.Error(), "unknown key") {
		t.Errorf("error should name the problem as an unknown key, got: %v", err)
	}
}

// The exact hazard this package exists to prevent: a missing required field
// decoding to "" and sailing on.
func TestValidateCatchesMissingRequiredField(t *testing.T) {
	var v tomlShape
	err := UnmarshalTOML([]byte("groups = [\"a\"]\n"), &v)
	if err == nil {
		t.Fatal("a missing required field decoded to the zero value and was accepted")
	}
	var fe *FieldError
	if !errors.As(err, &fe) {
		t.Fatalf("expected a FieldError naming the field, got %T: %v", err, err)
	}
	if fe.Field != "storage_location" {
		t.Errorf("FieldError named the wrong field: %q", fe.Field)
	}
}

func TestValidateRunsOnJSONToo(t *testing.T) {
	type jsonShape struct {
		Name string `json:"name"`
	}
	// A type without Validate must still decode fine.
	var j jsonShape
	if err := UnmarshalJSON([]byte(`{"name":"x"}`), &j); err != nil {
		t.Fatalf("non-Validator type failed to decode: %v", err)
	}
}

func TestReadFileDistinguishesAbsentFromMalformed(t *testing.T) {
	dir := t.TempDir()

	// Absent must surface as fs.ErrNotExist so callers can treat "no file" as
	// a legitimate state where the Python does.
	var v tomlShape
	err := ReadTOMLFile(filepath.Join(dir, "nope.toml"), &v)
	if !errors.Is(err, fs.ErrNotExist) {
		t.Errorf("absent file should wrap fs.ErrNotExist, got: %v", err)
	}

	// Malformed must not.
	bad := filepath.Join(dir, "bad.toml")
	if err := os.WriteFile(bad, []byte("this is not toml ==="), 0o644); err != nil {
		t.Fatal(err)
	}
	err = ReadTOMLFile(bad, &v)
	if err == nil {
		t.Fatal("malformed TOML was accepted")
	}
	if errors.Is(err, fs.ErrNotExist) {
		t.Error("malformed file was misreported as absent")
	}
	if !strings.Contains(err.Error(), bad) {
		t.Errorf("error should name the offending path, got: %v", err)
	}
}

func TestRequireNonZero(t *testing.T) {
	if err := RequireNonZero("T", "f", "x"); err != nil {
		t.Errorf("non-zero string rejected: %v", err)
	}
	if err := RequireNonZero("T", "f", ""); err == nil {
		t.Error("empty string accepted")
	}
	if err := RequireNonZero("T", "f", 0); err == nil {
		t.Error("zero int accepted")
	}
	if err := RequireNonZero("T", "f", 3); err != nil {
		t.Errorf("non-zero int rejected: %v", err)
	}
}

func TestFieldErrorMessageNamesTypeAndField(t *testing.T) {
	err := Missing("BoxMeta", "storage_location")
	if !strings.Contains(err.Error(), "BoxMeta") || !strings.Contains(err.Error(), "storage_location") {
		t.Errorf("message must name type and field, got: %v", err)
	}
}
