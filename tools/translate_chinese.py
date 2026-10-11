#!/usr/bin/env python3
"""Translate Chinese text in source comments, string literals, XML, and Markdown.

This tool edits only textual regions (comments, quoted strings, XML text/attributes,
and Markdown lines). It does not rename identifiers or rewrite binary assets.
Translation is performed using Google Translate through deep-translator.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
SUPPORTED_SUFFIXES = {
    ".kt", ".java", ".kts", ".gradle", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".xml", ".md", ".sh", ".py", ".properties", ".txt", ".yml", ".yaml",
    ".json", ".pro", ".cmake", ".toml", ".bat", ".ps1", ".html", ".js", ".ts",
}
EXCLUDED_DIRS = {
    ".git", ".gradle", ".idea", "build", ".cxx", ".externalNativeBuild",
    "node_modules", "vendor", "__pycache__",
}
EXCLUDED_FILES = {
    "translate_chinese.py", "gradle-wrapper.jar", "local.properties",
}

# Protect runtime placeholders, format strings, escapes and URLs from the translator.
PROTECTED_RE = re.compile(
    r"\$\{[^{}]*\}"                 # Kotlin/Gradle interpolation: ${value}
    r"|\$[A-Za-z_][A-Za-z0-9_]*"     # Kotlin/Gradle interpolation: $value
    r"|%[-+#0-9 .]*[a-zA-Z]"          # printf-style formats: %02X, %s, %.2f
    r"|\\(?:[\\\"'nrtbf]|u[0-9a-fA-F]{4})"  # escaped source characters
    r"|https?://[^\s)\]>]+"         # URLs, including Markdown links
)

@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str = "text"       # text, double, single, or markdown


def _has_han(text: str) -> bool:
    return bool(HAN_RE.search(text))


def _xml_spans(content: str) -> list[Span]:
    spans: list[Span] = []
    token_re = re.compile(r"<!--[\s\S]*?-->|<[^>]*>|[^<]+", re.S)
    for token in token_re.finditer(content):
        value = token.group(0)
        base = token.start()
        if value.startswith("<!--") and value.endswith("-->"):
            inner_start, inner_end = base + 4, token.end() - 3
            if _has_han(content[inner_start:inner_end]):
                spans.append(Span(inner_start, inner_end, "text"))
        elif value.startswith("<"):
            # Translate only attribute values that contain Chinese; keep quote marks/tags.
            for q in re.finditer(r'''(["'])(.*?)\1''', value, re.S):
                a, b = base + q.start(2), base + q.end(2)
                if _has_han(content[a:b]):
                    spans.append(Span(a, b, "double" if q.group(1) == '"' else "single"))
        else:
            # XML text node, e.g. <string name="open_file">打开文件</string>
            a, b = token.start(), token.end()
            if _has_han(content[a:b]):
                spans.append(Span(a, b, "text"))
    return spans


def _markdown_spans(content: str) -> list[Span]:
    # Translate each Chinese-containing line, including Chinese text in tables and
    # diagrams. URLs and code placeholders are protected before sending to translation.
    spans: list[Span] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        body_end = len(line.rstrip("\r\n"))
        body = line[:body_end]
        if _has_han(body):
            spans.append(Span(offset, offset + body_end, "markdown"))
        offset += len(line)
    return spans


def _code_spans(content: str, suffix: str) -> list[Span]:
    """Extract comment bodies and string literal contents without touching syntax."""
    spans: list[Span] = []
    n = len(content)
    i = 0
    hash_comments = suffix in {".sh", ".py", ".properties", ".pro", ".txt", ".cmake", ".toml", ".yml", ".yaml", ".ps1", ".bat"}
    single_quoted_strings = suffix in {".sh", ".py", ".yml", ".yaml", ".properties", ".ps1", ".bat"}
    c_style = suffix in {".kt", ".java", ".kts", ".gradle", ".cpp", ".cc", ".c", ".h", ".hpp", ".js", ".ts", ".html"}
    kotlin = suffix in {".kt", ".kts"}

    def add_span(a: int, b: int, kind: str = "text") -> None:
        if a <= b and _has_han(content[a:b]):
            spans.append(Span(a, b, kind))

    def skip_quoted(pos: int, quote: str, triple: bool = False) -> int:
        """Return the index after a quoted literal while balancing Kotlin expressions."""
        delim = '"""' if triple else quote
        j = pos + len(delim)
        while j < n:
            if content[j] == "\\" and not triple:
                j += 2
            elif content.startswith(delim, j):
                return j + len(delim)
            else:
                j += 1
        return n

    def interpolation_end(expr_start: int) -> int:
        """Find the matching } in a Kotlin ${...}, skipping nested strings/comments."""
        depth = 1
        j = expr_start
        while j < n:
            if content.startswith("//", j):
                nl = content.find("\n", j + 2)
                j = n if nl < 0 else nl + 1
                continue
            if content.startswith("/*", j):
                close = content.find("*/", j + 2)
                j = n if close < 0 else close + 2
                continue
            if content.startswith('"""', j):
                j = skip_quoted(j, '"', triple=True)
                continue
            if content[j] == '"':
                j = skip_quoted(j, '"')
                continue
            if content[j] == "'":
                j = skip_quoted(j, "'")
                continue
            if content[j] == "{":
                depth += 1
            elif content[j] == "}":
                depth -= 1
                if depth == 0:
                    return j
            j += 1
        return n

    def add_raw_span(a: int, b: int) -> None:
        """Translate human text inside Kotlin raw strings without damaging fixtures.

        JSON raw strings are split into the contents of their quoted values, while
        OBJ fixtures only translate comment lines. Unknown raw strings fall back to
        a single text span. This keeps JSON punctuation and mesh data untouched.
        """
        chunk = content[a:b]
        if not _has_han(chunk):
            return
        stripped = chunk.lstrip()
        leading = len(chunk) - len(stripped)
        if stripped.startswith(("{", "[")):
            json_string = re.compile(r'"((?:\\.|[^"\\])*)"', re.S)
            found = False
            for match in json_string.finditer(chunk):
                inner_start = a + match.start(1)
                inner_end = a + match.end(1)
                if _has_han(content[inner_start:inner_end]):
                    add_span(inner_start, inner_end, "raw")
                    found = True
            if found:
                return
        # OBJ test fixtures can contain comments, vertices, UVs, faces and group
        # declarations. Translate comments only so numerical geometry stays exact.
        if re.search(r"(?m)^\s*(?:#|v\s|vt\s|vn\s|f\s|g\s|o\s)", chunk):
            offset = a
            for line in chunk.splitlines(keepends=True):
                body_end = len(line.rstrip("\r\n"))
                body = line[:body_end]
                stripped_line = body.lstrip()
                if _has_han(body) and stripped_line.startswith("#"):
                    hash_pos = body.find("#", len(body) - len(stripped_line))
                    add_span(offset + hash_pos + 1, offset + body_end, "raw")
                elif _has_han(body) and not re.match(r"^\s*(?:v|vt|vn|f|g|o)\s", body):
                    # Handle human prose in an otherwise line-based fixture, but
                    # never rewrite vertex/face/group syntax.
                    add_span(offset, offset + body_end, "raw")
                offset += len(line)
            return
        add_span(a, b, "raw")

    def scan_kotlin_string(start: int, triple: bool = False) -> int:
        """Translate Kotlin text chunks and recursively scan strings inside ${...}."""
        delim = '"""' if triple else '"'
        literal_kind = "raw" if triple else "double"
        j = start + len(delim)
        segment_start = j
        while j < n:
            if not triple and content[j] == "\\":
                j += 2
                continue
            if content.startswith(delim, j):
                if triple:
                    add_raw_span(segment_start, j)
                else:
                    add_span(segment_start, j, literal_kind)
                return j + len(delim)
            if content.startswith("${", j):
                if triple:
                    add_raw_span(segment_start, j)
                else:
                    add_span(segment_start, j, literal_kind)
                expr_start = j + 2
                brace_close = interpolation_end(expr_start)
                # The expression may contain its own quoted strings; scan those for
                # Chinese without translating Kotlin operators or variable names.
                nested_text = content[expr_start:brace_close]
                for nested in _code_spans(nested_text, suffix):
                    spans.append(Span(expr_start + nested.start, expr_start + nested.end, nested.kind))
                j = min(brace_close + 1, n)
                segment_start = j
                continue
            j += 1
        if triple:
            add_raw_span(segment_start, n)
        else:
            add_span(segment_start, n, literal_kind)
        return n

    while i < n:
        if c_style and content.startswith("//", i):
            end = content.find("\n", i)
            if end < 0:
                end = n
            add_span(i + 2, end)
            i = end
            continue
        if c_style and content.startswith("/*", i):
            close = content.find("*/", i + 2)
            end = n if close < 0 else close
            add_span(i + 2, end)
            i = n if close < 0 else close + 2
            continue

        ch = content[i]
        if hash_comments and ch == "#":
            line_start = content.rfind("\n", 0, i) + 1
            if not content[line_start:i].strip():
                end = content.find("\n", i)
                if end < 0:
                    end = n
                add_span(i + 1, end)
                i = end
                continue

        if ch == "`" and kotlin:
            # Kotlin permits backtick-quoted test/function identifiers; translate these
            # too so CJK test names do not remain in the source tree.
            close = content.find("`", i + 1)
            end = n if close < 0 else close
            add_span(i + 1, end, "backtick")
            i = n if close < 0 else close + 1
            continue
        if ch == '"':
            if kotlin and content.startswith('"""', i):
                i = scan_kotlin_string(i, triple=True)
                continue
            if kotlin:
                i = scan_kotlin_string(i, triple=False)
                continue
            j = i + 1
            while j < n:
                if content[j] == "\\":
                    j += 2
                elif content[j] == '"':
                    break
                else:
                    j += 1
            end = min(j, n)
            add_span(i + 1, end, "double")
            i = min(j + 1, n)
            continue
        if ch == "'" and c_style:
            # Kotlin/Java/C/C++ use single-quoted character literals. Skip them so a
            # character like '"' cannot make the scanner mistake it for a string start.
            j = i + 1
            while j < n:
                if content[j] == "\\":
                    j += 2
                elif content[j] == "'":
                    break
                else:
                    j += 1
            i = min(j + 1, n)
            continue
        if ch == "'" and single_quoted_strings:
            j = i + 1
            while j < n:
                if content[j] == "\\":
                    j += 2
                elif content[j] == "'":
                    break
                else:
                    j += 1
            end = min(j, n)
            add_span(i + 1, end, "single")
            i = min(j + 1, n)
            continue
        i += 1
    return spans

