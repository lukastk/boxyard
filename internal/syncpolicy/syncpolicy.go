// Package syncpolicy decides how often each part of each box is checked, and
// whether its DATA is stored packed.
//
// Ported from pts/mod/_sync_policy.pct.py. See
// _dev/SYNC-CADENCE-DESIGN-NOTE.md for the reasoning; this is the mechanism.
//
// Three rules carry the whole design:
//
//  1. An absent policy configuration changes NOTHING. A config.toml with no
//     [sync_policies.*] tables makes DueBoxes return every box, every time —
//     exactly today's behaviour, where a supervisor loop syncs everything on a
//     fixed sleep. The feature is opt-in per fleet, and a machine that has not
//     opted in must not quietly start skipping boxes.
//  2. Resolution is per DIMENSION, not per policy. A box takes its cadence from
//     conf/sync.toml and its Compress from the group policy if that is what
//     each level states. Type and schedule are independent axes.
//  3. Ambiguity is REFUSED, never joined. A box matching two policies that
//     disagree on one dimension is an error a person settles.
//
// On the state this keeps: DueBoxes needs "when did we last CHECK this box",
// which is NOT what the sync records hold — a .rec timestamp is the last
// TRANSFER, so scheduling on it would make an unchanged box permanently overdue
// and check it every tick. So a check record is written per (box, part) under
// sync_checks/.
//
// That state degrades in ONE direction on purpose: a check record that is
// missing, unreadable or malformed means "due now" and "assume changed", never
// "up to date". Losing it costs work, never correctness.
package syncpolicy

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/pelletier/go-toml/v2"
)

// SyncChecksRelPath is the directory under boxyard_data_path holding check records.
const SyncChecksRelPath = "sync_checks"

// BoxSyncConfFilename is a box's own policy override, inside its conf/.
const BoxSyncConfFilename = "sync.toml"

// Dimensions a box may override in its own conf/. Kept explicit rather than
// derived from SyncPolicyConfig because `groups` is a policy-level concept that
// a single box must not be able to set.
var boxOverridable = []string{"data_interval", "meta_interval", "compress"}

// SchedulableParts are the parts a cadence can be set for.
//
// CONF deliberately has none: it is tiny, it rides the DATA sync that needs it
// (its rclone filters are read before the DATA transfer), and a separate
// cadence for it would be a knob with no question behind it.
var SchedulableParts = []enums.BoxPart{enums.PartData, enums.PartMeta}

func isSchedulable(part enums.BoxPart) bool {
	for _, p := range SchedulableParts {
		if p == part {
			return true
		}
	}
	return false
}

// PolicyConflict reports a box matching two policies that state DIFFERENT
// values for one dimension.
type PolicyConflict struct {
	BoxIndexName string
	Dimension    string
	Choices      map[string]string
}

func (e *PolicyConflict) Error() string {
	names := make([]string, 0, len(e.Choices))
	for name := range e.Choices {
		names = append(names, name)
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		parts = append(parts, fmt.Sprintf("%s=%s", name, e.Choices[name]))
	}
	return fmt.Sprintf(
		"Box '%s' matches policies that disagree on '%s': %s. Set '%s' in the "+
			"box's own conf/%s to settle it, or stop the box matching more than "+
			"one of these policies.",
		e.BoxIndexName, e.Dimension, strings.Join(parts, ", "), e.Dimension,
		BoxSyncConfFilename)
}

// ResolvedPolicy is the effective settings for one box, plus where each came from.
//
// Sources exists so doctor and an --explain flag can answer "why is this box on
// a 7-day cadence" without the user reverse-engineering the resolution order.
type ResolvedPolicy struct {
	DataIntervalSeconds int
	HasDataInterval     bool
	MetaIntervalSeconds int
	HasMetaInterval     bool
	Compress            bool
	Sources             map[string]string
}

// IntervalSeconds returns the cadence for a part, and whether one is set.
func (r ResolvedPolicy) IntervalSeconds(part enums.BoxPart) (int, bool) {
	switch part {
	case enums.PartData:
		return r.DataIntervalSeconds, r.HasDataInterval
	case enums.PartMeta:
		return r.MetaIntervalSeconds, r.HasMetaInterval
	}
	return 0, false
}

