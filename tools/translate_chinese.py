#!/usr/bin/env python3
"""Translate Chinese text in source comments, strings, XML, and Markdown.

Only selected human-readable text regions are edited; identifiers, code syntax,
URLs, format placeholders, binary assets, and 3D mesh data are preserved. Translation
uses the local Argos Translate zh -> en model, so each string is translated offline
without making one web request per string.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "tools" / ".translation-cache.json"
_TRANSLATION_CACHE: dict[str, str] = {}
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
    "translate_chinese.py", "gradle-wrapper.jar", "local.properties", ".translation-cache.json",
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


def _cache_key(text: str) -> str:
    return hashlib.sha256(("zh-CN\0en\0" + text).encode("utf-8")).hexdigest()


def _load_translation_cache() -> None:
    global _TRANSLATION_CACHE
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _TRANSLATION_CACHE = {
                str(key): str(value)
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        else:
            _TRANSLATION_CACHE = {}
    except (OSError, json.JSONDecodeError):
        _TRANSLATION_CACHE = {}
    # Create the cache file immediately so GitHub Actions can persist partial progress.
    _save_translation_cache()


def _save_translation_cache() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(_TRANSLATION_CACHE, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp_path.replace(CACHE_PATH)


def _request_translate(text: str, translator) -> str:
    """Translate locally with Argos; no rate-limited external API requests."""
    if not text:
        return text
    try:
        result = translator.translate(text)
    except Exception as exc:
        raise RuntimeError(f"Local Argos translation failed: {exc}") from exc
    if result is None or not str(result).strip():
        raise RuntimeError("Local Argos translation returned an empty result")
    return str(result)


def _load_argos_translator():
    """Return the installed Chinese-to-English translation, installing its model if needed."""
    try:
        import argostranslate.package as argos_package
        import argostranslate.translate as argos_translate
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency. Install with: python -m pip install -r tools/requirements-translate.txt"
        ) from exc

    def find_translation():
        languages = argos_translate.get_installed_languages()
        source = next((language for language in languages if language.code == "zh"), None)
        target = next((language for language in languages if language.code == "en"), None)
        if source is None or target is None:
            return None
        return source.get_translation(target)

    translation = find_translation()
    if translation is not None:
        print("Using cached Argos Chinese → English model.")
        return translation

    print("Installing the Argos Chinese → English language model (first run only)...")
    try:
        argos_package.update_package_index()
        package = next(
            (item for item in argos_package.get_available_packages()
             if item.from_code == "zh" and item.to_code == "en"),
            None,
        )
        if package is None:
            raise RuntimeError("Argos package index has no Chinese (zh) → English (en) model")
        model_path = package.download()
        argos_package.install_from_path(model_path)
    except Exception as exc:
        raise RuntimeError(f"Could not download/install the Argos zh→en model: {exc}") from exc

    translation = find_translation()
    if translation is None:
        raise RuntimeError("Argos zh→en model installed, but the translation was not registered")
    return translation

def _translate_plain_run(text: str, translator) -> str:
    """Translate a plain-text run while preserving its exact surrounding whitespace."""
    if not _has_han(text):
        return text
    left = len(text) - len(text.lstrip())
    right = len(text.rstrip())
    if right <= left:
        return text
    leading, body, trailing = text[:left], text[left:right], text[right:]
    translated = _request_translate(body, translator).strip()
    return leading + translated + trailing


def _translate_preserving_protected(text: str, translator) -> str:
    """Translate only unprotected text runs; never send code tokens to the model.

    Argos can alter artificial markers such as ZXQPH00000ZXQ. Splitting around the
    original placeholders is safer: the protected parts never enter the translator,
    so interpolation expressions, format specifiers, escapes and URLs are restored
    byte-for-byte without relying on the model to preserve a marker.
    """
    pieces: list[str] = []
    cursor = 0
    for match in PROTECTED_RE.finditer(text):
        pieces.append(_translate_plain_run(text[cursor:match.start()], translator))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(_translate_plain_run(text[cursor:], translator))
    return "".join(pieces)


def _translate_batch_uncached(items: list[str], translator) -> list[str]:
    """Translate uncached fragments and persist progress in small batches.

    This intentionally translates each plain-text fragment independently instead
    of concatenating fragments with synthetic boundary tokens. The local model may
    normalize or drop such tokens, which can corrupt source placeholders or make the
    entire run fail. Saving every ten completed fragments preserves useful progress
    without rewriting the cache file for every single fragment.
    """
    results: list[str] = []
    for index, item in enumerate(items):
        try:
            body, (leading, trailing) = _strip_edges(item)
            translated_body = _translate_preserving_protected(body, translator)
            final_value = leading + translated_body + trailing
            results.append(final_value)
            _TRANSLATION_CACHE[_cache_key(item)] = final_value
        except Exception:
            # Flush completed translations before propagating an error.
            _save_translation_cache()
            raise

        if (index + 1) % 10 == 0 or index + 1 == len(items):
            _save_translation_cache()

    return results


def _translate_batch(items: list[str], translator) -> list[str]:
    """Reuse cached translations and translate only fragments not seen in earlier runs."""
    results: list[str | None] = [None] * len(items)
    pending_items: list[str] = []
    pending_indices: list[int] = []
    for index, item in enumerate(items):
        cached = _TRANSLATION_CACHE.get(_cache_key(item))
        if cached is not None:
            results[index] = cached
        else:
            pending_indices.append(index)
            pending_items.append(item)

    if pending_items:
        translated = _translate_batch_uncached(pending_items, translator)
        for index, value in zip(pending_indices, translated):
            results[index] = value
            _TRANSLATION_CACHE[_cache_key(items[index])] = value
        _save_translation_cache()

    return [str(value) for value in results]

def _strip_edges(text: str) -> tuple[str, tuple[str, str]]:
    left = len(text) - len(text.lstrip())
    right = len(text.rstrip())
    return text[left:right], (text[:left], text[right:])


def _translate_one(text: str, translator) -> str:
    if not text:
        return text
    return _request_translate(text, translator).strip()

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
    _load_translation_cache()
    print(f"Loaded {len(_TRANSLATION_CACHE)} cached translation(s).")
    translator = _load_argos_translator()
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
