// Package groupexpr compiles boolean group-filter expressions — the
// `filter_expr` of a virtual box group — into a predicate over a box's group
// names.
//
// Grammar (precedence NOT > AND > OR, all binary operators left-associative):
//
//	or    := and ("OR" and)*
//	and   := not ("AND" not)*
//	not   := "NOT" not | "(" or ")" | IDENT
//
// Operators are matched case-insensitively, so "AND", "and" and "And" are the
// same operator, and a config may freely mix "NOT archived" with "not proj".
// An operator only matches at a word boundary, so "android", "oracle" and
// "notebook" tokenize as identifiers rather than as an operator plus a suffix.
//
// Identifiers are group names and are case-SENSITIVE: "Backend" does not match
// a box in group "backend". An identifier is a run of letters, digits, '_',
// '-' and '/' — note that '-' and '/' are identifier characters, so "not-a" is
// one identifier, not a negation of "a".
//
// This is a port of the Python boxyard._utils.logical_expressions module and
// deliberately reproduces its acceptance and rejection behaviour. The one
// intentional difference is *when* errors surface: the Python builds a
// predicate that re-parses the token stream on every call and therefore raises
// structural errors (unmatched parenthesis, dangling operator, ...) at call
// time. Here Parse validates the whole expression up front and returns an
// error, so the returned predicate is total. This is safe precisely because
// the Python parser's control flow never consults the group set — validity is
// a property of the expression alone.
package groupexpr

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
)

// maxDepth caps parser recursion. Unlike Python — which raises a catchable
// RecursionError — an unbounded recursive descent in Go dies on a stack
// overflow that cannot be recovered from, so a pathological config value would
// take the whole process down. The limit mirrors CPython's default recursion
// limit and is orders of magnitude above any real expression.
const maxDepth = 1000

// Parse compiles a group filter expression into a predicate over group names.
// It returns a descriptive error for malformed input; a nil error guarantees
// the predicate is usable and reusable (it is safe for concurrent use and
// never mutates the slice it is given).
func Parse(expression string) (func(groups []string) bool, error) {
	tokens, err := tokenize(expression)
	if err != nil {
		return nil, err
	}
	if len(tokens) == 0 {
		return nil, errors.New("empty expression")
	}

	p := &parser{tokens: tokens}
	pred, err := p.parseOr(0)
	if err != nil {
		return nil, err
	}
	if p.pos < len(p.tokens) {
		return nil, fmt.Errorf("unexpected token at position %d: %s", p.pos, p.tokens[p.pos].text)
	}

	return func(groups []string) bool {
		set := make(map[string]struct{}, len(groups))
		for _, g := range groups {
			set[g] = struct{}{}
		}
		return pred(set)
	}, nil
}

// predicate is a compiled sub-expression evaluated against a box's group set.
type predicate func(groups map[string]struct{}) bool

type tokenKind int

const (
	tokIdent tokenKind = iota
	tokAnd
	tokOr
	tokNot
	tokLParen
	tokRParen
)

type token struct {
	kind tokenKind
	// text is the group name for identifiers and the canonical spelling
	// ("AND", "OR", "NOT", "(", ")") for everything else, so error messages
	// name the operator rather than however the config happened to case it.
	text string
}

// isIdentifierChar reports whether a rune can be part of a group name. It
// mirrors Python's `c.isalnum() or c in "_-/"`; Python's str.isalnum() is true
// for any Unicode letter or number, hence IsLetter/IsNumber rather than an
// ASCII test.
func isIdentifierChar(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' || r == '-' || r == '/'
}

// matchesKeyword reports whether the runes at i spell kw (ASCII
// case-insensitively) and end on a word boundary. kw must be uppercase ASCII.
func matchesKeyword(runes []rune, i int, kw string) bool {
	if i+len(kw) > len(runes) {
		return false
	}
	for k := 0; k < len(kw); k++ {
		c := runes[i+k]
		if c >= 'a' && c <= 'z' {
			c -= 'a' - 'A'
		}
		if c != rune(kw[k]) {
			return false
		}
	}
	// A keyword only counts if it is not glued to more identifier characters,
	// so "android" stays one identifier.
	return i+len(kw) >= len(runes) || !isIdentifierChar(runes[i+len(kw)])
}

