"""
codegen.py — Traverses the AST from parser.py and generates LLVM IR using modern llvmlite.
"""

from typing import Dict, List, Optional, Tuple
import llvmlite.binding as llvm
import llvmlite.ir as ir
import re

# Importing AST nodes directly from parser.py
from parser import (
    AsmBlockNode,
    AsmOperandNode,
    BinaryOpNode,
    BlockNode,
    BreakNode,
    ContinueNode,
    EnumDeclNode,
    FnCallNode,
    ForNode,
    FunctionDeclNode,
    IdentifierNode,
    IfNode,
    LiteralNode,
    MemberAccessNode,
    ProgramNode,
    ReturnNode,
    StructDeclNode,
    UnaryOpNode,
    VarDeclNode,
    WhileNode,
)


class CodeGenError(Exception):
    pass


_LLVM_INITIALIZED = False


def initialize_llvm() -> None:
    """Initialize LLVM's native target support once per process."""
    global _LLVM_INITIALIZED
    if _LLVM_INITIALIZED:
        return
    try:
        llvm.initialize()
    except RuntimeError as error:
        # llvmlite 0.49+ initializes LLVM automatically and rejects the
        # legacy call; older supported versions still require it.
        if "initialization is now handled automatically" not in str(error):
            raise
    llvm.initialize_native_target()
    llvm.initialize_native_asmprinter()
    _LLVM_INITIALIZED = True