def extract_spans(content: str, suffix: str) -> list[Span]:
    if suffix.lower() == ".md":
        return _markdown_spans(content)
    if suffix.lower() == ".xml":
        return _xml_spans(content)
    return _code_spans(content, suffix.lower())


def _protect(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    idx = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal idx
        token = f"ZXQPH{idx:05d}ZXQ"
        mapping[token] = match.group(0)
        idx += 1
        return token

    return PROTECTED_RE.sub(replace, text), mapping


def _restore(text: str, mapping: dict[str, str]) -> str:
    for token, original in mapping.items():
        if token not in text:
            raise ValueError(f"Translation service changed a protected placeholder: {token}")
        text = text.replace(token, original)
    return text


def _escape_literal(text: str, quote: str) -> str:
    """Escape quote characters newly introduced into a source string literal."""
    out: list[str] = []
    backslashes = 0
    for ch in text:
        if ch == quote and backslashes % 2 == 0:
            out.append("\\")
        out.append(ch)
        if ch == "\\":
            backslashes += 1
        else:
            backslashes = 0
    return "".join(out)


def _translate_batch(items: list[str], translator) -> list[str]:
    """Translate many fragments in one request, falling back to per-fragment calls."""
    results: list[str | None] = [None] * len(items)
    start_token = [f"ZXQSEG{i:05d}ZXQ" for i in range(len(items))]
    end_token = [f"ZXQEND{i:05d}ZXQ" for i in range(len(items))]
    protected: list[str] = []
    maps: list[dict[str, str]] = []
    for item in items:
        body, edge = _strip_edges(item)
        body_masked, mapping = _protect(body)
        # Keep edge whitespace outside the translated region.
        maps.append({**mapping, "__LEADING__": edge[0], "__TRAILING__": edge[1]})
        protected.append(body_masked)

    # Google Translate's public endpoint has a request-size limit. Work in chunks.
    pos = 0
    while pos < len(items):
        end = pos
        size = 0
        while end < len(items):
            estimate = len(protected[end]) + 36
            if end > pos and size + estimate > 3200:
                break
            if estimate > 4500:
                # An individual fragment this large is translated separately below.
                break
            size += estimate
            end += 1
        if end == pos:
            end = pos + 1
            pos += 1
            results[end - 1] = _translate_one(protected[end - 1], translator)
            continue

        payload = "\n".join(
            f"{start_token[j]}{protected[j]}{end_token[j]}" for j in range(pos, end)
        )
        translated = None
        for attempt in range(3):
            try:
                translated = translator.translate(payload)
                if translated:
                    break
            except Exception:
                if attempt == 2:
                    translated = None
                else:
                    time.sleep(1.5 * (attempt + 1))
        if translated:
            found_all = True
            for j in range(pos, end):
                a = translated.find(start_token[j])
                b = translated.find(end_token[j], a + len(start_token[j])) if a >= 0 else -1
                if a < 0 or b < 0:
                    found_all = False
                    break
                results[j] = translated[a + len(start_token[j]):b].strip()
            if not found_all:
                for j in range(pos, end):
                    results[j] = None
        pos = end

    for i, value in enumerate(results):
        if value is None:
            value = _translate_one(protected[i], translator)
        mapping = maps[i]
        body = _restore(value, {k: v for k, v in mapping.items() if not k.startswith("__")})
        results[i] = mapping["__LEADING__"] + body + mapping["__TRAILING__"]
    return [str(x) for x in results]


def _strip_edges(text: str) -> tuple[str, tuple[str, str]]:
    left = len(text) - len(text.lstrip())
    right = len(text.rstrip())
    return text[left:right], (text[:left], text[right:])


def _translate_one(text: str, translator) -> str:
    if not text:
        return text
    last_error = None
    for attempt in range(3):
        try:
            result = translator.translate(text)
            if result:
                return result.strip()
            last_error = RuntimeError("The translation service returned an empty result")
        except Exception as exc:  # network rate-limits are retried, then surfaced clearly
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Google Translate failed: {last_error}") from last_error


def iter_candidate_files() -> Iterable[Path]:
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.name in EXCLUDED_FILES or rel.parts[0] == ".git":
            continue
        if path.suffix.lower() in SUPPORTED_SUFFIXES or path.name == "CMakeLists.txt":
            yield path


def read_utf8(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def translate_repository(write: bool) -> tuple[int, int]:
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency. Install it with: python -m pip install deep-translator==1.11.4"
        ) from exc

    translator = GoogleTranslator(source="zh-CN", target="en")
    edits = 0
    translated_regions = 0
    for path in sorted(iter_candidate_files()):
        content = read_utf8(path)
        if content is None:
            continue
        spans = extract_spans(content, path.suffix.lower() or ".txt")
        spans = [span for span in spans if _has_han(content[span.start:span.end])]
        if not spans:
            continue

        fragments = [content[span.start:span.end] for span in spans]
        replacements = _translate_batch(fragments, translator)
        new_content = content
        # Apply replacements backwards so offsets remain valid.
        for span, replacement in reversed(list(zip(spans, replacements))):
            # XML needs entity escaping instead of programming-language quote escaping.
            if path.suffix.lower() == ".xml":
                import html
                replacement = html.escape(html.unescape(replacement), quote=True)
            elif span.kind in {"double", "single"}:
                quote = '"' if span.kind == "double" else "'"
                replacement = _escape_literal(replacement, quote)
            elif span.kind == "backtick":
                # Kotlin backtick identifiers cannot contain several punctuation
                # characters. Convert those to hyphens while keeping the name legal.
                replacement = re.sub(r"[.;/\\\[\]<>:`]", "-", replacement)
                replacement = re.sub(r"-+", "-", replacement).strip("- ") or "translated_name"
            new_content = new_content[:span.start] + replacement + new_content[span.end:]

        if new_content != content:
            translated_regions += len(spans)
            edits += 1
            if write:
                path.write_text(new_content, encoding="utf-8", newline="")
            print(f"{'Translated' if write else 'Would translate'}: {path.relative_to(ROOT)} ({len(spans)} regions)")

    return edits, translated_regions


def check_repository() -> int:
    leftovers: list[tuple[Path, int, str]] = []
    for path in sorted(iter_candidate_files()):
        content = read_utf8(path)
        if content is None:
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if _has_han(line):
                excerpt = line.strip().replace("\t", " ")
                leftovers.append((path.relative_to(ROOT), line_number, excerpt[:180]))
    if leftovers:
        print(f"Found {len(leftovers)} line(s) that still contain Chinese:", file=sys.stderr)
        for path, line_number, excerpt in leftovers[:100]:
            print(f"  {path}:{line_number}: {excerpt}", file=sys.stderr)
        if len(leftovers) > 100:
            print(f"  ... and {len(leftovers) - 100} more", file=sys.stderr)
        return 1
    print("Chinese-text check passed: no CJK characters remain in supported text files.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="translate and update files")
    group.add_argument("--check", action="store_true", help="fail if Chinese text remains")
    group.add_argument("--dry-run", action="store_true", help="list files that contain translatable Chinese")
    args = parser.parse_args()

    if args.check:
        return check_repository()
    if args.dry_run:
        count_files = count_regions = 0
        for path in sorted(iter_candidate_files()):
            content = read_utf8(path)
            if content is None:
                continue
            spans = [s for s in extract_spans(content, path.suffix.lower() or ".txt") if _has_han(content[s.start:s.end])]
            if spans:
                count_files += 1
                count_regions += len(spans)
                print(f"{path.relative_to(ROOT)}: {len(spans)} regions")
        print(f"Found {count_regions} translatable region(s) in {count_files} file(s).")
        return 0

    try:
        edits, regions = translate_repository(write=True)
    except Exception as exc:
        print(f"Translation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Translated {regions} region(s) across {edits} file(s).")
    return check_repository()


if __name__ == "__main__":
    raise SystemExit(main())
