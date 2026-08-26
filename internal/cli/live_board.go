package cli

import (
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"github.com/lukastk/boxyard/internal/enums"
)

// liveBoard is the in-place region `multi-sync` repaints while boxes are in
// flight — rich's `Live`, in the shape boxyard uses it.
//
// What it shows is the set of boxes whose status is still "Syncing...", each
// as its own two-line block. Finished boxes are printed ABOVE the region and
// scroll away; the region itself only ever holds work in progress.
//
// It runs ONLY on a terminal, and that is not a shortcut. rich's Live refreshes
// nothing when the console is not a terminal — it prints the final render once,
// on exit — so the supervisor's log contains the durable lines and no frames.
// That is the output every automated comparison sees, and it is byte-identical
// between the two implementations (parity/cross_impl_sync.sh compares the whole
// of it). The animation itself is not comparable by any diff, so this
// reproduces its CONTENT and its cadence rather than pretending to reproduce
// its control codes.
type liveBoard struct {
	// mu is the CALLER's mutex, not one of its own. A repaint reads the same
	// stats a finishing box writes, so two mutexes here would be a data race
	// dressed up as synchronisation.
	mu      *sync.Mutex
	out     io.Writer
	enabled bool
	// height is how many lines the region currently occupies, which is what a
	// repaint has to walk back over.
	height int
	stop   chan struct{}
	done   chan struct{}
	// render returns the region's current lines. It is called with mu held.
	render func() []string
	// finish makes Finish idempotent: it is both deferred and called on the
	// normal path, and closing `stop` twice would panic.
	finish sync.Once
}

func newLiveBoard(out io.Writer, enabled bool, mu *sync.Mutex, render func() []string) *liveBoard {
	return &liveBoard{out: out, enabled: enabled, mu: mu, render: render}
}

// Start begins repainting at rich's four frames a second.
func (b *liveBoard) Start() {
	if !b.enabled {
		return
	}
	b.stop, b.done = make(chan struct{}), make(chan struct{})
	go func() {
		defer close(b.done)
		ticker := time.NewTicker(250 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-b.stop:
				return
			case <-ticker.C:
				b.mu.Lock()
				b.repaint(b.render())
				b.mu.Unlock()
			}
		}
	}()
}

// Above runs fn with the region erased, so whatever it prints lands above the
// region rather than inside it, and repaints afterwards.
func (b *liveBoard) Above(fn func()) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.erase()
	fn()
	if b.enabled {
		b.repaint(b.render())
	}
}

// Finish erases the region for good and stops the repainting. It is safe to
// call more than once.
func (b *liveBoard) Finish() {
	b.finish.Do(func() {
		// stop is nil when Start was never called. Guarded rather than
		// assumed: closing a nil channel panics, and this runs at the end of
		// every multi-sync pass.
		if b.enabled && b.stop != nil {
			close(b.stop)
			<-b.done
		}
		b.mu.Lock()
		defer b.mu.Unlock()
		b.erase()
	})
}

// erase removes the region. The caller holds mu.
func (b *liveBoard) erase() {
	if !b.enabled || b.height == 0 {
		return
	}
	// Up to the top of the region, then clear everything below the cursor.
	fmt.Fprintf(b.out, "\x1b[%dA\x1b[0J", b.height)
	b.height = 0
}

// repaint replaces the region with lines. The caller holds mu.
func (b *liveBoard) repaint(lines []string) {
	if !b.enabled {
		return
	}
	b.erase()
	if len(lines) == 0 {
		return
	}
	fmt.Fprintln(b.out, strings.Join(lines, "\n"))
	b.height = len(lines)
}

// inFlightBoard renders the blocks for every box still syncing, in the order
// they STARTED — matching the Python, which iterates its `sync_stats` dict and
// so gets insertion order.
func inFlightBoard(started []int, stats []syncStat, names []string, parts []enums.BoxPart) []string {
	var markup []string
	for _, num := range started {
		if stats[num].Status != "Syncing..." {
			continue
		}
		markup = append(markup, boxResultMarkup(stats[num], names[num], len(stats), parts)...)
	}
	if len(markup) == 0 {
		return nil
	}
	lines := renderBoxResult(markup)
	// The Python strips the joined board, so a leading or trailing blank line
	// never appears.
	for len(lines) > 0 && strings.TrimSpace(lines[0]) == "" {
		lines = lines[1:]
	}
	for len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}
