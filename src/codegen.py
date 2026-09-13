"""
codegen.py — Percorre a AST (nodes.py) e produz assembly NASM
(x86-64, Linux, System V calling convention), usando os trechos
definidos em templates.py.

Estratégia geral:
  - Cada `def nome(...) { ... }` vira o label `func_nome`.
  - Instruções soltas no nível do módulo (fora de qualquer `def`) são
    agrupadas dentro de uma função sintética `func_main`, chamada por
    `_start`.
  - Variáveis locais (parâmetros e `let` dentro de função) vivem na pilha,
    referenciadas via [rbp-offset].
  - Variáveis globais (`let` no nível do módulo) vivem em .bss.
  - Literais de string são deduplicados e colocados em .data.
  - Blocos `asm { ... }` são emitidos verbatim (o usuário assume controle
    total do que acontece ali).
"""

from typing import Dict, List, Optional

from utils import nodes as ast
from utils import templates as tmpl


class CodeGenError(Exception):
    pass


class Scope:
    """Tabela de símbolos de uma função: nome -> deslocamento (rbp-relativo)."""

    def __init__(self):
        self.offsets: Dict[str, int] = {}
        self.next_offset = 8  # primeiro slot logo abaixo do rbp salvo

    def declare(self, name: str) -> int:
        if name in self.offsets:
            return self.offsets[name]
        offset = self.next_offset
        self.offsets[name] = offset
        self.next_offset += 8
        return offset

    def lookup(self, name: str) -> Optional[int]:
        return self.offsets.get(name)


