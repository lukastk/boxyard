// Package strict provides the decoding discipline that boxyard's Python
// implementation gets from pydantic, and that Go does not give by default.
//
// # WHY THIS EXISTS
//
// Every boxyard model in Python inherits a StrictModel with
// ConfigDict(extra="forbid"), plus required fields and validators. That
// strictness is load-bearing, not decorative:
//
//   - A boxmeta.toml with a typo'd or unknown key fails LOUDLY, rather than
//     silently losing the value it was meant to set.
//   - `boxyard doctor`'s broken-registration check depends on a malformed
//     registration being detectable as a parse failure.
//   - The repo's standing rule is "ALWAYS prefer loud errors and exceptions
//     over silent failures" and "no defensive fallback values that mask bugs".
//
// Go's defaults invert every part of that. encoding/json and every TOML
// library silently ignore unknown keys, and a missing `storage_location`
// decodes to "" — a zero value that will be joined into a remote path and
// fail somewhere far from its cause, or worse, not fail at all.
//
// A naive port would quietly convert boxyard's loudest safety property into
// its quietest bug. So: all model decoding in this codebase goes through this
// package. Do not call toml.Unmarshal or json.Unmarshal on a model directly.
//
// NESTING: Validate() is called on the top-level value only. A type with
// nested models is responsible for calling Validate() on its children — this
// is deliberate, so that every validation path is visible in the type that
// owns it rather than happening by reflection.
package strict

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/pelletier/go-toml/v2"
)

// Validator is implemented by models that can check their own invariants.
// Decoding a Validator through this package always runs Validate.
type Validator interface {
	Validate() error
}

// FieldError describes a model field that failed validation. It carries the
// owning type and field so the message names the thing the user has to fix.
type FieldError struct {
	Type   string
	Field  string
	Reason string
}

func (e *FieldError) Error() string {
	return fmt.Sprintf("%s.%s: %s", e.Type, e.Field, e.Reason)
}

// Missing reports a required field that was absent or left at its zero value.
func Missing(typeName, field string) error {
	return &FieldError{Type: typeName, Field: field, Reason: "is required but was missing or empty"}
}

// Invalid reports a field whose value is present but unacceptable.
func Invalid(typeName, field, reason string) error {
	return &FieldError{Type: typeName, Field: field, Reason: reason}
}

// RequireNonZero returns an error if v is the zero value for its type. Use it
// in Validate methods for fields that Python marks as required.
func RequireNonZero[T comparable](typeName, field string, v T) error {
	var zero T
	if v == zero {
		return Missing(typeName, field)
	}
	return nil
}

// validate runs Validate if v implements Validator.
func validate(v any) error {
	if val, ok := v.(Validator); ok {
		return val.Validate()
	}
	return nil
}

// UnmarshalTOML decodes TOML into v, rejecting unknown keys, then validates.
func UnmarshalTOML(data []byte, v any) error {
	dec := toml.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		var se *toml.StrictMissingError
		if errors.As(err, &se) {
			return fmt.Errorf("unknown key in TOML: %s", se.String())
		}
		return fmt.Errorf("invalid TOML: %w", err)
	}
	return validate(v)
}

// UnmarshalJSON decodes JSON into v, rejecting unknown keys and trailing
// content, then validates. Rejecting trailing content matches Python's
// json.loads, which refuses "{} garbage".
func UnmarshalJSON(data []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return fmt.Errorf("invalid JSON: unexpected trailing content")
	}
	return validate(v)
}

// ReadTOMLFile reads path and decodes it strictly into v. A missing file is
// returned as-is (wrapping fs.ErrNotExist) so callers can distinguish "absent"
// — which is sometimes a legitimate state — from "present but malformed",
// which never is.
func ReadTOMLFile(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := UnmarshalTOML(data, v); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// ReadJSONFile reads path and decodes it strictly into v. As with
// ReadTOMLFile, a missing file is returned unwrapped.
func ReadJSONFile(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := UnmarshalJSON(data, v); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// FormatPydanticTime renders a time the way pydantic serialises a timezone-aware
// UTC datetime: RFC3339 with SIX fractional digits, but with the fractional
// part OMITTED ENTIRELY when the microsecond component is zero.
//
// That conditional is easy to miss and impossible to guess. A ULID carries
// milliseconds, so a sync record's timestamp normally looks like
// "2026-06-01T11:09:00.415000Z" — but roughly one ULID in a thousand lands on
// a whole second, and pydantic then writes "2026-06-01T11:09:00Z". A fixed
// six-digit layout produces ".000000Z" for those and silently diverges from
// every record the Python implementation writes.
//
// Go's own layouts cannot express this: ".000000" always pads and ".999999"
// trims ALL trailing zeros, so 415000µs would become ".415".
func FormatPydanticTime(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05Z")
	}
	return t.Format("2006-01-02T15:04:05.000000Z")
}

// MarshalJSONCompact encodes v the way pydantic's model_dump_json does: no
// spaces after separators, and no HTML escaping. Go's encoding/json escapes
// <, > and & by default, which would corrupt values round-tripping through
// the Python implementation.
//
// Sync records and tombstones written by this function land on a shared remote
// and are parsed by the Python implementation on five other machines, so the
// byte format is a cross-implementation contract.
func MarshalJSONCompact(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	// Encode appends a newline; pydantic's model_dump_json does not.
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}
