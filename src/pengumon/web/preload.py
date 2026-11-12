"""The page's module graph: which JavaScript files the dashboard loads at start, found by reading their `import`s.

The browser finds ES modules one import level at a time (a file is fetched, parsed, and only then are its imports
known), which over HTTP/1.1 makes the cold load a long chain of round trips. The page lists every module of the start
as <link rel="modulepreload"> so they are fetched together (templates/index.html). Only static imports with a relative
specifier are followed: a dynamic import() is not (a lazy dialog stays lazy), and the files are served as they are, so
there is nothing else to resolve.
"""

import posixpath
import re
from pathlib import Path
from typing import List, Sequence, Tuple

# `import a, { b } from './x.js'`, `export * from './x.js'`, `export { a } from './x.js'` (the names may span lines) and the
# side-effect form `import './x.js'`. `import(` and `export const a = 'text'` are not matched: after the keyword comes
# `from '...'` or a bare string, nothing else.
_IMPORT = re.compile(r"""(?:^|[;{}\n])\s*(?:import|export)\b(?:[^'";`]*?\bfrom)?\s*(['"])([^'"\n]+)\1""")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.MULTILINE)


def static_imports(source: str) -> List[str]:
    """The specifiers of the static imports and re-exports of a module's source, in order."""
    source = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))
    return [m.group(2) for m in _IMPORT.finditer(source)]


def module_closure(static: Path, entries: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(the entry modules and everything they import, the imports that name no shipped file), both as paths relative to
    `static` in the order they are met (an entry, then what it imports, breadth first). Only relative specifiers count."""
    seen: List[str] = []
    missing: List[str] = []
    queue = list(entries)
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        if not (static / path).is_file():
            missing.append(path)
            continue
        seen.append(path)
        source = (static / path).read_text(encoding="utf-8")
        for spec in static_imports(source):
            if spec.startswith("."):
                queue.append(posixpath.normpath(posixpath.join(posixpath.dirname(path), spec)))
    return seen, missing
