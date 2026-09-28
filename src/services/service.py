"""ENE-C2-127 — deterministic domain services (no framework imports, no LLM).

MarketExtractAnomalyService: normalizes an already-approved, quality-checked JEPX-derived electricity-market
extract (30-min-granularity × area source rows) into a canonical signal set, screens each (area, metric)
group against the seeded, organisation-owned anomaly policy + baseline period + threshold policy for
policy-defined anomalies (missing_interval / duplicate / discontinuity / threshold_exception), retrieves the
cited anomaly-policy / baseline / threshold clauses, and composes a candidate analyst triage worklist.

Everything here is deterministic and auditable (interval-sequence gap / duplicate / step-change detection +
threshold banding + keyed clause composition) — there is **no LLM** (no model in config/agent.yaml, no LLM
dependency in pyproject, no LLM call anywhere in src/). Source rows are keyed by an opaque, non-reversible
``source_row_id`` surrogate; the raw row reference and any free-text metadata are never carried into the
worklist, and the S-3 output gate re-redacts anything that leaks. Seeded anomaly / baseline / threshold
policy are overridable by CoE (a change-controlled engineer MR + specialist review) without touching node
logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (opaque_id): every caller identifier (source_row_id / extract_id) is UNCONDITIONALLY
#       tokenized to a deterministic, non-reversible opaque surrogate so a free-text / PII value (even a bare
#       name like ``Alice`` / ``Taro.Yamada`` / ``TaroYamada``, no spaces/symbols) can never reach a citation
#       or the worklist. Tokenizing is a privacy measure — it does NOT assert the value is
#       authorized/verifiable. Surrogates are one-way hashes; graph state never stores a surrogate→raw rejoin
#       map, so the opaque ID is non-linkable back to the source system.
#   (2) PROVENANCE (resolve_provenance): a caller ``source`` becomes a grounded CITATION only when it is
#       resolvable against the authorized provenance registry (names a trusted market system of record). Any
#       other free text (a name, ``unknown``, a fabricated value, or a caller value merely SHAPED like a
#       surrogate ``src:1a2b3c4d``) is NOT verifiable provenance → it yields NO citation → S-3 blocks the
#       worklist as CITATION_INCOMPLETE (fail-closed). "Tokenized" is never sufficient for a citation.
# Tokenization is UNCONDITIONAL (no syntactic passthrough): a caller value merely *shaped* like a surrogate
# (``row:deadbeef``) is re-hashed, never trusted, so it can never forge an internal join key. Identifiers /
# provenance are resolved exactly once at S-1 (pre_process); downstream trusts that resolution verbatim.
_SAFE_TOKEN = re.compile(r"^[a-z0-9_\-]{1,48}$")

# Authorized provenance registry: the market / data systems of record a service operator trusts as
# verifiable data sources. A caller ``source`` is accepted as a grounded citation ONLY when its leading
# namespace names one of these (the "trusted context"). This is the deploying org's / CoE's registry —
# overridable without touching node logic; it is a SEMANTIC allowlist of authorized systems, not a syntactic
# character class.
AUTHORIZED_PROVENANCE_SYSTEMS = frozenset(
    {
        "jepx",
        "jepx_feed",
        "jepx_spot",
        "spot_market",
        "intraday_market",
        "power_exchange",
        "occto",
        "escj",
        "tso",
        "bg",
        "balancing_group",
        "market_data_feed",
        "market_feed",
        "extract_feed",
        "quality_validated_feed",
        "data_platform",
        "market_data_platform",
        "mdp",
        "etrm",
        "trading_system_of_record",
        "system_of_record",
        "sor",
        "authorized_feed",
        "curated_extract",
    }
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def opaque_id(value: Any, prefix: str) -> str:
    """PRIVACY tokenize a caller identifier to a deterministic, non-reversible opaque surrogate
    ``<prefix>:<sha8>``.

    Caller identifiers are **always** tokenized — no syntactic passthrough — so a name (with or without
    spaces) can never survive into a citation or the worklist, and a caller value merely *shaped* like a
    surrogate (``row:deadbeef``) is re-hashed rather than trusted (it can never forge an internal join key).
    Same input → same surrogate (worklist / citations / summary stay joinable within one invocation). This is
    a privacy measure only; it makes no claim that the identifier is authorized, and no surrogate→raw rejoin
    map is ever kept.
    """
    return f"{prefix}:{_sha8(str(value or '').strip())}"


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller ``source`` to a grounded, privacy-tokenized CITATION — or ``None``.

    Provenance validation (separate from privacy) and the **single** resolution point (S-1 / pre_process).
    A citation is emitted **only** when the source names an authorized market system of record
    (``<authorized-namespace>[:<ref>]``). Any other value — a name, ``unknown``, a fabricated value, **or a
    value that merely looks like a surrogate (``src:1a2b3c4d``)** — is not verifiable provenance and returns
    ``None`` so the S-3 gate blocks the worklist as CITATION_INCOMPLETE (fail-closed). When authorized, the
    raw label is never used verbatim: the citation is a privacy hash (``src:<sha8>``) of the authorized
    reference. No synthetic provenance is fabricated.

    ★ Forged-surrogate defence: there is **no format-based passthrough**. A caller-supplied ``src:<hex>`` has
    namespace ``src`` (not an authorized system of record), so it resolves to ``None`` — it is dropped here at
    S-1 and can never reach a citation. Because provenance is resolved exactly once (here), the produced
    ``src:<sha8>`` is the trusted citation downstream and is **never** fed back through this function (which
    would, correctly, reject it), so no forged value can imitate an internal surrogate.
    """
    text = str(value or "").strip()
    if not text:
        return None
    namespace = text.split(":", 1)[0].strip().lower()
    if namespace not in AUTHORIZED_PROVENANCE_SYSTEMS:
        return None  # unverifiable / forged-surrogate provenance → fail-closed (no citation → needs_review)
    return "src:" + _sha8(text)


