// Package sysinfo reports facts about the machine boxyard is running on.
package sysinfo

import (
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
)

var (
	hostnameOnce sync.Once
	hostname     string
)

// Hostname returns the name boxyard records as creator_hostname and
// syncer_hostname.
//
// On macOS this is the PRETTY computer name from `scutil --get ComputerName`
// ("Lukas's MacBook Pro"), not the POSIX hostname — the Python does the same,
// and the value is persisted into boxmeta.toml and into every sync record on
// the shared remote, so the two implementations must agree exactly.
//
// Resolved once per process, as the Python effectively does per call site.
func Hostname() string {
	hostnameOnce.Do(func() {
		if runtime.GOOS == "darwin" {
			if out, err := exec.Command("scutil", "--get", "ComputerName").Output(); err == nil {
				if name := strings.TrimSpace(string(out)); name != "" {
					hostname = name
					return
				}
			}
			// Fall through to the POSIX hostname, as the Python does when
			// scutil fails for any reason.
		}
		name, err := os.Hostname()
		if err != nil {
			// platform.node() returns "" rather than raising when the hostname
			// cannot be determined; match that instead of inventing a value.
			name = ""
		}
		hostname = name
	})
	return hostname
}
