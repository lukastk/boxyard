package cli

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// A failing command must write nothing to stdout.
//
// Many error paths used fmt.Printf with no destination, so the message landed
// on stdout while the command exited 1. A caller that pipes stdout and
// reasonably discards stderr then reads the error text as data — which is what
// happened downstream: a cockpit command read a box's groups with
//
//	boxyard list-groups --box "$index" 2>/dev/null | while read -r g; do ...
//
// and forwarded each line to `boxyard new -g`. With the box absent, the error
// line became a `-g` argument, and it surfaced two layers later as a
// validation error quoting the error text back as an invalid group name.
//
// This is a LINT over the source rather than a list of sites, because the fix
// was an 18-site sweep and the thing worth protecting is the rule: a new error
// path added next month is what would drift.

var printCall = regexp.MustCompile(`fmt\.(Printf|Println)\(`)
var nonZeroExit = regexp.MustCompile(`os\.Exit\([1-9]`)

func TestNoErrorPrintsToStdoutBeforeExiting(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}

	var offenders []string
	checked := 0
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		body, err := os.ReadFile(filepath.Join(".", e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		lines := strings.Split(string(body), "\n")
		for i, line := range lines {
			if !printCall.MatchString(line) {
				continue
			}
			checked++
			k := i + 1
			for k < len(lines) && strings.TrimSpace(lines[k]) == "" {
				k++
			}
			if k < len(lines) && nonZeroExit.MatchString(lines[k]) {
				offenders = append(offenders,
					strings.TrimSpace(e.Name()+":"+itoa(i+1)+"  "+strings.TrimSpace(line)))
			}
		}
	}

	if checked == 0 {
		t.Fatal("found no fmt.Print calls at all — the scan is broken, not the code")
	}
	if len(offenders) > 0 {
		t.Errorf("these print to stdout and then exit non-zero, so a caller piping "+
			"stdout reads them as data:\n  %s", strings.Join(offenders, "\n  "))
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