# Quality-validation statuses that permit anomaly triage (S-1 acceptance gate). An extract lacking a passed
# upstream quality-validation status is not triaged — it degrades to an out-of-scope safe answer.
ACCEPTED_QUALITY_STATUSES = frozenset(
    {
        "passed",
        "pass",
        "validated",
        "quality_validated",
        "quality_passed",
        "approved",
        "ok",
    }
)

# ── seeded anomaly taxonomy: policy-defined type → human-readable description ──
ANOMALY_TAXONOMY: dict[str, str] = {
    "missing_interval": "A 30-minute interval expected within the observed range is absent for the area/metric",
    "duplicate": "The same (area, metric, interval) source row appears more than once",
    "discontinuity": "A step change between consecutive intervals exceeds the baseline discontinuity threshold",
    "threshold_exception": "A value falls outside the approved threshold band for the metric",
    "unclassified": "No policy-defined anomaly type matched; routed for manual analyst review",
}

# ── seeded, organisation-owned anomaly policy (authorized clauses, CoE-calibratable) ──
# clause_id@version is a stable, citable reference to the approved anomaly-policy clause for each type.
ANOMALY_POLICY: dict[str, dict[str, Any]] = {
    "missing_interval": {
        "clause_id": "AP-MISS",
        "version": "v2",
        "description": "Missing 30-min interval within observed coverage",
    },
    "duplicate": {
        "clause_id": "AP-DUP",
        "version": "v2",
        "description": "Duplicate interval entry for an (area, metric)",
    },
    "discontinuity": {
        "clause_id": "AP-DISC",
        "version": "v2",
        "description": "Baseline-relative discontinuity (step change) between intervals",
    },
    "threshold_exception": {
        "clause_id": "AP-THRESH",
        "version": "v2",
        "description": "Value outside the approved metric threshold band",
    },
    "unclassified": {
        "clause_id": "AP-UNCLASS",
        "version": "v2",
        "description": "Unclassified anomaly → manual analyst review",
    },
}

