package models

import (
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/config"
)

func idTestConfig(charset string, length int, format config.BoxTimestampFormat) *config.Config {
	return &config.Config{
		BoxSubidCharacterSet: charset,
		BoxSubidLength:       length,
		BoxTimestampFormat:   format,
	}
}

func TestGenerateUniqueBoxIDAvoidsCollisions(t *testing.T) {
	// A one-character alphabet of length 1 makes the subid space exactly one
	// value, so the only possible id is already taken. The retry must give up
	// LOUDLY rather than return a duplicate — a duplicate box id is what the
	// `duplicate-box-id` doctor check exists to catch.
	cfg := idTestConfig("a", 1, config.TimestampDateOnly)
	ts, err := FormatCreationTimestamp(cfg, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	taken := map[string]bool{ts + "_a": true}

	if _, _, err := GenerateUniqueBoxID(cfg, taken, ""); err == nil {
		t.Fatal("generated an id that was already in use")
	} else if !strings.Contains(err.Error(), "box_subid_length") {
		t.Errorf("the error should name the setting to change, got: %v", err)
	}
}

func TestGenerateUniqueBoxIDSucceedsWhenSpaceAllows(t *testing.T) {
	cfg := idTestConfig("abcdefghijklmnopqrstuvwxyz0123456789", 6, config.TimestampDateOnly)
	ts, subid, err := GenerateUniqueBoxID(cfg, map[string]bool{}, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(subid) != 6 {
		t.Errorf("subid %q is not 6 characters", subid)
	}
	if len(ts) != 8 {
		t.Errorf("date-only timestamp %q is not 8 characters", ts)
	}
	// The pair must parse back through the same splitter the rest of the code
	// uses, or the box would be unreadable the moment it is written.
	if _, _, err := splitBoxID(ts + "_" + subid); err != nil {
		t.Errorf("the generated id does not parse: %v", err)
	}
}

func TestGenerateUniqueBoxIDIsNotDeterministic(t *testing.T) {
	// crypto/rand, not a seeded PRNG: several boxes created in one process must
	// not collide with each other.
	cfg := idTestConfig("abcdefghijklmnopqrstuvwxyz0123456789", 6, config.TimestampDateOnly)
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		_, subid, err := GenerateUniqueBoxID(cfg, map[string]bool{}, "")
		if err != nil {
			t.Fatal(err)
		}
		seen[subid] = true
	}
	if len(seen) < 45 {
		t.Errorf("only %d distinct subids in 50 draws — the source looks deterministic", len(seen))
	}
}

func TestFormatCreationTimestampHonoursTheConfiguredFormat(t *testing.T) {
	when := time.Date(2026, 8, 25, 14, 5, 9, 0, time.UTC)

	got, err := FormatCreationTimestamp(idTestConfig("a", 1, config.TimestampDateOnly), when)
	if err != nil || got != "20260825" {
		t.Errorf("date_only: got %q (%v), want 20260825", got, err)
	}
	got, err = FormatCreationTimestamp(idTestConfig("a", 1, config.TimestampDateAndTime), when)
	if err != nil || got != "20260825_140509" {
		t.Errorf("date_and_time: got %q (%v), want 20260825_140509", got, err)
	}
	// An unknown format must be loud: it decides the shape of every box id, and
	// guessing would produce ids that do not match the rest of the yard.
	if _, err := FormatCreationTimestamp(idTestConfig("a", 1, "weekly"), when); err == nil {
		t.Error("an invalid timestamp format was silently accepted")
	}
}

func TestCreateBoxSubidHandlesMultiByteCharacters(t *testing.T) {
	// Indexed over runes, not bytes — slicing a multi-byte character apart
	// would put invalid UTF-8 in a directory name.
	s, err := createBoxSubid("日本語ホスト", 8)
	if err != nil {
		t.Fatal(err)
	}
	if len([]rune(s)) != 8 {
		t.Errorf("got %d runes, want 8: %q", len([]rune(s)), s)
	}
	for _, r := range s {
		if !strings.ContainsRune("日本語ホスト", r) {
			t.Errorf("character %q is not from the configured set", r)
		}
	}
}

// A caller-supplied timestamp must be the one the collision check uses.
//
// Python let the generator pick a timestamp, checked `<generated>_<subid>`, and
// then overwrote the timestamp with the caller's — so `--creation-timestamp-utc`
// could mint a box id that had never been checked against anything.
func TestGenerateUniqueBoxIDHonoursFixedTimestamp(t *testing.T) {
	cfg := idTestConfig("ab", 1, config.TimestampDateOnly)
	// Two possible ids for a given timestamp, one of them taken. Repeated
	// because the subid is random: a generator checking the WRONG timestamp
	// still returns "b" half the time, and a single draw would pass by luck.
	for i := 0; i < 20; i++ {
		ts, subid, err := GenerateUniqueBoxID(cfg, map[string]bool{"20240102_a": true}, "20240102")
		if err != nil {
			t.Fatalf("GenerateUniqueBoxID: %v", err)
		}
		if ts != "20240102" {
			t.Fatalf("timestamp = %q, want the one that was passed in", ts)
		}
		if subid != "b" {
			t.Fatalf("subid = %q, want b (a is taken for this timestamp)", subid)
		}
	}
}

func TestGenerateUniqueBoxIDFixedTimestampExhausted(t *testing.T) {
	cfg := idTestConfig("ab", 1, config.TimestampDateOnly)
	taken := map[string]bool{"20240102_a": true, "20240102_b": true}
	if _, _, err := GenerateUniqueBoxID(cfg, taken, "20240102"); err == nil {
		t.Fatal("expected an error when every id for the fixed timestamp is taken")
	}
}