// ReadBoxSyncOverride reads a box's own conf/sync.toml, or an empty map.
//
// A box without the file is the normal case — almost no box will have one. A
// box WITH an unparseable one is a loud failure: it was written deliberately,
// so silently ignoring it would apply a cadence its author did not ask for and
// never say so.
func ReadBoxSyncOverride(cfg *config.Config, bm *models.BoxMeta) (map[string]any, error) {
	confDir, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return nil, err
	}
	path := filepath.Join(confDir, BoxSyncConfFilename)
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	var parsed map[string]any
	if err := toml.Unmarshal(data, &parsed); err != nil {
		return nil, fmt.Errorf("%s: not valid TOML: %w", path, err)
	}
	var unknown []string
	for key := range parsed {
		known := false
		for _, ok := range boxOverridable {
			if key == ok {
				known = true
				break
			}
		}
		if !known {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return nil, fmt.Errorf(
			"%s: unknown key(s) %v. A box may set %v; 'groups' is a policy-level "+
				"concept and cannot be set per box.",
			path, unknown, boxOverridable)
	}
	return parsed, nil
}

// MatchingPolicies returns the named policies whose Groups intersect this box's.
//
// The "default" policy is NOT included: it is the floor every box falls back
// to, so counting it as a match would make every box that matches any policy
// look ambiguous.
func MatchingPolicies(cfg *config.Config, bm *models.BoxMeta) map[string]*config.SyncPolicyConfig {
	out := map[string]*config.SyncPolicyConfig{}
	boxGroups := map[string]bool{}
	for _, g := range bm.Groups {
		boxGroups[g] = true
	}
	for name, policy := range cfg.SyncPolicies {
		if name == "default" {
			continue
		}
		for _, g := range policy.Groups {
			if boxGroups[g] {
				out[name] = policy
				break
			}
		}
	}
	return out
}

// resolveDimension resolves ONE dimension: box override, matched policies, default.
//
// Two matched policies stating the SAME value is not a conflict — a box in both
// `archived` and `dormant`, where both map to the same cold settings, has been
// asked for one thing twice. Only genuinely different values raise.
func resolveDimension(
	boxIndexName, dimension string,
	override map[string]any,
	stated map[string]string,
	def string, defSource string,
) (string, string, error) {
	if raw, ok := override[dimension]; ok && raw != nil {
		return fmt.Sprintf("%v", raw), "conf/" + BoxSyncConfFilename, nil
	}
	distinct := map[string]bool{}
	for _, v := range stated {
		distinct[v] = true
	}
	if len(distinct) > 1 {
		return "", "", &PolicyConflict{
			BoxIndexName: boxIndexName, Dimension: dimension, Choices: stated,
		}
	}
	if len(stated) > 0 {
		names := make([]string, 0, len(stated))
		for name := range stated {
			names = append(names, name)
		}
		sort.Strings(names)
		return stated[names[0]], "sync_policies." + names[0], nil
	}
	return def, defSource, nil
}

