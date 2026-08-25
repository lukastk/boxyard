package models

import (
	"crypto/rand"
	"fmt"
	"math/big"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
)

// maxIDAttempts bounds the collision retry, matching Python.
const maxIDAttempts = 100

// GenerateUniqueBoxID returns a (timestamp, subid) pair whose combined box id
// is not already in existingIDs.
//
// The retry loop is not paranoia. With a DATE_ONLY timestamp format — the live
// configuration — every box created on the same day shares the timestamp half,
// so the whole id space for that day is just the subid: 36^6 here, but only
// ~36^5 under a shorter configured length. Collisions are unlikely, not
// impossible, and a duplicate box id is the failure that produced the
// `duplicate-box-id` doctor check.
//
// Randomness comes from crypto/rand rather than math/rand: this is an
// identifier that must not collide, and a seeded PRNG shared across a process
// that creates several boxes in a loop is exactly how you get one.
// A non-empty fixedTimestamp is used verbatim in place of "now". The caller
// that passes one (`new_box`, for --creation-timestamp-utc) needs the collision
// check to run against the id it will ACTUALLY write: Python used to let this
// function pick a timestamp, check `<generated>_<subid>`, and then substitute
// its own timestamp afterwards — so the id written was never the id checked.
func GenerateUniqueBoxID(cfg *config.Config, existingIDs map[string]bool, fixedTimestamp string) (timestamp, subid string, err error) {
	for i := 0; i < maxIDAttempts; i++ {
		ts := fixedTimestamp
		if ts == "" {
			ts, err = FormatCreationTimestamp(cfg, time.Now().UTC())
			if err != nil {
				return "", "", err
			}
		}
		sub, err := createBoxSubid(cfg.BoxSubidCharacterSet, cfg.BoxSubidLength)
		if err != nil {
			return "", "", err
		}
		if !existingIDs[ts+"_"+sub] {
			return ts, sub, nil
		}
	}
	return "", "", fmt.Errorf(
		"could not generate a box id not already in use after %d attempts; "+
			"box_subid_length (%d) may be too short for the number of boxes created today",
		maxIDAttempts, cfg.BoxSubidLength)
}

// FormatCreationTimestamp renders t in the configured format. An unknown format
// is a loud error rather than a silent fallback to one of them: the format
// decides the shape of every box id, and guessing would produce ids that do not
// match the rest of the yard.
func FormatCreationTimestamp(cfg *config.Config, t time.Time) (string, error) {
	switch cfg.BoxTimestampFormat {
	case config.TimestampDateAndTime:
		return t.Format(boxconst.BoxTimestampFormat), nil
	case config.TimestampDateOnly:
		return t.Format(boxconst.BoxTimestampFormatDateOnly), nil
	default:
		return "", fmt.Errorf("invalid box timestamp format: %q", cfg.BoxTimestampFormat)
	}
}

func createBoxSubid(characterSet string, length int) (string, error) {
	if characterSet == "" {
		return "", fmt.Errorf("box_subid_character_set is empty")
	}
	if length <= 0 {
		return "", fmt.Errorf("box_subid_length must be positive, got %d", length)
	}
	// Indexed over RUNES, not bytes: a multi-byte character in the set would
	// otherwise be sliced apart and produce invalid UTF-8 in a directory name.
	runes := []rune(characterSet)
	out := make([]rune, length)
	max := big.NewInt(int64(len(runes)))
	for i := range out {
		n, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", err
		}
		out[i] = runes[n.Int64()]
	}
	return string(out), nil
}
