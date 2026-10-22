package main

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode"
)

type TokenType int

const (
	tokNumber TokenType = iota
	tokIdent
	tokPlus
	tokMinus
	tokMul
	tokDiv
	tokPow
	tokLParen
	tokRParen
	tokEOF
)

type Token struct {
	typ TokenType
	val string
	num float64
}

type Lexer struct {
	input []rune
	pos   int
}

func newLexer(input string) *Lexer {
	return &Lexer{input: []rune(input), pos: 0}
}

func (l *Lexer) peek() rune {
	if l.pos >= len(l.input) {
		return 0
	}
	return l.input[l.pos]
}

func (l *Lexer) next() rune {
	if l.pos >= len(l.input) {
		return 0
	}
	ch := l.input[l.pos]
	l.pos++
	return ch
}

func (l *Lexer) NextToken() (Token, error) {
	for {
		ch := l.peek()
		if ch == 0 {
			return Token{typ: tokEOF}, nil
		}
		if unicode.IsSpace(ch) {
			l.next()
			continue
		}
		break
	}

	ch := l.peek()
	if ch == 0 {
		return Token{typ: tokEOF}, nil
	}

	if unicode.IsDigit(ch) || ch == '.' {
		var sb strings.Builder
		hasDot := false
		for {
			c := l.peek()
			if unicode.IsDigit(c) {
				sb.WriteRune(l.next())
			} else if c == '.' && !hasDot {
				hasDot = true
				sb.WriteRune(l.next())
			} else {
				break
			}
		}
		numStr := sb.String()
		if numStr == "." {
			return Token{}, errors.New("invalid number '.'")
		}
		val, err := strconv.ParseFloat(numStr, 64)
		if err != nil {
			return Token{}, fmt.Errorf("invalid number %q: %w", numStr, err)
		}
		return Token{typ: tokNumber, num: val, val: numStr}, nil
	}

	if unicode.IsLetter(ch) {
		var sb strings.Builder
		for {
			c := l.peek()
			if unicode.IsLetter(c) || unicode.IsDigit(c) {
				sb.WriteRune(l.next())
			} else {
				break
			}
		}
		ident := sb.String()
		return Token{typ: tokIdent, val: ident}, nil
	}

	l.next()
	switch ch {
	case '+':
		return Token{typ: tokPlus, val: "+"}, nil
	case '-':
		return Token{typ: tokMinus, val: "-"}, nil
	case '*':
		return Token{typ: tokMul, val: "*"}, nil
	case '/':
		return Token{typ: tokDiv, val: "/"}, nil
	case '^':
		return Token{typ: tokPow, val: "^"}, nil
	case '(':
		return Token{typ: tokLParen, val: "("}, nil
	case ')':
		return Token{typ: tokRParen, val: ")"}, nil
	default:
		return Token{}, fmt.Errorf("unexpected character: %q", ch)
	}
}

type Parser struct {
	tokens []Token
	pos    int
}

func newParser(tokens []Token) *Parser {
	return &Parser{tokens: tokens, pos: 0}
}

func (p *Parser) peek() Token {
	if p.pos >= len(p.tokens) {
		return Token{typ: tokEOF}
	}
	return p.tokens[p.pos]
}

func (p *Parser) next() Token {
	tok := p.peek()
	p.pos++
	return tok
}

func (p *Parser) match(typ TokenType) bool {
	if p.peek().typ == typ {
		p.next()
		return true
	}
	return false
}

func (p *Parser) parseExpr() (float64, error) {
	return p.parseAddSub()
}

func (p *Parser) parseAddSub() (float64, error) {
	left, err := p.parseMulDiv()
	if err != nil {
		return 0, err
	}

	for {
		if p.match(tokPlus) {
			right, err := p.parseMulDiv()
			if err != nil {
				return 0, err
			}
			left = left + right
		} else if p.match(tokMinus) {
			right, err := p.parseMulDiv()
			if err != nil {
				return 0, err
			}
			left = left - right
		} else {
			break
		}
	}
	return left, nil
}

func (p *Parser) parseMulDiv() (float64, error) {
	left, err := p.parsePow()
	if err != nil {
		return 0, err
	}

	for {
		if p.match(tokMul) {
			right, err := p.parsePow()
			if err != nil {
				return 0, err
			}
			left = left * right
		} else if p.match(tokDiv) {
			right, err := p.parsePow()
			if err != nil {
				return 0, err
			}
			if right == 0.0 {
				return 0, errors.New("division by zero")
			}
			left = left / right
			if math.IsInf(left, 0) || math.IsNaN(left) {
				return 0, errors.New("division by zero")
			}
		} else {
			break
		}
	}
	return left, nil
}

func (p *Parser) parsePow() (float64, error) {
	base, err := p.parseUnary()
	if err != nil {
		return 0, err
	}

	if p.match(tokPow) {
		exp, err := p.parsePow() // Right-associative
		if err != nil {
			return 0, err
		}
		res := math.Pow(base, exp)
		if math.IsNaN(res) || math.IsInf(res, 0) {
			return 0, errors.New("invalid exponentiation")
		}
		return res, nil
	}

	return base, nil
}

func (p *Parser) parseUnary() (float64, error) {
	if p.match(tokPlus) {
		return p.parseUnary()
	}
	if p.match(tokMinus) {
		val, err := p.parseUnary()
		if err != nil {
			return 0, err
		}
		return -val, nil
	}
	return p.parsePrimary()
}

func (p *Parser) parsePrimary() (float64, error) {
	tok := p.peek()

	if tok.typ == tokNumber {
		p.next()
		return tok.num, nil
	}

	if tok.typ == tokIdent {
		p.next()
		fnName := strings.ToLower(tok.val)
		if !p.match(tokLParen) {
			return 0, fmt.Errorf("expected '(' after function %s", fnName)
		}
		arg, err := p.parseExpr()
		if err != nil {
			return 0, err
		}
		if !p.match(tokRParen) {
			return 0, fmt.Errorf("missing ')' after function argument")
		}

		switch fnName {
		case "sqrt":
			if arg < 0 {
				return 0, errors.New("square root of negative number")
			}
			return math.Sqrt(arg), nil
		case "sin":
			return math.Sin(arg), nil
		case "cos":
			return math.Cos(arg), nil
		default:
			return 0, fmt.Errorf("unknown function %q", fnName)
		}
	}

	if p.match(tokLParen) {
		val, err := p.parseExpr()
		if err != nil {
			return 0, err
		}
		if !p.match(tokRParen) {
			return 0, errors.New("missing closing parenthesis ')'")
		}
		return val, nil
	}

	return 0, fmt.Errorf("unexpected token: %s", tok.val)
}

func evaluate(expr string) (float64, error) {
	trimmed := strings.TrimSpace(expr)
	if trimmed == "" {
		return 0, errors.New("empty expression")
	}

	lexer := newLexer(trimmed)
	var tokens []Token
	for {
		tok, err := lexer.NextToken()
		if err != nil {
			return 0, err
		}
		tokens = append(tokens, tok)
		if tok.typ == tokEOF {
			break
		}
	}

	parser := newParser(tokens)
	result, err := parser.parseExpr()
	if err != nil {
		return 0, err
	}

	if parser.peek().typ != tokEOF {
		return 0, fmt.Errorf("unexpected trailing token: %s", parser.peek().val)
	}

	if math.IsNaN(result) || math.IsInf(result, 0) {
		return 0, errors.New("result is non-finite")
	}

	return result, nil
}
