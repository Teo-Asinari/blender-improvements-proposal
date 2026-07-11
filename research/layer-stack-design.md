# Layer-Stack Add-on — Design Document

**Status:** design for implementation. Supersedes nothing; consumes
[layer-stack-requirements.md](layer-stack-requirements.md) (R1–R4, A1–A6 are
binding), [ucupaint-internals.md](ucupaint-internals.md) (the autopsy; §7
steal/avoid list is binding), and
[../experiments/gpu_paint_spike/FINDINGS.md](../experiments/gpu_paint_spike/FINDINGS.md)
(measured GPU numbers, v0.2.0; the multi-channel v0.3.0 extension is in
progress and its numbers are marked *pending v0.3.0 measurement* below).

**Target platform:** Blender 5.1.2 (the version everything in this repo is
probed against). Python add-on, GPL-compatible, clean-room (no code derived
from any studied add-on; Ucupaint is cited as prior art under GPL-3.0+).

---

## 1. Vision & scope

**One sentence:** a layers panel for Blender materials — a non-destructive
PBR layer stack that compiles to shader nodes, painted with Blender's native
brush, with the responsiveness and legibility of a dedicated texturing tool.

The user story that must work end-to-end in v1:

> Select an unwrapped mesh → click *New Layer Stack* → pick the Principled
> template → get Base Color / Metallic / Roughness / Height / Emission / SSS
> channels correctly created and wired → add a fill layer (rust color, high
> roughness) → add a paint mask → paint the mask with the native brush and
> watch the rust appear on *every* participating channel at once → drop a
> paint layer on top for detail work → reorder, change blend modes, tweak
> opacities — all without touching the shader editor, ever, and without the
> UI hitching on every tweak.

### v1 IS (phases 1–3, the MVP line)

- **Layer stack**: paint layers, fill layers, groups (organizational — see
  §2.4), reorder, blend mode, opacity, visibility, solo.
- **Channel registry** (§3): data-driven channels including Emission
  Color/Strength (R2) and Subsurface Weight/Radius (R3); template materials;
  per-layer sparse channel participation with shared-mask semantics (R1).
- **The compiler** (§4): model → spec → reconcile with minimal deltas,
  debounced; the node graph is a build artifact, never the source of truth
  (A1).
- **Native-brush painting** (§5): select layer → paint, with automatic
  canvas switching and color-space guards. Native paint = native undo.
- **Paint masks** (§6): image masks painted the same way.

### v1 is explicitly NOT (decided, not deferred-by-vagueness)

- **GPU stroke engine** — phase 6. The spike proved the numbers (§8); v1
  paints with Blender's brush precisely so undo, color management, tablet
  handling, and brush options come for free while the stack matures.
- **Undo for GPU strokes** — a phase 6 productization item, not an MVP
  blocker, because MVP painting is native.
- **UDIM** — **out of v1, decided.** Ucupaint's tile-1001 disk-rename hack
  (autopsy §4) is a maintenance bomb. Re-evaluate at phase 5 by probing
  whether Blender 5.x exposes per-tile pixel access; until then the stack is
  single-tile per image.
- **True per-channel independent brush content in one stroke** — Blender-core
  limitation (no multi-channel stroke API); out of add-on scope, in scope for
  the upstream proposal (R1). The shared-source/shared-mask semantics below
  deliver the workflow value.
- **Painted tangent-space normal layers** — Height channel + one
  normal-from-height conversion at chain end covers v1 (A2). Direct normal
  painting is an open question for phase 5+ (§12).
- **Image atlases, adjustment/filter layers, anchor points, layer transforms
  on paint layers** (§5.4), Photoshop-perceptual blending mode (§4.8),
  vertex-color layers, non-Principled targets.

### Non-negotiable qualities (from R4 + the autopsy)

- No full-tree rebuild on property edits; slider drags never mutate the node
  graph (§4.6).
