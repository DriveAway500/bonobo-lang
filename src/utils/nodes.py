"""
nodes.py — Definições dos nós da árvore sintática abstrata (AST)
da linguagem "Bonobo".
"""

from dataclasses import dataclass, field
from typing import List, Optional


class Node:
    """Classe-base de todos os nós da AST (apenas para type-checking/isinstance)."""
    pass


# ---------------------------------------------------------------------------
# Estrutura do programa
# ---------------------------------------------------------------------------
@dataclass
class Program(Node):
    body: List[Node]


@dataclass
class Param(Node):
    name: str
    variadic: bool = False


@dataclass
class FunctionDef(Node):
    name: str
    params: List[Param]
    body: 'Block'


@dataclass
class Block(Node):
    statements: List[Node]


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------
@dataclass
class LetStmt(Node):
    """
    let NOME = EXPR;
    let TIPO(NOME) = EXPR;   -> açúcar sintático: equivale a
                                 let NOME = TIPO(EXPR);
                                 (usado em `let str(message) = "...";`)
    """
    name: str
    caster: Optional[str]
    value: Node


@dataclass
class Assign(Node):
    """NOME = EXPR;  (atribuição a variável já declarada)"""
    name: str
    value: Node


@dataclass
class IfStmt(Node):
    condition: Node
    then_block: Block
    else_block: Optional[Block] = None


@dataclass
class WhileStmt(Node):
    condition: Node
    body: Block


@dataclass
class ForStmt(Node):
    init: Optional[Node]
    condition: Optional[Node]
    update: Optional[Node]
    body: Block


@dataclass
class ReturnStmt(Node):
    value: Optional[Node]


@dataclass
class AsmStmt(Node):
    """Bloco assembly destinado a uma seção específica do output."""
    code: str
    section: str = 'text'
    returns: bool = False


@dataclass
class ExprStmt(Node):
    expr: Node


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------
@dataclass
class BinOp(Node):
    op: str          # tipo do token: PLUS, MINUS, EQ, BIT_AND, SHL, ...
    left: Node
    right: Node


@dataclass
class UnaryOp(Node):
    op: str          # MINUS, PLUS, BIT_NOT
    operand: Node


@dataclass
class Call(Node):
    callee: str
    args: List[Node] = field(default_factory=list)


@dataclass
class Number(Node):
    value: int


@dataclass
class String(Node):
    value: str


@dataclass
class Ident(Node):
    name: str