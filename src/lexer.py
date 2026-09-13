"""
lexer.py — Analisador léxico da linguagem "Bonobo".

Converte o código-fonte (string) em uma lista de Tokens, usando a
especificação de tokens fornecida (TOKEN_SPECIFICATION), ignorando
comentários e espaços em branco.
"""

import re
from typing import Iterator, List, NamedTuple, Tuple


class Token(NamedTuple):
    type: str
    value: str
    line: int
    col: int


# ---------------------------------------------------------------------------
# Especificação de tokens (ordem importa: primeiro casamento vence)
# ---------------------------------------------------------------------------
TOKEN_SPECIFICATION: List[Tuple[str, str]] = [
    # Comments (single-line with #)
    ('COMMENT',      r'#.*'),

    # Keywords
    ('LET',          r'\blet\b'),
    ('IF',           r'\bif\b'),
    ('ELSE',         r'\belse\b'),
    ('WHILE',        r'\bwhile\b'),
    ('FOR',          r'\bfor\b'),
    ('DEF',          r'\bdef\b'),
    ('RETURN',       r'\breturn\b'),
    ('ASM_RETURN_BLOCK', r'(?s:\basm_return\s*\{.*?\})'),
    ('ASM_DATA_BLOCK',  r'(?s:\basm_data\s*\{.*?\})'),
    ('ASM_RODATA_BLOCK', r'(?s:\basm_rodata\s*\{.*?\})'),
    ('ASM_BSS_BLOCK',   r'(?s:\basm_bss\s*\{.*?\})'),
    ('ASM_TEXT_BLOCK',  r'(?s:\basm_text\s*\{.*?\})'),
    ('ASM_BLOCK',       r'(?s:\basm\s*\{.*?\})'),

    # Literals & Identifiers
    ('NUMBER',       r'\b\d+\b'),
    ('STRING',       r'"[^"]*"'),
    ('IDENT',        r'\b[a-zA-Z_][a-zA-Z0-9_]*\b'),

    # Bitwise Operators (place multi-char operators before single-char)
    ('SHL',          r'<<'),
    ('SHR',          r'>>'),
    ('BIT_AND',      r'&'),
    ('BIT_OR',       r'\|'),
    ('BIT_XOR',      r'\^'),
    ('BIT_NOT',      r'~'),

    # Relational Operators
    ('EQ',           r'=='),
    ('NEQ',          r'!='),
    ('LE',           r'<='),
    ('GE',           r'>='),
    ('LT',           r'<'),
    ('GT',           r'>'),

    # Arithmetic Operators
    ('PLUS',         r'\+'),
    ('MINUS',        r'-'),
    ('MUL',          r'\*'),
    ('DIV',          r'/'),

    # Symbols
    ('ASSIGN',       r'='),
    ('LPAREN',       r'\('),
    ('RPAREN',       r'\)'),
    ('COMMA',        r','),
    ('ELLIPSIS',     r'\.\.\.'),
    ('LBRACE',       r'\{'),
    ('RBRACE',       r'\}'),
    ('SEMI',         r';'),

    # Whitespace and unknown characters
    ('SKIP',         r'[ \t\n]+'),
    ('MISMATCH',     r'.'),
]

MASTER_REGEX = re.compile(
    '|'.join(f'(?P<{name}>{pattern})' for name, pattern in TOKEN_SPECIFICATION)
)

# Tipos de token que não chegam ao parser
IGNORED_TYPES = {'SKIP', 'COMMENT'}


class LexError(Exception):
    """Levantado quando um caractere não reconhecido é encontrado."""

    def __init__(self, char: str, line: int, col: int):
        super().__init__(
            f"Caractere inesperado {char!r} na linha {line}, coluna {col}"
        )
        self.char = char
        self.line = line
        self.col = col


def tokenize(source: str) -> List[Token]:
    """Transforma o código-fonte em uma lista de Tokens, terminando em EOF."""
    tokens: List[Token] = []
    line = 1
    line_start = 0

    for mo in MASTER_REGEX.finditer(source):
        kind = mo.lastgroup
        value = mo.group()
        col = mo.start() - line_start + 1

        if kind == 'MISMATCH':
            raise LexError(value, line, col)

        if kind not in IGNORED_TYPES:
            tokens.append(Token(kind, value, line, col))

        newlines = value.count('\n')
        if newlines:
            line += newlines
            line_start = mo.start() + value.rfind('\n') + 1

    tokens.append(Token('EOF', '', line, 0))
    return tokens


def iter_tokens(source: str) -> Iterator[Token]:
    """Versão em gerador de tokenize(), útil para depuração."""
    yield from tokenize(source)


if __name__ == '__main__':
    import sys

    text = sys.stdin.read() if not sys.stdin.isatty() else 'let x = 1 + 2;'
    for tok in tokenize(text):
        print(tok)