- Zero-delta reconciliation causes zero datablock mutation → no spurious
  EEVEE recompiles (autopsy §6, issue #122 is the cautionary tale).
- Every operator is discoverable three ways: sidebar panel, menu entry,
  F3 search (house rule from the shipped add-ons; never popover-only).

---

## 2. Data model

### 2.1 Where state lives (A4)

The root PropertyGroup is registered as a `PointerProperty` on
`bpy.types.ShaderNodeTree` and populated on the **generated root node
group's tree** — the single best structural decision in Ucupaint (autopsy
§7 steal #1):

```python
class StackState(bpy.types.PropertyGroup):
    is_stack: BoolProperty(default=False)      # marks "this tree is ours"
    schema_version: IntProperty(default=1)     # A5, from day one
    blender_version: IntVectorProperty(size=3) # stamped at creation/migration
    channels: CollectionProperty(type=ChannelState)
    layers:   CollectionProperty(type=LayerState)
    active_layer_uid: StringProperty()
    # NO halt/batch flags here — batching is runtime-only (§4.7)

bpy.types.ShaderNodeTree.impasto = PointerProperty(type=StackState)
```

Consequences (all inherited free):

- **Persistence**: PropertyGroups on an ID datablock serialize into the
  .blend automatically.
- **Append/link portability**: appending the root node group brings the
  whole stack; per-layer trees are referenced by group-node instances inside
  it, so they travel too. Images travel if packed (the *New Stack* operator
  offers pack-by-default).
- **Undo**: Blender's undo rolls PropertyGroups and node trees back
  together; because all node references are name-derived (§4.4) nothing
  dangles. Runtime caches (spec hashes, §4.5) are dropped in an `undo_post`
  handler and lazily rebuilt.
- Material-level state is minimal: a small `MaterialState` remembers only
  the Principled links we displaced, so removing the stack restores the
  material (Ucupaint's `ori_bsdf` pattern).

### 2.2 Stable IDs (A3)

Every user-visible entity (Layer, Mask, Channel) gets an immutable
`uid = secrets.token_hex(4)` at creation. Two rules make the whole
index-corruption class from the autopsy impossible:

1. **`PropertyGroup.name` IS the uid.** Collection items are keyed by
   `name`, so `layers["c3a91f02"]` works, and — critically — animation
   F-Curve `data_path`s written against these properties use the *string
   key* form (`impasto.layers["c3a91f02"].opacity`), which **survives
   reorder with zero rewriting**. No `remap_layer_fcurves` regex surgery,
   ever. The user-facing name is a separate `label: StringProperty`.
2. **Indices are presentation order only.** The `layers` collection order is
   the top-to-bottom display order (what `UILayout.template_list` wants);
   reorder = `layers.move(a, b)` and nothing else, because every
   cross-reference (`active_layer_uid`, `parent_uid`, bindings) is by uid.

### 2.3 Entities

```
StackState
 ├─ channels: {ChannelState}          # which registry channels this stack enables
 │    name = channel key (e.g. "base_color")   ← registry key, not a uid
 │    enabled, bake state, socket-override (advanced)
 └─ layers: [LayerState]              # flat, display-ordered
      name = uid
      label, layer_type ∈ {PAINT, FILL, GROUP}
      parent_uid: StringProperty()    # "" = root; GROUP hierarchy
      visible, opacity, blend_mode    # layer-wide defaults
      image_name: StringProperty()    # PAINT: the canvas Image (by name, lazy)
      uv_map: StringProperty()
      bindings: {ChannelBinding}      # SPARSE — only channels this layer touches
      masks: [MaskState]
```

```
ChannelBinding                        # per-layer-per-channel (R1)
   name = channel key
   enabled: BoolProperty              # quick mid-session toggle (R1) — UNIFORM-class
   mode ∈ {SHARED, VALUE, COLOR}      # SHARED: consume the layer's source
                                      # VALUE/COLOR: this channel deposits a constant
                                      #   (FILL layers are all-VALUE/COLOR by nature;
                                      #    on PAINT layers this is the "override")
   value: FloatProperty / color: FloatVectorProperty
   blend_mode, opacity                # per-channel, defaulting from layer + registry
   use_masks: BoolProperty(default=True)   # mask gating per channel (R1)
```

```
MaskState
   name = uid
   label, mask_type ∈ {IMAGE}         # phase 4 adds AO, CURVATURE, CAVITY, EDGE
   image_name, uv_map
   blend ∈ {MULTIPLY, ADD, SUBTRACT}, invert, opacity, visible
   bake_info: (phase 4) provenance for re-bake — generator params, source objects
```

**Sparseness is the point** (avoid-list #1): a layer that only touches Base
Color and Roughness has exactly two bindings. Adding a channel to the stack
touches *no* layer. There is no index alignment between `stack.channels` and
`layer.bindings` — lookups are by key — so Ucupaint's
`YFixChannelMissmatch` corruption class cannot exist.

**R1 semantics, restated concretely:** one layer has one source (its paint
image, or nothing for FILL) and one mask chain. Each binding decides whether
that channel participates (`enabled`), what it deposits (the shared source,
or a per-channel constant), how it blends, and whether the shared masks gate
it (`use_masks`). One painted gesture into the layer's image or mask
therefore lands on every enabled channel simultaneously — structurally, not
faked. Per-mask-per-channel gating (Ucupaint's finer grain) is deliberately
collapsed to per-binding `use_masks` for v1; revisit only on user demand.

### 2.4 Groups — schema now, cheap semantics first

`parent_uid` hierarchy exists in the schema from day one (retrofitting it is
a migration; A5 says pay early). Phase 1 compiler semantics are
deliberately **pass-through**: a group contributes no sub-chain; its
visibility, opacity, and masks multiply into each descendant's effective
blend factor (§4.3). That is exactly Photoshop's pass-through group and
costs the compiler ~10 lines. Isolated-group blending (composite the group,
then blend once) is a later, purely-compiler feature — the data model
already supports it.

### 2.5 Versioning from v0 (A5)

- `schema_version` + `blender_version` stamped on every stack tree.
- A `load_post` migration runner executes ordered, gated migration blocks
  `(from_schema, blender_range) → fn(stack)` and re-stamps. Shipped in v0
  with zero migrations registered — the machinery must exist before the
  first regret.
- Any bundled utility node groups (phase 5's channel-pack helpers, etc.)
  carry a `revision` int in a metadata node and are hot-patched on load,
  Ucupaint-style (autopsy §7 steal #4). V1 generates all nodes directly and
  bundles no lib .blend, which keeps this dormant but designed-for.

---

## 3. Channel registry (A6)

Channels are **data, not code**. The registry is a frozen module-level table
(pure Python, importable without `bpy` for tests):

```python
@dataclass(frozen=True)
class ChannelDef:
    key: str                 # stable identifier, snake_case, never renamed
    label: str
    socket: str              # Principled BSDF input name, e.g. "Emission Color"
    kind: str                # 'COLOR' | 'SCALAR' | 'VECTOR'
    colorspace: str          # 'sRGB' | 'Non-Color'  (resolved via RNA probe, §5.3)
    default_value: tuple     # chain seed = the Principled default
    default_blend: str       # 'MIX' | 'MULTIPLY' | ...
    panel_group: str         # UI grouping: 'Core', 'Emission', 'Subsurface', ...
```

### 3.1 The standard set

| key | socket | kind | colorspace | default blend | panel group |
|---|---|---|---|---|---|
| `base_color` | Base Color | COLOR | sRGB | MIX | Core |
| `metallic` | Metallic | SCALAR | Non-Color | MIX | Core |
| `roughness` | Roughness | SCALAR | Non-Color | MIX | Core |
| `height` | *(→ Bump → Normal)* | SCALAR | Non-Color | MIX | Core |
| `alpha` | Alpha | SCALAR | Non-Color | MIX | Core |
| `emission_color` | Emission Color | COLOR | sRGB | MIX | Emission *(R2)* |
| `emission_strength` | Emission Strength | SCALAR | Non-Color | MIX | Emission *(R2)* |
| `sss_weight` | Subsurface Weight | SCALAR | Non-Color | MIX | Subsurface *(R3)* |
| `sss_radius` | Subsurface Radius | VECTOR | Non-Color | MIX | Subsurface *(R3)* |
| `sss_scale` | Subsurface Scale | SCALAR | Non-Color | MIX | Subsurface (extra) |
| `sss_ior` | Subsurface IOR | SCALAR | Non-Color | MIX | Subsurface (extra) |
| `sss_anisotropy` | Subsurface Anisotropy | SCALAR | Non-Color | MIX | Subsurface (extra) |

Notes:

- `height` is the one special channel: its chain output feeds a single
  `ShaderNodeBump` at chain end → Principled *Normal* (A2). No per-layer
  height plumbing, no directional fan-out, ever.
- `emission_color` is painted under color management like Base Color;
  `emission_strength` is a raw scalar — this is exactly the R2 split.
- `sss_radius` is a *distance* vector: **painted as color, stored
  Non-Color** (a red-dominant radius is authored by painting red, but the
  values are metric, not perceptual). The paint-canvas guard (§5.3) knows
  this per the registry, not per a hardcoded exception.
- Custom user channels (arbitrary socket targets) are a natural registry
  extension — post-v1, no schema change needed since `ChannelState.name` is
  just a key and an advanced socket-override field already exists.

### 3.2 Templates

A template is nothing but a named list of channel keys:

```python
TEMPLATES = {
  "Principled — Standard":  ["base_color", "metallic", "roughness", "height"],
  "Principled — Full":      [... all Core + Emission + Subsurface weight/radius],
  "Emissive prop":          ["base_color", "roughness", "emission_color", "emission_strength"],
  "Skin / organic":         ["base_color", "roughness", "height", "sss_weight", "sss_radius"],
}
```

*New Layer Stack* takes a template; channels can be added/removed later from
the stack's channel manager UI (adding = one `ChannelState` + one compile;
no layer is touched, per §2.3).

### 3.3 Per-layer participation defaults

- New **paint layer**: binds `base_color` only (SHARED). Adding channels is
  one click per chip in the layer's channel row (§9).
- New **fill layer**: the *add* operator asks nothing; it creates the layer
  with a `base_color` COLOR binding and the channel chips immediately allow
  enabling roughness-with-value, etc. (The Substance "fill layer + black
  mask" workflow is: fill layer → add mask → invert → paint the mask.)
- Binding `enabled` toggles are UNIFORM-class (§4.6) — flipping them
  mid-session is instant, satisfying R1's "quick to flip".

---

## 4. The compiler (the long pole)

This section is the build spec for phase 1. Everything else layers on it.

### 4.1 Shape

```
PropertyGroups ──snapshot()──▶ StackModel (plain dataclasses, no bpy)
StackModel ──compile(model, registry)──▶ GraphSpec (pure function)
GraphSpec ──reconcile(spec, trees)──▶ minimal bpy mutations (idempotent)
```

Three modules with a hard dependency rule:

| module | imports bpy? | contents |
|---|---|---|
| `model.py` | no | dataclasses for StackModel + GraphSpec, registry, `compile()` |
| `snapshot.py` | yes | PropertyGroups → StackModel (read-only walk) |
| `reconcile.py` | yes | GraphSpec → node-tree deltas; the only writer of node trees |

`compile()` never sees bpy; `reconcile()` never sees the stack model. The
seam between them — GraphSpec — is JSON-serializable, which is the entire
testing story (§10).

### 4.2 GraphSpec format

```python
@dataclass(frozen=True)
class NodeSpec:
    name: str                      # deterministic, §4.4
    bl_idname: str                 # e.g. "ShaderNodeMix"
    props: tuple[tuple[str, Any], ...]    # node RNA props: blend_type, data_type, ...
    inputs: tuple[tuple[str, Any], ...]   # default_values for UNLINKED inputs,
                                          #   keyed by socket identifier

@dataclass(frozen=True)
class LinkSpec:
    src: tuple[str, str]           # (node name, output socket identifier)
    dst: tuple[str, str]           # (node name, input socket identifier)

@dataclass(frozen=True)
class SocketSpec:                  # tree interface item
    name: str; in_out: str; socket_type: str

@dataclass(frozen=True)
class TreeSpec:
    key: str                       # "root" | "material" | layer uid
    interface: tuple[SocketSpec, ...]
    nodes: tuple[NodeSpec, ...]
    links: tuple[LinkSpec, ...]

@dataclass(frozen=True)
class GraphSpec:
    trees: tuple[TreeSpec, ...]    # deterministic order
```

All tuples, all frozen, deterministically ordered → a spec has a stable
`sha1(json.dumps(spec, sort_keys=True))` per tree. That hash is the
incrementality mechanism (§4.5).

Socket identifiers are RNA `socket.identifier` strings (stable across
Blender's socket renames in a given version), not indices.

### 4.3 What compile() emits

**Per-layer tree** (`key = layer.uid`, realized as node group
`.Impasto Layer <uid>`) — only for layers that need one (PAINT layers and
any layer with masks; a bare FILL layer with constant bindings compiles to
*no* layer tree, its constants go straight into root-chain blend inputs):

```
[UV Map] → [Image Texture (paint image)] ──┐
                                           ├─ outputs: one per SHARED binding:
                    (per-binding taps) ────┘    "ch:<key>"  (color/value)
[mask0 img] → [invert?] ─┐
[mask1 img] → ...        ├─ [multiply chain] → output "mask"  (single scalar)
                         ┘
```

Interface width per layer = (#SHARED bindings) + 1 mask socket. A layer
touching three channels has four outputs. There is no per-channel alpha, no
height fan-out, no ` Group`-suffixed re-export — this is A2's narrow
protocol and the direct fix for the autopsy's ">20 sockets per layer"
multiplier.

**Root tree** (`key = "root"`, the tree carrying `impasto`) — one **linear
chain per enabled channel**:

```
for channel C:
  seed = C.default_value (a Value/RGB node, or Group Input if exposed)
  for layer L bottom→top where binding(L, C).enabled:
      blend_L = ShaderNodeMix(data_type='RGBA', blend_type=binding.blend_mode,
                              clamp_result=False)
      inputs:  A = previous chain value
               B = layer-group output "ch:C"     (SHARED)
                   | constant color/value        (VALUE/COLOR — spec input, no node)
               Factor = effective_factor(L, C)   (§ below)
      chain = blend_L
  if C is height:  chain → ShaderNodeBump → group output "Normal"
  else:            chain → group output C.label
```

`effective_factor(L, C)` is computed as a small factor sub-expression:
`layer.visible? × layer.opacity × binding.opacity ×
(mask_output if binding.use_masks and L has masks else 1) ×
(product over ancestors G of: G.visible? × G.opacity × G.mask)` — the
pass-through group semantics of §2.4. When everything is constant it folds
into the Factor socket's `default_value` (a uniform, no extra nodes); a
Math-multiply node is emitted only when a mask output participates.
Visibility contributes as a `0.0/1.0` constant *inside that fold* — which is
what makes the eye-icon a uniform update (§4.6).

Scalar and vector channels also use `ShaderNodeMix(data_type='RGBA')` so all
blend modes work uniformly on grayscale; one node type everywhere keeps the
reconciler and the golden files boring (a virtue).

**Material tree** (`key = "material"`): the group-node instance, links from
group outputs to Principled sockets (per registry `socket`), the Bump-fed
Normal link, and restoration bookkeeping for displaced original links.
Compiler-managed like everything else, so a user deleting a link gets it
self-healed on the next reconcile.

### 4.4 Node naming — the role table (kills the StringProperty shrapnel)

Every machine-owned node is named deterministically:

```
"ps:<entity-uid-or-root>:<role>"
e.g.  ps:c3a91f02:src        the layer's image node
      ps:c3a91f02:mask.9be1d1c4:src
      ps:root:ch.roughness:blend.c3a91f02
      ps:root:ch.height:bump
```

- Resolution is `tree.nodes.get(name)` — lazy, undo-safe (steal #2) — via
  one helper. **Zero `StringProperty` node-name slots anywhere** (avoid #2):
  the name *is derivable* from (uid, role), so the schema never grows a slot
  per feature.
- The `ps:` prefix marks machine ownership: the reconciler deletes unknown
  `ps:` nodes and never touches anything else. Generated trees get a frame
  node label: *"Generated by <addon> — do not edit; changes will be
  reconciled away."*
- Uids are unique per stack, so no random-suffix disambiguation is needed;
  Blender's per-tree name uniqueness is satisfied by construction.

### 4.5 reconcile(spec, tree) — the only graph writer

Per TreeSpec, in order; **every step compares before writing** so a no-op
pass performs zero datablock mutations (the EEVEE-recompile guard, steal
#3):

1. **Hash gate.** `if cached_hash(tree) == spec_hash: return 0 deltas` — the
   common case for undirtied trees. Caches live in a module dict keyed by
   tree name; dropped on `undo_post` / `load_post`.
2. **Interface diff** (layer trees): compare `tree.interface.items_tree` to
   `spec.interface` as ordered (name, in_out, socket_type) lists; add /
   remove / retype only mismatches. Interface edits re-sync every group
   instance — this is the most expensive mutation class, and narrow
   interfaces (§4.3) keep it rare and small. (5.1-alpha landmine from the
   autopsy: do not set socket *subtype* on creation.)
3. **Node diff** by name: desired map vs `{n for n in tree.nodes if
   n.name.startswith("ps:")}`. Remove extras; create missing; wrong
   `bl_idname` → delete + recreate. For each node, set `props` and unlinked
   `inputs` default_values **only if `!=` current** (floats compared with
   `isclose`; we wrote them, so drift means user/undo interference and gets
   repaired).
4. **Link diff**: desired set of `((src_node, src_sock), (dst_node,
   dst_sock))` vs actual links among `ps:` nodes; remove non-desired, add
   missing. Existing-link check before create (steal #3).
5. **Layout**: only newly created nodes get positions (computed in the spec
   as a `props` entry from chain index — cheap, deterministic). **No
   rearrange pass exists** (avoid #5). The graph is machine-owned; cosmetic
   layout of old nodes is nobody's job.
6. Update the hash cache; return a delta count (logged behind a debug pref —
   the autopsy's 71 timing sites institutionalized properly).

Idempotence and self-healing fall out: reconcile twice → second pass is
zero-delta; user tampers with the tree → next reconcile repairs it. There is
exactly one *Rebuild Stack* operator (drops hash caches, full reconcile) as
the user-facing repair story — not a zoo of fix operators.

### 4.6 Trigger classification: UNIFORM vs STRUCTURAL

Every property `update=` callback does exactly one of two cheap things.
**No callback ever mutates the graph directly** (A1).

| class | examples | action |
|---|---|---|
| **UNIFORM** | layer/binding opacity, fill value/color, binding `enabled`\*, layer `visible`, mask opacity/invert-strength, solo | write the (already-linked or default_value) socket via role lookup, immediately — `default_value` writes update shader uniforms without an EEVEE recompile — then mark the model dirty for the debounced compile |
| **STRUCTURAL** | add/remove/reorder layer or mask or channel, blend_mode change, binding mode change (SHARED↔VALUE), mask add, uv_map change, group re-parent | mark dirty; debounced compile+reconcile does the work |

\* `binding.enabled` and `layer.visible` are the interesting ones. The
compiler *includes* disabled-but-present participants with factor 0 for a
grace period, so toggling is an instant uniform write (factor → 0). A
**second, longer debounce** (default 3 s idle) recompiles with the
disabled participants *pruned*, slimming the shader. Result: scrubbing eye
icons is instant and recompile-free; the graph converges to minimal when the
user pauses. (This is the fix for Ucupaint's abandoned
`disable_quick_toggle` / trash-group scars.)

Uniform writes stay consistent with the compiler by construction: the
debounced compile re-emits the same value into the spec, the hash matches,
reconcile is a no-op.

**Debounce mechanics:** a module-level dirty set + `bpy.app.timers` one-shot
at 100 ms (structural) / 3 s (prune). Timer fires → snapshot → compile →
reconcile dirty trees → clear. Slider drags mark dirty repeatedly; the timer
coalesces. This is precisely the debounce Ucupaint users asked for in issue
#122 and could never get because their callbacks mutate the graph directly.

### 4.7 Batching

Bulk operators (add layer with mask, template creation, migrations) wrap
work in a context manager:

```python
with stack_edit_session():   # module-level runtime flag, NOT a PropertyGroup
    ...many model edits...
# __exit__ runs one compile+reconcile, even on exception
```

Runtime-only + context-managed fixes Ucupaint's persisted-stuck-flag failure
mode (steal #8's caveat) — an exception can't leave the .blend wedged.

### 4.8 Color pipeline (decided up front — the autopsy's most-regretted area)

- **Blending happens in scene-linear**, full stop. COLOR-kind images are
  created sRGB; Blender's image node converts to linear at sample time; the
  chain blends linear; Principled receives linear. Multiply/Screen/etc. are
  therefore *render-correct*, and may differ from Photoshop's
  display-referred results. This is documented user-facing behavior, not a
  toggle, in v1. A perceptual-blend option, if ever demanded, arrives as a
  versioned migration-aware feature — never a silent change (two of
  Ucupaint's shipped migrations exist to fix exactly this regret).
- **No manual gamma/linearize nodes** in the graph. Color space is carried
  by the image datablock and guarded at paint time (§5.3).
- Color space *names* are resolved by RNA enum probe with prefix-match
  fallback (steal #5), never hardcoded literals.

### 4.9 Failure & repair story

- Reconcile wraps per-tree application; an exception logs, leaves the hash
  cache invalidated (so the next pass retries), and reports one actionable
  error in the UI header — never a half-persisted flag.
- Missing image datablock (user deleted it): layer chip shows a warning
  icon; compile substitutes the channel default so the material stays valid;
  *Rebuild* or re-assign fixes.
- A `load_post` pass runs migrations (§2.5) then one full reconcile —
  self-healing anything an older version or a manual edit left behind.

### 4.10 Cost model (why this meets A1)

- compile(): pure Python over a few hundred dataclass objects — microseconds
  to low milliseconds even for large stacks; always run whole-stack (no
  dirty-scope bookkeeping bugs), with per-tree hash gates making
  *application* incremental.
- reconcile() on a typical edit touches one layer tree + one channel chain
  segment: a handful of node/link mutations → one EEVEE recompile, which is
  the irreducible cost of a structural change.
- Slider drags / toggles: zero graph mutations → zero recompiles → the
  viewport hitch that defines Ucupaint's feel (issue #122) structurally
  cannot happen.

---

## 5. Painting v1 — native brush into the active layer

### 5.1 Flow

Select layer (click in the stack list) → the add-on makes that layer's image
the active paint canvas → paint in the viewport with Blender's Texture Paint
tools. That's the whole user-visible story; everything below is making it
never mis-fire.

### 5.2 Canvas switching

- One function, `activate_paint_target(entity)`, is the only code that
  touches paint state (Ucupaint's `set_active_paint_slot_entity` is the
  pattern; ours is smaller). It sets
  `tool_settings.image_paint.mode = 'IMAGE'` and
  `tool_settings.image_paint.canvas = image` — canvas mode deliberately
  bypasses the material's paint-slot list, so we never fight slot inference.
  (The 5.1 paint-slot mismatch hack from the autopsy is retired upstream; we
  target 5.1.2 and never enter the slot system anyway.)
- Triggers: active-layer change, mask-edit toggle (§6), entering Texture
  Paint mode with a stack present.
- The layer's `uv_map` is made the active UV layer on the mesh at the same
  moment, so strokes land where the shader samples.

### 5.3 Color-space guards

Image creation is registry-driven: `new_layer_image(channel_kind)` sets
size (stack default, 2048 initially), colorspace per `ChannelDef`
(sRGB / Non-Color via RNA-probed names), alpha, float depth (8-bit for
COLOR, 8-bit for masks, float toggle per stack for height). On every canvas
activation the guard re-checks `image.colorspace_settings.name` against the
registry and repairs (with an info report) if the user changed it — silent
sRGB-crushed scalars are the classic failure Kiln's README complains
about, and the same one-table discipline prevents it here.

### 5.4 WYSIWYG rule

Paint layers have **no mapping transforms** in v1 (fill layers may gain
procedural mapping later). This is a deliberate scope cut: transformed paint
layers force Ucupaint's temp-UV-refresh nag (autopsy §3), the worst kind of
UX debt. A paint layer's image is always sampled through the plain UV map
it was painted in — the stroke you make is the texels you get.

### 5.5 Undo

Native brush = native paint undo, untouched. Stack operations are standard
operators with `REGISTER | UNDO`. The two undo systems interleave the way
Blender users already expect (paint stroke undo vs operator undo); we add
no handler-based undo magic in v1. Known Blender-side weirdness with undo
depth outside Object Mode (autopsy issue #252/#342) is documented as a
limitation, not fought.

### 5.6 Multi-channel effect of one stroke

Because participation is per-binding on the *layer* (R1), painting the
layer's single image immediately affects every channel whose binding is
SHARED — the shader samples one image into several chains. Painting a
*mask* affects every channel with `use_masks` on. No stroke plumbing is
involved; it's pure graph topology, which is why it works with the native
brush today and with the GPU engine later without model changes.

---

## 6. Masks

### 6.1 v1: paint masks

- `Add Mask` on a layer creates a grayscale Non-Color image (white or black
  per a button choice — "reveal all" / "hide all"), appends a `MaskState`,
  compiles (one multiply into the layer's mask chain).
- **Mask edit toggle**: clicking the mask thumbnail (or a chip) sets the
  paint canvas to the mask image; the layer row shows a clear
  "painting mask" indicator; clicking the layer thumbnail switches back.
  The active-canvas indicator must be *always visible* — losing track of
  "am I painting the layer or the mask?" is a top texturing-tool
  frustration.
- Mask ops: invert, fill black/white, duplicate, per-mask opacity/blend
  (MULTIPLY default; ADD/SUBTRACT for combining), visibility.

### 6.2 Phase 4: generator masks via the bake machinery

AO / curvature / cavity / edge masks are **bakes into mask images**, not
live node chains (avoid #6 — in-graph feature maximalism is a permanent
compile tax; issue #361's 66k-line shader is the tombstone). Reuse of
Kiln's proven patterns:

- Settings snapshot/restore in a `finally` (Kiln does engine+selection;
  we extend with the autopsy §4 book/restore checklist: samples=1, denoise
  off, modifiers, material_override…).
- Emit-bake through a temp emission node for arbitrary quantities
  (curvature via geometry/bevel setups, AO via AO node), `type='EMIT'`,
  Cycles, CPU-retry-on-GPU-failure wrapper.
- One `BAKE_TYPES`-style table drives naming, colorspace, node wiring — the
  exact extension seam Kiln's README already advertises.
- **Provenance on the image** (`MaskState.bake_info`): generator type +
  params, so *Re-bake* and *Re-bake All Masks* are one click after mesh
  edits. Generator masks are thus cheap "smart masks": bake once, paint on
  top (a paint mask above a generator mask in the same layer's chain), rerun
  when the mesh changes.
- Multi-object/UDIM/multi-material bake complications: out of scope until
  phase 5; single mesh, active material, like Kiln v1.

---

## 7. Bake-down & export (phase 5 — sketch only)

- **Flatten**: per enabled channel, emit-bake the root chain's channel
  output to a flat image (temp emission on a copy of the material — the
  autopsy §4 skeleton wholesale, including the trap checklist: use_clear
  semantics, margin, depsgraph-handler halt, GPU→CPU retry). Normal channel:
  bake the *post-Bump* normal via a temp Principled + `type='NORMAL'`.
- **`use_baked` display toggle**: reconcile can wire group outputs from
  baked images instead of live chains (a compiler mode flag → one
  structural compile). This is the pressure-release valve for very deep
  stacks in heavy scenes.
- **Channel packing**: numpy compositing of baked grayscale channels into
  RGBA (e.g. occlusion-roughness-metallic layouts); pack specs are data
  (`{"r": "ao", "g": "roughness", "b": "metallic"}`).
- **Export presets** as data: a preset = list of (pack spec | single
  channel, filename pattern, colorspace, bit depth, format). Ship generic
  presets ("glTF-style ORM", "Unity-style mask map", "Separate 8-bit PNGs")
  with neutral, factual naming.
- Explicitly re-probe UDIM per-tile pixel access on the then-current Blender
  before any UDIM commitment (§1).

---

## 8. GPU stroke engine integration (phase 6)

The spike (v0.2.0, measured 2026-07-11 on a Quadro RTX 5000 Max-Q, OpenGL,
4096²) settles the feasibility questions:

- **Dab cost: 0.02–0.03 ms/dab** submission at 4K steady state (~100× under
  the 2 ms target); 203 dabs/s sustained over a 6.6 s scribble,
  event-rate-bound, not GPU-bound; stroke-end drain ≈ 3 ms.
- **Stroke-end sync-back: ~158–174 ms at 4K** via
  `fb.read_color(..., data=Buffer-wrapping-numpy)` (probe
  `fb_read_into_numpy_buffer=yes`) + `pixels.foreach_set` (~70 ms of that).
  ~90 ms raw GPU→CPU transfer + ~70 ms `image.pixels` write are irreducible
  from Python.
- **`gpu.types.Buffer` has no numpy bridge** (probe
  `buffer_to_numpy_path=to_list_fallback`) — the direct-read path is the
  *only* viable bulk readback on 5.1; this is the citable API gap for the
  upstream pitch.

### How it slots in

The engine paints **the same images the stack already owns** — the layer's
canvas or a mask — so the data model, compiler, and shader graph are
untouched by the engine's existence. Integration surface is exactly
`activate_paint_target()` (§5.2) plus a modal operator that replaces the
native brush when the user opts in.

- **Multi-channel dabs (MRT)**: one dab pass writing several attachments
  (layer image + per-channel override images) with per-attachment
  color/value — the v0.3.0 extension currently in progress. Per-dab and
  sync-back costs for N attachments: *pending v0.3.0 measurement*. The
  design assumption to validate: submission stays event-rate-bound and
  sync-back scales ~linearly with attachment count (readback + foreach_set
  per image).
- **Region sync**: track the stroke's dab-union bbox in UV space; read back
  and `foreach_set` only that scissored region. At the measured ~5 ms/Mpx
  write and ~6 ms/Mpx read, a typical partial-canvas stroke syncs in tens of
  ms — worth doing before productization, trivial with
  `fb.read_color(x, y, w, h, ...)`.
- **Productization requirements** (the honest gap list, from FINDINGS
  limitations): undo (snapshot the affected region before the stroke, push a
  restore step — bounded memory, unlike full-canvas history), color
  management (brush color → storage space conversion per registry
  colorspace), seam padding/dilation post-pass, brush params
  (size/hardness/pressure curves at parity with the spike's set, plus
  spacing/falloff options), per-dab mesh-rasterization cost mitigation
  (UV-bbox scissor) for dense meshes, and the occlusion-epsilon refinement
  (view-space depth compare).
- **Fallback posture**: the native brush path (§5) remains fully supported;
  the GPU engine is an opt-in acceleration, so a driver/backend probe
  failure degrades to a working tool, not a broken one.

---

## 9. UX blueprint (R4 — the differentiator)

Design references: Substance Painter's layer semantics (fill layers +
masks + per-channel participation) and 3DCoat's paint-room directness
(pick a thing, paint, see it everywhere). The user's Ucupaint rejection is
non-specific, so the standard is *those* workflows plus his live reactions
to our prototypes — ship small, watch, iterate (phase 7).

### 9.1 Principles

1. **The stack is one glance.** Layer name, thumbnail, visibility, blend,
   opacity, and *which channels it touches* readable without expanding
   anything.
2. **Two clicks max** from "I want rust on the top edges" to painting it.
3. **Never lose the canvas.** What you are currently painting (layer X /
   mask of layer X) is permanently indicated, in the panel *and* viewport
   header text.
4. **No shader editor, ever.** Any workflow that ends "…then open the node
   editor" is a design bug.
5. Quiet visuals: standard Blender widgets, no icon spam, single-purpose
   rows, alignment over decoration. Refined/minimal, per the user's taste.

### 9.2 Panel layout (N-panel, tab named after the add-on)

```
┌─ [AddonName] ────────────────────────────┐
│ Stack: MyMaterial            [⚙ manage]  │   ⚙ → channels manager
│ Channels: [C][M][R][H][E][S]   Solo: [–] │   stack-level chips + channel solo
├──────────────────────────────────────────┤
│ [+ Paint] [+ Fill] [+ Group]  [🗑] [▲][▼] │   add / delete / move
│ ┌──────────────────────────────────────┐ │
│ │ 👁 ▣ Scratches         CR···   ⛆ 75% │ │   ▣=thumb, CR = channel chips,
│ │ 👁 ▣ Rust fill  [M]    CRM··   ⛆100% │◀── active; [M]=has mask,
│ │     └ mask ▣  (painting mask ✎)      │ │   expanded mask row when active
│ │ 👁 ▸ Base group                      │ │   ▸ collapsed group
│ └──────────────────────────────────────┘ │
├─ Active layer: Rust fill ────────────────┤
│ Blend [Mix ▾]   Opacity [====○ 100%]     │
│ Channels                                 │
│  ☑ Base Color   (color ▣)  Mix    100%  │   per-binding rows: enable chip,
│  ☑ Roughness    (0.85   )  Mix    100%  │   payload (SHARED/value/color),
│  ☐ Metallic                              │   blend + opacity, mask-gate
│  ☐ Emission Color   ☐ Emission Strength │
│  ▸ Subsurface                            │   registry panel_group collapse
│ Masks                          [+ Mask]  │
│  👁 Paint mask   Multiply  [✎ edit]      │
└──────────────────────────────────────────┘
```

- **Channel chips** on each layer row are the R1 surface: tiny toggle
  letters (C/M/R/H/E/S from registry labels) — click to flip participation
  instantly (UNIFORM-class, §4.6). Chips of non-participating channels
  render dim, not hidden, so the affordance is discoverable.
- **Channel solo** (stack header): temporarily views one channel as
  emission on the mesh — the isolation workflow for judging a roughness
  paint job. Implemented as a preview wire (one structural compile in, one
  out); Esc or clicking [–] restores.
- **Layer solo**: Ctrl-click a layer's eye (uniform factors, instant).
- Reorder: UIList + move buttons and standard drag handle behavior; group
  membership by drag-into / explicit "move into group" (Blender UILists
  don't do free drag-drop — accepted; keyboard `Ctrl+Up/Down` bindings on
  the move operators help).

### 9.3 Where things live

- **N-panel tab** (as sketched) in *both* 3D Viewport (Object + Texture
  Paint modes) and, later, Image Editor — the panel is the product.
- **Menus**: all operators under a submenu in the Texture Paint *Paint* menu
  and Object *Material* context, entries prefixed with the add-on name so F3
  substring search finds them (house rule, proven by Kiln/overlay).
- **A dedicated workspace** ("Painting" tab: viewport + image editor +
  sidebar pre-opened) ships in phase 7 as an *operator that creates it*, not
  a hard default.
- **Never popover-only** — every popover control has a panel/menu twin.

### 9.4 Core interaction inventory (v1 acceptance surface)

add paint/fill/group layer · delete · duplicate · reorder/reparent ·
rename (double-click) · eye/solo · blend+opacity (layer, binding) · channel
chip toggle · binding payload edit (value/color swatch inline) · add/edit/
invert/remove mask · mask-edit toggle with visible mode indicator · channel
solo preview · stack create-from-template · channel add/remove · rebuild.

Each maps to one operator with tooltip, menu entry, and headless test.

---

## 10. Testing strategy

The architecture was chosen to make the hard part testable without Blender:
`compile()` is a pure function over dataclasses.

### 10.1 Pure-logic tests (pytest, no bpy, CI-fast)

- **Golden specs**: canonical models (single fill layer; paint+mask;
  3-layer multi-channel with group; every template) → committed
  `tests/golden/*.json` GraphSpecs. Diffs in review show *exactly* what a
  compiler change does to generated graphs.
- **Property-based invariants** (hypothesis if available, else seeded
  random):
  - determinism: same model → byte-identical spec;
  - locality: reordering layers changes only the root TreeSpec hash — every
    layer TreeSpec hash is unchanged;
  - toggle equivalence: binding disabled ≡ participant absent (post-prune
    spec) for the affected chain;
  - group pass-through: group(opacity a) ⊃ layer(opacity b) folds to factor
    a·b;
  - uid stability: any sequence of model ops never changes an existing uid,
    and spec node names referencing surviving uids persist.
- Registry sanity: keys unique, sockets exist in a pinned Principled socket
  list, colorspace ∈ {sRGB, Non-Color}, every template key resolves.

### 10.2 Headless suite (house style: real binary, sentinel greps)

Per the shipped add-ons' pattern (`run_tests.sh`, per-file sentinels like
`COMPILER_TESTS_PASSED`, wrapper greps because Blender exits 0 on script
exceptions):

- **Reconciler**: apply golden specs to real ShaderNodeTrees; assert node/
  link/interface topology; apply twice → **zero deltas** on the second pass
  (the recompile-avoidance contract, asserted, not hoped); tamper (delete a
  node, add a rogue link) → reconcile repairs; wrong-idname replacement.
- **Model lifecycle**: create stack from template on a real material;
  Principled sockets driven; save/reload round-trip preserves uids, order,
  bindings (the Scene-vs-WindowManager lesson, asserted); append the group
  from another .blend → stack intact; undo across an operator → lazy refs
  resolve, hash caches rebuild.
- **Painting glue**: canvas switching sets image+UV; colorspace guard
  repairs a tampered image; new-image parameters per registry.
- **Register lifecycle**: register/unregister/re-register, menu entries
  present, F3-discoverable operator labels prefixed.
- Phase 4 adds a real headless CPU emit-bake test (Kiln proved headless
  Cycles bakes work on 5.1.2 — reuse that harness shape and its
  statistical-assertion trick).

### 10.3 GUI checklist (per-release manual pass, README-style)

Responsiveness feel (slider drag with a 10-layer stack in Material Preview —
no hitching), eye-toggle latency, EEVEE recompile count sanity (debug
delta log), paint-lands-where-clicked per channel, mask-edit indicator,
channel solo round-trip, UIList reorder ergonomics, undo interleaving
(paint stroke vs stack op).

---

## 11. Phased roadmap

Phases as agreed; estimates in agent-sessions (one session ≈ one focused
implementation-agent run with tests green at the end).

| # | Phase | Deliverable | Acceptance criteria | Est. |
|---|---|---|---|---|
| 1 | **Core stack + compiler** | model/snapshot/compile/reconcile modules; Stack/Layer/Binding/Mask schema (masks compiled, UI later); fill layers; groups (pass-through); debounce; base_color+roughness hardcoded-registry stub; minimal panel (list + add/remove/reorder/opacity/blend) | golden + invariant suites green; headless: zero-delta re-apply, tamper-repair, save/reload, undo; slider drag mutates zero nodes (asserted via delta log) | 5 |
| 2 | **Channel registry** | full registry incl. Emission + SSS (R2/R3); templates; channel manager UI; binding rows + chips; channel solo | template creates correctly-wired Principled channels with right colorspaces; chip toggle is uniform-class (no recompile); registry sanity tests | 2 |
| 3 | **Native paint** | paint layers + paint masks end-to-end: image creation, canvas switching, colorspace guards, mask-edit toggle, WYSIWYG rule | GUI: select-layer→paint→visible on all bound channels; mask painting gates all `use_masks` bindings; guards repair tampered colorspace (headless) | 3 |
| | | | **← MVP line** | **10** |
| 4 | **Smart masks** | generator masks (AO/curvature/cavity/edge) via emit-bake machinery; provenance + re-bake; paint-over-generator stacking | headless CPU bake produces non-degenerate mask (stat assertions); re-bake after mesh edit; settings restored on failure paths | 3 |
| 5 | **Bake-down & export** | per-channel flatten; `use_baked` toggle; channel packing; export presets | headless: flattened channel ≈ live chain (pixel tolerance); ORM pack correct per spec; presets write correct files/colorspaces | 3 |
| 6 | **GPU strokes** | spike engine productized as opt-in brush: multi-channel MRT dabs, region sync, stroke undo, color mgmt, dilation | FINDINGS parity or better (≤0.1 ms/dab submit; region sync << full-canvas ~170 ms\@4K); undo restores region; probe-failure degrades to native brush; *v0.3.0 numbers inform final targets* | 5 |
| 7 | **UX iteration** | dedicated workspace; polish pass from user's live feedback; discoverability audit; docs/README house-style | user-driven; each iteration ships behind the same test gates | 3 |

**Total ≈ 24 sessions; MVP ≈ 10.** Phase 1 is the long pole and the only
phase with architectural risk; everything after it is additive by design.

---

## 12. Risks & open questions (ranked)

1. **EEVEE recompile latency on structural edits of deep stacks.** The
   design eliminates *spurious* recompiles, but a legitimate add/reorder on
   a 30-layer × 8-channel stack still compiles a big shader. *Mitigations:*
   narrow chains (A2) keep shader size linear; two-tier debounce; `use_baked`
   valve (phase 5); measure early — phase 1 acceptance includes a delta/
   recompile log. *Open item:* the requirements doc's performance budget
   (max layers × channels on the user's hardware) — instrument phase 1 and
   fix the budget with real numbers.
2. **Color pipeline regret.** The most-regretted decision class in the
   studied codebase (two shipped migrations). *Mitigation:* linear-blending
   decision made now, in writing (§4.8); registry-driven colorspaces; guard
   at paint time; migration machinery live from v0 in case we're wrong
   anyway.
3. **Sampler/image limits & datablock growth.** Every paint layer/mask is
   an image; GPU sampler ceilings (issue #315: 8 on old macOS/Intel GL;
   ~dozens elsewhere) put a hard lid on live stack depth. *Mitigation:*
   accepted for v1 (target hardware is the user's RTX); `use_baked` valve;
   atlases only if real usage hits the wall (they cost complexity — Ucupaint
   needed a whole subsystem).
4. **Blender API churn** (691 version gates in the autopsy's subject).
   *Mitigation:* single `compat.py` choke point from day one; target 5.1.2
   only at first; probe-don't-version-check where possible; known 5.x
   items pre-listed (socket-subtype bug, interface API, brush renames).
5. **F-Curve name-keyed paths assumption** (§2.2) — that
   `layers["<uid>"].opacity` paths animate and survive reorder needs a
   phase 1 headless probe *before* the schema freezes; if collection-key
   paths misbehave, fall back to uid-keyed lookup helpers + documented
   no-animation-of-stack-props for v1. Also inherit the autopsy's warning
   that custom PropertyGroup animation may need a frame-change re-evaluation
   nudge — treat animated stack properties as a stretch goal, not v1.
6. **UIList ergonomics ceiling** (no free drag-drop, limited row layout).
   *Mitigation:* move-operator keybindings; accept for v1; a custom-drawn
   `gpu`-based list is a phase 7 option only if the user's feedback demands
   it (the overlay add-on proves we can draw).
7. **Multi-channel GPU dab scaling unknown** — MRT attachment count vs
   sync-back cost is *pending v0.3.0 measurement*. *Mitigation:* phase 6 is
   last; v0.3.0 answers this before any phase 6 session starts; region sync
   shrinks the constant regardless.
8. **Normal-map layers** (painting/importing tangent normals, not height).
   Open question for phase 5+: blend normals at chain end (RNM/UDN-style
   combine) vs bake-time-only. Not needed for the v1 story; schema
   accommodates a future `normal` registry entry.
9. **Undo interleaving confusion** (paint undo vs operator undo, Blender's
   own known weirdness). *Mitigation:* v1 rides native behavior and
   documents it; no custom undo handlers until the GPU engine forces the
   question in phase 6.

---

## 13. Name candidates

Kitchen-adjacent and playful, in the Kiln spirit; all checked to be
generic dictionary words (no product-name collisions in the 3D-tool space
that would read as trademark-adjacent):

1. **Impasto** — a stack of pancakes; *the* layer-stack pun, sits next to
   Kiln like they came from the same kitchen. Short, memorable,
   F3-searchable. *(recommended)*
2. **Parfait** — layers you can see through the glass; evokes the
   translucent blend-mode stack; "perfect" pun built in.
3. **Baklava** — many thin layers, rich result; distinctive, fun to say.
4. **Strata** — geology-flavored, sober but still evocative; safest if the
   kitchen theme ever wears thin.
5. **PBR Layers** — the straight one: says exactly what it does, best for
   discoverability in an add-on listing; zero personality by design.

Naming note: operator/menu labels get the chosen name as prefix (e.g.
"Impasto: Add Fill Layer") for F3 substring search, per house convention.