class CodeGen:
    def __init__(self):
        self.data_lines: List[str] = []
        self.rodata_lines: List[str] = []
        self.bss_lines: List[str] = []
        self.text_lines: List[str] = []

        self.globals: Dict[str, str] = {}        # nome -> label .bss
        self.functions: Dict[str, ast.FunctionDef] = {}
        self.string_pool: Dict[str, str] = {}     # conteúdo -> label .data

        self._label_count = 0
        self.scope: Optional[Scope] = None

    # ---- utilidades -----------------------------------------------------
    def new_label(self, prefix: str = 'L') -> str:
        self._label_count += 1
        return f"{prefix}{self._label_count}"

    def emit(self, line: str = '') -> None:
        self.text_lines.append(line)

    def string_label(self, value: str) -> str:
        if value in self.string_pool:
            return self.string_pool[value]
        label = f"str_{len(self.string_pool)}"
        self.string_pool[value] = label
        self.data_lines.append(
            tmpl.STRING_DATA.format(label=label, bytes=f'"{value}"')
        )
        return label

    def var_ref(self, name: str) -> str:
        if self.scope is not None:
            offset = self.scope.lookup(name)
            if offset is not None:
                return f"[rbp-{offset}]"
        if name in self.globals:
            return f"[{self.globals[name]}]"
        raise CodeGenError(f"Variável não declarada: {name!r}")

    def declare_target(self, name: str) -> str:
        """Reserva armazenamento para `name` (local se dentro de função,
        global/.bss caso contrário) e devolve o operando de memória."""
        if self.scope is not None:
            offset = self.scope.declare(name)
            return f"[rbp-{offset}]"
        label = f"var_{name}"
        if name not in self.globals:
            self.globals[name] = label
            self.bss_lines.append(tmpl.GLOBAL_VAR_BSS.format(label=label))
        return f"[{label}]"

    # ---- ponto de entrada -------------------------------------------------
    def generate(self, program: ast.Program) -> str:
        # primeiro passo: registra assinaturas de todas as funções
        for node in program.body:
            if isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node

        self.emit(tmpl.GLOBAL_START)
        self.emit()
        self.emit(tmpl.SECTION_TEXT_HEADER)
        self.emit()

        main_stmts: List[ast.Node] = []
        for node in program.body:
            if isinstance(node, ast.FunctionDef):
                self.gen_function(node)
                self.emit()
            else:
                main_stmts.append(node)

        self.gen_synthetic_main(main_stmts)
        self.emit()
        self.emit(tmpl.START_ENTRY)

        parts = [tmpl.HEADER]
        if self.data_lines:
            parts.append(tmpl.SECTION_DATA_HEADER)
            parts.extend(self.data_lines)
            parts.append('')
        if self.rodata_lines:
            parts.append(tmpl.SECTION_RODATA_HEADER)
            parts.extend(self.rodata_lines)
            parts.append('')
        if self.bss_lines:
            parts.append(tmpl.SECTION_BSS_HEADER)
            parts.extend(self.bss_lines)
            parts.append('')
        parts.extend(self.text_lines)
        return '\n'.join(parts) + '\n'

    def gen_synthetic_main(self, statements: List[ast.Node]) -> None:
        """Instruções soltas no módulo viram o corpo de `func_main`."""
        self.emit(tmpl.FUNC_PROLOGUE.format(label='func_main'))
        self.scope = Scope()
        for stmt in statements:
            self.gen_statement(stmt)
        self.scope = None
        if not self.statement_terminates(statements[-1] if statements else None):
            self.emit('    xor rax, rax')
            self.emit(tmpl.FUNC_EPILOGUE)

    # ---- funções ------------------------------------------------------------
    def gen_function(self, node: ast.FunctionDef) -> None:
        label = f"func_{node.name}"
        self.emit(tmpl.FUNC_PROLOGUE.format(label=label))
        self.scope = Scope()

        for i, param in enumerate(node.params):
            if param.variadic:
                continue
            offset = self.scope.declare(param.name)
            if i < len(tmpl.ARG_REGISTERS):
                reg = tmpl.ARG_REGISTERS[i]
                self.emit(tmpl.PARAM_SPILL.format(offset=offset, reg=reg, name=param.name))

        for stmt in node.body.statements:
            self.gen_statement(stmt)

        self.scope = None
        if not self.statement_terminates(
            node.body.statements[-1] if node.body.statements else None
        ):
            self.emit(tmpl.FUNC_EPILOGUE)

    def statement_terminates(self, node: Optional[ast.Node]) -> bool:
        if isinstance(node, (ast.ReturnStmt, ast.AsmStmt)):
            return isinstance(node, ast.ReturnStmt) or node.returns
        if isinstance(node, ast.Block):
            return self.statement_terminates(
                node.statements[-1] if node.statements else None
            )
        if isinstance(node, ast.IfStmt) and node.else_block is not None:
            return (
                self.statement_terminates(node.then_block)
                and self.statement_terminates(node.else_block)
            )
        return False

    # ---- statements -----------------------------------------------------------
    def gen_statement(self, node: ast.Node) -> None:
        method = getattr(self, f"gen_{type(node).__name__}", None)
        if method is None:
            raise CodeGenError(f"Statement não suportado: {type(node).__name__}")
        method(node)

    def gen_LetStmt(self, node: ast.LetStmt) -> None:
        value_expr = node.value
        if node.caster:
            # açúcar: `let TIPO(NOME) = EXPR;` -> avalia TIPO(EXPR)
            value_expr = ast.Call(node.caster, [value_expr])
        self.gen_expr(value_expr)
        target = self.declare_target(node.name)
        self.emit(f"    mov {target}, rax")

    def gen_Assign(self, node: ast.Assign) -> None:
        self.gen_expr(node.value)
        target = self.declare_target(node.name)
        self.emit(f"    mov {target}, rax")

    def gen_ExprStmt(self, node: ast.ExprStmt) -> None:
        self.gen_expr(node.expr)

    def gen_Block(self, node: ast.Block) -> None:
        for stmt in node.statements:
            self.gen_statement(stmt)

    def gen_IfStmt(self, node: ast.IfStmt) -> None:
        else_label = self.new_label('else')
        end_label = self.new_label('endif')

        self.gen_expr(node.condition)
        self.emit(tmpl.JUMP_IF_FALSE.format(label=else_label if node.else_block else end_label))
        self.gen_statement(node.then_block)

        if node.else_block:
            self.emit(tmpl.JUMP.format(label=end_label))
            self.emit(tmpl.LABEL.format(label=else_label))
            self.gen_statement(node.else_block)

        self.emit(tmpl.LABEL.format(label=end_label))

    def gen_WhileStmt(self, node: ast.WhileStmt) -> None:
        start_label = self.new_label('while')
        end_label = self.new_label('endwhile')

        self.emit(tmpl.LABEL.format(label=start_label))
        self.gen_expr(node.condition)
        self.emit(tmpl.JUMP_IF_FALSE.format(label=end_label))
        self.gen_statement(node.body)
        self.emit(tmpl.JUMP.format(label=start_label))
        self.emit(tmpl.LABEL.format(label=end_label))

    def gen_ForStmt(self, node: ast.ForStmt) -> None:
        if node.init:
            self.gen_statement(node.init)

        start_label = self.new_label('for')
        end_label = self.new_label('endfor')

        self.emit(tmpl.LABEL.format(label=start_label))
        if node.condition:
            self.gen_expr(node.condition)
            self.emit(tmpl.JUMP_IF_FALSE.format(label=end_label))
        self.gen_statement(node.body)
        if node.update:
            self.gen_expr(node.update)
        self.emit(tmpl.JUMP.format(label=start_label))
        self.emit(tmpl.LABEL.format(label=end_label))

    def gen_ReturnStmt(self, node: ast.ReturnStmt) -> None:
        if node.value is not None:
            self.gen_expr(node.value)
        else:
            self.emit('    xor rax, rax')
        self.emit(tmpl.FUNC_EPILOGUE)

    def gen_AsmStmt(self, node: ast.AsmStmt) -> None:
        if node.returns and node.section != 'text':
            raise CodeGenError("asm_return só pode ser usado na seção .text")

        if node.section == 'data':
            target = self.data_lines
        elif node.section == 'rodata':
            target = self.rodata_lines
        elif node.section == 'bss':
            target = self.bss_lines
        elif node.section == 'text':
            target = self.text_lines
        else:
            raise CodeGenError(f"Seção assembly não suportada: {node.section!r}")

        target.append(tmpl.INLINE_ASM_BEGIN)
        for line in node.code.splitlines():
            target.append(f"    {line.strip()}" if line.strip() else '')
        target.append(tmpl.INLINE_ASM_END)
        if node.returns:
            target.append(tmpl.FUNC_EPILOGUE)

    # ---- expressões ------------------------------------------------------------
    def gen_expr(self, node: ast.Node) -> None:
        method = getattr(self, f"gen_{type(node).__name__}", None)
        if method is None:
            raise CodeGenError(f"Expressão não suportada: {type(node).__name__}")
        method(node)

    def gen_Number(self, node: ast.Number) -> None:
        self.emit(f"    mov rax, {node.value}")

    def gen_String(self, node: ast.String) -> None:
        label = self.string_label(node.value)
        self.emit(f"    lea rax, [{label}]")

    def gen_Ident(self, node: ast.Ident) -> None:
        self.emit(f"    mov rax, {self.var_ref(node.name)}")

    def gen_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.gen_expr(node.operand)
        instr = tmpl.UNARY_OP_INSTR.get(node.op)
        if instr is None:
            raise CodeGenError(f"Operador unário não suportado: {node.op}")
        self.emit(f"    {instr}")

    def gen_BinOp(self, node: ast.BinOp) -> None:
        self.gen_expr(node.left)
        self.emit('    push rax')
        self.gen_expr(node.right)
        self.emit('    mov rbx, rax')
        self.emit('    pop rax')

        if node.op == 'DIV':
            self.emit(tmpl.DIV_OP_INSTR)
        elif node.op in tmpl.BIN_OP_INSTR:
            self.emit(f"    {tmpl.BIN_OP_INSTR[node.op]}")
        elif node.op in tmpl.COMPARISON_SETCC:
            self.emit('    cmp rax, rbx')
            self.emit('    xor rax, rax')
            self.emit(f"    {tmpl.COMPARISON_SETCC[node.op]}")
        else:
            raise CodeGenError(f"Operador binário não suportado: {node.op}")

    def gen_Call(self, node: ast.Call) -> None:
        for i, arg in enumerate(node.args):
            self.gen_expr(arg)
            if i < len(tmpl.ARG_REGISTERS):
                self.emit(f"    mov {tmpl.ARG_REGISTERS[i]}, rax")
            else:
                self.emit('    push rax')  # args extras vão na pilha (7º+)
        if node.callee not in self.functions:
            raise CodeGenError(f"Função não definida: {node.callee!r}")
        self.emit(f"    call func_{node.callee}")


def generate(program: ast.Program) -> str:
    """Atalho: gera o assembly NASM completo a partir do Program."""
    return CodeGen().generate(program)