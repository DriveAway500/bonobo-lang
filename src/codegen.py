"""
codegen.py — Traverses the AST from parser.py and generates LLVM IR using modern llvmlite.
"""

from typing import Dict, List, Optional, Tuple
import llvmlite.ir as ir
import re

# Importing AST nodes directly from parser.py
from parser import (
    AsmBlockNode,
    AsmOperandNode,
    BinaryOpNode,
    BlockNode,
    FnCallNode,
    FunctionDeclNode,
    IdentifierNode,
    IfNode,
    LiteralNode,
    ProgramNode,
    ReturnNode,
    StructDeclNode,
    UnaryOpNode,
    VarDeclNode,
    WhileNode,
)


class CodeGenError(Exception):
    pass


class LLVMCodeGenerator:
    """AST visitor that generates modern LLVM IR (Opaque Pointers compliant)."""

    def __init__(self) -> None:
        self.module = ir.Module(name="main_module")
        self.builder: Optional[ir.IRBuilder] = None
        self.current_fn: Optional[ir.Function] = None

        self.symbol_table: Dict[str, ir.AllocaInstr] = {}
        self.symbol_types: Dict[str, ir.Type] = {}
        self.type_map: Dict[str, ir.Type] = {
            "bool": ir.IntType(1),
            "char": ir.IntType(8),
            "short": ir.IntType(16),
            "int": ir.IntType(32),
            "signed": ir.IntType(32),
            "unsigned": ir.IntType(32),
            "long": ir.IntType(64),
            "float": ir.FloatType(),
            "double": ir.DoubleType(),
            "void": ir.VoidType(),
        }
        self.ptr_type = ir.PointerType()
        self.type_map["string"] = self.ptr_type
        self.type_map["str"] = self.ptr_type
        self.struct_types: Dict[str, ir.IdentifiedStructType] = {}
        self.string_constants: Dict[str, ir.GlobalVariable] = {}

    def _get_llvm_type(self, type_str: Optional[str]) -> ir.Type:
        if not type_str:
            return self.type_map["int"]
        if type_str in self.type_map:
            return self.type_map[type_str]
        if type_str in self.struct_types:
            return self.struct_types[type_str]
        raise CodeGenError(f"Unknown type: {type_str}")

    @staticmethod
    def _decode_string_literal(value: str) -> str:
        """Convert a lexer string token into the value expected by LLVM APIs."""
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return bytes(value[1:-1], "utf-8").decode("unicode_escape")
        return value

    def _is_float(self, value_type: ir.Type) -> bool:
        return isinstance(value_type, (ir.FloatType, ir.DoubleType))

    def _is_integer(self, value_type: ir.Type) -> bool:
        return isinstance(value_type, ir.IntType)

    def _zero(self, value_type: ir.Type) -> ir.Constant:
        if isinstance(value_type, ir.PointerType):
            return ir.Constant(value_type, None)
        return ir.Constant(value_type, 0.0 if self._is_float(value_type) else 0)

    def _coerce(self, value: ir.Value, target_type: ir.Type) -> ir.Value:
        source_type = value.type
        if source_type == target_type:
            return value
        if self._is_integer(source_type) and self._is_integer(target_type):
            if source_type.width < target_type.width:
                return self.builder.sext(value, target_type)
            if source_type.width > target_type.width:
                return self.builder.trunc(value, target_type)
            return value
        if self._is_integer(source_type) and self._is_float(target_type):
            return self.builder.sitofp(value, target_type)
        if self._is_float(source_type) and self._is_integer(target_type):
            return self.builder.fptosi(value, target_type)
        if isinstance(source_type, ir.FloatType) and isinstance(target_type, ir.DoubleType):
            return self.builder.fpext(value, target_type)
        if isinstance(source_type, ir.DoubleType) and isinstance(target_type, ir.FloatType):
            return self.builder.fptrunc(value, target_type)
        raise CodeGenError(f"Cannot convert {source_type} to {target_type}")

    def _as_bool(self, value: ir.Value) -> ir.Value:
        if value.type == ir.IntType(1):
            return value
        if self._is_integer(value.type):
            return self.builder.icmp_signed("!=", value, self._zero(value.type))
        if self._is_float(value.type):
            return self.builder.fcmp_ordered("!=", value, self._zero(value.type))
        if isinstance(value.type, ir.PointerType):
            return self.builder.icmp_unsigned("!=", value, self._zero(value.type))
        raise CodeGenError(f"{value.type} cannot be used as a condition")

    def generate(self, node) -> ir.Module:
        """Main entry point to visit nodes."""
        method_name = f"visit_{type(node).__name__}"
        visitor = getattr(self, method_name, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node):
        raise CodeGenError(f"No visit_{type(node).__name__} method defined.")

    # ---- AST Node Visitors --------------------------------------------------

    def visit_ProgramNode(self, node: ProgramNode) -> ir.Module:
        for stmt in node.statements:
            if isinstance(stmt, StructDeclNode):
                self.generate(stmt)
        for stmt in node.statements:
            if isinstance(stmt, FunctionDeclNode):
                self._declare_function(stmt)
        for stmt in node.statements:
            if isinstance(stmt, FunctionDeclNode):
                self._define_function(stmt)
        return self.module

    def visit_StructDeclNode(self, node: StructDeclNode):
        struct_type = self.module.context.get_identified_type(node.name)
        self.struct_types[node.name] = struct_type
        struct_type.set_body(*(self._get_llvm_type(field_type) for _, field_type in node.fields))
        return struct_type

    def _declare_function(self, node: FunctionDeclNode) -> ir.Function:
        param_types = [self._get_llvm_type(ptype) for _, ptype in node.params]
        ret_type = self._get_llvm_type(node.return_type)
        if node.name in self.module.globals:
            return self.module.globals[node.name]
        func = ir.Function(self.module, ir.FunctionType(ret_type, param_types), name=node.name)
        for argument, (name, _) in zip(func.args, node.params):
            argument.name = name
        return func

    def _define_function(self, node: FunctionDeclNode) -> ir.Function:
        func = self.module.globals[node.name]
        entry_block = func.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry_block)
        self.current_fn = func
        self.symbol_table = {}
        self.symbol_types = {}

        for arg, (pname, ptype) in zip(func.args, node.params):
            alloc_type = self._get_llvm_type(ptype)
            alloca = self.builder.alloca(alloc_type, name=pname)
            self.builder.store(arg, alloca)
            self.symbol_table[pname] = alloca
            self.symbol_types[pname] = alloc_type

        self.generate(node.body)

        if not self.builder.block.is_terminated:
            if isinstance(func.function_type.return_type, ir.VoidType):
                self.builder.ret_void()
            else:
                self.builder.ret(self._zero(func.function_type.return_type))

        self.current_fn = None
        self.builder = None
        return func

    def visit_BlockNode(self, node: BlockNode) -> None:
        for stmt in node.statements:
            if not self.builder.block.is_terminated:
                self.generate(stmt)

    def visit_VarDeclNode(self, node: VarDeclNode) -> ir.AllocaInstr:
        value = self.generate(node.value) if node.value is not None else None
        llvm_type = self._get_llvm_type(node.type) if node.type else (
            value.type if value is not None else self.type_map["int"]
        )
        alloca = self.builder.alloca(llvm_type, name=node.name)

        if value is not None:
            self.builder.store(self._coerce(value, llvm_type), alloca)

        self.symbol_table[node.name] = alloca
        self.symbol_types[node.name] = llvm_type
        return alloca

    def visit_AsmOperandNode(self, node: AsmOperandNode) -> Tuple[str, ir.Value, Optional[str]]:
        """Evaluates operand expression and returns constraint, value and target variable name."""
        target_var = None
        if isinstance(node.expression, IdentifierNode):
            target_var = node.expression.name
            if target_var not in self.symbol_table:
                raise CodeGenError(f"Undefined variable in asm operand: {target_var}")
            
            # If constraint indicates output ('='), pass alloca directly or evaluate
            if node.constraint.startswith("="):
                val = self.symbol_table[target_var]
            else:
                ptr = self.symbol_table[target_var]
                val = self.builder.load(ptr, typ=self.symbol_types[target_var], name=f"{target_var}_asm_in")
        else:
            val = self.generate(node.expression)

        return node.constraint, val, target_var

    def visit_AsmBlockNode(self, node: AsmBlockNode) -> Optional[ir.Instruction]:
        asm_template = self._decode_string_literal(node.template)

        # Process output operands
        out_constraints = []
        out_types = []
        out_targets = []

        for out_op in node.outputs:
            constraint, val_ptr, target_name = self.visit_AsmOperandNode(out_op)
            out_constraints.append(constraint)
            target_type = self.symbol_types[target_name]
            out_types.append(target_type)
            out_targets.append(target_name)

        # Process input operands
        in_constraints = []
        in_values = []
        in_types = []

        for in_op in node.inputs:
            constraint, val, _ = self.visit_AsmOperandNode(in_op)
            in_constraints.append(self._decode_string_literal(constraint))
            in_values.append(val)
            in_types.append(val.type)

        # Combine constraints (Outputs, Inputs, Clobbers)
        all_constraints = [
            self._decode_string_literal(constraint)
            for constraint in out_constraints + in_constraints + node.clobbers
        ]
        constraint_str = ",".join(all_constraints)

        # Determine assembly function signature
        if len(out_types) == 0:
            ret_type = ir.VoidType()
        elif len(out_types) == 1:
            ret_type = out_types[0]
        else:
            ret_type = ir.LiteralStructType(out_types)

        asm_fn_type = ir.FunctionType(ret_type, in_types)
        inline_asm = ir.InlineAsm(
            asm_fn_type,
            asm_template,
            constraint_str,
            side_effect=node.is_volatile
        )

        res = self.builder.call(inline_asm, in_values)

        # Map execution outputs back to variables
        if len(out_targets) == 1:
            self.builder.store(res, self.symbol_table[out_targets[0]])
        elif len(out_targets) > 1:
            for idx, name in enumerate(out_targets):
                val_extracted = self.builder.extract_value(res, idx)
                self.builder.store(val_extracted, self.symbol_table[name])

        return res

    def visit_BinaryOpNode(self, node: BinaryOpNode) -> ir.Value:
        if node.op == "=":
            if not isinstance(node.left, IdentifierNode) or node.left.name not in self.symbol_table:
                raise CodeGenError("Assignment target must be a declared variable")
            target_type = self.symbol_types[node.left.name]
            value = self._coerce(self.generate(node.right), target_type)
            self.builder.store(value, self.symbol_table[node.left.name])
            return value

        left = self.generate(node.left)
        right = self.generate(node.right)
        if node.op in ("&&", "||"):
            left, right = self._as_bool(left), self._as_bool(right)
            return self.builder.and_(left, right) if node.op == "&&" else self.builder.or_(left, right)

        if self._is_float(left.type) or self._is_float(right.type):
            common_type = ir.DoubleType() if isinstance(left.type, ir.DoubleType) or isinstance(right.type, ir.DoubleType) else ir.FloatType()
            left, right = self._coerce(left, common_type), self._coerce(right, common_type)
            arithmetic = {"+": self.builder.fadd, "-": self.builder.fsub, "*": self.builder.fmul, "/": self.builder.fdiv, "%": self.builder.frem}
            comparisons = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!="}
            if node.op in arithmetic:
                return arithmetic[node.op](left, right)
            if node.op in comparisons:
                return self.builder.fcmp_ordered(comparisons[node.op], left, right)

        if self._is_integer(left.type) and self._is_integer(right.type):
            common_type = left.type if left.type.width >= right.type.width else right.type
            left, right = self._coerce(left, common_type), self._coerce(right, common_type)
            arithmetic = {"+": self.builder.add, "-": self.builder.sub, "*": self.builder.mul, "/": self.builder.sdiv, "%": self.builder.srem, "&": self.builder.and_, "|": self.builder.or_, "^": self.builder.xor, "<<": self.builder.shl, ">>": self.builder.ashr}
            comparisons = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "!="}
            if node.op in arithmetic:
                return arithmetic[node.op](left, right)
            if node.op in comparisons:
                return self.builder.icmp_signed(comparisons[node.op], left, right)
        raise CodeGenError(f"Binary operator {node.op} not implemented for {left.type} and {right.type}")

    def visit_UnaryOpNode(self, node: UnaryOpNode):
        if node.op == "&":
            if not isinstance(node.operand, IdentifierNode):
                raise CodeGenError("Address-of operator requires a variable")
            if node.operand.name not in self.symbol_table:
                raise CodeGenError(f"Undefined variable: {node.operand.name}")
            return self.symbol_table[node.operand.name]

        value = self.generate(node.operand)
        if node.op == "-":
            return self.builder.fsub(self._zero(value.type), value) if self._is_float(value.type) else self.builder.neg(value)
        if node.op == "!":
            return self.builder.icmp_unsigned("==", self._as_bool(value), ir.Constant(ir.IntType(1), 0))
        if node.op == "~" and self._is_integer(value.type):
            return self.builder.not_(value)
        raise CodeGenError(f"Unary operator {node.op} not implemented for {value.type}")

    def visit_LiteralNode(self, node: LiteralNode) -> ir.Constant:
        value = node.value
        if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
            text = bytes(value[1:-1], "utf-8").decode("unicode_escape") + "\0"
            global_value = self.string_constants.get(text)
            if global_value is None:
                array_type = ir.ArrayType(ir.IntType(8), len(text.encode("utf-8")))
                global_value = ir.GlobalVariable(self.module, array_type, name=f".str.{len(self.string_constants)}")
                global_value.linkage = "private"
                global_value.global_constant = True
                global_value.initializer = ir.Constant(array_type, bytearray(text.encode("utf-8")))
                self.string_constants[text] = global_value
            zero = ir.Constant(ir.IntType(32), 0)
            return self.builder.gep(global_value, [zero, zero], inbounds=True, name="strtmp")
        if isinstance(value, str) and ("." in value or "e" in value.lower()):
            return ir.Constant(ir.DoubleType(), float(value))
        return ir.Constant(ir.IntType(32), int(value, 0) if isinstance(value, str) else int(value))

    def visit_IdentifierNode(self, node: IdentifierNode) -> ir.Instruction:
        if node.name not in self.symbol_table:
            raise CodeGenError(f"Undefined variable: {node.name}")
        ptr = self.symbol_table[node.name]
        return self.builder.load(ptr, typ=self.symbol_types[node.name], name=node.name)

    def visit_FnCallNode(self, node: FnCallNode) -> ir.Instruction:
        func = self.module.globals.get(node.name)
        if not isinstance(func, ir.Function):
            raise CodeGenError(f"Undefined function: {node.name}")
        expected_types = func.function_type.args
        if len(expected_types) != len(node.args):
            raise CodeGenError(f"Function {node.name} expects {len(expected_types)} argument(s)")
        args = [self._coerce(self.generate(arg), expected_types[index]) for index, arg in enumerate(node.args)]
        if isinstance(func.function_type.return_type, ir.VoidType):
            return self.builder.call(func, args)
        return self.builder.call(func, args, name="calltmp")

    def visit_ReturnNode(self, node: ReturnNode) -> ir.Instruction:
        return_type = self.current_fn.function_type.return_type
        if node.expression is None:
            if not isinstance(return_type, ir.VoidType):
                raise CodeGenError("Non-void function must return a value")
            return self.builder.ret_void()
        if isinstance(return_type, ir.VoidType):
            raise CodeGenError("Void function cannot return a value")
        return self.builder.ret(self._coerce(self.generate(node.expression), return_type))

    def visit_IfNode(self, node: IfNode) -> None:
        cond_val = self._as_bool(self.generate(node.condition))

        then_bb = self.current_fn.append_basic_block("then")
        else_bb = (
            self.current_fn.append_basic_block("else")
            if node.else_branch
            else None
        )
        merge_bb = self.current_fn.append_basic_block("ifcont")

        self.builder.cbranch(cond_val, then_bb, else_bb or merge_bb)

        self.builder.position_at_start(then_bb)
        self.generate(node.then_branch)
        if not then_bb.is_terminated:
            self.builder.branch(merge_bb)

        if else_bb:
            self.builder.position_at_start(else_bb)
            self.generate(node.else_branch)
            if not else_bb.is_terminated:
                self.builder.branch(merge_bb)

        self.builder.position_at_start(merge_bb)

    def visit_WhileNode(self, node: WhileNode) -> None:
        cond_bb = self.current_fn.append_basic_block("while.cond")
        body_bb = self.current_fn.append_basic_block("while.body")
        end_bb = self.current_fn.append_basic_block("while.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_start(cond_bb)

        cond_val = self._as_bool(self.generate(node.condition))
        self.builder.cbranch(cond_val, body_bb, end_bb)

        self.builder.position_at_start(body_bb)
        self.generate(node.body)
        if not self.builder.block.is_terminated:
            self.builder.branch(cond_bb)

        self.builder.position_at_start(end_bb)