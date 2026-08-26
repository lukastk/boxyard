// Package enums holds the small shared enumerations used across boxyard's
// models, sync engine and CLI. Ported from src/boxyard/_enums.py.
//
// These are string types rather than integers because their values appear in
// on-disk paths (BoxPart names the sync-record file, e.g. "data.rec") and in
// CLI arguments, so the string form is the contract.
package enums

import "fmt"

// BoxPart identifies one of the three independently-synced parts of a box.
type BoxPart string

const (
	PartData BoxPart = "data"
	PartMeta BoxPart = "meta"
	PartConf BoxPart = "conf"
)

// AllBoxParts is the default set synced when none is specified. Order matters:
// conf carries the rclone filter rules and is synced BEFORE data so the rules
// are current before they are applied.
var AllBoxParts = []BoxPart{PartData, PartMeta, PartConf}

// BoxPartNames is the accepted CLI spelling of every box part, in the order
// the Python enum declares them (that order is what a --help listing shows).
// The Valid predicates read from these slices so a new member cannot be added
// to one and forgotten in the other.
var BoxPartNames = []string{string(PartData), string(PartMeta), string(PartConf)}

func (p BoxPart) Valid() bool { return contains(BoxPartNames, string(p)) }

func contains(names []string, v string) bool {
	for _, n := range names {
		if n == v {
			return true
		}
	}
	return false
}

func ParseBoxPart(s string) (BoxPart, error) {
	p := BoxPart(s)
	if !p.Valid() {
		return "", fmt.Errorf("invalid box part %q (want data, meta or conf)", s)
	}
	return p, nil
}

// SyncSetting selects how much risk a sync is allowed to take.
type SyncSetting string

const (
	// SyncCareful refuses anything that could lose data; the only setting that
	// supports automatic direction detection.
	SyncCareful SyncSetting = "careful"
	SyncReplace SyncSetting = "replace"
	SyncForce   SyncSetting = "force"
)

// SyncSettingNames is the accepted CLI spelling of every sync setting.
var SyncSettingNames = []string{string(SyncCareful), string(SyncReplace), string(SyncForce)}

func (s SyncSetting) Valid() bool { return contains(SyncSettingNames, string(s)) }

// SyncDirection is the direction of a transfer.
type SyncDirection string

const (
	DirectionPush SyncDirection = "push" // local -> remote
	DirectionPull SyncDirection = "pull" // remote -> local
)

// SyncDirectionNames is the accepted CLI spelling of every sync direction.
// NOTE: these are NOT the sync RECORD's direction names ("to_local"/
// "to_remote"), which is what a doctor hint once told people to pass here.
var SyncDirectionNames = []string{string(DirectionPush), string(DirectionPull)}

func (d SyncDirection) Valid() bool { return contains(SyncDirectionNames, string(d)) }

// RenameScope selects which sides of a box a rename applies to.
type RenameScope string

const (
	RenameLocal  RenameScope = "local"
	RenameRemote RenameScope = "remote"
	RenameBoth   RenameScope = "both"
)

// RenameScopeNames is the accepted CLI spelling of every rename scope.
var RenameScopeNames = []string{string(RenameLocal), string(RenameRemote), string(RenameBoth)}

func (r RenameScope) Valid() bool { return contains(RenameScopeNames, string(r)) }

// SyncNameDirection selects which side wins when reconciling a box's name.
type SyncNameDirection string

const (
	NameToLocal  SyncNameDirection = "to_local"
	NameToRemote SyncNameDirection = "to_remote"
)

// SyncNameDirectionNames is the accepted CLI spelling of every name-sync
// direction. These ARE "to_local"/"to_remote" — `sync-name` reconciles a
// name between the two sides and has no push/pull spelling.
var SyncNameDirectionNames = []string{string(NameToLocal), string(NameToRemote)}

func (d SyncNameDirection) Valid() bool { return contains(SyncNameDirectionNames, string(d)) }
