// Package perms implements boxyard's exec-bit sidecar manifest.
//
// # WHY THIS EXISTS
//
// boxyard's only real storage backend is `hetzner-box`, an rclone `sftp`
// remote, and rclone's SFTP backend has no metadata support at all (see
// rclone#7310, still open). So a file's Unix mode has nowhere to live on the
// wire: everything boxyard pushes lands on the far side with the destination's
// umask, and every `+x` is silently dropped. `--metadata` does not save us
// either — rclone only syncs metadata when the object itself is re-uploaded,
// so a bare `chmod +x foo.sh` (no content change) would never propagate.
//
// The fix (research doc `_dev/research/preserving-permissions.md`, option B1,
// shipped in Python v0.3.0) is to carry the mode as ordinary *content*: a
// sidecar `.boxyard-perms.json` at the root of a box's DATA part, generated
// before a push and applied after a pull. Same trick rclone itself uses for
// symlinks with `--links`/`.rclonelink`, and the same granularity git uses —
// only the executable bit, nothing else.
//
// v1 is deliberately ADDITIVE-ONLY: applying a manifest restores `+x` and
// never clears it. Removing an exec bit remotely will not remove it locally.
//
// # THE FILE FORMAT IS A CROSS-IMPLEMENTATION CONTRACT
//
// `.boxyard-perms.json` is synced to a shared remote and is read and written
// by BOTH the Python and Go implementations, on six machines, throughout the
// migration window. Python writes it with:
//
//	json.dumps({"version": 1, "executable": [...]}, indent=1, sort_keys=True) + "\n"
//
// which produces (real example, from ~/dev/…__trading-agent-alpaca):
//
//	{
//	 "executable": [
//	  "install-cron.sh",
//	  "run-session.sh"
//	 ],
//	 "version": 1
//	}
//
// One-space indent, keys sorted (so "executable" precedes "version"), item
// separator "," with no trailing space, key separator ": ", trailing newline.
//
// encoding/json cannot produce those bytes: it escapes `<`, `>` and `&`, it
// emits non-ASCII raw where Python's ensure_ascii=True escapes it as \uXXXX,
// and it has no notion of Python's surrogateescape for filenames that are not
// valid UTF-8. Byte-identical output matters for more than tidiness —
// GenerateManifest only rewrites when the bytes change, so a Go encoder that
// disagreed with Python's by a single byte would make every box flip-flop
// between the two implementations, dirtying mtimes and re-transferring the
// manifest on every sync, forever. So this package carries its own encoder and
// string decoder, pinned to Python's output by golden-byte tests.
//
// # DELIBERATE DEVIATIONS FROM THE PYTHON
//
// Two, both in the direction of the repo's "loud errors, never silent
// fallbacks" rule; everything else — which files are walked, symlink handling,
// the exact mode arithmetic — matches the Python exactly, bug-for-bug (see
// buildManifest's note about non-regular files).
//
//  1. A present-but-corrupt manifest: Python prints a warning to stderr and
//     returns []. It does that because it has no error channel back to a
//     caller who wants the sync to finish anyway. Go does, so ApplyManifest
//     returns a descriptive error and the *caller* (the sync path) decides
//     whether to warn and continue. An ABSENT manifest stays what it is in
//     Python — a legitimate, expected state (boxes predating v0.3.0 have
//     none), reported as (nil, nil).
//
//  2. An unreadable directory mid-walk: Python's os.walk swallows it. That is
//     a silent data-integrity hole — a partial walk writes a *shrunken*
//     manifest, which pushes to the remote and permanently loses exec bits for
//     every file it failed to see. BuildManifest returns the error instead. A
//     file that vanishes between readdir and stat is still tolerated, because
//     that race is genuinely expected in a live box.
package perms

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/lukastk/boxyard/internal/boxconst"
)

// ManifestVersion is the schema version this package writes. v1 = exec bit
// only, additive-only on apply.
const ManifestVersion = 1

