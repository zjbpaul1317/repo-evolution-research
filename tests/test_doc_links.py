from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/repo-evolution-research"
DOCS = [
    ROOT / "README.md",
    ROOT / "README.en.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "THIRD_PARTY_NOTICES.md",
    ROOT / "docs/maintaining.md",
    SKILL / "SKILL.md",
    *sorted((SKILL / "references").glob("*.md")),
    ROOT / "docs/collector-authoring.md",
]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _relative_targets(text: str) -> list[str]:
    targets: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in LINK.findall(line):
            target = target.split("#", 1)[0].strip()
            if target and "://" not in target and not target.startswith("mailto:"):
                targets.append(target)
    return targets


def test_relative_links_resolve() -> None:
    missing = [
        f"{doc.relative_to(ROOT)} -> {target}"
        for doc in DOCS
        for target in _relative_targets(doc.read_text(encoding="utf-8"))
        if not (doc.parent / target).exists()
    ]
    assert not missing, "Broken relative links:\n" + "\n".join(missing)


def test_skill_routes_every_reference() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    missing = [
        path.name
        for path in sorted((SKILL / "references").glob("*.md"))
        if path.name not in text
    ]
    assert not missing, "Unrouted references:\n" + "\n".join(missing)


def test_package_contract() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    assert re.search(r"^name:\s*repo-evolution-research$", frontmatter, re.M)
    assert re.search(r"^description:", frontmatter, re.M)
    assert "contracts.md" not in text
    assert not (SKILL / "references/contracts.md").exists()

    interface = (SKILL / "agents/openai.yaml").read_text(encoding="utf-8")
    assert "$repo-evolution-research" in interface
    match = re.search(r'short_description:\s*"([^"]+)"', interface)
    assert match is not None
    assert 25 <= len(match.group(1)) <= 64