# ── seeded baseline period policy per metric (baseline_ref + expected cadence) ──
BASELINE_POLICY: dict[str, dict[str, Any]] = {
    "price": {
        "granularity_minutes": 30,
        "expected_intervals_per_day": 48,
        "baseline_mean": 12.0,
        "clause_id": "BL-PRICE",
        "version": "v3",
    },
    "volume": {
        "granularity_minutes": 30,
        "expected_intervals_per_day": 48,
        "baseline_mean": 1000.0,
        "clause_id": "BL-VOLUME",
        "version": "v3",
    },
}

# ── seeded threshold policy per metric (min/max band + discontinuity step, threshold_ref) ──
THRESHOLD_POLICY: dict[str, dict[str, Any]] = {
    "price": {"min": 0.01, "max": 100.0, "discontinuity_step": 30.0, "clause_id": "TH-PRICE", "version": "v3"},
    "volume": {"min": 0.0, "max": 100000.0, "discontinuity_step": 5000.0, "clause_id": "TH-VOLUME", "version": "v3"},
}

_MISSING_INTERVAL_HIGH = 3  # >= this many missing intervals in a group = high severity
_SEVERITY_WEIGHT = {"high": 2, "med": 1}
# Priority ordering for the worklist (threshold breach / critical first).
_TYPE_RANK = {"threshold_exception": 4, "missing_interval": 3, "discontinuity": 2, "duplicate": 1, "unclassified": 0}


