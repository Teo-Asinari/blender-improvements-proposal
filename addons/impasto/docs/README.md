# Impasto documentation

Impasto 0.14.1 documentation is organized by authority and lifecycle.

## Start here

- [Add-on README](../README.md) — installation, current features, workflow,
  limitations, and validation.
- [Roadmap](../ROADMAP.md) — authoritative open work.
- [Changelog](../CHANGELOG.md) — shipped release history.

## Current technical references

- [Stencil workflow](STENCIL_WORKFLOW.md) — projection, coverage, and
  grayscale-derived normal relief.
- [Emission and subsurface painting](EMISSION_SUBSURFACE_PAINT.md) — channel
  semantics and preview behavior.
- [High-resolution performance](HIGH_RESOLUTION_PERFORMANCE.md) — memory,
  latency estimates, and qualification requirements.
- [GPU IBL preview](GPU_IBL_PREVIEW.md) — current resident lighting model.
- [GPU preview acceptance contract](GPU_PREVIEW_REVIEW.md) — preview scope and
  numerical requirements.
- [GPU stack baseline](GPU_STACK_BASELINE.md) — stack-composition semantics and
  current same-UV boundary.

## Design background

- [GPU-resident redesign](GPU_RESIDENT_REDESIGN.md) — historical design
  rationale that led to the current resident engine. Some failure descriptions
  refer to older prototypes; use the add-on README for current behavior.

## Archive

- [Legacy combined progress log](archive/PROGRESS_LEGACY.md) — preserved
  milestone, plan, and session history; not authoritative.
- [0.7.0 session handoff](archive/SESSION_2026-07-13.md) — historical snapshot;
  not authoritative.