class LLVMCodeGenerator:
    """AST visitor that generates modern LLVM IR (Opaque Pointers compliant)."""

    def __init__(self) -> None:
        initialize_llvm()
        self.context = ir.Context()
        self.module = ir.Module(name="main_module", context=self.context)
        self.builder: Optional[ir.IRBuilder] = None
        self.current_fn: Optional[ir.Function] = None

        self.symbol_table: Dict[str, ir.AllocaInstr] = {}
        self.symbol_types: Dict[str, ir.Type] = {}
        self.loop_break_stack: List[ir.BasicBlock] = []
        self.loop_continue_stack: List[ir.BasicBlock] = []
        self.type_map: Dict[str, ir.Type] = {
            # Only genuine LLVM type names live here. Arbitrary widths not
            # listed (i2, i24, i512, ...) are still resolved dynamically by
            # the iN regex in _get_llvm_type.
            "i1": ir.IntType(1),
            "i8": ir.IntType(8),
            "i16": ir.IntType(16),
            "i32": ir.IntType(32),
            "i64": ir.IntType(64),
            "i128": ir.IntType(128),
            "half": self._optional_llvm_type("HalfType"),
            "bfloat": self._optional_llvm_type("BFloatType"),
            "float": ir.FloatType(),
            "double": ir.DoubleType(),
            "fp128": self._optional_llvm_type("FP128Type"),
            "x86_fp80": self._optional_llvm_type("X86_FP80Type"),
            "ppc_fp128": self._optional_llvm_type("PPC_FP128Type"),
            "x86_mmx": self._optional_llvm_type("MMXType"),
            "x86_amx": self._optional_llvm_type("AMXType"),
            "label": self._optional_llvm_type("LabelType"),
            "metadata": self._optional_llvm_type("MetaDataType"),
            "token": self._optional_llvm_type("TokenType"),
            "void": ir.VoidType(),
        }
        self.type_map = {name: value for name, value in self.type_map.items() if value is not None}
        self.ptr_type = ir.PointerType()
        self.type_map["ptr"] = self.ptr_type
        self.type_map["string"] = self.ptr_type
        self.type_map["str"] = self.ptr_type
        self.struct_types: Dict[str, ir.IdentifiedStructType] = {}
        self.struct_fields: Dict[str, Dict[str, Tuple[int, ir.Type]]] = {}
        self.enum_values: Dict[str, int] = {}
        self.enum_types: Dict[str, Dict[str, int]] = {}
        # Each enum picks its own backing integer type (default i32, or
        # whatever native type follows the ":" in its declaration, e.g.
        # "enum PacketType : i8 { ... }"). enum_underlying_types maps the
        # enum's own name to that type — mirrors how struct_types backs
        # struct names — so it resolves wherever a type is expected
        # (params, fields, var decls, return types). enum_value_types keeps
        # the same per-entry type alongside enum_values (bare and qualified
        # variant names) so a constant is always built with the right width.
        self.enum_underlying_types: Dict[str, ir.Type] = {}
        self.enum_value_types: Dict[str, ir.Type] = {}
        self.pointer_pointees: Dict[str, ir.Type] = {}
        self.string_constants: Dict[str, ir.GlobalVariable] = {}

    @staticmethod
    def _optional_llvm_type(type_name: str) -> Optional[ir.Type]:
        type_class = getattr(ir, type_name, None)
        return type_class() if type_class is not None else None

    def _get_llvm_type(self, type_str: Optional[str]) -> ir.Type:
        if not type_str:
            return self.type_map["i32"]
        if type_str in self.type_map:
            return self.type_map[type_str]
        if type_str in self.struct_types:
            return self.struct_types[type_str]
        if type_str in self.enum_underlying_types:
            return self.enum_underlying_types[type_str]
        if type_str.endswith("*"):
            return ir.PointerType()
        integer_match = re.fullmatch(r"i([1-9][0-9]*)", type_str)
        if integer_match:
            return ir.IntType(int(integer_match.group(1)))
        array_match = re.fullmatch(r"\[\s*([1-9][0-9]*)\s+x\s+(.+)\s*\]", type_str)
        if array_match:
            return ir.ArrayType(self._get_llvm_type(array_match.group(2)), int(array_match.group(1)))
        vector_match = re.fullmatch(r"<\s*([1-9][0-9]*)\s+x\s+(.+)\s*>", type_str)
        if vector_match:
            return ir.VectorType(self._get_llvm_type(vector_match.group(2)), int(vector_match.group(1)))
        if type_str.startswith("{") and type_str.endswith("}"):
            fields = self._split_type_list(type_str[1:-1])
            return ir.LiteralStructType([self._get_llvm_type(field) for field in fields])
        raise CodeGenError(f"Unknown type: {type_str}")

    @staticmethod
    def _split_type_list(type_list: str) -> List[str]:
        """Split aggregate members without splitting nested LLVM types."""
        parts = []
        start = 0
        depth = 0
        for index, character in enumerate(type_list):
            if character in "[{<":
                depth += 1
            elif character in "]}>":
                depth -= 1
            elif character == "," and depth == 0:
                parts.append(type_list[start:index].strip())
                start = index + 1
        final_part = type_list[start:].strip()
        if final_part:
            parts.append(final_part)
        return parts

    @staticmethod
    def _decode_string_literal(value: str) -> str:
        """Convert a lexer string token into the value expected by LLVM APIs."""
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return bytes(value[1:-1], "utf-8").decode("unicode_escape")
        return value

    def _is_float(self, value_type: ir.Type) -> bool:
        float_classes = [
            getattr(ir, type_name, None)
            for type_name in (
                "HalfType", "BFloatType", "FloatType", "DoubleType",
                "FP128Type", "X86_FP80Type", "PPC_FP128Type",
            )
        ]
        return isinstance(value_type, tuple(type_class for type_class in float_classes if type_class))

    def _is_integer(self, value_type: ir.Type) -> bool:
        return isinstance(value_type, ir.IntType)

    @staticmethod
    def _is_pointer(value_type: ir.Type) -> bool:
        return isinstance(value_type, ir.PointerType)

    @staticmethod
    def _is_vector(value_type: ir.Type) -> bool:
        return isinstance(value_type, ir.VectorType)

    def _zero(self, value_type: ir.Type) -> ir.Constant:
        if isinstance(value_type, ir.PointerType):
            return ir.Constant(value_type, None)
        return ir.Constant(value_type, 0.0 if self._is_float(value_type) else 0)

    # LLVM integer types are signless, so signedness is selected by the
    # conversion helper rather than encoded in the type map.
    conversion_map = {
        ("integer", "integer"): ("trunc", "zext", "sext"),
        ("integer", "float"): ("uitofp", "sitofp", "bitcast"),
        ("float", "integer"): ("fptoui", "fptosi", "bitcast"),
        ("float", "float"): ("fptrunc", "fpext"),
        ("integer", "pointer"): ("inttoptr",),
        ("pointer", "integer"): ("ptrtoint",),
        ("pointer", "pointer"): ("bitcast", "addrspacecast"),
    }

    def _type_category(self, value_type: ir.Type) -> Optional[str]:
        if self._is_integer(value_type):
            return "integer"
        if self._is_float(value_type):
            return "float"
        if self._is_pointer(value_type):
            return "pointer"
        if self._is_vector(value_type):
            element_type = value_type.element
            if self._is_integer(element_type):
                return "integer"
            if self._is_float(element_type):
                return "float"
        return None

    @staticmethod
    def _float_rank(value_type: ir.Type) -> int:
        type_name = str(value_type)
        return {"half": 16, "bfloat": 16, "float": 32, "double": 64,
                "x86_fp80": 80, "fp128": 128, "ppc_fp128": 128}.get(type_name, 0)

    def _coerce(self, value: ir.Value, target_type: ir.Type, signed: bool = True) -> ir.Value:
        """Apply a legal LLVM conversion between first-class value types."""
        source_type = value.type
        if source_type == target_type:
            return value

        source_category = self._type_category(source_type)
        target_category = self._type_category(target_type)
        if source_category is None or target_category is None:
            raise CodeGenError(f"Cannot convert {source_type} to {target_type}")

        source_is_vector = self._is_vector(source_type)
        target_is_vector = self._is_vector(target_type)
        if source_is_vector != target_is_vector:
            raise CodeGenError(f"Cannot convert {source_type} to {target_type}")
        if source_is_vector and source_type.count != target_type.count:
            raise CodeGenError(f"Cannot convert vectors with different lengths: {source_type} to {target_type}")

        source_scalar = source_type.element if source_is_vector else source_type
        target_scalar = target_type.element if target_is_vector else target_type

        if source_category == "integer" and target_category == "integer":
            if source_scalar.width < target_scalar.width:
                return self.builder.sext(value, target_type) if signed else self.builder.zext(value, target_type)
            if source_scalar.width > target_scalar.width:
                return self.builder.trunc(value, target_type)
            return self.builder.bitcast(value, target_type)

        if source_category == "integer" and target_category == "float":
            return (self.builder.sitofp if signed else self.builder.uitofp)(value, target_type)
        if source_category == "float" and target_category == "integer":
            return (self.builder.fptosi if signed else self.builder.fptoui)(value, target_type)
        if source_category == "float" and target_category == "float":
            if self._float_rank(source_scalar) < self._float_rank(target_scalar):
                return self.builder.fpext(value, target_type)
            return self.builder.fptrunc(value, target_type)

        if source_category == "integer" and target_category == "pointer":
            return self.builder.inttoptr(value, target_type)
        if source_category == "pointer" and target_category == "integer":
            return self.builder.ptrtoint(value, target_type)
        if source_category == "pointer" and target_category == "pointer":
            source_address_space = getattr(source_type, "addrspace", 0)
            target_address_space = getattr(target_type, "addrspace", 0)
            if source_address_space != target_address_space:
                return self.builder.addrspacecast(value, target_type)
            return self.builder.bitcast(value, target_type)

        raise CodeGenError(f"Cannot convert {source_type} to {target_type}")

    def _coerce_unsigned(self, value: ir.Value, target_type: ir.Type) -> ir.Value:
        """Unsigned counterpart for integer/floating conversions."""
        return self._coerce(value, target_type, signed=False)

    def _bitcast(self, value: ir.Value, target_type: ir.Type) -> ir.Value:
        """Reinterpret equal-width scalar or vector bits without conversion."""
        source_type = value.type
        source_scalar = source_type.element if self._is_vector(source_type) else source_type
        target_scalar = target_type.element if self._is_vector(target_type) else target_type
        if self._is_vector(source_type) != self._is_vector(target_type):
            raise CodeGenError(f"Cannot bitcast {source_type} to {target_type}")
        if self._is_vector(source_type) and source_type.count != target_type.count:
            raise CodeGenError(f"Cannot bitcast vectors with different lengths: {source_type} to {target_type}")
        source_bits = self._type_bit_width(source_scalar)
        target_bits = self._type_bit_width(target_scalar)
        if source_bits is None or target_bits is None or source_bits != target_bits:
            raise CodeGenError(f"Cannot bitcast different-width types: {source_type} to {target_type}")
        return self.builder.bitcast(value, target_type)

    def _type_bit_width(self, value_type: ir.Type) -> Optional[int]:
        if self._is_integer(value_type):
            return value_type.width
        if self._is_float(value_type):
            return self._float_rank(value_type)
        return None

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

    def generate_optimized_ir(self, node, opt_level: int = 3) -> str:
        """Generate verified LLVM IR and run LLVM's target optimization pipeline.

        This method consumes the generator's module once. Create a new
        ``LLVMCodeGenerator`` when compiling another AST.
        """
        if isinstance(opt_level, bool) or not isinstance(opt_level, int):
            raise ValueError("opt_level must be an integer from 0 to 3")
        if opt_level not in range(4):
            raise ValueError("opt_level must be an integer from 0 to 3")

        raw_module = self.generate(node)
        optimized_module = llvm.parse_assembly(str(raw_module))
        optimized_module.verify()

        target_machine = llvm.Target.from_default_triple().create_target_machine()
        if hasattr(llvm, "PassManagerBuilder"):
            pass_manager_builder = llvm.PassManagerBuilder()
            pass_manager_builder.opt_level = opt_level
            module_pass_manager = llvm.ModulePassManager()
            target_machine.add_analysis_passes(module_pass_manager)
            pass_manager_builder.populate(module_pass_manager)
            module_pass_manager.run(optimized_module)
        else:
            # LLVM 22 removed the legacy PassManagerBuilder API. Its new
            # pipeline exposes speed levels 0..2, so O3 uses the strongest
            # available default pipeline just like O2 on that API.
            tuning_options = llvm.create_pipeline_tuning_options(
                speed_level=min(opt_level, 2)
            )
            pass_builder = llvm.create_pass_builder(target_machine, tuning_options)
            module_pass_manager = pass_builder.getModulePassManager()
            module_pass_manager.run(optimized_module, pass_builder)
        optimized_module.verify()
        return str(optimized_module)

    def generic_visit(self, node):
        raise CodeGenError(f"No visit_{type(node).__name__} method defined.")

    # ---- AST Node Visitors --------------------------------------------------

    def visit_ProgramNode(self, node: ProgramNode) -> ir.Module:
        # Enums first: they have no dependencies on other declarations and
        # resolve to a plain integer type, so registering all of them up
        # front lets struct fields / function signatures reference an enum
        # declared later in the source, matching how nested struct-in-struct
        # already relies on declaration order for by-value fields.
        for stmt in node.statements:
            if isinstance(stmt, EnumDeclNode):
                self.generate(stmt)
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
        field_types = [self._get_llvm_type(field_type) for _, field_type in node.fields]
        struct_type.set_body(*field_types)
        self.struct_fields[node.name] = {
            field_name: (index, field_types[index])
            for index, (field_name, _) in enumerate(node.fields)
        }
        return struct_type

    def visit_EnumDeclNode(self, node: EnumDeclNode):
        # No explicit "enum Name : type { ... }" backing type -> default to
        # this language's native i32, same as an untyped "let" would get.
        underlying_type = (
            self._get_llvm_type(node.underlying_type)
            if node.underlying_type
            else self.type_map["i32"]
        )
        if not self._is_integer(underlying_type):
            raise CodeGenError(
                f"Enum {node.name} underlying type must be an integer type, got {node.underlying_type}"
            )
        self.enum_underlying_types[node.name] = underlying_type

        next_value = 0
        self.enum_types[node.name] = {}
        for name, explicit_value in node.variants:
            if explicit_value is not None:
                next_value = explicit_value
            qualified_name = f"{node.name}.{name}"
            if name in self.enum_values or qualified_name in self.enum_values:
                raise CodeGenError(f"Duplicate enum variant: {name}")
            self.enum_values[name] = next_value
            self.enum_values[qualified_name] = next_value
            self.enum_value_types[name] = underlying_type
            self.enum_value_types[qualified_name] = underlying_type
            self.enum_types[node.name][name] = next_value
            next_value += 1

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
        self.pointer_pointees = {}

        for arg, (pname, ptype) in zip(func.args, node.params):
            alloc_type = self._get_llvm_type(ptype)
            alloca = self.builder.alloca(alloc_type, name=pname)
            self.builder.store(arg, alloca)
            self.symbol_table[pname] = alloca
            self.symbol_types[pname] = alloc_type
            if ptype.endswith("*") and ptype[:-1] in self.struct_types:
                self.pointer_pointees[pname] = self.struct_types[ptype[:-1]]

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
            value.type if value is not None else self.type_map["i32"]
        )
        alloca = self.builder.alloca(llvm_type, name=node.name)

        if value is not None:
            if isinstance(llvm_type, ir.ArrayType):
                # Array locals are declared with a placeholder scalar
                # initializer (e.g. "let buf: [512 x i8] = 0;") that just
                # reserves the stack space; it isn't coercible into the
                # array type itself. We deliberately do NOT emit a store of
                # a zeroinitializer constant here: for buffers this large,
                # LLVM's backend lowers a single big aggregate store into a
                # call to memset(), which fails to link in a freestanding
                # binary (no libc, custom _start, raw syscalls). Programs
                # using these scratch buffers are expected to fill them
                # explicitly before reading, as this source already does.
                pass
            else:
                self.builder.store(self._coerce(value, llvm_type), alloca)

        self.symbol_table[node.name] = alloca
        self.symbol_types[node.name] = llvm_type
        if node.type and node.type.endswith("*") and node.type[:-1] in self.struct_types:
            self.pointer_pointees[node.name] = self.struct_types[node.type[:-1]]
        elif isinstance(node.value, UnaryOpNode) and node.value.op == "&":
            _, pointee_type = self._address_of(node.value.operand)
            self.pointer_pointees[node.name] = pointee_type
        return alloca

    def visit_AsmOperandNode(self, node: AsmOperandNode) -> Tuple[str, ir.Value, Optional[str]]:
        """Evaluates operand expression and returns constraint, value and target variable name."""
        target_var = None
        # node.constraint is the raw lexer token and still carries its
        # surrounding quotes (e.g. '"=r"'), so the '=' prefix that marks an
        # output constraint must be checked on the decoded string — checking
        # the raw token here always fails (it starts with '"'), which used
        # to route every output operand through the input/load branch below.
        decoded_constraint = self._decode_string_literal(node.constraint)
        if isinstance(node.expression, IdentifierNode):
            target_var = node.expression.name
            if target_var not in self.symbol_table:
                raise CodeGenError(f"Undefined variable in asm operand: {target_var}")
            
            # If constraint indicates output ('='), pass alloca directly or evaluate
            if decoded_constraint.startswith("="):
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
            # Assignment through a pointer dereference: *(addr_expr) = value;
            # The language treats raw integer addresses as byte pointers, so
            # dereferenced stores/loads always operate on a single byte (i8).
            if isinstance(node.left, UnaryOpNode) and node.left.op == "*":
                address = self.generate(node.left.operand)
                pointer = self.builder.inttoptr(address, self.ptr_type)
                value = self._coerce(self.generate(node.right), ir.IntType(8))
                self.builder.store(value, pointer)
                return value

            if isinstance(node.left, MemberAccessNode):
                target_address, target_type = self._member_address(node.left)
                value = self._coerce(self.generate(node.right), target_type)
                self.builder.store(value, target_address)
                return value
            if not isinstance(node.left, IdentifierNode) or node.left.name not in self.symbol_table:
                raise CodeGenError("Assignment target must be a declared variable or struct member")
            target_type = self.symbol_types[node.left.name]
            value = self._coerce(self.generate(node.right), target_type)
            self.builder.store(value, self.symbol_table[node.left.name])
            return value

        left = self.generate(node.left)
        right = self.generate(node.right)

        # This language treats pointers as raw integer addresses everywhere
        # else (e.g. "let base_addr: long = some_ptr;"), so arithmetic and
        # comparisons involving a pointer operand implicitly decay it to its
        # i64 address, matching how the rest of the program already uses
        # pointer values in address arithmetic like "num_buf + written_len".
        if self._is_pointer(left.type):
            left = self.builder.ptrtoint(left, ir.IntType(64))
        if self._is_pointer(right.type):
            right = self.builder.ptrtoint(right, ir.IntType(64))

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

        if node.op == "*":
            # Pointer dereference. Addresses are plain integers in this
            # language, so we cast to a byte pointer and load a single byte.
            address = self.generate(node.operand)
            pointer = self.builder.inttoptr(address, self.ptr_type)
            return self.builder.load(pointer, typ=ir.IntType(8), name="deref")

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
            if node.name in self.enum_values:
                return ir.Constant(self.enum_value_types[node.name], self.enum_values[node.name])
            raise CodeGenError(f"Undefined variable: {node.name}")
        ptr = self.symbol_table[node.name]
        return self.builder.load(ptr, typ=self.symbol_types[node.name], name=node.name)

    def visit_FnCallNode(self, node: FnCallNode) -> ir.Instruction:
        if node.name in self.struct_types:
            field_types = self.struct_types[node.name].elements
            if len(field_types) != len(node.args):
                raise CodeGenError(
                    f"Struct {node.name} expects {len(field_types)} field value(s)"
                )
            value = ir.Constant(self.struct_types[node.name], None)
            for index, (argument, field_type) in enumerate(zip(node.args, field_types)):
                value = self.builder.insert_value(
                    value, self._coerce(self.generate(argument), field_type), index
                )
            return value
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

    def _member_address(self, node: MemberAccessNode) -> Tuple[ir.Value, ir.Type]:
        """Return a field address and type, retaining enough type information for opaque pointers."""
        if node.through_pointer:
            if isinstance(node.value, UnaryOpNode) and node.value.op == "&":
                base_address, base_type = self._address_of(node.value.operand)
            elif isinstance(node.value, IdentifierNode):
                if node.value.name not in self.pointer_pointees:
                    raise CodeGenError("The -> operator requires a typed struct pointer")
                pointer_slot = self.symbol_table[node.value.name]
                base_address = self.builder.load(
                    pointer_slot, typ=self.symbol_types[node.value.name], name="struct_ptr"
                )
                base_type = self.pointer_pointees[node.value.name]
            else:
                raise CodeGenError("The -> operator requires a typed struct pointer")
        else:
            base_address, base_type = self._address_of(node.value)

        if not isinstance(base_type, ir.IdentifiedStructType):
            raise CodeGenError(f"Member access requires a struct, got {base_type}")
        fields = self.struct_fields.get(base_type.name, {})
        if node.member not in fields:
            raise CodeGenError(f"Struct {base_type.name} has no member '{node.member}'")
        index, field_type = fields[node.member]
        zero = ir.Constant(ir.IntType(32), 0)
        gep_args = {
            "inbounds": True,
            "name": f"{node.member}_addr",
        }
        if getattr(base_address.type, "is_opaque", False):
            gep_args["source_etype"] = base_type
        address = self.builder.gep(
            base_address, [zero, ir.Constant(ir.IntType(32), index)], **gep_args
        )
        return address, field_type

    def _resolve_enum_member(self, enum_name: str, variant_name: str) -> ir.Constant:
        qualified_name = f"{enum_name}.{variant_name}"
        if qualified_name not in self.enum_values:
            raise CodeGenError(f"Enum {enum_name} has no variant '{variant_name}'")
        return ir.Constant(self.enum_value_types[qualified_name], self.enum_values[qualified_name])

    def _address_of(self, node) -> Tuple[ir.Value, ir.Type]:
        if isinstance(node, IdentifierNode):
            if node.name not in self.symbol_table:
                raise CodeGenError(f"Undefined variable: {node.name}")
            return self.symbol_table[node.name], self.symbol_types[node.name]
        if isinstance(node, MemberAccessNode):
            return self._member_address(node)
        raise CodeGenError("A struct member base must be a variable or another member")

    def visit_MemberAccessNode(self, node: MemberAccessNode) -> ir.Value:
        if isinstance(node.value, IdentifierNode):
            enum_name = node.value.name
            qualified_name = f"{enum_name}.{node.member}"
            if qualified_name in self.enum_values:
                return self._resolve_enum_member(enum_name, node.member)
        address, field_type = self._member_address(node)
        return self.builder.load(address, typ=field_type, name=node.member)

    def visit_ReturnNode(self, node: ReturnNode) -> ir.Instruction:
        return_type = self.current_fn.function_type.return_type
        if node.expression is None:
            if not isinstance(return_type, ir.VoidType):
                raise CodeGenError("Non-void function must return a value")
            return self.builder.ret_void()
        if isinstance(return_type, ir.VoidType):
            raise CodeGenError("Void function cannot return a value")
        return self.builder.ret(self._coerce(self.generate(node.expression), return_type))

    def visit_BreakNode(self, node: BreakNode) -> None:
        if not self.loop_break_stack:
            raise CodeGenError("break can only be used inside a loop")
        self.builder.branch(self.loop_break_stack[-1])

    def visit_ContinueNode(self, node: ContinueNode) -> None:
        if not self.loop_continue_stack:
            raise CodeGenError("continue can only be used inside a loop")
        self.builder.branch(self.loop_continue_stack[-1])

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
        # Check the block the builder currently sits in, not then_bb itself:
        # nested control flow (e.g. another if/while inside this branch)
        # moves the builder to a different block by the time we get here.
        if not self.builder.block.is_terminated:
            self.builder.branch(merge_bb)

        if else_bb:
            self.builder.position_at_start(else_bb)
            self.generate(node.else_branch)
            # Same reasoning: an "else if" chain leaves the builder inside
            # the innermost nested if's own merge block, not else_bb.
            if not self.builder.block.is_terminated:
                self.builder.branch(merge_bb)

        self.builder.position_at_start(merge_bb)

    def visit_WhileNode(self, node: WhileNode) -> None:
        cond_bb = self.current_fn.append_basic_block("while.cond")
        body_bb = self.current_fn.append_basic_block("while.body")
        end_bb = self.current_fn.append_basic_block("while.end")

        self.loop_break_stack.append(end_bb)
        self.loop_continue_stack.append(cond_bb)
        try:
            self.builder.branch(cond_bb)
            self.builder.position_at_start(cond_bb)

            cond_val = self._as_bool(self.generate(node.condition))
            self.builder.cbranch(cond_val, body_bb, end_bb)

            self.builder.position_at_start(body_bb)
            self.generate(node.body)
            if not self.builder.block.is_terminated:
                self.builder.branch(cond_bb)

            self.builder.position_at_start(end_bb)
        finally:
            self.loop_break_stack.pop()
            self.loop_continue_stack.pop()

    def visit_ForNode(self, node: ForNode) -> None:
        cond_bb = self.current_fn.append_basic_block("for.cond")
        body_bb = self.current_fn.append_basic_block("for.body")
        update_bb = self.current_fn.append_basic_block("for.update")
        end_bb = self.current_fn.append_basic_block("for.end")

        if node.init is not None:
            self.generate(node.init)

        self.builder.branch(cond_bb)
        self.builder.position_at_start(cond_bb)

        cond_val = self._as_bool(self.generate(node.condition)) if node.condition is not None else ir.Constant(ir.IntType(1), 1)
        self.builder.cbranch(cond_val, body_bb, end_bb)

        self.builder.position_at_start(body_bb)
        self.loop_break_stack.append(end_bb)
        self.loop_continue_stack.append(update_bb)
        try:
            self.generate(node.body)
            if not self.builder.block.is_terminated:
                self.builder.branch(update_bb)
        finally:
            self.loop_break_stack.pop()
            self.loop_continue_stack.pop()

        self.builder.position_at_start(update_bb)
        if node.update is not None:
            self.generate(node.update)
        if not self.builder.block.is_terminated:
            self.builder.branch(cond_bb)

        self.builder.position_at_start(end_bb)