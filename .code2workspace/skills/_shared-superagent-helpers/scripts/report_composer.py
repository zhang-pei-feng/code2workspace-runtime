#!/usr/bin/env python3
"""Shared compose helpers for report-oriented project skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common import write_json, write_text


def parse_lane_file(path: Path) -> dict[str, object]:
    """Parse one lane note into metadata and evidence text."""
    if not path.exists():
        return {
            "exists": False,
            "title": path.stem,
            "skills": [],
            "sources": [],
            "evidence_lines": [],
            "has_evidence": False,
            "missing_reasons": ["lane file not found"],
        }

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {
            "exists": True,
            "title": path.stem,
            "skills": [],
            "sources": [],
            "evidence_lines": [],
            "has_evidence": False,
            "missing_reasons": ["lane file is empty"],
        }

    lines = text.splitlines()
    title = lines[0].lstrip("# ").strip() or path.stem
    skills: list[str] = []
    sources: list[str] = []
    evidence_lines: list[str] = []
    table_candidates: list[dict[str, str]] = []
    current_table_candidate: dict[str, str] | None = None

    def flush_table_candidate() -> None:
        nonlocal current_table_candidate
        if current_table_candidate:
            table_candidates.append(current_table_candidate)
            current_table_candidate = None

    for raw in lines[1:]:
        stripped = raw.strip()
        if not stripped:
            flush_table_candidate()
            continue
        lowered = stripped.lower()
        if lowered.startswith("subagent:") or lowered.startswith("purpose:"):
            continue
        if lowered.startswith("table candidate:"):
            flush_table_candidate()
            current_table_candidate = {
                "title": stripped.split(":", 1)[1].strip(),
            }
            continue
        if current_table_candidate is not None:
            candidate_fields = {
                "metric:": "metric",
                "value:": "value",
                "unit:": "unit",
                "time:": "time",
                "scope:": "scope",
                "source:": "source",
                "note:": "note",
            }
            matched_field = False
            for prefix, key in candidate_fields.items():
                if lowered.startswith(prefix):
                    current_table_candidate[key] = stripped.split(":", 1)[1].strip()
                    matched_field = True
                    break
            if matched_field:
                continue
            flush_table_candidate()
        if lowered.startswith("skill:"):
            skills.append(stripped.split(":", 1)[1].strip())
            continue
        if lowered.startswith("source:"):
            sources.append(stripped.split(":", 1)[1].strip())
            continue
        evidence_lines.append(stripped)

    flush_table_candidate()

    missing_reasons: list[str] = []
    if not skills:
        missing_reasons.append("missing Skill metadata")
    if not sources:
        missing_reasons.append("missing Source metadata")
    if not evidence_lines:
        missing_reasons.append("missing evidence narrative")

    return {
        "exists": True,
        "title": title,
        "skills": skills,
        "sources": sources,
        "evidence_lines": evidence_lines,
        "table_candidates": table_candidates,
        "has_evidence": bool(skills and sources and evidence_lines),
        "missing_reasons": missing_reasons,
    }


def collect_lane_info(
    lanes_dir: Path,
    *,
    lane_files: list[str] | tuple[str, ...] | None = None,
) -> dict[str, dict[str, object]]:
    """Return parsed lane info keyed by file name."""
    names = (
        list(lane_files)
        if lane_files is not None
        else [path.name for path in sorted(lanes_dir.glob("*.md"))]
    )
    return {
        name: parse_lane_file(lanes_dir / name)
        for name in names
    }


def render_lane_evidence(
    lane_file: str,
    lane_info: dict[str, object],
    *,
    missing_prefix: str = "Missing or incomplete evidence",
) -> str:
    """Render either lane evidence or an explicit incomplete message."""
    evidence_lines = [str(item) for item in lane_info["evidence_lines"]]
    if evidence_lines:
        return "\n".join(evidence_lines)

    reasons = ", ".join(str(item) for item in lane_info["missing_reasons"])
    return f"_This lane is incomplete. {missing_prefix}: {reasons} ({lane_file})._"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def write_composed_report(
    *,
    run_dir: Path,
    title: str,
    sections: list[dict[str, Any]],
    lane_info_by_file: dict[str, dict[str, object]],
    header_lines: list[str] | None = None,
    required_lane_files: list[str] | tuple[str, ...] | None = None,
    min_report_chars: int = 0,
    source_section_mode: str = "flat",
    extra_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write `final_report.md` and `report_diagnostics.json`."""
    required = list(required_lane_files or [])
    unique_sources = _dedupe(
        [
            str(source)
            for lane_info in lane_info_by_file.values()
            for source in lane_info["sources"]
        ]
    )
    unique_skills = _dedupe(
        [
            str(skill)
            for lane_info in lane_info_by_file.values()
            for skill in lane_info["skills"]
        ]
    )
    missing_required_lanes = [
        lane_file
        for lane_file in required
        if not bool(lane_info_by_file.get(lane_file, {}).get("has_evidence"))
    ]
    evidence_lane_count = sum(
        1 for lane_info in lane_info_by_file.values() if bool(lane_info["has_evidence"])
    )

    lines = [f"# {title}", ""]
    if header_lines:
        lines.extend(header_lines)
        if header_lines[-1] != "":
            lines.append("")

    for section in sections:
        lines.extend([f"## {section['title']}", "", str(section["body"]), ""])

    lines.extend(["## Sources", ""])
    if source_section_mode == "by_lane":
        any_metadata = False
        for section in sections:
            lane_file = section.get("lane_file")
            if lane_file is None:
                continue
            lane_info = lane_info_by_file.get(str(lane_file))
            lines.extend([f"### {section['title']}"])
            if lane_info is None:
                lines.extend(["- No usable source metadata was recorded for this lane.", ""])
                continue
            skills = _dedupe([str(item) for item in lane_info["skills"]])
            sources = _dedupe([str(item) for item in lane_info["sources"]])
            if skills or sources:
                any_metadata = True
                lines.extend(f"- Skill: {item}" for item in skills)
                lines.extend(f"- Source: {item}" for item in sources)
            else:
                lines.append("- No usable source metadata was recorded for this lane.")
            lines.append("")
        if not any_metadata:
            lines.append("- No explicit sources were recorded in lane notes.")
    else:
        if unique_sources:
            lines.extend(f"- {item}" for item in unique_sources)
        else:
            lines.append("- No explicit sources were recorded in lane notes.")
        lines.append("")
        if unique_skills:
            lines.append("### Evidence Layers")
            lines.append("")
            lines.extend(f"- {item}" for item in unique_skills)
            lines.append("")

    report_text = "\n".join(lines).rstrip() + "\n"
    report_path = run_dir / "final_report.md"
    diagnostics_path = run_dir / "report_diagnostics.json"
    report_char_count = len(report_text)
    meets_min_report_chars = report_char_count >= int(min_report_chars)

    if required:
        complete = not missing_required_lanes
    else:
        complete = bool(evidence_lane_count > 0 and unique_sources)
    complete = bool(complete and meets_min_report_chars)

    diagnostics: dict[str, Any] = {
        "run_dir": str(run_dir),
        "lane_count": len(lane_info_by_file),
        "source_count": len(unique_sources),
        "skill_count": len(unique_skills),
        "evidence_lane_count": evidence_lane_count,
        "missing_required_lanes": missing_required_lanes,
        "complete": complete,
        "report_char_count": report_char_count,
        "meets_min_report_chars": meets_min_report_chars,
        "lane_diagnostics": {
            lane_file: {
                "title": str(info["title"]),
                "skills": list(info["skills"]),
                "sources": list(info["sources"]),
                "table_candidate_count": len(info.get("table_candidates", [])),
                "has_evidence": bool(info["has_evidence"]),
                "missing_reasons": list(info["missing_reasons"]),
            }
            for lane_file, info in lane_info_by_file.items()
        },
    }
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)

    write_text(report_path, report_text)
    write_json(diagnostics_path, diagnostics)
    return {
        "report_path": str(report_path),
        "diagnostics_path": str(diagnostics_path),
        **diagnostics,
    }
