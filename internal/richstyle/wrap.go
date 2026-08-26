package richstyle

import (
	"regexp"
	"strings"
)

// This file is rich's line-wrapping, transcribed.
//
// It matters more than it looks. `boxyard list --view groups` and `--view
// tree` are rendered by a rich Console, and rich WRAPS every line to the
// console width — so on any yard with long box names the two implementations
// disagree about the CONTENT of the view, not just its styling, and they
// disagree in a pipe as well as on a terminal. The port printed each label on
// one long line.
//
// Everything here mirrors rich/_wrap.py and rich/cells.py exactly, including
// the parts that look wrong: a "word" carries its TRAILING whitespace, the fit
// test measures the word with that whitespace stripped but the running offset
// advances by the word WITH it (so a trailing space may overhang the width),
// and an over-long word is folded by cells rather than moved down whole.

// CellLen is rich's cell_len: the width of a string in terminal cells.
func CellLen(s string) int {
	total := 0
	for _, r := range s {
		total += cellWidth(r)
	}
	return total
}

// cellWidth is rich's get_character_cell_size.
func cellWidth(r rune) int {
	cp := int32(r)
	// rich's own special case, before the table: C0/C1 controls are zero-width.
	if (cp != 0 && cp < 32) || (cp >= 0x7F && cp < 0xA0) {
		return 0
	}
	if cp > cellTable[len(cellTable)-1][1] {
		return 1
	}
	lo, hi := 0, len(cellTable)-1
	for lo <= hi {
		mid := (lo + hi) / 2
		start, end, width := cellTable[mid][0], cellTable[mid][1], cellTable[mid][2]
		switch {
		case cp < start:
			hi = mid - 1
		case cp > end:
			lo = mid + 1
		default:
			return int(width)
		}
	}
	return 1
}

// isSingleCellWidths is rich's _is_single_cell_widths: the fast path for text
// that is entirely one-cell-per-rune.
func isSingleCellWidths(s string) bool {
	for _, r := range s {
		if cellWidth(r) != 1 {
			return false
		}
	}
	return true
}

// chopCells is rich's chop_cells: split text into pieces each at most width
// cells wide. The single-cell fast path slices by CODEPOINT count, which is
// what rich does, so the two agree on where a fold lands.
func chopCells(s string, width int) []string {
	if width <= 0 {
		return []string{s}
	}
	runes := []rune(s)
	if isSingleCellWidths(s) {
		var out []string
		for i := 0; i < len(runes); i += width {
			end := i + width
			if end > len(runes) {
				end = len(runes)
			}
			out = append(out, string(runes[i:end]))
		}
		return out
	}
	var out []string
	lineSize, lineStart := 0, 0
	for i, r := range runes {
		size := cellWidth(r)
		if lineSize+size > width {
			out = append(out, string(runes[lineStart:i]))
			lineStart = i
			lineSize = 0
		}
		lineSize += size
	}
	if lineSize > 0 {
		out = append(out, string(runes[lineStart:]))
	}
	return out
}

// reWord is rich's re_word: a word plus the whitespace on either side of it.
var reWord = regexp.MustCompile(`^\s*\S+\s*`)

// wordSpan is one match of reWord, in BYTE offsets.
type wordSpan struct {
	start, end int
	word       string
}

func splitWords(text string) []wordSpan {
	var out []wordSpan
	pos := 0
	for pos < len(text) {
		loc := reWord.FindStringIndex(text[pos:])
		if loc == nil {
			break
		}
		start, end := pos+loc[0], pos+loc[1]
		out = append(out, wordSpan{start, end, text[start:end]})
		pos = end
	}
	return out
}

// divideLine is rich's divide_line: the byte offsets at which text should be
// broken to fit within width cells.
func divideLine(text string, width int) []int {
	var breaks []int
	cellOffset := 0
	for _, w := range splitWords(text) {
		start := w.start
		wordLength := CellLen(strings.TrimRight(w.word, " \t\n\r\v\f"))
		if width-cellOffset >= wordLength {
			cellOffset += CellLen(w.word)
			continue
		}
		if wordLength > width {
			// Too long for any line: fold it across several.
			folded := chopCells(w.word, width)
			for i, line := range folded {
				if start != 0 {
					breaks = append(breaks, start)
				}
				if i == len(folded)-1 {
					cellOffset = CellLen(line)
				} else {
					start += len(line)
				}
			}
			continue
		}
		if cellOffset != 0 && start != 0 {
			breaks = append(breaks, start)
			cellOffset = CellLen(w.word)
		}
	}
	return breaks
}

// Wrap breaks text into the lines rich would render it as.
//
// The trailing space of a wrapped line is KEPT: rich does not strip it.
func Wrap(text string, width int) []string {
	if width <= 0 || CellLen(text) <= width {
		return []string{text}
	}
	breaks := divideLine(text, width)
	if len(breaks) == 0 {
		return []string{text}
	}
	var lines []string
	prev := 0
	for _, b := range breaks {
		if b < prev || b > len(text) {
			// A break outside the string would silently truncate the label.
			return []string{text}
		}
		lines = append(lines, text[prev:b])
		prev = b
	}
	return append(lines, text[prev:])
}