// ResolvePolicy returns the effective sync policy for one box.
//
// With no [sync_policies.*] configured at all this returns no intervals —
// meaning "no cadence, always due" — which is what keeps an un-opted-in fleet
// behaving exactly as it does today.
func ResolvePolicy(cfg *config.Config, bm *models.BoxMeta) (ResolvedPolicy, error) {
	var zero ResolvedPolicy
	override, err := ReadBoxSyncOverride(cfg, bm)
	if err != nil {
		return zero, err
	}
	matched := MatchingPolicies(cfg, bm)
	def := cfg.SyncPolicies["default"]

	statedFor := func(dimension string) map[string]string {
		out := map[string]string{}
		for name, p := range matched {
			switch dimension {
			case "data_interval":
				if p.DataInterval != "" {
					out[name] = p.DataInterval
				}
			case "meta_interval":
				if p.MetaInterval != "" {
					out[name] = p.MetaInterval
				}
			case "compress":
				if p.Compress != nil {
					out[name] = fmt.Sprintf("%v", *p.Compress)
				}
			}
		}
		return out
	}
	defaultFor := func(dimension string) (string, string) {
		if def == nil {
			return "", "unset"
		}
		switch dimension {
		case "data_interval":
			return def.DataInterval, "sync_policies.default"
		case "meta_interval":
			return def.MetaInterval, "sync_policies.default"
		case "compress":
			if def.Compress == nil {
				return "", "sync_policies.default"
			}
			return fmt.Sprintf("%v", *def.Compress), "sync_policies.default"
		}
		return "", "unset"
	}

	sources := map[string]string{}
	resolved := map[string]string{}
	for _, dimension := range boxOverridable {
		defValue, defSource := defaultFor(dimension)
		value, source, err := resolveDimension(
			bm.IndexName(), dimension, override, statedFor(dimension), defValue, defSource)
		if err != nil {
			return zero, err
		}
		resolved[dimension] = value
		sources[dimension] = source
	}

	out := ResolvedPolicy{Sources: sources}
	for _, dimension := range []string{"data_interval", "meta_interval"} {
		raw := resolved[dimension]
		if raw == "" {
			continue
		}
		seconds, err := config.ParseInterval(raw, sources[dimension]+"."+dimension)
		if err != nil {
			return zero, err
		}
		if dimension == "data_interval" {
			out.DataIntervalSeconds, out.HasDataInterval = seconds, true
		} else {
			out.MetaIntervalSeconds, out.HasMetaInterval = seconds, true
		}
	}
	switch resolved["compress"] {
	case "", "false":
		out.Compress = false
		if resolved["compress"] == "" {
			out.Sources["compress"] = "built-in default (False)"
		}
	case "true":
		out.Compress = true
	default:
		return zero, fmt.Errorf(
			"Box '%s': compress must be true or false (from %s); got %q",
			bm.IndexName(), sources["compress"], resolved["compress"])
	}
	return out, nil
}

// CheckRecord is the record of the last successful CHECK of one (box, part).
type CheckRecord struct {
	LastCheckedUnix float64 `json:"last_checked_unix"`
	RemoteModTime   *string `json:"remote_modtime"`
	RemoteSize      *int64  `json:"remote_size"`
}

// CheckRecordPath is where a (box, part) check record lives.
func CheckRecordPath(cfg *config.Config, boxIndexName string, part enums.BoxPart) string {
	return filepath.Join(cfg.BoxyardDataPath, SyncChecksRelPath, boxIndexName, string(part)+".json")
}

// ReadCheckRecord returns the record, or nil.
//
// nil means "never checked, or the record is unusable" and every caller must
// read it as "do the work". Corruption is deliberately NOT an error: this file
// is a local optimisation, regenerated by doing the sync it would have skipped,
// and refusing to sync a box because a cache file got truncated would turn a
// harmless local problem into a stalled box.
func ReadCheckRecord(cfg *config.Config, boxIndexName string, part enums.BoxPart) *CheckRecord {
	data, err := os.ReadFile(CheckRecordPath(cfg, boxIndexName, part))
	if err != nil {
		return nil
	}
	// Decoded through a generic map first so a right-key/wrong-type record
	// (last_checked_unix: "soon") is rejected the way the Python rejects it,
	// rather than silently becoming 0 and reading as "checked in 1970".
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil
	}
	value, ok := raw["last_checked_unix"].(float64)
	if !ok {
		return nil
	}
	record := &CheckRecord{LastCheckedUnix: value}
	if modtime, ok := raw["remote_modtime"].(string); ok {
		record.RemoteModTime = &modtime
	}
	if size, ok := raw["remote_size"].(float64); ok {
		n := int64(size)
		record.RemoteSize = &n
	}
	return record
}

// WriteCheckRecord records that a (box, part) was successfully checked.
//
// Written via a temp file in the same directory then renamed, so a crash
// mid-write leaves either the old record or the new one, never a truncated file
// that reads as "never checked" and silently costs a full pass.
//
// A caller with no stamp to offer must not ERASE the one already recorded: an
// ordinary multi-sync pass records only a timestamp, and wiping the stamp would
// disarm the skip filter every time an unfiltered pass ran — which is every
// machine, every day. Carrying an older stamp forward is the SAFE direction: if
// the remote moved since, the next listing reports different values and the box
// is synced. A stale stamp can only cause extra work, never a wrong skip.
func WriteCheckRecord(
	cfg *config.Config, boxIndexName string, part enums.BoxPart,
	nowUnix float64, remoteModTime *string, remoteSize *int64,
) error {
	path := CheckRecordPath(cfg, boxIndexName, part)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	if remoteModTime == nil && remoteSize == nil {
		if previous := ReadCheckRecord(cfg, boxIndexName, part); previous != nil {
			remoteModTime, remoteSize = previous.RemoteModTime, previous.RemoteSize
		}
	}
	data, err := json.Marshal(CheckRecord{
		LastCheckedUnix: nowUnix, RemoteModTime: remoteModTime, RemoteSize: remoteSize,
	})
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*.json")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, path); err != nil {
		os.Remove(tmpName)
		return err
	}
	return nil
}

