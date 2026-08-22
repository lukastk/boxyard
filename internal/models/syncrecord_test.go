package models

import (
	"strings"
	"testing"
)

// Goldens produced by the live Python implementation and confirmed by a
// differential run over 10 ULIDs, all byte-identical.
const (
	goldenRecord = `{"ulid":"01KT1DYTKZZWNDP4EYPA338HJX","timestamp":"2026-06-01T11:09:00.415000Z","sync_complete":true,"syncer_hostname":"h"}`
	goldenULID   = "01KT1DYTKZZWNDP4EYPA338HJX"
	goldenTime   = "2026-06-01T11:09:00.415000Z"
)

func TestMarshalMatchesPydantic(t *testing.T) {
	r := SyncRecord{ULID: goldenULID, SyncComplete: true, SyncerHostname: "h"}
	got, err := r.Marshal()
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if string(got) != goldenRecord {
		t.Errorf("byte mismatch with pydantic\n got: %s\nwant: %s", got, goldenRecord)
	}
}

// The timestamp is DERIVED from the ULID, and a ULID carries only
// milliseconds — so the last three fractional digits are always zero. Go's
// RFC3339Nano would trim them and break byte-compatibility.
func TestTimestampDerivedFromULIDKeepsSixDigits(t *testing.T) {
	r := SyncRecord{ULID: goldenULID, SyncComplete: true, SyncerHostname: "h"}
	out, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), goldenTime) {
		t.Errorf("timestamp not rendered with six fractional digits and Z: %s", out)
	}
}

// The 1-in-1000 case: a ULID on a whole second. Python writes no fractional
// part at all, and a fixed six-digit layout would emit ".000000Z". Verified
// against the live Python across 2028 ULIDs including 20 boundary cases.
func TestULIDOnWholeSecondOmitsFraction(t *testing.T) {
	const boundary = "01KT1DYT70GFETBNYAJ1Z7DV9X" // 2026-06-01T11:09:00.000Z
	r := SyncRecord{ULID: boundary, SyncComplete: true, SyncerHostname: "h"}
	out, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"ulid":"01KT1DYT70GFETBNYAJ1Z7DV9X","timestamp":"2026-06-01T11:09:00Z","sync_complete":true,"syncer_hostname":"h"}`
	if string(out) != want {
		t.Errorf("boundary ULID mismatch\n got: %s\nwant: %s", out, want)
	}
}

func TestRoundTripPythonWrittenRecord(t *testing.T) {
	r, err := UnmarshalSyncRecord([]byte(goldenRecord))
	if err != nil {
		t.Fatalf("could not parse a Python-written record: %v", err)
	}
	if r.ULID != goldenULID || !r.SyncComplete || r.SyncerHostname != "h" {
		t.Errorf("fields lost: %+v", r)
	}
	back, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if string(back) != goldenRecord {
		t.Errorf("round trip not byte-stable\n got: %s\nwant: %s", back, goldenRecord)
	}
}

// The real record on disk carries a hostname with U+2019, emitted raw.
func TestUnicodeHostnameSurvivesRoundTrip(t *testing.T) {
	const real = `{"ulid":"01KT1DYTKZZWNDP4EYPA338HJX","timestamp":"2026-06-01T11:09:00.415000Z","sync_complete":true,"syncer_hostname":"Lukas’s MacBook Pro"}`
	r, err := UnmarshalSyncRecord([]byte(real))
	if err != nil {
		t.Fatal(err)
	}
	if r.SyncerHostname != "Lukas’s MacBook Pro" {
		t.Errorf("hostname mangled: %q", r.SyncerHostname)
	}
	back, _ := r.Marshal()
	if string(back) != real {
		t.Errorf("not byte-stable\n got: %s\nwant: %s", back, real)
	}
}

func TestNewSyncRecordIsSelfConsistent(t *testing.T) {
	r := NewSyncRecord(true, "somehost")
	if err := r.Validate(); err != nil {
		t.Fatalf("freshly created record failed validation: %v", err)
	}
	if r.SyncerHostname != "somehost" {
		t.Errorf("hostname = %q", r.SyncerHostname)
	}
	if r.Timestamp == "" || r.ULID == "" {
		t.Errorf("incomplete record: %+v", r)
	}
	ts, err := r.Time()
	if err != nil {
		t.Fatal(err)
	}
	if got := ts.Format("2006-01-02T15:04:05.000000Z"); got != r.Timestamp {
		t.Errorf("Time() %q disagrees with Timestamp %q", got, r.Timestamp)
	}
}

func TestNewSyncRecordUsesLocalHostnameWhenBlank(t *testing.T) {
	r := NewSyncRecord(false, "")
	if r.SyncerHostname == "" {
		t.Error("blank hostname was not filled in from the machine")
	}
}

// The Python model_validator rejects a timestamp that disagrees with its ULID.
func TestValidateRejectsMismatchedTimestamp(t *testing.T) {
	r := SyncRecord{
		ULID:           goldenULID,
		Timestamp:      "1999-01-01T00:00:00.000000Z",
		SyncComplete:   true,
		SyncerHostname: "h",
	}
	err := r.Validate()
	if err == nil {
		t.Fatal("a timestamp disagreeing with the ULID was accepted")
	}
	if !strings.Contains(err.Error(), "timestamp") {
		t.Errorf("error should name the field, got: %v", err)
	}
}

func TestValidateRejectsBadULID(t *testing.T) {
	r := SyncRecord{ULID: "not-a-ulid", SyncComplete: true, SyncerHostname: "h"}
	if err := r.Validate(); err == nil {
		t.Fatal("invalid ULID accepted")
	}
}

func TestValidateRejectsMissingFields(t *testing.T) {
	for _, r := range []SyncRecord{
		{ULID: "", SyncComplete: true, SyncerHostname: "h"},
		{ULID: goldenULID, SyncComplete: true, SyncerHostname: ""},
	} {
		if err := r.Validate(); err == nil {
			t.Errorf("accepted an incomplete record: %+v", r)
		}
	}
}

// Python defaults `timestamp` from the ULID after validation, so a record
// without one is still coherent — accept and fill rather than reject.
func TestMissingTimestampIsFilledFromULID(t *testing.T) {
	r := SyncRecord{ULID: goldenULID, SyncComplete: true, SyncerHostname: "h"}
	if err := r.Validate(); err != nil {
		t.Fatalf("record without a timestamp rejected: %v", err)
	}
	out, err := r.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), goldenTime) {
		t.Errorf("timestamp was not filled in from the ULID: %s", out)
	}
}

func TestUnmarshalRejectsUnknownFields(t *testing.T) {
	bad := `{"ulid":"` + goldenULID + `","timestamp":"` + goldenTime + `","sync_complete":true,"syncer_hostname":"h","surprise":1}`
	if _, err := UnmarshalSyncRecord([]byte(bad)); err == nil {
		t.Fatal("unknown field in a sync record was silently accepted")
	}
}