// Manifest is the decoded shape of `.boxyard-perms.json`.
//
// Executable holds slash-separated paths relative to the box's DATA root,
// sorted, listing every file whose owner-execute bit was set when the manifest
// was generated.
//
// There are no struct tags here on purpose: this type is never handed to
// encoding/json. Encoding goes through encode(), which reproduces Python's
// json.dumps byte for byte; decoding goes through decodeManifest().
type Manifest struct {
	Version    int
	Executable []string
}

// prunedDirNames are directory names never descended into when building a
// manifest. The first five mirror boxconst.DefaultRcloneExclude — boxyard does
// not sync them, so their exec bits are not part of the box's synced content
// and would only bloat the manifest with thousands of .venv/node_modules
// entries that do not exist on the remote. `.git` *is* synced, but its
// internal executables are noise (disabled `*.sample` hooks), so its exec bits
// are intentionally not preserved.
//
// Matching is on the exact directory name, at any depth. `.venv-scallop` is
// NOT pruned; real manifests in ~/dev contain its contents.
var prunedDirNames = map[string]struct{}{
	".venv":        {},
	".pixi":        {},
	".trunk":       {},
	"node_modules": {},
	"__pycache__":  {},
	".git":         {},
}

// BuildManifest walks root and returns the manifest describing which files
// under it are executable. It does not touch the filesystem beyond reading.
//
// Only the owner-execute bit (S_IXUSR, 0o100) is considered. Symlinks are
// excluded — rclone ships those separately via --links — as are `.DS_Store`
// files, the pruned directories above, and the root-level manifest file
// itself. A manifest file nested deeper (`sub/.boxyard-perms.json`) is *not*
// excluded, matching Python, which compares against the root path only.
//
// An empty (or entirely non-executable) tree yields a valid manifest with an
// empty Executable list, not an error.
func BuildManifest(root string) (Manifest, error) {
	info, err := os.Stat(root)
	if err != nil {
		return Manifest{}, fmt.Errorf("perms: cannot read %q: %w", root, err)
	}
	if !info.IsDir() {
		return Manifest{}, fmt.Errorf("perms: %q is not a directory", root)
	}

	manifestPath := filepath.Join(root, boxconst.BoxPermsManifestRelPath)
	execs := []string{}

	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			// Deliberate deviation from Python's os.walk, which swallows this.
			// See the package doc: a partial walk silently shrinks the
			// manifest and loses exec bits on the remote.
			return fmt.Errorf("perms: walking %q: %w", p, err)
		}
		if d.IsDir() {
			// The root is never pruned by its own name, only its descendants,
			// matching os.walk's pruning of `dirnames` below the current dir.
			if p != root {
				if _, pruned := prunedDirNames[d.Name()]; pruned {
					return fs.SkipDir
				}
			}
			return nil
		}
		if d.Name() == ".DS_Store" {
			return nil
		}
		// Symlinks (including symlinks to directories, which WalkDir reports
		// as non-dir entries and never descends into) are shipped by rclone
		// --links, so their mode is not ours to record.
		if d.Type()&fs.ModeSymlink != 0 {
			return nil
		}
		if p == manifestPath {
			return nil
		}
		fi, err := d.Info()
		if err != nil {
			// Mirrors Python's `except OSError: continue`: a file that
			// disappeared between readdir and stat is an expected race in a
			// live box, not a reason to fail the whole push.
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return fmt.Errorf("perms: stat %q: %w", p, err)
		}
		// NOTE: no IsRegular() check, matching Python. os.walk lists fifos,
		// sockets and device nodes among `filenames`, and Python's builder
		// stats them and records any with S_IXUSR set — despite its docstring
		// saying "regular file". Filtering them here would be *more* correct
		// but would make the two implementations disagree on the bytes, so
		// every sync would rewrite the manifest. ApplyManifest skips them, so
		// the only cost is a stray line. Bug-for-bug on purpose.
		if fi.Mode().Perm()&0o100 == 0 {
			return nil
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return fmt.Errorf("perms: relativising %q against %q: %w", p, root, err)
		}
		execs = append(execs, filepath.ToSlash(rel))
		return nil
	})
	if err != nil {
		return Manifest{}, err
	}

	sortExecPaths(execs)
	return Manifest{Version: ManifestVersion, Executable: execs}, nil
}

