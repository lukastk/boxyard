// Package boxconst holds the constants shared across boxyard.
//
// Ported from the Python src/boxyard/const.py. Values here are part of
// boxyard's on-disk contract — path layout, filenames, timestamp formats — and
// are read and written by both implementations during the migration window.
// Changing one changes the format.
package boxconst

// Default locations. These are only defaults; the live values come from config.
const (
	DefaultConfigPath        = "~/.config/boxyard/config.toml"
	DefaultDataPath          = "~/.boxyard"
	DefaultUserBoxesPath     = "~/boxes"
	DefaultUserBoxGroupsPath = "~/box-groups"
)

// Paths relative to the boxyard data directory.
const (
	SyncRecordsRelPath = "sync_records"
	LocalStoreRelPath  = "local_store"
	SyncBackupsRelPath = "sync_backups"
	RemoteIndexesRel   = "remote_indexes"
	LocksRelPath       = "locks"
)

// Paths relative to a storage location's store root.
const (
	RemoteBoxesRelPath  = "boxes"
	RemoteBackupRelPath = "sync_backups"
)

// Paths relative to a box root.
const (
	BoxDataRelPath     = "data"
	BoxMetafileRelPath = "boxmeta.toml"
	BoxConfRelPath     = "conf"

	// BoxPermsManifestRelPath is a sidecar at the root of a box's DATA part
	// recording which files are executable, so +x survives sync over backends
	// that cannot carry Unix mode (SFTP). Ships as ordinary synced content.
	BoxPermsManifestRelPath = ".boxyard-perms.json"
)

// SoftInterruptCount is how many interrupts are absorbed as "finish the current
// box then stop" before the process gives up and dies.
const SoftInterruptCount = 3

// Suspend detection. time.Monotonic does not advance while the machine is
// asleep but the wall clock does, so a divergence between them means the
// machine was suspended and every connection an rclone child holds is dead.
const (
	SuspendPollIntervalSeconds    = 5.0
	SuspendDetectThresholdSeconds = 60.0
)

// RcloneListingTimeoutSeconds is a wall-clock ceiling for rclone calls whose
// work is inherently bounded (listings and metadata reads). Transfers are NOT
// bounded by this — a big box legitimately takes hours.
const RcloneListingTimeoutSeconds = 600.0

const DefaultFakeStoreRelPath = "fake_store"

// DefaultRcloneExclude is the fallback exclude list used when a box has no
// conf/.rclone_exclude of its own.
const DefaultRcloneExclude = `.venv/
.pixi/
.trunk/
node_modules/
__pycache__/

.DS_Store`

// Box timestamp layouts. These are Go layouts equivalent to the Python
// strftime formats "%Y%m%d_%H%M%S" and "%Y%m%d" respectively.
const (
	BoxTimestampFormat         = "20060102_150405"
	BoxTimestampFormatDateOnly = "20060102"
)

const (
	DefaultBoxSubidCharacterSet = "abcdefghijklmnopqrstuvwxyz0123456789"
	DefaultBoxSubidLength       = 5
	DefaultMaxConcurrentRclone  = 3
)

// Timestamp layouts matching pydantic's serialisation of a timezone-aware UTC
// datetime. Which one applies depends on the microsecond component: pydantic
// OMITS the fractional part entirely when it is zero. Use
// strict.FormatPydanticTime rather than either of these directly.
const (
	PydanticTimestampLayout            = "2006-01-02T15:04:05.000000Z"
	PydanticTimestampLayoutWholeSecond = "2006-01-02T15:04:05Z"
)

// Environment variables.
const (
	EnvBoxyardConfigPath = "BOXYARD_CONFIG_PATH"
	EnvDefaultBoxGroups  = "DEFAULT_BOX_GROUPS"
	EnvBoxyardRclone     = "BOXYARD_RCLONE"
)
