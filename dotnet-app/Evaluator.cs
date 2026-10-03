using System.Globalization;

namespace DotnetApp;

public enum TokenType
{
    Number,
    Ident,
    Plus,
    Minus,
    Mul,
    Div,
    Pow,
    LParen,
    RParen,
    EOF
}

public readonly record struct Token(TokenType Type, string Val, double Num);

public class Lexer
{
    private readonly string _input;
    private int _pos;

    public Lexer(string input)
    {
        _input = input;
        _pos = 0;
    }

    private char Peek() => _pos >= _input.Length ? '\0' : _input[_pos];
    private char Next() => _pos >= _input.Length ? '\0' : _input[_pos++];

    public Token NextToken()
    {
        while (true)
        {
            var ch = Peek();
            if (ch == '\0') return new Token(TokenType.EOF, "", 0);
            if (char.IsWhiteSpace(ch))
            {
                Next();
                continue;
            }
            break;
        }

        var c = Peek();
        if (c == '\0') return new Token(TokenType.EOF, "", 0);

        if (char.IsDigit(c) || c == '.')
        {
            var start = _pos;
            var hasDot = false;
            while (true)
            {
                var cur = Peek();
                if (char.IsDigit(cur))
                {
                    Next();
                }
                else if (cur == '.')
                {
                    if (hasDot)
                    {
                        throw new ArgumentException($"invalid number '{_input.Substring(start, _pos - start + 1)}'");
                    }
                    hasDot = true;
                    Next();
                }
                else
                {
                    break;
                }
            }

            var numStr = _input.Substring(start, _pos - start);
            if (numStr == ".")
            {
                throw new ArgumentException("invalid number '.'");
            }
            if (!double.TryParse(numStr, NumberStyles.Float, CultureInfo.InvariantCulture, out var val))
            {
                throw new ArgumentException($"invalid number '{numStr}'");
            }
            return new Token(TokenType.Number, numStr, val);
        }

        if (char.IsLetter(c))
        {
            var start = _pos;
            while (char.IsLetterOrDigit(Peek()))
            {
                Next();
            }
            var ident = _input.Substring(start, _pos - start);
            return new Token(TokenType.Ident, ident, 0);
        }

        Next();
        return c switch
        {
            '+' => new Token(TokenType.Plus, "+", 0),
            '-' => new Token(TokenType.Minus, "-", 0),
            '*' => new Token(TokenType.Mul, "*", 0),
            '/' => new Token(TokenType.Div, "/", 0),
            '^' => new Token(TokenType.Pow, "^", 0),
            '(' => new Token(TokenType.LParen, "(", 0),
            ')' => new Token(TokenType.RParen, ")", 0),
            _ => throw new ArgumentException($"unexpected character: '{c}'")
        };
    }
}

public class Parser
{
    private readonly List<Token> _tokens;
    private int _pos;

    public Parser(List<Token> tokens)
    {
        _tokens = tokens;
        _pos = 0;
    }

    public Token Peek() => _pos >= _tokens.Count ? new Token(TokenType.EOF, "", 0) : _tokens[_pos];
    private Token Next() => _pos >= _tokens.Count ? new Token(TokenType.EOF, "", 0) : _tokens[_pos++];

    private bool Match(TokenType type)
    {
        if (Peek().Type == type)
        {
            Next();
            return true;
        }
        return false;
    }

    public double ParseExpr() => ParseAddSub();

    private double ParseAddSub()
    {
        var left = ParseMulDiv();

        while (true)
        {
            if (Match(TokenType.Plus))
            {
                left += ParseMulDiv();
            }
            else if (Match(TokenType.Minus))
            {
                left -= ParseMulDiv();
            }
            else
            {
                break;
            }
        }
        return left;
    }

    private double ParseMulDiv()
    {
        var left = ParsePow();

        while (true)
        {
            if (Match(TokenType.Mul))
            {
                left *= ParsePow();
            }
            else if (Match(TokenType.Div))
            {
                var right = ParsePow();
                if (right == 0.0)
                {
                    throw new DivideByZeroException("division by zero");
                }
                left /= right;
                if (double.IsNaN(left) || double.IsInfinity(left))
                {
                    throw new DivideByZeroException("division by zero");
                }
            }
            else
            {
                break;
            }
        }
        return left;
    }

    private double ParsePow()
    {
        var baseVal = ParseUnary();

        if (Match(TokenType.Pow))
        {
            var expVal = ParsePow(); // Right-associative
            var res = Math.Pow(baseVal, expVal);
            if (double.IsNaN(res) || double.IsInfinity(res))
            {
                throw new InvalidOperationException("invalid exponentiation");
            }
            return res;
        }

        return baseVal;
    }

    private double ParseUnary()
    {
        if (Match(TokenType.Plus))
        {
            return ParseUnary();
        }
        if (Match(TokenType.Minus))
        {
            return -ParseUnary();
        }
        return ParsePrimary();
    }

    private double ParsePrimary()
    {
        var tok = Peek();

        if (tok.Type == TokenType.Number)
        {
            Next();
            return tok.Num;
        }

        if (tok.Type == TokenType.Ident)
        {
            Next();
            var fnName = tok.Val.ToLowerInvariant();
            if (!Match(TokenType.LParen))
            {
                throw new ArgumentException($"expected '(' after function {fnName}");
            }
            var arg = ParseExpr();
            if (!Match(TokenType.RParen))
            {
                throw new ArgumentException("missing ')' after function argument");
            }

            return fnName switch
            {
                "sqrt" => arg < 0 ? throw new ArgumentException("square root of negative number") : Math.Sqrt(arg),
                "sin" => Math.Sin(arg),
                "cos" => Math.Cos(arg),
                _ => throw new ArgumentException($"unknown function '{fnName}'")
            };
        }

        if (Match(TokenType.LParen))
        {
            var val = ParseExpr();
            if (!Match(TokenType.RParen))
            {
                throw new ArgumentException("missing closing parenthesis ')'");
            }
            return val;
        }

        throw new ArgumentException($"unexpected token: '{tok.Val}'");
    }
}

public static class Evaluator
{
    public static double Evaluate(string? expr)
    {
        if (string.IsNullOrWhiteSpace(expr))
        {
            throw new ArgumentException("empty expression");
        }

        var lexer = new Lexer(expr.Trim());
        var tokens = new List<Token>();
        while (true)
        {
            var tok = lexer.NextToken();
            tokens.Add(tok);
            if (tok.Type == TokenType.EOF) break;
        }

        var parser = new Parser(tokens);
        var result = parser.ParseExpr();
        if (parser.Peek().Type != TokenType.EOF)
        {
            throw new ArgumentException($"unexpected trailing token: '{parser.Peek().Val}'");
        }
        return result;
    }
}