def _num(value: Any, default: float | None = None) -> float | None:
    """Coerce to float; non-numeric → default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def is_quality_validated(status: Any) -> bool:
    """S-1 acceptance gate: True only if the extract carries a passed upstream quality-validation status."""
    return str(status or "").strip().lower() in ACCEPTED_QUALITY_STATUSES


class MarketExtractAnomalyService:
    """Deterministic normalization, anomaly detection, clause retrieval, and worklist composition."""

    # ── normalization ────────────────────────────────────────────────────────
    @staticmethod
    def normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate + canonicalize source rows into a signal set. Rows without a ``source_row_id`` are
        dropped.

        The raw row reference and free-text metadata are intentionally reduced to opaque IDs / constrained
        vocab — they are never carried verbatim into the worklist. ``source_row_id`` is always
        privacy-tokenized. ``source`` was already resolved to a grounded citation (``src:<sha8>``) or ``None``
        by pre_process (S-1), the single provenance-resolution point — a forged surrogate was dropped there.
        normalize trusts that value verbatim; it never re-resolves and never fabricates provenance.
        """
        out: list[dict[str, Any]] = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("source_row_id") or raw.get("row_id") or raw.get("id") or "").strip()
            if not raw_id:
                continue
            # Tokenize unconditionally (no forgeable passthrough). pre_process (S-1) already tokenized
            # source_row_id to an opaque surrogate; re-tokenizing here is deterministic (same input → same
            # surrogate), so the worklist and citations stay joinable within this invocation.
            source_row_id = opaque_id(raw_id, "row")
            source = raw.get("source")  # already resolved (src:<sha8> or None) at S-1

            area_raw = str(raw.get("area") or "").strip().lower()
            area = area_raw if _SAFE_TOKEN.match(area_raw) else "unknown"
            metric_raw = str(raw.get("metric") or "").strip().lower()
            metric = metric_raw if metric_raw in THRESHOLD_POLICY else "unknown"

            out.append(
                {
                    "source_row_id": source_row_id,
                    "area": area,
                    "metric": metric,
                    "slot": _int(raw.get("slot")),  # 30-min slot index (0..47) or None
                    "value": _num(raw.get("value")),
                    "source": source,
                }
            )
        return out

    # ── detection ─────────────────────────────────────────────────────────────
    @staticmethod
    def detect(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Detect policy-defined anomalies per (area, metric) group. Deterministic threshold / interval /
        step-change matching only — the free-text metadata is never interpreted semantically, so prompt-like
        text in a supplied field can never influence detection."""
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault((r["area"], r["metric"]), []).append(r)

        anomalies: list[dict[str, Any]] = []
        for (area, metric), grp in sorted(groups.items()):
            anomalies.extend(MarketExtractAnomalyService._detect_group(area, metric, grp))
        return anomalies

    @staticmethod
    def _detect_group(area: str, metric: str, grp: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        threshold = THRESHOLD_POLICY.get(metric)

        # duplicate: an (area, metric, slot) appearing more than once.
        by_slot: dict[int, list[dict[str, Any]]] = {}
        for r in grp:
            if r["slot"] is not None:
                by_slot.setdefault(r["slot"], []).append(r)
        dup_rows = [r for rows_ in by_slot.values() if len(rows_) > 1 for r in rows_]
        if dup_rows:
            dup_slots = sorted(s for s, rows_ in by_slot.items() if len(rows_) > 1)
            sev = "high" if len(dup_slots) >= 2 else "med"
            found.append(
                MarketExtractAnomalyService._mk(
                    "duplicate", area, metric, dup_rows, sev, f"duplicate slots={dup_slots}"
                )
            )

        # missing_interval: gap within the observed [min_slot, max_slot] range.
        present = sorted({r["slot"] for r in grp if r["slot"] is not None})
        if len(present) >= 2:
            expected = set(range(present[0], present[-1] + 1))
            missing = sorted(expected - set(present))
            if missing:
                sev = "high" if len(missing) >= _MISSING_INTERVAL_HIGH else "med"
                cited = [r for r in grp if r["slot"] is not None]
                found.append(
                    MarketExtractAnomalyService._mk(
                        "missing_interval",
                        area,
                        metric,
                        cited,
                        sev,
                        f"missing_slots={missing[:8]} (count={len(missing)})",
                    )
                )

        # discontinuity: consecutive present slots whose value step exceeds the baseline threshold.
        if threshold is not None:
            step_limit = threshold["discontinuity_step"]
            ordered = sorted(
                (r for r in grp if r["slot"] is not None and r["value"] is not None), key=lambda r: r["slot"]
            )
            for prev, cur in zip(ordered, ordered[1:]):
                if cur["slot"] == prev["slot"]:
                    continue
                delta = abs(cur["value"] - prev["value"])
                if delta >= step_limit:
                    sev = "high" if delta >= 2 * step_limit else "med"
                    found.append(
                        MarketExtractAnomalyService._mk(
                            "discontinuity",
                            area,
                            metric,
                            [prev, cur],
                            sev,
                            f"step={round(delta, 3)} between slot {prev['slot']}->{cur['slot']} "
                            f"(limit={step_limit})",
                        )
                    )

        # threshold_exception: value outside the approved band for the metric.
        if threshold is not None:
            lo, hi = threshold["min"], threshold["max"]
            for r in grp:
                v = r["value"]
                if v is None:
                    continue
                if v < lo or v > hi:
                    over = (v - hi) if v > hi else (lo - v)
                    band = max(hi - lo, 1e-9)
                    sev = "high" if over >= 0.5 * band else "med"
                    found.append(
                        MarketExtractAnomalyService._mk(
                            "threshold_exception",
                            area,
                            metric,
                            [r],
                            sev,
                            f"value={round(v, 3)} outside band [{lo}, {hi}]",
                        )
                    )
        return found

    @staticmethod
    def _mk(
        anomaly_type: str, area: str, metric: str, cited_rows: list[dict[str, Any]], severity: str, evidence: str
    ) -> dict[str, Any]:
        """Build one detected anomaly. ``cited_source_rows`` are the tokenized ids; ``citation`` is the shared
        authorized provenance — or ``None`` if any cited row is ungrounded (→ S-3 fail-closed)."""
        cited_ids = sorted({r["source_row_id"] for r in cited_rows})
        sources = [r["source"] for r in cited_rows]
        # Grounded only if every cited row resolved to an authorized provenance citation at S-1.
        grounded = bool(sources) and all(s for s in sources)
        citation = sources[0] if grounded else None
        anomaly_id = "anom:" + _sha8(f"{anomaly_type}|{area}|{metric}|{'|'.join(cited_ids)}")
        return {
            "anomaly_id": anomaly_id,
            "anomaly_type": anomaly_type,
            "anomaly_description": ANOMALY_TAXONOMY.get(anomaly_type, ""),
            "area": area,
            "metric": metric,
            "severity": severity,
            "severity_score": _SEVERITY_WEIGHT[severity],
            "priority_rank": _TYPE_RANK[anomaly_type],
            "matched_drivers": [{"anomaly_type": anomaly_type, "severity": severity, "evidence": evidence}],
            "cited_source_rows": cited_ids,
            "citation": citation,
        }

    # ── clause retrieval ───────────────────────────────────────────────────────
    @staticmethod
    def retrieve_references(anomaly: dict[str, Any]) -> dict[str, Any]:
        """Deterministically retrieve the cited anomaly-policy + baseline + threshold clauses for one detected
        anomaly. Clause refs are ``<clause_id>@<version>`` from the seeded authorized policy."""
        policy = ANOMALY_POLICY.get(anomaly["anomaly_type"], ANOMALY_POLICY["unclassified"])
        policy_refs = [f"{policy['clause_id']}@{policy['version']}"]

        baseline = BASELINE_POLICY.get(anomaly["metric"])
        baseline_ref = f"{baseline['clause_id']}@{baseline['version']}" if baseline else None
        threshold = THRESHOLD_POLICY.get(anomaly["metric"])
        threshold_ref = f"{threshold['clause_id']}@{threshold['version']}" if threshold else None
        return {"policy_refs": policy_refs, "baseline_ref": baseline_ref, "threshold_ref": threshold_ref}

    # ── worklist composition ────────────────────────────────────────────────────
    @staticmethod
    def compose_group(anomaly: dict[str, Any], refs: dict[str, Any]) -> dict[str, Any]:
        """Compose the per-anomaly worklist group entry (needs-review, cited).

        The entry is a candidate only — the final triage / data-quality decision defers to a human analyst.
        """
        return {
            "anomaly_id": anomaly["anomaly_id"],
            "anomaly_type": anomaly["anomaly_type"],
            "anomaly_description": anomaly["anomaly_description"],
            "area": anomaly["area"],
            "metric": anomaly["metric"],
            "severity": anomaly["severity"],
            "severity_score": anomaly["severity_score"],
            "priority_rank": anomaly["priority_rank"],
            "matched_drivers": anomaly["matched_drivers"],
            "cited_source_rows": anomaly["cited_source_rows"],
            "cited_policy_refs": refs["policy_refs"],
            "baseline_ref": refs["baseline_ref"],
            "threshold_ref": refs["threshold_ref"],
            "status_kind": "needs_review",
            "note": "Candidate anomaly triage only — the final triage / data-quality decision is an "
            "authorized analyst's; this agent proposes cited anomaly groups and does not decide.",
            "citation": anomaly["citation"],
        }

    @staticmethod
    def triage_summary(groups: list[dict[str, Any]]) -> dict[str, Any]:
        """Portfolio-level rollup: anomaly count, type distribution, anomalies needing urgent review."""
        distribution: dict[str, int] = {}
        for g in groups:
            distribution[g["anomaly_type"]] = distribution.get(g["anomaly_type"], 0) + 1
        urgent = [
            g["anomaly_id"] for g in groups if g["anomaly_type"] == "threshold_exception" or g["severity"] == "high"
        ]
        return {
            "total_anomalies": len(groups),
            "type_distribution": distribution,
            "anomalies_needing_urgent_review": urgent,
        }