// DueResult is what DueBoxes found: the boxes to sync, and the boxes it could
// not decide about.
//
// Conflicts are RETURNED rather than raised. Raising would let one
// misconfigured box halt a whole pass, and dropping it silently would be the
// fallback this codebase forbids. Instead a conflicted box is reported AND
// included in Due — syncing it is the safe direction, since the ambiguity is
// only about how OFTEN, never about whether it is allowed.
type DueResult struct {
	Due       []string
	Conflicts []*PolicyConflict
	Skipped   []string
}

// DueBoxes returns which boxes are due for a part sync at nowUnix, most overdue
// first.
//
// Pure local: reads the check records and the box groups already on disk, and
// makes ZERO remote calls. Measured at 171 ms across 590 boxes, which is what
// lets the scheduling loop wake every 15 minutes without costing anything.
//
// A box with no cadence — because no policy configured one — is ALWAYS due.
// That is what makes an un-opted-in config behave exactly as it does today.
func DueBoxes(
	cfg *config.Config, boxMetas []*models.BoxMeta,
	part enums.BoxPart, nowUnix float64,
) (DueResult, error) {
	var result DueResult
	if !isSchedulable(part) {
		return result, fmt.Errorf(
			"%s is not schedulable; schedulable parts are %v", part, SchedulableParts)
	}
	result.Due = []string{}
	result.Skipped = []string{}

	// Positive infinity, so a never-checked or conflicted box sorts ahead of
	// anything merely overdue.
	const alwaysFirst = 1e308
	overdueBy := map[string]float64{}

	for _, bm := range boxMetas {
		indexName := bm.IndexName()
		policy, err := ResolvePolicy(cfg, bm)
		if err != nil {
			var conflict *PolicyConflict
			if asConflict(err, &conflict) {
				result.Conflicts = append(result.Conflicts, conflict)
				result.Due = append(result.Due, indexName)
				overdueBy[indexName] = alwaysFirst
				continue
			}
			return result, err
		}

		interval, ok := policy.IntervalSeconds(part)
		if !ok {
			result.Due = append(result.Due, indexName)
			overdueBy[indexName] = alwaysFirst
			continue
		}

		record := ReadCheckRecord(cfg, indexName, part)
		if record == nil {
			result.Due = append(result.Due, indexName)
			overdueBy[indexName] = alwaysFirst
			continue
		}

		age := nowUnix - record.LastCheckedUnix
		if age >= float64(interval) {
			result.Due = append(result.Due, indexName)
			overdueBy[indexName] = age - float64(interval)
		} else {
			result.Skipped = append(result.Skipped, indexName)
		}
	}

	// Most overdue first. A machine that was off comes back with everything due
	// at once; ordering by overdue-ness means the longest-neglected boxes go
	// first rather than whatever order the registry happened to hold.
	sort.SliceStable(result.Due, func(i, j int) bool {
		a, b := result.Due[i], result.Due[j]
		if overdueBy[a] != overdueBy[b] {
			return overdueBy[a] > overdueBy[b]
		}
		return a < b
	})
	return result, nil
}

func asConflict(err error, target **PolicyConflict) bool {
	if c, ok := err.(*PolicyConflict); ok {
		*target = c
		return true
	}
	return false
}

