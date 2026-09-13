"""
parser.py — Analisador sintático (recursive-descent) da linguagem "Bonobo".

Gramática (informal):

    program      := (function_def | statement)*

    function_def := DEF IDENT LPAREN param_list? RPAREN block
    param_list   := param (COMMA param)*
    param        := IDENT | ELLIPSIS

    block        := LBRACE statement* RBRACE

    statement    := let_stmt | if_stmt | while_stmt | for_stmt
                  | return_stmt | asm_stmt | block | expr_stmt

    let_stmt     := LET IDENT ASSIGN expr SEMI
                  | LET IDENT LPAREN IDENT RPAREN ASSIGN expr SEMI
                    # segunda forma: `let str(message) = "...";`
                    # açúcar para  `let message = str("...");`

    if_stmt      := IF LPAREN expr RPAREN block (ELSE block)?
    while_stmt   := WHILE LPAREN expr RPAREN block
    for_stmt     := FOR LPAREN (let_stmt | expr_stmt | SEMI)
                         expr? SEMI expr? RPAREN block
    return_stmt  := RETURN expr? SEMI
    asm_stmt     := ASM_BLOCK SEMI?
                  | ASM_RETURN_BLOCK SEMI?
    expr_stmt    := expr SEMI

    expr         := assignment
    assignment   := equality (ASSIGN assignment)?
    equality     := relational ((EQ | NEQ) relational)*
    relational   := bit_or ((LT|GT|LE|GE) bit_or)*
    bit_or       := bit_xor (BIT_OR bit_xor)*
    bit_xor      := bit_and (BIT_XOR bit_and)*
    bit_and      := shift (BIT_AND shift)*
    shift        := additive ((SHL|SHR) additive)*
    additive     := multiplicative ((PLUS|MINUS) multiplicative)*
    multiplicative := unary ((MUL|DIV) unary)*
    unary        := (PLUS|MINUS|BIT_NOT) unary | call
    call         := primary (LPAREN arg_list? RPAREN)*
    primary      := NUMBER | STRING | IDENT | LPAREN expr RPAREN
"""

from typing import List, Optional

from utils import nodes as ast
from lexer import Token, tokenize


