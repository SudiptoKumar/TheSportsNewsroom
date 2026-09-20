from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
logger = logging.getLogger("sports-games-hub.observability")


class PipelineReport:
    """Run-level telemetry so every candidate drop and slow stage is explainable."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.counts: dict[str, int] = {}
        self.reasons: dict[str, Counter] = defaultdict(Counter)
        self.rejections: list[dict] = []
        self.sources: list[dict] = []
        self.timings: dict[str, float] = {}
        self.published: list[dict] = []
        self.ai: dict[str, int] = {"reserved": 0, "used": 0, "mandatory_used": 0, "reserve_released": 0}
        self.candidate_ledger: dict[str, dict] = {}
        self.errors: list[str] = []

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def candidate(self, stage: str, candidate_id: str, outcome: str, reason: str = "") -> None:
        if not candidate_id:
            return
        row = self.candidate_ledger.setdefault(candidate_id, {"stages": {}})
        row.setdefault("stages", {})[stage] = {
            "outcome": outcome,
            "reason": reason[:180],
        }
        row["last_stage"] = stage
        row["last_outcome"] = outcome

    def candidate_gate(self, stage: str, candidate_ids: Iterable[str]) -> list[str]:
        expected = {str(cid) for cid in candidate_ids if str(cid)}
        seen = {cid for cid, row in self.candidate_ledger.items() if stage in row.get("stages", {})}
        missing = sorted(expected - seen)
        self.count(f"ledger.{stage}.expected", len(expected))
        self.count(f"ledger.{stage}.accounted", len(expected) - len(missing))
        self.count(f"ledger.{stage}.unaccounted", len(missing))
        for cid in missing:
            self.candidate(stage, cid, "unaccounted", "no_terminal_decision")
        return missing

    def candidate_terminal(self, candidate_id: str, outcome: str, reason: str = "") -> None:
        self.candidate("terminal", candidate_id, outcome, reason)

    def finalize_candidates(self, candidate_ids: Iterable[str]) -> list[str]:
        """Require every run candidate to reach an explicit terminal outcome.

        A terminal outcome is deliberately separate from intermediate states such as
        classified/verified/selected. This prevents a candidate from disappearing simply
        because a later stage narrowed its list.
        """
        expected = [str(cid) for cid in candidate_ids if str(cid)]
        missing = self.candidate_gate("terminal", expected)
        return missing

    def problems(self) -> list[str]:
        return [
            f"{key}={value}"
            for key, value in self.counts.items()
            if "unaccounted" in key and int(value) > 0
        ]

    def assert_no_unaccounted(self) -> None:
        # Any stage-level accounting leak is a correctness failure, not only the final
        # terminal gate. This keeps intermediate funnel shrinkage from being hidden by a
        # later stage or by a fixture that happens to terminally label the candidate.
        problems = self.problems()
        if problems:
            raise RuntimeError("Candidate ledger has unaccounted item(s): " + "; ".join(problems))

    def reject(self, stage: str, reason: str, title: str = "", detail: str = "", candidate_id: str = "") -> None:
        self.reasons[stage][reason] += 1
        self.rejections.append({
            "stage": stage,
            "reason": reason,
            "title": title[:100],
            "detail": detail[:180],
            "candidate_id": candidate_id,
        })

    def source(
        self,
        url: str,
        ok: bool,
        evidence_chars: int = 0,
        tier: str | int = "",
        error: str = "",
        elapsed: float = 0.0,
        fallback: bool = False,
    ) -> None:
        from .utils import domain_of

        self.sources.append({
            "url": url,
            "domain": domain_of(url),
            "ok": bool(ok),
            "evidence_chars": int(evidence_chars),
            "tier": tier,
            "error": error[:180],
            "elapsed": round(elapsed, 3),
            "fallback": bool(fallback),
        })

    def publish(self, lane: str, title: str, ok: bool = True) -> None:
        self.published.append({"lane": lane, "title": title[:100], "ok": bool(ok)})

    def error(self, message: str) -> None:
        self.errors.append(str(message)[:300])

    @contextmanager
    def stage(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + time.monotonic() - t0

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "total_seconds": round(time.monotonic() - self.started, 3),
            "counts": dict(self.counts),
            "rejections": list(self.rejections),
            "reject_reasons": {k: dict(v) for k, v in self.reasons.items()},
            "sources": list(self.sources),
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
            "published": list(self.published),
            "ai": dict(self.ai),
            "candidate_ledger": dict(self.candidate_ledger),
            "errors": list(self.errors),
        }

    def render(self, max_rejections: int = 40) -> str:
        data = self.to_dict()
        lines = ["════════ SPORTS NEWSROOM RUN ════════", f"Total time  {data['total_seconds']:.1f}s"]
        if self.timings:
            lines += ["", "Timing"]
            lines += [f"  {k:<30}{v:6.1f}s" for k, v in self.timings.items()]
        if self.counts:
            sections: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for key, n in self.counts.items():
                sec, _, name = key.partition(".")
                sections[sec].append((name or sec, n))
            for sec, rows in sections.items():
                lines += ["", sec.capitalize()]
                lines += [f"  {name:<30}{n:5d}" for name, n in rows]
        lines += [
            "", "AI Budget",
            f"  reserved (initial)          {self.ai.get('reserved', 0):5d}",
            f"  reserve currently held      {self.ai.get('reserve_held', 0):5d}",
            f"  used                        {self.ai.get('used', 0):5d}",
            f"  mandatory used               {self.ai.get('mandatory_used', 0):5d}",
        ]
        if self.reasons:
            lines += ["", "Rejected, by reason"]
            for stage, counter in self.reasons.items():
                lines.append(f"  {stage}")
                lines += [f"    {reason:<26}{n:4d}" for reason, n in counter.most_common()]
        if self.sources:
            ok = sum(1 for s in self.sources if s["ok"])
            ev = sum(1 for s in self.sources if s["evidence_chars"] > 0)
            lines += [
                "", "Sources",
                f"  fetched ok                  {ok:5d} / {len(self.sources)}",
                f"  evidence extracted          {ev:5d}",
            ]
            for s in self.sources:
                if not s["ok"]:
                    lines.append(f"  FAIL {s['domain'] or s['url'][:45]:<45} {s['error'][:45]}")
        if self.rejections:
            lines += ["", f"Why rejected (first {max_rejections})"]
            for item in self.rejections[:max_rejections]:
                detail = f" | {item['detail']}" if item["detail"] else ""
                lines.append(
                    f"  [{item['stage']}] {item['reason']} | {item['title']}{detail}"
                )
        lines += ["", "Published"]
        lines += [
            f"  {p['lane']:<18}{'[OK]' if p['ok'] else '[FAIL]'} {p['title']}"
            for p in self.published
        ] or ["  (nothing)"]
        lines.append(f"  TOTAL {len(self.published)}")
        if self.errors:
            lines += ["", "Errors"] + [f"  {e}" for e in self.errors[:10]]
        return "\n".join(lines)

    def emit(self, log: logging.Logger | None = None) -> None:
        rendered = self.render()
        writer = log.info if log else print
        for line in rendered.splitlines():
            writer(line)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("\n## Sports Newsroom run report\n```text\n")
                fh.write(rendered)
                fh.write("\n```\n")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)


class AiBudget:
    """Logical AI-operation budget with a protected, releasable mandatory reserve."""

    def __init__(self, total: int, reserved_for_mandatory: int = 8, report: PipelineReport | None = None) -> None:
        self.total = max(0, int(total))
        self.reserved_initial = min(max(0, int(reserved_for_mandatory)), self.total)
        self.reserved = self.reserved_initial
        self.used = 0
        self.mandatory_used = 0
        self.reserve_released = 0
        self.report = report
        if report:
            report.ai["reserved"] = self.reserved_initial
            report.ai["reserve_held"] = self.reserved

    def take(self, lane: str) -> bool:
        if self.used >= self.total:
            return False
        if lane == "mandatory":
            self.used += 1
            self.mandatory_used += 1
        else:
            remaining_reserved = max(0, self.reserved - self.mandatory_used)
            if self.used >= self.total - remaining_reserved:
                return False
            self.used += 1
        if self.report:
            self.report.ai["used"] = self.used
            self.report.ai["mandatory_used"] = self.mandatory_used
        return True

    def release_unused_mandatory(self) -> int:
        unused = max(0, self.reserved - self.mandatory_used)
        if unused:
            self.reserved -= unused
            self.reserve_released += unused
            if self.report:
                self.report.ai["reserved"] = self.reserved_initial
                self.report.ai["reserve_held"] = self.reserved
                self.report.ai["reserve_released"] = self.reserve_released
        return unused

    def remaining(self, lane: str = "discovery") -> int:
        if lane == "mandatory":
            return max(0, self.total - self.used)
        remaining_reserved = max(0, self.reserved - self.mandatory_used)
        return max(0, self.total - self.used - remaining_reserved)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.total


def balanced_pool(
    cands: Iterable[T],
    family_of: Callable[[T], str],
    score_of: Callable[[T], float],
    quotas: dict[str, int],
    cap: int,
) -> list[T]:
    """Diversity-first selection, then score-based fill with diminishing family returns.

    Quotas are a representation target, not an absolute cap. Underrepresented
    families receive a modest bonus during top-up, while exceptional high-score
    candidates can still enter the pool.
    """
    cands = list(cands)
    by_family: dict[str, list[T]] = defaultdict(list)
    for candidate in cands:
        by_family[family_of(candidate) or "other"].append(candidate)
    for items in by_family.values():
        items.sort(key=score_of, reverse=True)

    picked: list[T] = []
    used = Counter()
    while len(picked) < cap:
        best = None
        best_key = None
        for family, items in by_family.items():
            pos = used[family]
            family_quota = max(1, quotas.get(family, 1))
            if pos >= len(items) or pos >= family_quota:
                continue
            candidate = items[pos]
            key = (float(score_of(candidate)) + (3.0 if pos == 0 else 0.0), -pos)
            if best_key is None or key > best_key:
                best_key = key
                best = (family, candidate)
        if best is None:
            break
        family, candidate = best
        picked.append(candidate)
        used[family] += 1

    selected_ids = {id(x) for x in picked}
    rest = [x for x in cands if id(x) not in selected_ids]
    while rest and len(picked) < cap:
        def adjusted(x: T) -> float:
            family = family_of(x) or "other"
            return float(score_of(x)) + max(0.0, 2.5 - 0.9 * used[family])
        rest.sort(key=adjusted, reverse=True)
        candidate = rest.pop(0)
        picked.append(candidate)
        used[family_of(candidate) or "other"] += 1
    return picked[:cap]


def select_quality_gated(
    items: Iterable[T], score_of: Callable[[T], float], min_score: float, max_posts: int
) -> list[T]:
    good = [item for item in items if score_of(item) >= min_score]
    return sorted(good, key=score_of, reverse=True)[:max_posts]
