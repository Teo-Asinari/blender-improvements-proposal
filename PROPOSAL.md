# Blender Improvements Proposal

A proposal for enhancing Blender with features and workflows inspired by 3DCoat, Substance Painter, and other specialized tools. The goal is to bring Blender's sculpting, painting, and UX capabilities closer to — or beyond — dedicated 3D content creation software, either through add-ons or upstream contributions.

---

## Proposed Features

### 1. Voxel-Based Sculpting

Implement a voxel sculpting workflow similar to 3DCoat's Voxel Room. Blender currently relies on mesh-based sculpting (multires, dyntopo), which has fundamental limitations when it comes to topology-free modeling.

A true voxel engine would enable:

- **Topology-free sculpting** — no stretching, pinching, or polygon-count artifacts
- **Real-time boolean/CSG operations** on sculpted volumes
- **Seamless resolution changes** without mesh dependency
- **Volume manipulation** — add, subtract, and blend material freely
- **Clean retopology pass** after voxel sculpting is complete

**Research needed:** Blender's data model, OpenVDB integration status, 3DCoat's voxel engine approach, performance considerations for real-time voxel rendering.

---

### 2. PBR Texture Painting with Layers

Blender's texture painting system is one of its weakest areas compared to dedicated tools. This proposal aims to bring it to parity with Substance Painter and 3DCoat.

#### Current Problems (Well-Documented)

- **No native layer system.** Blender only has separate image texture slots that must be manually wired into shader nodes. There is no layer stack, no blend modes, no opacity control, no masks within the paint interface.
- **Single-channel painting only.** You can only paint to one PBR channel at a time (e.g., Base Color OR Roughness), requiring constant slot switching. No simultaneous multi-channel painting.
- **Persistent brush lag.** Documented across multiple Blender versions — soften/smear brushes, large brush sizes, and tablet input all suffer from significant lag ([T58465](https://developer.blender.org/T58465), [T74753](https://developer.blender.org/T74753), [T60965](https://developer.blender.org/T60965), [Issue #93796](https://projects.blender.org/blender/blender/issues/93796)).
- **Broken viewport preview.** Painted changes don't appear in real-time in Cycles or Material Preview mode — only Solid shading reflects changes live ([Issue #86787](https://projects.blender.org/blender/blender/issues/86787), [T101032](https://developer.blender.org/T101032)).
- **Memory issues.** Undo history on 4K–16K textures causes runaway RAM consumption. The undo memory limit setting doesn't reliably cap usage.
- **Manual PBR setup.** Each channel texture must be created separately, marked with correct color space (Non-Color for roughness/metallic), and manually connected to the Principled BSDF node.

#### Proposed Solution

**A. Layer Stack System**

- Full non-destructive layer stack with blend modes (multiply, overlay, screen, etc.), per-layer opacity, and visibility toggles
- Fill layers, adjustment layers, and procedural layers
- Smart masks (curvature-based, ambient occlusion, cavity, edge detection)
- Layer groups and clipping masks
- Dedicated, streamlined layer management panel in the paint workspace — no need to navigate the Shader Node Editor to manage paint layers

**B. PBR Multi-Channel Painting**

- **Template materials:** Select a PBR template (e.g., Principled BSDF) that automatically creates and wires all necessary channel textures (Base Color, Roughness, Metallic, Normal, Height, Emissive, AO, etc.)
- **Per-channel toggles:** Enable or disable individual PBR channels to customize which channels exist for a given material — not every material needs every channel
- **Simultaneous multi-channel painting:** A single brush stroke can affect multiple channels at once (e.g., paint a scratch that is both lighter in base color and higher in roughness)
- **Channel isolation:** Quickly solo/isolate a single channel for focused painting
- **Smart defaults:** Correct color spaces assigned automatically (Non-Color for roughness, metallic, normal, etc.)

**C. Performance**

- GPU-accelerated painting pipeline to eliminate brush lag
- Efficient undo system that doesn't consume unbounded memory on large textures
- Real-time viewport preview in all shading modes, not just Solid
- Optimized handling of 4K–8K+ texture resolutions

#### Prior Art

- Blender developers published a [Layered Textures Design proposal](https://code.blender.org/2022/02/layered-textures-design/) in February 2022 but it has not shipped as of 2026.
- The [Paint Mode Design Discussion](https://devtalk.blender.org/t/paint-mode-design-discussion-feedback/24243) proposed building a unified Paint Mode on top of Sculpt Mode's architecture.
- Third-party add-ons (Layer Painter, PBR Painter 3, Ucupaint, HAS Paint Layers) demonstrate demand and feasibility but are limited by Blender's architecture.

---

### 3. Interactive Brush Size & Intensity Control (3DCoat-Style)

In 3DCoat, holding the right mouse button and dragging allows interactive adjustment of brush **size** (horizontal drag) and **intensity/strength** (vertical drag) in a single gesture, without menus or keyboard shortcuts. This is significantly faster than Blender's current `F`-key resize workflow.

**Detailed specification to follow.**

---

### 4. UX and Navigation Improvements (3DCoat-Inspired)

General improvements to viewport navigation, tool switching, and overall workflow efficiency, drawing from 3DCoat's design decisions.

**Specific pain points and proposed solutions to follow.**

---

## Implementation Strategy

The implementation path is under investigation. Possible approaches:

| Approach | Pros | Cons |
|---|---|---|
| **Blender Add-on (Python)** | Easy to distribute, no fork needed | Limited by Python API, poor performance for heavy features |
| **C/C++ Patch (PR to Blender)** | Full access to internals, best performance | Must align with Blender Foundation roadmap and standards |
| **Hybrid** | Core engine in C/C++, UI/workflow in Python | More complex build/distribution |

Some features (brush gesture control, basic layer UI) may be achievable as add-ons. Others (voxel sculpting, GPU paint pipeline) almost certainly require C/C++ work.

---

## Research Checklist

- [ ] Blender's sculpt and paint source code architecture
- [ ] 3DCoat's voxel engine — what makes it effective
- [ ] OpenVDB integration in Blender (current status and potential for sculpting)
- [ ] GPU texture painting techniques and Blender's current GPU paint path
- [ ] Blender Python API limitations for real-time input handling
- [ ] Existing add-ons (Layer Painter, PBR Painter, Ucupaint) — capabilities and limitations
- [ ] Blender Foundation contribution process and coding standards
- [ ] Substance Painter's PBR channel workflow for reference
- [ ] 3DCoat's brush size/intensity gesture implementation details

---

## References

- [3DCoat](https://3dcoat.com/)
- [Substance Painter](https://www.adobe.com/products/substance3d-painter.html)
- [Blender Developer Documentation](https://developer.blender.org/)
- [Blender Source Code](https://projects.blender.org/blender/blender)
- [Layered Textures Design — Blender Developers Blog (2022)](https://code.blender.org/2022/02/layered-textures-design/)
- [Paint Mode Design Discussion](https://devtalk.blender.org/t/paint-mode-design-discussion-feedback/24243)
- [2025-01-28 Sculpt, Paint & Texture Module Meeting](https://devtalk.blender.org/t/2025-1-28-sculpt-paint-texture-module-meeting/38779)

---

*This is a living document. Technical details and research findings will be added as the project progresses.*
