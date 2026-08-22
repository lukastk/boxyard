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

func (p BoxPart) Valid() bool {
	return p == PartData || p == PartMeta || p == PartConf
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

func (s SyncSetting) Valid() bool {
	return s == SyncCareful || s == SyncReplace || s == SyncForce
}

// SyncDirection is the direction of a transfer.
type SyncDirection string

const (
	DirectionPush SyncDirection = "push" // local -> remote
	DirectionPull SyncDirection = "pull" // remote -> local
)

func (d SyncDirection) Valid() bool {
	return d == DirectionPush || d == DirectionPull
}

// RenameScope selects which sides of a box a rename applies to.
type RenameScope string

const (
	RenameLocal  RenameScope = "local"
	RenameRemote RenameScope = "remote"
	RenameBoth   RenameScope = "both"
)

func (r RenameScope) Valid() bool {
	return r == RenameLocal || r == RenameRemote || r == RenameBoth
}

// SyncNameDirection selects which side wins when reconciling a box's name.
type SyncNameDirection string

const (
	NameToLocal  SyncNameDirection = "to_local"
	NameToRemote SyncNameDirection = "to_remote"
)

func (d SyncNameDirection) Valid() bool {
	return d == NameToLocal || d == NameToRemote
}
