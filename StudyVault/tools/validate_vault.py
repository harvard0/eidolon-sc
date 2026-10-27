"""Validate this StudyVault without importing the application or using the network.

Run from the project root: python StudyVault/tools/validate_vault.py
Only validation-report.json is written. All other inputs are read-only.
"""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import ast
import json
import re
import subprocess
import sys


VAULT = Path(__file__).resolve().parent.parent
ROOT = VAULT.parent
TOOLS = VAULT / "tools"
ERRORS: list[str] = []
WARNINGS: list[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        ERRORS.append(message)


def inside_project(path: str, *, is_file: bool = True) -> bool:
    candidate = (ROOT / path).resolve()
    return candidate.is_relative_to(ROOT) and (candidate.is_file() if is_file else candidate.exists())


def frontmatter(text: str, label: str) -> tuple[dict, str]:
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if match is None:
        ERRORS.append(f"{label}: missing frontmatter")
        return {}, text
    fields = {}
    for line in match[1].splitlines():
        key, separator, value = line.partition(": ")
        if not separator:
            ERRORS.append(f"{label}: invalid metadata line {line!r}")
            continue
        require(key not in fields, f"{label}: duplicate metadata key {key}")
        try:
            # Authoring uses JSON scalars and arrays, a valid YAML subset.
            fields[key] = json.loads(value)
        except json.JSONDecodeError:
            ERRORS.append(f"{label}: invalid metadata value for {key}")
    return fields, match[2]


def static_routes() -> list[dict]:
    routes = []
    for source in sorted((ROOT / "backend/src/eidolon").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                    continue
                method = decorator.func.attr
                if method not in {"get", "post", "patch", "put", "delete"} or not decorator.args:
                    continue
                arg = decorator.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/api/"):
                    routes.append({"method": method.upper(), "path": arg.value,
                                   "file": source.relative_to(ROOT).as_posix(),
                                   "line": decorator.lineno, "handler": node.name})
    return sorted(routes, key=lambda r: (r["file"], r["line"]))


def main() -> int:
    registry = json.loads((TOOLS / "tag-registry.json").read_text(encoding="utf-8"))
    files = sorted(VAULT.rglob("*.md"))
    names = Counter(file.stem for file in files)
    require(all(count == 1 for count in names.values()), "note names must be unique")
    for tag in registry:
        require(bool(re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", tag)), f"invalid registered tag: {tag}")

    notes = {}
    sources = set()
    referenced_tests = set()
    wiki_links = 0
    for file in files:
        label = file.relative_to(VAULT).as_posix()
        text = file.read_text(encoding="utf-8")
        fields, body = frontmatter(text, label)
        notes[file.stem] = (file, fields, body)
        for key in ("module", "path", "keywords", "tags", "source_commit", "source_files"):
            require(bool(fields.get(key)), f"{label}: missing {key}")
        require(isinstance(fields.get("keywords"), list), f"{label}: keywords must be a list")
        for keyword in fields.get("keywords", []):
            require(bool(re.fullmatch(r"[a-z0-9-]+", keyword)), f"{label}: keyword must be English: {keyword}")
        require(inside_project(fields.get("path", ""), is_file=False), f"{label}: path missing/outside project")
        require(bool(re.fullmatch(r"[0-9a-f]{40}", fields.get("source_commit", ""))), f"{label}: invalid baseline")
        for tag in fields.get("tags", []):
            require(tag in registry, f"{label}: unregistered tag {tag}")
        for source in fields.get("source_files", []):
            sources.add(source)
            require(inside_project(source), f"{label}: missing/outside source: {source}")
        require(body.startswith("\n# " + file.stem + "\n") or body.startswith("# " + file.stem + "\n"), f"{label}: H1 differs from filename")
        require("## Related Notes" in body or "## Related Modules" in body, f"{label}: no related notes/modules")
        require(len(re.findall(r"^```", body, re.M)) % 2 == 0, f"{label}: unbalanced code fences")
        require(not re.search(r"\b[A-Za-z]:[\\/]|file://|/Users/|/home/", text), f"{label}: absolute local path found")
        require("\ufffd" not in text, f"{label}: replacement character found")
        require("STRUCTURAL_RESULT_PENDING" not in text, f"{label}: unfinished validation placeholder")
        for target in re.findall(r"\[\[([^\]]+)\]\]", body):
            wiki_links += 1
            destination = target.split("|", 1)[0].split("#", 1)[0]
            require(not destination or destination in names, f"{label}: unresolved wiki-link {target}")
        for test in re.findall(r"(?:backend/)?tests/test_[a-z0-9_]+\.py", body):
            project_test = test if test.startswith("backend/") else "backend/" + test
            referenced_tests.add(project_test)
            require(inside_project(project_test), f"{label}: nonexistent pytest file: {project_test}")
        for test in re.findall(r"(?:frontend/)?src/[a-zA-Z0-9_./-]+\.test\.tsx?", body):
            project_test = test if test.startswith("frontend/") else "frontend/" + test
            referenced_tests.add(project_test)
            require(inside_project(project_test), f"{label}: nonexistent Vitest file: {project_test}")

    moc = notes.get("学习地图", (None, {}, ""))[2]
    for title in names:
        if title != "学习地图":
            require("[["+title+"]]" in moc, f"MOC does not directly link to {title}")
    for heading in ("Architecture Overview", "Module Map", "API Surface", "Getting Started", "Tag Index", "Onboarding Path"):
        require(heading in moc, f"MOC missing {heading}")
    for tag in registry:
        require("`#"+tag+"`" in moc, f"MOC missing registered tag {tag}")

    modules = {title: note for title, note in notes.items()
               if note[1].get("module") not in {"dashboard", "architecture", "exercises", "api"}}
    exercise_count = 0
    for title, (file, fields, body) in modules.items():
        for section in ("Purpose", "Key Files", "Public Interface", "Internal Flow", "Dependencies", "Configuration", "Testing", "Related Notes"):
            require("## " + section in body, f"{title}: missing module section {section}")
        require("```text\n" in body, f"{title}: no ASCII flow")
        require("module-"+fields["module"] in fields.get("tags", []), f"{title}: missing parent module tag")
        exercise_title = title + "练习"
        require(exercise_title in notes, f"{title}: missing exercises")
        if exercise_title in notes:
            exercise = notes[exercise_title][2]
            questions = re.findall(r"^## 练习 [0-9]+ \[(trace|config|debug|extend)\]", exercise, re.M)
            answers = re.findall(r"^> \[!answer\]- 查看答案\s*$", exercise, re.M)
            require(len(questions) >= 5, f"{title}: fewer than 5 exercises")
            require(len(questions) == len(answers), f"{title}: exercises/answers mismatch")
            require(set(questions) == {"trace", "config", "debug", "extend"}, f"{title}: exercise types missing")
            require("[[" + title + "]]" in exercise and "[[" + exercise_title + "]]" in body, f"{title}: missing concept/exercise backlink")
            exercise_count += len(questions)
        for other in modules:
            if other != title and "[[" + other + "]]" in body:
                require("[[" + title + "]]" in modules[other][2], f"module dependency link not bidirectional: {title} -> {other}")
    require(len(modules) == 13, f"expected 13 modules, got {len(modules)}")
    require(exercise_count == 65, f"expected 65 exercises, got {exercise_count}")

    recorded = json.loads((TOOLS / "api-surface.json").read_text(encoding="utf-8"))
    current = static_routes()
    require(recorded == current, "API surface changed; recheck source and regenerate inventory")
    catalogue = notes.get("请求接口目录", (None, {}, ""))[2]
    for route in current:
        require(f"| {route['method']} | `{route['path']}` |" in catalogue, f"catalogue missing {route['method']} {route['path']}")
    current_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    baselines = {note[1].get("source_commit") for note in notes.values()}
    require(len(baselines) == 1, "notes use inconsistent source commits")
    if baselines != {current_commit}:
        WARNINGS.append("HEAD differs from documented source baseline; semantic review is needed")

    report = {
        "status": "passed" if not ERRORS else "failed",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": next(iter(baselines), None),
        "notes": len(notes), "modules": len(modules), "exercise_files": len(modules),
        "exercises": exercise_count, "folded_answers": exercise_count,
        "wiki_links": wiki_links, "registered_tags": len(registry),
        "source_files": len(sources), "referenced_test_files": len(referenced_tests),
        "api_routes": len(current), "errors": ERRORS, "warnings": WARNINGS,
        "scope": "Structural and source-reference validation only; application tests and rendered Obsidian view not run.",
    }
    (TOOLS / "validation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not ERRORS else 1


if __name__ == "__main__":
    sys.exit(main())
