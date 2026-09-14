import os
import subprocess

def execute_command(command: str) -> int:
    """Executes a shell command and returns its exit status code."""
    return subprocess.run(command, shell=True).returncode


def run_build_pipeline(ir_filepath: str, executable_filepath: str) -> None:
    """Compiles LLVM IR (.ll) to an executable using Clang."""
    clang_cmd = f"clang -O2 {ir_filepath} -o {executable_filepath} -nostdlib"

    if subprocess.run(clang_cmd, shell=True).returncode != 0:
        raise RuntimeError(f"Clang compilation failed for target '{ir_filepath}'")