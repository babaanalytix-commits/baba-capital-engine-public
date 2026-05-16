# Venue-Side SL Detection

**Status:** shipped 2026-05-16 (auto_sl Phase 2).
**Scope:** all manually-managed positions across GRVT, Pacifica, and (planned) Hyperliquid.

---

## Problem

The reconciler tags every venue-detected position whose origin it cannot
explain as `manual_directional`, with `has_stop_loss=false`. That flag
triggers the `sl_queue_worker` to attempt placement of a defensive stop-loss
on the operator's behalf.

This was correct behaviour for genuinely-naked positions. It was the **wrong**
behaviour for positions where the operator had already set a stop-loss via
the venue UI or a Telegram one-tap signal flow. The worker would burn its
retry budget fighting against existing protection — a classic case of
defense-in-depth defeating itself.

The bug class also degrades risk reporting: dashboards counted protected
positions as naked, so the daily audit produced false-urgent items.

---

## Design

A new module sits between the manifest writer and the placement primitive:

```
position_reconciler.venue_sl_detector
    └── detect_existing_sl(venue, asset, side) -> {has_sl, source, detail}
```

### Per-venue strategies

- **GRVT.** Calls `open_orders_v1` filtered to `reduce_only=True` orders
  whose `metadata.trigger` is populated, and whose legs reference the
  position's instrument. Any matching order proves protection exists.
- **Pacifica.** Calls `GET /positions` and inspects the nested
  `stop_loss` field on the matching position (set when the operator hits
  the position-attached `set_position_tpsl` endpoint).
- **Hyperliquid.** Deferred — the existing `PositionMonitor` trigger cache
  already provides authoritative state and HL hasn't exhibited the same
  false-naked-leg failure mode at scale.

### Failure semantics

`detect_existing_sl` never raises. On any internal error (missing creds,
venue 5xx, schema drift) it returns `{has_sl: False, source: "error"}`.
Callers treat that as **"unknown, proceed with placement"**, never as a
license to skip protection.

The asymmetry is deliberate:

> The cost of a false negative — a duplicate-placement retry — is much
> cheaper than the cost of a false positive — a real position left naked
> because the detector silently *claimed* a stop existed when it did not.

---

## Integration points

1. **`sl_queue_worker.process_one`** consults the detector **before** every
   placement attempt. On `has_sl=True`, it consumes the queue file, flips
   the manifest flag, and exits without touching the venue.
2. **`reconcile_existing_sls`** (CLI one-shot) walks every manifest entry
   with `has_stop_loss=false` and runs the same detection. Matching entries
   are flipped and their corresponding queue files are moved to `.done/`.
3. **(planned)** The position reconciler will call the detector at
   manifest-write time, so positions originate with the correct flag value
   and never need a back-fix.

---

## Observability

Every flip writes audit fields on the manifest entry:

```json
{
  "has_stop_loss": true,
  "auto_sl_status": "detected_on_venue",
  "auto_sl_detected_at": "2026-05-16T16:00:00Z",
  "auto_sl_detector_source": "grvt_open_orders",
  "auto_sl_detector_detail": "1 trigger(s): [...]"
}
```

This means every "we believe this position is protected" claim is
traceable to a specific venue query at a specific timestamp — no silent
assumptions, no inherited state from a prior reconciler run.

---

## Lessons baked in

- **Symmetric failure-mode design.** Defensive code that mirrors real
  failure-mode asymmetry (cost of FN vs FP) — and codifies that asymmetry
  in the API contract.
- **Detector-before-placement, not detector-instead-of-placement.** The
  queue worker still functions identically on confirmed naked positions;
  detection only short-circuits the verified-safe case.
- **Manifest as a cache, not as a source of truth.** Venue state always
  wins on conflict; manifest entries are reconciled against venue truth
  on every detection pass.