// GenerateManifest writes `.boxyard-perms.json` at root from the tree's
// current modes, and reports whether it wrote anything.
//
// It only writes when the content would change. An unchanged box must not get
// a fresh mtime — that would look like a spurious edit to boxyard's sync-status
// check, and would make rclone re-transfer the manifest for nothing.
//
// Two cases return (false, nil) rather than an error, matching Python, because
// both are legitimate expected states:
//
//   - root is not a directory (nothing to describe);
//   - root has no executables *and* has no manifest yet — a clean box stays
//     clean rather than gaining a dotfile full of nothing. An existing
//     manifest is still kept accurate, including shrinking it to an empty list.
func GenerateManifest(root string) (bool, error) {
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return false, nil
	}

	manifest, err := BuildManifest(root)
	if err != nil {
		return false, err
	}

	manifestPath := filepath.Join(root, boxconst.BoxPermsManifestRelPath)
	content := manifest.encode()

	existing, err := os.ReadFile(manifestPath)
	switch {
	case err == nil:
		if string(existing) == string(content) {
			return false, nil
		}
	case errors.Is(err, fs.ErrNotExist):
		if len(manifest.Executable) == 0 {
			return false, nil
		}
	default:
		return false, fmt.Errorf("perms: reading existing manifest %q: %w", manifestPath, err)
	}

	// Plain truncate-and-write, like Python's Path.write_text. 0o666 is the
	// creation mode (umask applies); an existing manifest keeps its own mode.
	if err := os.WriteFile(manifestPath, content, 0o666); err != nil {
		return false, fmt.Errorf("perms: writing manifest %q: %w", manifestPath, err)
	}
	return true, nil
}

// ApplyManifest reads `.boxyard-perms.json` at root and restores `+x` on the
// files it lists, returning the manifest-relative paths whose mode it changed.
//
// Additive only: for each listed file the execute bits are set to mirror the
// read bits (0o644 -> 0o755, 0o664 -> 0o775, 0o640 -> 0o750). Bits are never
// cleared, so a file that is executable on disk but absent from the manifest
// stays executable. setuid/setgid/sticky are preserved.
//
// Entries that are missing, symlinks, or not regular files are skipped — the
// manifest describes the whole box, but a local checkout may legitimately be
// missing files via rclone excludes.
//
// No manifest at all is a no-op returning (nil, nil): boxes created before
// v0.3.0 have none. A manifest that is present but malformed is an error (see
// the package doc) — the caller decides whether that should fail the sync.
func ApplyManifest(root string) ([]string, error) {
	manifestPath := filepath.Join(root, boxconst.BoxPermsManifestRelPath)

	fi, err := os.Stat(manifestPath)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("perms: cannot stat manifest %q: %w", manifestPath, err)
	}
	if !fi.Mode().IsRegular() {
		return nil, nil
	}

	data, err := os.ReadFile(manifestPath)
	if err != nil {
		return nil, fmt.Errorf("perms: reading manifest %q: %w", manifestPath, err)
	}
	manifest, err := decodeManifest(data)
	if err != nil {
		return nil, fmt.Errorf("perms: malformed perms manifest %q: %w", manifestPath, err)
	}

	var changed []string
	for _, rel := range manifest.Executable {
		// Python joins with pathlib's `/`, where an absolute entry REPLACES
		// the root outright ("/etc/shadow" would be chmod'd in place) and
		// ".." escapes it. The manifest arrives from a shared remote, so it is
		// not trusted input; a Python-written manifest can never contain such
		// an entry, which makes one a sign of corruption or tampering.
		if err := checkManifestPath(rel); err != nil {
			return nil, fmt.Errorf("perms: malformed perms manifest %q: %w", manifestPath, err)
		}
		p := filepath.Join(root, filepath.FromSlash(rel))

		info, err := os.Lstat(p)
		if err != nil {
			continue // missing locally (rclone excludes, partial pull) — expected
		}
		if !info.Mode().IsRegular() {
			continue // symlink, directory, fifo, device
		}

		mode := info.Mode()
		perm := mode.Perm()
		newPerm := perm | ((perm & 0o444) >> 2) // mirror read bits into exec bits
		if newPerm == perm {
			continue
		}
		special := mode & (fs.ModeSetuid | fs.ModeSetgid | fs.ModeSticky)
		if err := os.Chmod(p, special|newPerm); err != nil {
			return nil, fmt.Errorf("perms: chmod %q: %w", p, err)
		}
		changed = append(changed, rel)
	}
	return changed, nil
}

