package richstyle

// TreeNode is one node of a rich Tree. Label is markup; Children are rendered
// beneath it with the guide characters rich draws.
type TreeNode struct {
	Label    string
	Children []*TreeNode
}

// Add appends a child and returns it, so a caller can build a tree the way the
// Python does with `node = tree.add(label)`.
func (n *TreeNode) Add(label string) *TreeNode {
	child := &TreeNode{Label: label}
	n.Children = append(n.Children, child)
	return child
}

// RenderTree writes a rich Tree the way rich writes it: guides in the ASCII
// box-drawing set, and every label WRAPPED to the width left over after its
// prefix, with continuation lines carrying the guide down.
//
// The wrapping is the part a port forgets. rich hands each label to a Console
// that wraps it, so on a real yard — where box names run past eighty columns
// once the group suffix is added — the two implementations disagree about how
// many LINES the view has, in a pipe as much as on a terminal.
func RenderTree(root *TreeNode, width int, enable, noColour bool) ([]string, error) {
	var out []string
	label, err := renderLabel(root.Label, width, enable, noColour)
	if err != nil {
		return nil, err
	}
	out = append(out, label...)
	rest, err := renderChildren(root.Children, "", width, enable, noColour)
	if err != nil {
		return nil, err
	}
	return append(out, rest...), nil
}

func renderChildren(nodes []*TreeNode, prefix string, width int, enable, noColour bool) ([]string, error) {
	var out []string
	for i, node := range nodes {
		last := i == len(nodes)-1
		branch, carry := "├── ", "│   "
		if last {
			branch, carry = "└── ", "    "
		}
		firstPrefix := prefix + branch
		contPrefix := prefix + carry

		available := width - CellLen(firstPrefix)
		if available <= 0 {
			// The guides alone fill the console, leaving no room for the
			// label. rich emits NOTHING for such a node — not a blank line,
			// not a bare guide — and nothing for its subtree either, whose
			// prefixes are longer still. Falling back to "print it unwrapped"
			// here would put a 100-column line inside a 12-column view.
			continue
		}
		lines, err := renderLabel(node.Label, available, enable, noColour)
		if err != nil {
			return nil, err
		}
		for j, line := range lines {
			if j == 0 {
				out = append(out, firstPrefix+line)
			} else {
				out = append(out, contPrefix+line)
			}
		}
		rest, err := renderChildren(node.Children, contPrefix, width, enable, noColour)
		if err != nil {
			return nil, err
		}
		out = append(out, rest...)
	}
	return out, nil
}

// renderLabel wraps markup to width and renders each resulting line.
//
// The wrap is measured on the PLAIN text and then applied to the SEGMENTS,
// because rich wraps a parsed Text: the tags are not part of the width, and a
// break may land anywhere — mid-segment, or inside an escaped bracket. Cutting
// the markup string at the break offsets instead looks like it works and then
// silently drops every break that falls inside an escaped `\[`, which is
// exactly where the group suffix puts them.
func renderLabel(markup string, width int, enable, noColour bool) ([]string, error) {
	segs, err := Segments(markup)
	if err != nil {
		return nil, err
	}
	plain := Plain(segs)
	if width <= 0 || CellLen(plain) <= width {
		line, err := RenderSegments(segs, enable, noColour)
		if err != nil {
			return nil, err
		}
		return []string{line}, nil
	}

	lines := make([]string, 0, 4)
	for _, lineSegs := range sliceSegments(segs, divideLine(plain, width)) {
		// CROP, don't strip. rich keeps the trailing space a wrap leaves
		// behind when it fits inside the width and loses it when it doesn't,
		// because the Console crops every line to the console width rather
		// than tidying its whitespace. Stripping unconditionally and keeping
		// unconditionally are each wrong on about half the lines of a real
		// yard.
		line, err := RenderSegments(cropSegments(lineSegs, width), enable, noColour)
		if err != nil {
			return nil, err
		}
		lines = append(lines, line)
	}
	return lines, nil
}

// cropSegments cuts segments off at width cells.
func cropSegments(segs []segment, width int) []segment {
	total := 0
	for _, s := range segs {
		total += CellLen(s.text)
	}
	if total <= width {
		return segs
	}
	var out []segment
	used := 0
	for _, s := range segs {
		n := CellLen(s.text)
		if used+n <= width {
			out = append(out, s)
			used += n
			continue
		}
		var b []rune
		for _, r := range s.text {
			w := cellWidth(r)
			if used+w > width {
				break
			}
			b = append(b, r)
			used += w
		}
		if len(b) > 0 {
			out = append(out, segment{text: string(b), stack: s.stack})
		}
		break
	}
	return out
}

// sliceSegments cuts a segment list at the given plain-text byte offsets,
// keeping each piece's styles intact.
func sliceSegments(segs []segment, breaks []int) [][]segment {
	if len(breaks) == 0 {
		return [][]segment{segs}
	}
	var out [][]segment
	var cur []segment
	next := 0
	pos := 0

	for _, seg := range segs {
		text := seg.text
		for len(text) > 0 {
			if next >= len(breaks) {
				cur = append(cur, segment{text: text, stack: seg.stack})
				pos += len(text)
				break
			}
			cut := breaks[next] - pos
			if cut > len(text) {
				cur = append(cur, segment{text: text, stack: seg.stack})
				pos += len(text)
				break
			}
			if cut > 0 {
				cur = append(cur, segment{text: text[:cut], stack: seg.stack})
			}
			pos += cut
			text = text[cut:]
			out = append(out, cur)
			cur = nil
			next++
		}
	}
	out = append(out, cur)
	return out
}

// RenderLine renders one markup line the way `rich.Console.print` does: wrapped
// to width, each wrapped line cropped to it.
func RenderLine(markup string, width int, enable, noColour bool) ([]string, error) {
	return renderLabel(markup, width, enable, noColour)
}