class ParseError(Exception):
    def __init__(self, message: str, token: Token):
        super().__init__(
            f"{message} (encontrado {token.type} {token.value!r} na linha {token.line}, coluna {token.col})"
        )
        self.token = token


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    # ---- utilidades de navegação -----------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type != 'EOF':
            self.pos += 1
        return tok

    def check(self, *types: str) -> bool:
        return self.peek().type in types

    def match(self, *types: str) -> Optional[Token]:
        if self.check(*types):
            return self.advance()
        return None

    def expect(self, type_: str, message: Optional[str] = None) -> Token:
        if self.check(type_):
            return self.advance()
        raise ParseError(message or f"Esperado token {type_}", self.peek())

    # ---- ponto de entrada --------------------------------------------
    def parse_program(self) -> ast.Program:
        body = []
        while not self.check('EOF'):
            body.append(self.parse_top_level())
        return ast.Program(body)

    def parse_top_level(self) -> ast.Node:
        if self.check('DEF'):
            return self.parse_function_def()
        return self.parse_statement()

    # ---- funções -------------------------------------------------------
    def parse_function_def(self) -> ast.FunctionDef:
        self.expect('DEF')
        name = self.expect('IDENT').value
        self.expect('LPAREN')
        params = []
        if not self.check('RPAREN'):
            params.append(self.parse_param())
            while self.match('COMMA'):
                params.append(self.parse_param())
        self.expect('RPAREN')
        body = self.parse_block()
        return ast.FunctionDef(name, params, body)

    def parse_param(self) -> ast.Param:
        if self.match('ELLIPSIS'):
            return ast.Param(name='...', variadic=True)
        name = self.expect('IDENT').value
        return ast.Param(name)

    def parse_block(self) -> ast.Block:
        self.expect('LBRACE')
        statements = []
        while not self.check('RBRACE'):
            statements.append(self.parse_statement())
        self.expect('RBRACE')
        return ast.Block(statements)

    # ---- statements ------------------------------------------------------
    def parse_statement(self) -> ast.Node:
        if self.check('LET'):
            return self.parse_let()
        if self.check('IF'):
            return self.parse_if()
        if self.check('WHILE'):
            return self.parse_while()
        if self.check('FOR'):
            return self.parse_for()
        if self.check('RETURN'):
            return self.parse_return()
        if self.check(
            'ASM_BLOCK',
            'ASM_RETURN_BLOCK',
            'ASM_TEXT_BLOCK',
            'ASM_DATA_BLOCK',
            'ASM_RODATA_BLOCK',
            'ASM_BSS_BLOCK',
        ):
            return self.parse_asm()
        if self.check('LBRACE'):
            return self.parse_block()
        return self.parse_expr_statement()

    def parse_let(self) -> ast.LetStmt:
        self.expect('LET')
        first = self.expect('IDENT').value
        caster = None
        name = first
        if self.match('LPAREN'):
            # açúcar: `let TIPO(NOME) = EXPR;` -> name=NOME, caster=TIPO
            caster = first
            name = self.expect('IDENT').value
            self.expect('RPAREN')
        self.expect('ASSIGN')
        value = self.parse_expr()
        self.expect('SEMI')
        return ast.LetStmt(name=name, caster=caster, value=value)

    def parse_if(self) -> ast.IfStmt:
        self.expect('IF')
        self.expect('LPAREN')
        condition = self.parse_expr()
        self.expect('RPAREN')
        then_block = self.parse_block()
        else_block = None
        if self.match('ELSE'):
            else_block = self.parse_block()
        return ast.IfStmt(condition, then_block, else_block)

    def parse_while(self) -> ast.WhileStmt:
        self.expect('WHILE')
        self.expect('LPAREN')
        condition = self.parse_expr()
        self.expect('RPAREN')
        body = self.parse_block()
        return ast.WhileStmt(condition, body)

    def parse_for(self) -> ast.ForStmt:
        self.expect('FOR')
        self.expect('LPAREN')

        init = None
        if self.check('LET'):
            init = self.parse_let()          # já consome o SEMI
        elif not self.check('SEMI'):
            init = ast.ExprStmt(self.parse_expr())
            self.expect('SEMI')
        else:
            self.expect('SEMI')

        condition = None
        if not self.check('SEMI'):
            condition = self.parse_expr()
        self.expect('SEMI')

        update = None
        if not self.check('RPAREN'):
            update = self.parse_expr()
        self.expect('RPAREN')

        body = self.parse_block()
        return ast.ForStmt(init, condition, update, body)

    def parse_return(self) -> ast.ReturnStmt:
        self.expect('RETURN')
        value = None
        if not self.check('SEMI'):
            value = self.parse_expr()
        self.expect('SEMI')
        return ast.ReturnStmt(value)

    def parse_asm(self) -> ast.AsmStmt:
        tok = self.advance()
        raw = tok.value
        start = raw.index('{') + 1
        end = raw.rindex('}')
        code = raw[start:end].strip('\n')
        self.match('SEMI')  # ponto e vírgula opcional depois do bloco
        section = {
            'ASM_BLOCK': 'text',
            'ASM_RETURN_BLOCK': 'text',
            'ASM_TEXT_BLOCK': 'text',
            'ASM_DATA_BLOCK': 'data',
            'ASM_RODATA_BLOCK': 'rodata',
            'ASM_BSS_BLOCK': 'bss',
        }[tok.type]
        return ast.AsmStmt(code, section, returns=tok.type == 'ASM_RETURN_BLOCK')

    def parse_expr_statement(self) -> ast.ExprStmt:
        expr = self.parse_expr()
        self.expect('SEMI')
        return ast.ExprStmt(expr)

    # ---- expressões (precedence climbing) ---------------------------------
    def parse_expr(self) -> ast.Node:
        return self.parse_assignment()

    def parse_assignment(self) -> ast.Node:
        expr = self.parse_equality()
        if self.check('ASSIGN'):
            if not isinstance(expr, ast.Ident):
                raise ParseError("Alvo de atribuição inválido", self.peek())
            self.advance()
            value = self.parse_assignment()
            return ast.Assign(expr.name, value)
        return expr

    def _binop_level(self, next_level, *op_types: str) -> ast.Node:
        expr = next_level()
        while self.check(*op_types):
            op = self.advance().type
            right = next_level()
            expr = ast.BinOp(op, expr, right)
        return expr

    def parse_equality(self) -> ast.Node:
        return self._binop_level(self.parse_relational, 'EQ', 'NEQ')

    def parse_relational(self) -> ast.Node:
        return self._binop_level(self.parse_bit_or, 'LT', 'GT', 'LE', 'GE')

    def parse_bit_or(self) -> ast.Node:
        return self._binop_level(self.parse_bit_xor, 'BIT_OR')

    def parse_bit_xor(self) -> ast.Node:
        return self._binop_level(self.parse_bit_and, 'BIT_XOR')

    def parse_bit_and(self) -> ast.Node:
        return self._binop_level(self.parse_shift, 'BIT_AND')

    def parse_shift(self) -> ast.Node:
        return self._binop_level(self.parse_additive, 'SHL', 'SHR')

    def parse_additive(self) -> ast.Node:
        return self._binop_level(self.parse_multiplicative, 'PLUS', 'MINUS')

    def parse_multiplicative(self) -> ast.Node:
        return self._binop_level(self.parse_unary, 'MUL', 'DIV')

    def parse_unary(self) -> ast.Node:
        if self.check('PLUS', 'MINUS', 'BIT_NOT'):
            op = self.advance().type
            operand = self.parse_unary()
            return ast.UnaryOp(op, operand)
        return self.parse_call()

    def parse_call(self) -> ast.Node:
        expr = self.parse_primary()
        while self.check('LPAREN'):
            self.advance()
            args = []
            if not self.check('RPAREN'):
                args.append(self.parse_expr())
                while self.match('COMMA'):
                    args.append(self.parse_expr())
            self.expect('RPAREN')
            if not isinstance(expr, ast.Ident):
                raise ParseError("Somente identificadores podem ser chamados", self.peek())
            expr = ast.Call(expr.name, args)
        return expr

    def parse_primary(self) -> ast.Node:
        tok = self.peek()
        if tok.type == 'NUMBER':
            self.advance()
            return ast.Number(int(tok.value))
        if tok.type == 'STRING':
            self.advance()
            return ast.String(tok.value[1:-1])
        if tok.type == 'IDENT':
            self.advance()
            return ast.Ident(tok.value)
        if tok.type == 'LPAREN':
            self.advance()
            expr = self.parse_expr()
            self.expect('RPAREN')
            return expr
        raise ParseError("Expressão esperada", tok)


def parse(source: str) -> ast.Program:
    """Atalho: tokeniza e faz o parse do código-fonte, retornando o Program."""
    tokens = tokenize(source)
    return Parser(tokens).parse_program()