// checkManifestPath rejects entries that would resolve outside the box root.
func checkManifestPath(rel string) error {
	if strings.HasPrefix(rel, "/") || filepath.IsAbs(rel) {
		return fmt.Errorf("entry %q is an absolute path", rel)
	}
	cleaned := path.Clean(rel)
	if cleaned == ".." || strings.HasPrefix(cleaned, "../") {
		return fmt.Errorf("entry %q escapes the box root", rel)
	}
	return nil
}

// ---------------------------------------------------------------------------
// Encoding: byte-for-byte reproduction of Python's json.dumps
// ---------------------------------------------------------------------------

// sortExecPaths sorts in Python's `list.sort()` order — by Unicode code point,
// with non-UTF-8 bytes taken as their surrogateescape code points, exactly as
// the paths compare on the Python side. For pure-UTF-8 paths this is identical
// to a byte sort; it differs only when a box holds both a filename with an
// invalid byte (U+DC80..U+DCFF to Python, but byte 0x80..0xFF to Go) and one
// with an astral-plane character (a leading byte of 0xF0..0xF4). Rare, but a
// disagreement there means two implementations that never stop rewriting each
// other's manifest.
func sortExecPaths(paths []string) {
	slices.SortFunc(paths, comparePythonStrings)
}

func comparePythonStrings(a, b string) int {
	i, j := 0, 0
	for i < len(a) && j < len(b) {
		ra, sa := pythonRune(a, i)
		rb, sb := pythonRune(b, j)
		if ra != rb {
			if ra < rb {
				return -1
			}
			return 1
		}
		i += sa
		j += sb
	}
	switch {
	case i < len(a):
		return 1
	case j < len(b):
		return -1
	}
	return 0
}

// pythonRune decodes the code point at s[i] the way Python's os.fsdecode sees
// it: valid UTF-8 decodes normally, and any byte that is not part of a valid
// sequence becomes a lone surrogate U+DC80..U+DCFF (PEP 383 surrogateescape),
// which is what Python's json.dumps then writes as \udcXX. Verified against
// the live Python: a file named b"\xff-invalid-utf8.sh" is written as
// "\udcff-invalid-utf8.sh".
func pythonRune(s string, i int) (rune, int) {
	r, size := utf8.DecodeRuneInString(s[i:])
	if r == utf8.RuneError && size == 1 {
		return 0xDC00 | rune(s[i]), 1 // s[i] >= 0x80 whenever decoding failed
	}
	return r, size
}

const hexDigits = "0123456789abcdef"

// encode renders the manifest exactly as Python's
// `json.dumps(m, indent=1, sort_keys=True) + "\n"` does.
func (m Manifest) encode() []byte {
	out := make([]byte, 0, 64+32*len(m.Executable))
	out = append(out, "{\n \"executable\": ["...)
	if len(m.Executable) == 0 {
		out = append(out, ']')
	} else {
		for i, p := range m.Executable {
			if i > 0 {
				out = append(out, ',')
			}
			out = append(out, "\n  "...)
			out = appendPythonJSONString(out, p)
		}
		out = append(out, "\n ]"...)
	}
	out = append(out, ",\n \"version\": "...)
	out = fmt.Appendf(out, "%d", m.Version)
	out = append(out, "\n}\n"...)
	return out
}