// RemoteLooksUnchanged reports whether the remote object matches what was
// recorded at the last check.
//
// Both fields must be present and equal. A record that never captured them (an
// older boxyard wrote it) returns false — "assume changed" — so an upgrade
// costs one full pass rather than silently skipping every box.
//
// Size is compared as well as ModTime because rclone DOES preserve modification
// times across a push; ModTime alone is not enough to prove nothing moved.
func RemoteLooksUnchanged(record *CheckRecord, remoteModTime *string, remoteSize *int64) bool {
	if record == nil {
		return false
	}
	if record.RemoteModTime == nil || record.RemoteSize == nil {
		return false
	}
	if remoteModTime == nil || remoteSize == nil {
		return false
	}
	return *record.RemoteModTime == *remoteModTime && *record.RemoteSize == *remoteSize
}

// LocalMetaDiffersFromBase reports whether this machine holds a boxmeta edit
// the remote has not seen.
//
// No base means "cannot tell", reported as DIFFERS — the direction that costs a
// sync rather than skipping one.
//
// Compared by CONTENT rather than mtime, because the question is "is there an
// edit to push", and a file rewritten with identical content is not one.
func LocalMetaDiffersFromBase(cfg *config.Config, bm *models.BoxMeta) bool {
	base, err := models.ReadMetaBase(cfg, bm)
	if err != nil || base == nil {
		return true
	}
	onDiskPath, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		return true
	}
	if _, err := os.Stat(onDiskPath); err != nil {
		return true
	}
	onDisk, err := models.LoadBoxMetaFromPath(onDiskPath, models.BoxIdentity{
		CreationTimestampUTC: bm.CreationTimestampUTC,
		BoxSubid:             bm.BoxSubid,
		Name:                 bm.Name,
		StorageLocation:      bm.StorageLocation,
	})
	if err != nil {
		// Unreadable local boxmeta: let the real sync path deal with it and
		// report properly, rather than silently skipping the box here.
		//
		// Redundant in practice -- a failed load yields a nil BoxMeta and
		// metaEqual reports nil as unequal, so the box is needed either way --
		// and mutation testing says so. Kept because "corruption means check
		// it" should be readable at the point it is decided, not inferred from
		// how a helper treats nil.
		return true
	}
	return !metaEqual(base, onDisk)
}

func metaEqual(a, b *models.BoxMeta) bool {
	// A nil side is "not equal", stated rather than left to json.Marshal
	// rendering it as "null" and happening to differ. Mutation testing showed
	// the caller's explicit error guard is redundant BECAUSE of that
	// coincidence; the guard stays for clarity, and this makes the coincidence
	// a rule so neither place depends on the other's accident.
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	left, err1 := json.Marshal(a)
	right, err2 := json.Marshal(b)
	if err1 != nil || err2 != nil {
		return false
	}
	return string(left) == string(right)
}

// MetaBoxesNeedingSync splits boxes into (needs a META sync, provably does not).
//
// remoteListing maps index name -> (ModTime, Size) from ONE bulk rclone lsjson
// over the remote's boxes. A box missing from it is treated as needing work: it
// may be new here, deleted there, or on a storage location the listing did not
// cover, and every one of those wants the real sync path to look rather than
// this filter to decide.
//
// Skipping is ONLY ever an optimisation. Anything wrongly skipped is caught by
// the next unfiltered pass, since the DATA sync always syncs META too.
func MetaBoxesNeedingSync(
	cfg *config.Config, boxMetas []*models.BoxMeta,
	remoteListing map[string]RemoteStamp,
) (needed []string, skippable []string) {
	needed, skippable = []string{}, []string{}
	for _, bm := range boxMetas {
		indexName := bm.IndexName()
		// A missing entry yields the zero RemoteStamp, whose pointers are nil,
		// and RemoteLooksUnchanged reads a nil stamp as "changed". So an
		// unlisted box needs no special case -- and mutation testing showed the
		// `present` check that used to be here could not change any outcome,
		// which is the definition of dead code.
		stamp := remoteListing[indexName]
		record := ReadCheckRecord(cfg, indexName, enums.PartMeta)

		if !RemoteLooksUnchanged(record, stamp.ModTime, stamp.Size) {
			needed = append(needed, indexName)
			continue
		}
		if LocalMetaDiffersFromBase(cfg, bm) {
			needed = append(needed, indexName)
			continue
		}
		skippable = append(skippable, indexName)
	}
	return needed, skippable
}

// RemoteStamp is what the bulk listing reported for one remote boxmeta.
type RemoteStamp struct {
	ModTime *string
	Size    *int64
}
