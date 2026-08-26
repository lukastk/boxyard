package cli

import (
	"strings"
	"sync"
	"testing"

	"github.com/lukastk/boxyard/internal/enums"
)

func TestLiveBoardWritesNothingWhenDisabled(t *testing.T) {
	// Off a terminal, rich's Live refreshes nothing — and that output is the
	// supervisor's log, compared byte for byte against the Python. A stray
	// control code here would corrupt every pass on every machine.
	var out strings.Builder
	var mu sync.Mutex
	b := newLiveBoard(&out, false, &mu, func() []string { return []string{"in flight"} })
	b.Start()
	b.Above(func() { out.WriteString("a finished box\n") })
	b.Finish()
	if out.String() != "a finished box\n" {
		t.Errorf("got %q, want only the durable line", out.String())
	}
}

func TestLiveBoardRepaintsInPlace(t *testing.T) {
	var out strings.Builder
	var mu sync.Mutex
	lines := []string{"one", "two"}
	b := newLiveBoard(&out, true, &mu, func() []string { return lines })

	b.Above(func() {}) // draws the region for the first time
	if got := out.String(); got != "one\ntwo\n" {
		t.Fatalf("first paint = %q", got)
	}

	out.Reset()
	lines = []string{"three"}
	b.Above(func() { out.WriteString("finished\n") })
	// Walk back over the two lines drawn last time, clear from there, print
	// the finished line, then the new region.
	want := "\x1b[2A\x1b[0Jfinished\nthree\n"
	if got := out.String(); got != want {
		t.Errorf("repaint = %q, want %q", got, want)
	}

	out.Reset()
	b.Finish()
	if got := out.String(); got != "\x1b[1A\x1b[0J" {
		t.Errorf("finish = %q, want the region erased", got)
	}
}

func TestLiveBoardFinishIsIdempotent(t *testing.T) {
	// Finish is both deferred and called on the normal path. Closing the stop
	// channel twice would panic mid-sync.
	var out strings.Builder
	var mu sync.Mutex
	b := newLiveBoard(&out, true, &mu, func() []string { return nil })
	b.Start()
	b.Finish()
	b.Finish()
}

func TestInFlightBoardShowsOnlySyncingBoxesInStartOrder(t *testing.T) {
	t.Setenv("COLUMNS", "80")
	t.Setenv("FORCE_COLOR", "") // set-but-empty: rich reads this as "not a terminal"

	stats := []syncStat{
		{Num: 0, Status: "Success"},
		{Num: 1, Status: "Syncing..."},
		{Num: 2, Status: "Syncing..."},
	}
	names := []string{"20260822_aaaaa__done", "20260822_bbbbb__second", "20260822_ccccc__first"}
	// Started out of index order, which is what a throttled pool produces.
	started := []int{2, 0, 1}

	lines := inFlightBoard(started, stats, names, []enums.BoxPart{enums.PartData})
	joined := strings.Join(lines, "\n")

	if strings.Contains(joined, "__done") {
		t.Errorf("a finished box is still on the board:\n%s", joined)
	}
	first := strings.Index(joined, "__first")
	second := strings.Index(joined, "__second")
	if first < 0 || second < 0 {
		t.Fatalf("both in-flight boxes should be shown:\n%s", joined)
	}
	if first > second {
		t.Errorf("the board is not in start order:\n%s", joined)
	}
	if !strings.Contains(joined, "Results pending...") {
		t.Errorf("an in-flight box has no detail line:\n%s", joined)
	}
	if !strings.Contains(joined, "Syncing...") {
		t.Errorf("an in-flight box has no status:\n%s", joined)
	}
}

func TestInFlightBoardIsEmptyWithNothingInFlight(t *testing.T) {
	stats := []syncStat{{Num: 0, Status: "Success"}}
	if got := inFlightBoard([]int{0}, stats, []string{"x"}, []enums.BoxPart{enums.PartData}); got != nil {
		t.Errorf("got %q, want no region at all", got)
	}
}
