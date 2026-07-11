# Layer-Stack Add-on — Requirements (living document)

Requirements gathered for a clean-room PBR layer-painting add-on for Blender.
Architecture input: [ucupaint-internals.md](ucupaint-internals.md) (esp. §7 steal/avoid, §8 clean-room sketch).

## Confirmed requirements (from the user)

### R1 — Configurable-channel strokes
A paint stroke must be routable to a configurable set of channels. Implementation
semantics (the Python-achievable form): a stroke paints the layer's shared
source/mask once; the layer declares, per channel, whether it participates and
what it deposits (value, color, or texture override). One gesture → multiple
channels affected. Per-channel toggles must be quick to flip mid-session.
True per-channel independent brush *image content* in a single stroke is a
Blender-core limitation (no multi-channel stroke API); out of scope for the
add-on, in scope for the upstream proposal.

### R2 — Emission channels
First-class paintable channels for Principled BSDF **Emission Color** and
**Emission Strength** (luminosity maps). Correct color space per channel
(color: sRGB/Filmic-managed; strength: Non-Color scalar).

### R3 — Subsurface scattering channels
First-class paintable channels for Blender's Principled subsurface model:
**Subsurface Weight** (scalar mask) and **Subsurface Radius** (per-RGB vector;
paintable as color) at minimum; Scale/IOR/Anisotropy as configurable extras.
Non-Color for scalars/vectors.

### R4 — UX is the product (standing requirement)
The user has used Ucupaint and rejects its UX outright. Design UX-first against
3DCoat/Substance Painter workflow expectations. Responsiveness is a hard
requirement — see A1.

## Architecture requirements (from the Ucupaint study)

- **A1 — Compiler, not callbacks**: layer stack is the source of truth; the node
  graph is a build artifact. Pure `compile → spec → reconcile` with minimal
  delta application, debounced. No full-tree rewalks on property edits.
- **A2 — Narrow per-channel chains**: one chain per channel; normal-from-height
  computed once at chain end. No inter-layer socket fan-out explosion.
- **A3 — Stable IDs** for layers/masks (no index-aligned lists, no parent_idx
  regex surgery on reorder).
- **A4 — State on the node tree** (`PointerProperty` on the ShaderNodeTree) for
  free .blend persistence and append/link portability; node references by
  lazily-resolved unique names (undo-safe).
- **A5 — Versioning from v0**: two-axis migration (add-on version × Blender
  version); revision-stamped generated node groups patched on load.
- **A6 — Channel registry, not hardcoded channels**: the channel set is data
  (name, socket target, color space, default blend, value type), so Emission,
  SSS, and future channels are entries, not code forks.

## Open items

- [x] User's Ucupaint grievance list — RESOLVED as non-specific (2026-07-11):
      dislike is real but details have faded ("weird and counterintuitive").
      Consequence: design from 3DCoat/Substance reference workflows and the
      user's live reactions to our own prototypes, not against a Ucupaint
      checklist.
- [ ] Reference workflow walkthroughs (3DCoat paint room / Substance layer
      semantics) to fix target UX vocabulary
- [ ] Mask types for v1 (paint, curvature, AO, cavity, edge)
- [ ] Bake-down/export pipeline scope for v1
- [ ] Performance budget: max layers × channels at interactive framerates on
      the user's hardware