// appendPythonJSONString escapes s the way Python's json encoder does with its
// defaults (ensure_ascii=True). The differences from encoding/json that matter:
// `<`, `>` and `&` are NOT escaped; everything outside printable ASCII IS,
// including U+007F; escapes are lowercase \uXXXX; astral code points become a
// surrogate pair; `/` is left alone.
func appendPythonJSONString(dst []byte, s string) []byte {
	dst = append(dst, '"')
	for i := 0; i < len(s); {
		r, size := pythonRune(s, i)
		i += size
		switch r {
		case '"':
			dst = append(dst, '\\', '"')
		case '\\':
			dst = append(dst, '\\', '\\')
		case '\b':
			dst = append(dst, '\\', 'b')
		case '\f':
			dst = append(dst, '\\', 'f')
		case '\n':
			dst = append(dst, '\\', 'n')
		case '\r':
			dst = append(dst, '\\', 'r')
		case '\t':
			dst = append(dst, '\\', 't')
		default:
			switch {
			case r >= 0x20 && r < 0x7F:
				dst = append(dst, byte(r))
			case r < 0x10000:
				dst = appendUnicodeEscape(dst, uint16(r))
			default:
				r -= 0x10000
				dst = appendUnicodeEscape(dst, uint16(0xD800+(r>>10)))
				dst = appendUnicodeEscape(dst, uint16(0xDC00+(r&0x3FF)))
			}
		}
	}
	return append(dst, '"')
}

func appendUnicodeEscape(dst []byte, u uint16) []byte {
	return append(dst, '\\', 'u',
		hexDigits[(u>>12)&0xF], hexDigits[(u>>8)&0xF],
		hexDigits[(u>>4)&0xF], hexDigits[u&0xF])
}

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

// rawManifest is the decode target. The list elements stay raw so their string
// contents can be unescaped by hand: encoding/json turns the lone surrogates
// Python writes for non-UTF-8 filenames into U+FFFD, which would silently
// point the chmod at a path that does not exist.
//
// Unknown keys are tolerated rather than rejected, which is the one place this
// package does not follow the repo's strict-decoding rule. Python's reader
// looks up "executable" and ignores everything else; a Go reader that refused
// a key Python accepts would break a sync in a way Python would not, which is
// the wrong failure for a format two implementations share.
type rawManifest struct {
	Version    *int               `json:"version"`
	Executable *[]json.RawMessage `json:"executable"`
}

func decodeManifest(data []byte) (Manifest, error) {
	var raw rawManifest
	if err := json.Unmarshal(data, &raw); err != nil {
		return Manifest{}, fmt.Errorf("could not parse: %w", err)
	}
	if raw.Executable == nil {
		return Manifest{}, errors.New(`no "executable" key`)
	}

	execs := make([]string, 0, len(*raw.Executable))
	for i, elem := range *raw.Executable {
		s, err := unquotePythonJSONString(elem)
		if err != nil {
			return Manifest{}, fmt.Errorf("executable[%d]: %w", i, err)
		}
		execs = append(execs, s)
	}

	m := Manifest{Executable: execs}
	// Python's applier never looks at "version" — it applies v1 semantics to
	// whatever it is handed. Reproduced here rather than validated, so Go does
	// not reject a manifest Python would happily apply. Version 0 means the
	// field was absent.
	if raw.Version != nil {
		m.Version = *raw.Version
	}
	return m, nil
}

