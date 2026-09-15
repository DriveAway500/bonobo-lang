import ply.lex as lex

# Distinct token names
tokens = (
    'COMMENT', 'FLOAT_NUMBER', 'HEX_NUMBER', 'NUMBER',
    'STRING', 'IDENT', 'LOGICAL_AND', 'LOGICAL_OR', 'LOGICAL_NOT',
    'SHL', 'SHR', 'BIT_AND', 'BIT_OR', 'BIT_XOR', 'BIT_NOT',
    'EQ', 'NEQ', 'LE', 'GE', 'LT', 'GT',
    'INC', 'DEC', 'ADD_ASSIGN', 'SUB_ASSIGN', 'MUL_ASSIGN', 'DIV_ASSIGN',
    'PLUS', 'MINUS', 'MUL', 'DIV', 'MOD',
    'ARROW', 'ELLIPSIS', 'DOT', 'ASSIGN', 'COMMA', 'COLON', 'SEMI',
)

# Reserved words map directly to their token types, eliminating individual regexes
reserved = {
    'struct': 'KEYWORD_STRUCT', 'union': 'KEYWORD_UNION', 'enum': 'KEYWORD_ENUM',
    'typedef': 'KEYWORD_TYPEDEF', 'const': 'KEYWORD_CONST', 'volatile': 'KEYWORD_VOLATILE',
    'restrict': 'KEYWORD_RESTRICT', 'int': 'TYPE_INT', 'char': 'TYPE_CHAR',
    'float': 'TYPE_FLOAT', 'double': 'TYPE_DOUBLE', 'void': 'TYPE_VOID',
    'short': 'TYPE_SHORT', 'long': 'TYPE_LONG', 'signed': 'TYPE_SIGNED',
    'unsigned': 'TYPE_UNSIGNED', '_Bool': 'TYPE_BOOL', 'bool': 'TYPE_BOOL',
    'let': 'LET', 'if': 'IF', 'elif': 'ELIF', 'else': 'ELSE',
    'while': 'WHILE', 'for': 'FOR', 'def': 'DEF', 'fn': 'DEF',
    'return': 'RETURN', 'break': 'BREAK', 'continue': 'CONTINUE',

    # Inline Assembly Keywords
    'asm': 'KEYWORD_ASM',
    'template': 'KEYWORD_TEMPLATE',
    'outputs': 'KEYWORD_OUTPUTS',
    'inputs': 'KEYWORD_INPUTS',
    'clobbers': 'KEYWORD_CLOBBERS',
}

tokens = tokens + tuple(set(reserved.values()))

# Single-character literals can be handled directly by PLY without token rules
literals = ['(', ')', '[', ']', '{', '}']

# Complex regex rules defined as simple variables
t_FLOAT_NUMBER = r'\b\d+\.\d+([eE][+-]?\d+)?\b'
t_HEX_NUMBER = r'\b0[xX][0-9a-fA-F]+\b'
t_NUMBER = r'\b\d+\b'
t_STRING = r'"[^"]*"'

# Operators
t_LOGICAL_AND = r'&&'
t_LOGICAL_OR = r'\|\|'
t_LOGICAL_NOT = r'!'
t_SHL = r'<<'
t_SHR = r'>>'
t_BIT_AND = r'&'
t_BIT_OR = r'\|'
t_BIT_XOR = r'\^'
t_BIT_NOT = r'~'
t_EQ = r'=='
t_NEQ = r'!='
t_LE = r'<='
t_GE = r'>='
t_LT = r'<'
t_GT = r'>'
t_INC = r'\+\+'
t_DEC = r'--'
t_ADD_ASSIGN = r'\+='
t_SUB_ASSIGN = r'-='
t_MUL_ASSIGN = r'\*='
t_DIV_ASSIGN = r'/='
t_PLUS = r'\+'
t_MINUS = r'-'
t_MUL = r'\*'
t_DIV = r'/'
t_MOD = r'%'
t_ARROW = r'->'
t_ELLIPSIS = r'\.\.\.'
t_DOT = r'\.'
t_ASSIGN = r'='
t_COMMA = r','
t_COLON = r':'
t_SEMI = r';'

# Handle identifiers and keywords seamlessly
def t_IDENT(t):
    r'[a-zA-Z_][a-zA-Z0-9_]*'
    t.type = reserved.get(t.value, 'IDENT')
    return t

# Ignored characters (whitespace and single-line comments)
t_ignore = ' \t\n'
t_ignore_COMMENT = r'//.*'

def t_error(t):
    print(f"Illegal character '{t.value[0]}'")
    t.lexer.skip(1)

# Build the lexer
lexer = lex.lex()