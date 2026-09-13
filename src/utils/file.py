import os
import re


INCLUDE_PATTERN = re.compile(r"^\s*include\s+<([^>]+)>\s*$")

def write_file(filepath: str, content: str) -> None:
    """Writes content to a file, creating parent directories if necessary."""
    output_dir = os.path.dirname(filepath)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def read_file(filepath: str) -> str:
    """Reads a source file after prepending the files listed in include.bon."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Source file not found: {filepath}")

    source_dir = os.path.dirname(os.path.abspath(filepath))
    include_file = os.path.join(source_dir, "include.bon")
    included_sources = []

    if os.path.exists(include_file):
        with open(include_file, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                match = INCLUDE_PATTERN.match(line)
                if not match:
                    if line.strip():
                        raise ValueError(
                            f"Invalid include directive in {include_file} "
                            f"at line {line_number}"
                        )
                    continue

                include_path = os.path.join(source_dir, match.group(1))
                included_sources.append(_read_source_file(include_path))
                print(f"Included '{include_path}' from '{include_file}'")

    included_sources.append(_read_source_file(filepath))
    return "\n".join(included_sources)


def _read_source_file(filepath: str) -> str:
    """Reads one source file and reports a useful path when it is missing."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Source file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_base_name(filepath: str) -> str:
    """Strips the file extension from the path, returning the base path."""
    base_name, _ = os.path.splitext(filepath)
    return base_name


def remove_file(filepath: str) -> None:
    """Removes a file from the system if it exists."""
    if os.path.exists(filepath):
        os.remove(filepath)