// isSpace mirrors Python's str.isspace(), which — unlike unicode.IsSpace —
// also counts the four ASCII information separators U+001C..U+001F.
func isSpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

func tokenize(expression string) ([]token, error) {
	runes := []rune(strings.TrimSpace(expression))
	var tokens []token

	for i := 0; i < len(runes); {
		switch {
		case isSpace(runes[i]):
			i++
		case matchesKeyword(runes, i, "AND"):
			tokens = append(tokens, token{kind: tokAnd, text: "AND"})
			i += 3
		case matchesKeyword(runes, i, "OR"):
			tokens = append(tokens, token{kind: tokOr, text: "OR"})
			i += 2
		case matchesKeyword(runes, i, "NOT"):
			tokens = append(tokens, token{kind: tokNot, text: "NOT"})
			i += 3
		case runes[i] == '(':
			tokens = append(tokens, token{kind: tokLParen, text: "("})
			i++
		case runes[i] == ')':
			tokens = append(tokens, token{kind: tokRParen, text: ")"})
			i++
		default:
			start := i
			for i < len(runes) && isIdentifierChar(runes[i]) {
				i++
			}
			if i == start {
				// Position is a rune offset into the trimmed expression.
				return nil, fmt.Errorf("invalid character at position %d: %c", i, runes[i])
			}
			tokens = append(tokens, token{kind: tokIdent, text: string(runes[start:i])})
		}
	}

	return tokens, nil
}

// parser is a recursive-descent parser over the token stream. Positions in its
// error messages are token indices, matching the Python implementation.
type parser struct {
	tokens []token
	pos    int
}

func (p *parser) parseOr(depth int) (predicate, error) {
	left, err := p.parseAnd(depth)
	if err != nil {
		return nil, err
	}
	for p.pos < len(p.tokens) && p.tokens[p.pos].kind == tokOr {
		p.pos++
		right, err := p.parseAnd(depth)
		if err != nil {
			return nil, err
		}
		l, r := left, right
		left = func(groups map[string]struct{}) bool { return l(groups) || r(groups) }
	}
	return left, nil
}

func (p *parser) parseAnd(depth int) (predicate, error) {
	left, err := p.parseNot(depth)
	if err != nil {
		return nil, err
	}
	for p.pos < len(p.tokens) && p.tokens[p.pos].kind == tokAnd {
		p.pos++
		right, err := p.parseNot(depth)
		if err != nil {
			return nil, err
		}
		l, r := left, right
		left = func(groups map[string]struct{}) bool { return l(groups) && r(groups) }
	}
	return left, nil
}

func (p *parser) parseNot(depth int) (predicate, error) {
	if depth >= maxDepth {
		return nil, fmt.Errorf("expression nested too deeply (limit %d)", maxDepth)
	}
	if p.pos >= len(p.tokens) {
		return nil, errors.New("unexpected end of expression")
	}

	tok := p.tokens[p.pos]
	switch tok.kind {
	case tokNot:
		p.pos++
		inner, err := p.parseNot(depth + 1)
		if err != nil {
			return nil, err
		}
		return func(groups map[string]struct{}) bool { return !inner(groups) }, nil

	case tokLParen:
		p.pos++
		inner, err := p.parseOr(depth + 1)
		if err != nil {
			return nil, err
		}
		if p.pos >= len(p.tokens) || p.tokens[p.pos].kind != tokRParen {
			return nil, errors.New("unmatched opening parenthesis")
		}
		p.pos++
		return inner, nil

	case tokAnd, tokOr, tokRParen:
		return nil, fmt.Errorf("unexpected operator or parenthesis: %s", tok.text)

	default:
		name := tok.text
		p.pos++
		return func(groups map[string]struct{}) bool {
			_, ok := groups[name]
			return ok
		}, nil
	}
}