// unquotePythonJSONString unescapes one raw JSON string, undoing exactly what
// appendPythonJSONString produces — including mapping the surrogateescape
// range U+DC80..U+DCFF back to the raw bytes 0x80..0xFF that Python's
// os.fsencode would restore.
func unquotePythonJSONString(raw json.RawMessage) (string, error) {
	b := []byte(strings.TrimSpace(string(raw)))
	if len(b) < 2 || b[0] != '"' || b[len(b)-1] != '"' {
		return "", fmt.Errorf("expected a string, got %s", raw)
	}
	b = b[1 : len(b)-1]

	var sb strings.Builder
	sb.Grow(len(b))
	for i := 0; i < len(b); {
		c := b[i]
		if c != '\\' {
			if c < 0x20 {
				return "", fmt.Errorf("unescaped control character %#02x", c)
			}
			sb.WriteByte(c)
			i++
			continue
		}
		i++
		if i >= len(b) {
			return "", errors.New("string ends in a backslash")
		}
		switch b[i] {
		case '"', '\\', '/':
			sb.WriteByte(b[i])
			i++
		case 'b':
			sb.WriteByte('\b')
			i++
		case 'f':
			sb.WriteByte('\f')
			i++
		case 'n':
			sb.WriteByte('\n')
			i++
		case 'r':
			sb.WriteByte('\r')
			i++
		case 't':
			sb.WriteByte('\t')
			i++
		case 'u':
			r, n, err := readUnicodeEscape(b[i-1:])
			if err != nil {
				return "", err
			}
			i += n - 1
			if r < 0 { // a surrogateescape byte, not a code point
				sb.WriteByte(byte(-r))
			} else {
				sb.WriteRune(r)
			}
		default:
			return "", fmt.Errorf("unknown escape %q", "\\"+string(b[i]))
		}
	}
	return sb.String(), nil
}

// readUnicodeEscape consumes the \uXXXX escape (and its low-surrogate partner,
// if it is a surrogate pair) at the start of b. It returns the code point and
// the number of bytes consumed, or, for a byte smuggled through Python's
// surrogateescape, the NEGATED byte value.
func readUnicodeEscape(b []byte) (rune, int, error) {
	hi, err := readHex4(b)
	if err != nil {
		return 0, 0, err
	}
	if hi < 0xD800 || hi > 0xDFFF {
		return hi, 6, nil
	}
	if hi >= 0xD800 && hi <= 0xDBFF && len(b) >= 12 && b[6] == '\\' && b[7] == 'u' {
		lo, err := readHex4(b[6:])
		if err != nil {
			return 0, 0, err
		}
		if lo >= 0xDC00 && lo <= 0xDFFF {
			return 0x10000 + (hi-0xD800)<<10 + (lo - 0xDC00), 12, nil
		}
	}
	// A lone surrogate. Python only ever emits U+DC80..U+DCFF, for a filename
	// byte that was not valid UTF-8; anything else it could not turn back into
	// a path either (os.fsencode raises), so it is corruption.
	if hi >= 0xDC80 && hi <= 0xDCFF {
		return -(hi & 0xFF), 6, nil
	}
	return 0, 0, fmt.Errorf("lone surrogate \\u%04x", hi)
}

func readHex4(b []byte) (rune, error) {
	if len(b) < 6 {
		return 0, errors.New(`truncated \u escape`)
	}
	var r rune
	for _, c := range b[2:6] {
		var v rune
		switch {
		case c >= '0' && c <= '9':
			v = rune(c - '0')
		case c >= 'a' && c <= 'f':
			v = rune(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v = rune(c-'A') + 10
		default:
			return 0, fmt.Errorf(`bad \u escape %q`, b[:6])
		}
		r = r<<4 | v
	}
	return r, nil
}

// Adapter satisfies syncengine.Perms over the package functions, so the sync
// engine can be driven without the manifest and tested without touching file
// modes.
type Adapter struct{}

// Generate writes the exec-bit manifest at the root of a box's DATA, reporting
// whether anything was written.
func (Adapter) Generate(root string) (bool, error) { return GenerateManifest(root) }

// Apply restores the exec bit from the manifest, returning the paths it
// changed.
func (Adapter) Apply(root string) ([]string, error) { return ApplyManifest(root) }
