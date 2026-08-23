package syncengine

import (
	"os"
	"strings"
)

// LiteralExcludeNames reads an rclone exclude file and returns the patterns
// that are LITERAL names.
//
// Only literal names are returned — `node_modules/`, `.DS_Store` — with any
// trailing slash stripped. Glob patterns (`*.tmp`, `**/build`) are deliberately
// NOT interpreted: reimplementing rclone's filter language here would be a
// second, subtly-different implementation of the thing that actually decides
// what gets transferred.
//
// The consequence is a one-sided inaccuracy, which is the safe side to err on:
// a glob-excluded file can still make a box look modified (a false "needs
// push", which sync then resolves as a no-op), but nothing that WOULD be synced
// is ever skipped.
//
// A missing file yields an empty set, not an error — a box with no exclude file
// is an ordinary, expected state.
func LiteralExcludeNames(excludePath string) map[string]bool {
	names := map[string]bool{}
	if excludePath == "" {
		return names
	}
	data, err := os.ReadFile(excludePath)
	if err != nil {
		return names
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.ContainsAny(line, "*?[]{}") || strings.Contains(strings.TrimRight(line, "/"), "/") {
			continue // a glob or a path pattern -- not a literal name
		}
		names[strings.TrimRight(line, "/")] = true
	}
	return names
}
