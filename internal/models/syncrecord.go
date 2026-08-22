package models

import (
	"fmt"
	"time"

	"github.com/lukastk/boxyard/internal/strict"
	"github.com/lukastk/boxyard/internal/sysinfo"
	"github.com/oklog/ulid/v2"
)

// SyncRecord marks the outcome of one sync of one box part.
//
// A matching pair of records — one local, one remote — with the same ULID means
// the two sides are in sync. An INCOMPLETE record means a transfer was
// interrupted and the side it names may be corrupt. get_sync_status reads these
// to decide what, if anything, is safe to do.
//
// THE JSON IS A CROSS-IMPLEMENTATION CONTRACT. These files are written to the
// shared remote and read by the Python implementation on five other machines,
// so field order, separators and the timestamp layout must all match pydantic's
// model_dump_json byte for byte. See internal/strict.
type SyncRecord struct {
	ULID           string `json:"ulid"`
	Timestamp      string `json:"timestamp"`
	SyncComplete   bool   `json:"sync_complete"`
	SyncerHostname string `json:"syncer_hostname"`
}

// ulidTimestamp renders a ULID's embedded millisecond time the way pydantic
// serialises the Python ULID's .datetime.
//
// Usually that is six fractional digits — "2026-06-01T11:09:00.415000Z" —
// because a ULID carries milliseconds. But a ULID landing on a whole second
// has a zero microsecond component, and pydantic then omits the fraction
// entirely. See strict.FormatPydanticTime.
func ulidTimestamp(u ulid.ULID) string {
	return strict.FormatPydanticTime(ulid.Time(u.Time()))
}

// NewSyncRecord creates a record stamped with a fresh ULID and this machine's
// hostname. Pass an empty hostname to use the local one.
func NewSyncRecord(syncComplete bool, syncerHostname string) SyncRecord {
	if syncerHostname == "" {
		syncerHostname = sysinfo.Hostname()
	}
	u := ulid.Make()
	return SyncRecord{
		ULID:           u.String(),
		Timestamp:      ulidTimestamp(u),
		SyncComplete:   syncComplete,
		SyncerHostname: syncerHostname,
	}
}

// ParsedULID returns the record's ULID.
func (r SyncRecord) ParsedULID() (ulid.ULID, error) {
	u, err := ulid.ParseStrict(r.ULID)
	if err != nil {
		return ulid.ULID{}, fmt.Errorf("sync record has an invalid ULID %q: %w", r.ULID, err)
	}
	return u, nil
}

// Time returns the record's instant, taken from the ULID rather than the
// timestamp field. Validate guarantees the two agree.
func (r SyncRecord) Time() (time.Time, error) {
	u, err := r.ParsedULID()
	if err != nil {
		return time.Time{}, err
	}
	return ulid.Time(u.Time()).UTC(), nil
}

// Validate mirrors the Python model_validator: the timestamp field is derived
// from the ULID and must agree with it.
func (r SyncRecord) Validate() error {
	const t = "SyncRecord"
	if err := strict.RequireNonZero(t, "ulid", r.ULID); err != nil {
		return err
	}
	if err := strict.RequireNonZero(t, "syncer_hostname", r.SyncerHostname); err != nil {
		return err
	}
	u, err := r.ParsedULID()
	if err != nil {
		return strict.Invalid(t, "ulid", err.Error())
	}
	want := ulidTimestamp(u)
	if r.Timestamp == "" {
		// Python defaults the field from the ULID after validation. A record
		// missing it is still coherent, so accept and fill rather than reject.
		return nil
	}
	if r.Timestamp != want {
		return strict.Invalid(t, "timestamp",
			fmt.Sprintf("should be set to the ULID's datetime %q, got %q", want, r.Timestamp))
	}
	return nil
}

// Marshal renders the record exactly as pydantic's model_dump_json does.
func (r SyncRecord) Marshal() ([]byte, error) {
	if err := r.Validate(); err != nil {
		return nil, err
	}
	if r.Timestamp == "" {
		u, err := r.ParsedULID()
		if err != nil {
			return nil, err
		}
		r.Timestamp = ulidTimestamp(u)
	}
	return strict.MarshalJSONCompact(r)
}

// UnmarshalSyncRecord parses a record written by either implementation.
func UnmarshalSyncRecord(data []byte) (SyncRecord, error) {
	var r SyncRecord
	if err := strict.UnmarshalJSON(data, &r); err != nil {
		return SyncRecord{}, err
	}
	return r, nil
}
