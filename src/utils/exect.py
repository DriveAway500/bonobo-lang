import os


def execute_command(command: str) -> int:
    """Executes a shell command and returns its exit status code."""
    return os.system(command)


def run_build_pipeline(asm_filepath: str, executable_filepath: str) -> None:
    """Assembles with NASM and links with LD using os.system."""
    obj_filepath = f"{executable_filepath}.o"

    # 1. Assemble with NASM
    nasm_cmd = f"nasm -f elf64 {asm_filepath} -o {obj_filepath}"
    if os.system(nasm_cmd) != 0:
        raise RuntimeError(f"NASM assembly failed for target '{asm_filepath}'")

    # 2. Link with LD
    ld_cmd = f"ld {obj_filepath} -o {executable_filepath}"
    if os.system(ld_cmd) != 0:
        raise RuntimeError(f"LD linking failed for object '{obj_filepath}'")

    # Cleanup object file
    if os.path.exists(obj_filepath):
        os.remove(obj_filepath)