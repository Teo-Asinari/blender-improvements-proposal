# SPDX-License-Identifier: GPL-2.0-or-later
"""Stage 3: the bake gauntlet. One entry point — run_bake() — that does
everything Blender's manual high->low normal-bake setup spreads over
~15 steps and three editors:

validate pair + readiness checklist -> remember render engine and
selection -> switch to Cycles (baking requires it; the configured
Cycles device is left alone) -> create/reuse the bake image datablock
(Non-Color for normals) -> ensure the low-poly has a node material with
an Image Texture node targeting that image, made the ACTIVE node
(probed on 5.1.2: with bake target IMAGE_TEXTURES — the default — the
bake writes into the active image node of each target material; there
is no object/material-level bake_target property) -> select high, make
low active -> bpy.ops.object.bake(type=..., use_selected_to_active=
True, ...) with all settings passed as OPERATOR arguments (probed on
5.1.2: cage_extrusion / max_ray_distance / margin / normal_space are
operator props, so nothing in scene.render.bake needs mutating or
restoring) -> save the image to disk (dirs created) -> optionally wire
Image Texture -> Normal Map -> Principled BSDF Normal -> restore
engine + selection in a finally block.

Every failure raises KilnError with an actionable message; the
operator surfaces it via self.report — never a traceback.
"""

import os

import bpy

from . import flowcore
from . import readiness
from . import cage


class KilnError(Exception):
    """Actionable failure (message is shown via self.report)."""


# Node names: stable identifiers so re-bakes reuse instead of piling up.
BAKE_NODE_NAME = "Kiln Bake Target"
NORMAL_MAP_NODE_NAME = "Kiln Normal Map"

# Enum identifier -> pixels. Module-level so tests can shrink it.
RESOLUTIONS = {'1K': 1024, '2K': 2048, '4K': 4096}


def _validate_pair(settings):
    high = settings.high_object
    low = settings.low_object
    if high is None:
        raise KilnError("No high-poly object set (stage 1)")
    if low is None:
        raise KilnError("No low-poly object set (stage 1)")
    if high == low:
        raise KilnError("High-poly and low-poly are the same object")
    for label, ob in (("High", high), ("Low", low)):
        if ob.type != 'MESH':
            raise KilnError("%s-poly object %r is not a mesh"
                                % (label, ob.name))
        if ob.name not in bpy.context.view_layer.objects:
            raise KilnError(
                "%s-poly object %r is not in the current view layer"
                % (label, ob.name))
    return high, low


def ensure_bake_image(low_name, bake_type, resolution_px):
    """Create or reuse the bake image datablock (name from the
    flowcore.BAKE_TYPES table); rescales a reused image to the
    requested resolution; sets the colorspace per type."""
    name = flowcore.image_name(low_name, bake_type)
    info = flowcore.BAKE_TYPES[bake_type]
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, resolution_px, resolution_px,
                                  alpha=False, float_buffer=False)
    elif tuple(img.size) != (resolution_px, resolution_px):
        img.scale(resolution_px, resolution_px)
    img.colorspace_settings.name = ('Non-Color' if info["non_color"]
                                    else 'sRGB')
    return img


def ensure_material_target(low, img):
    """Ensure the low-poly has a node material whose ACTIVE node is an
    Image Texture pointing at ``img`` (the 5.1.2 bake-target
    mechanism). Returns (material, tex_node). Reuses the named node on
    re-bakes. NOTE: compare nodes with ``==`` — ``nodes.active is
    node`` is False even right after assignment (RNA wrapper identity,
    probed on 5.1.2)."""
    mat = low.active_material
    if mat is None:
        mat = bpy.data.materials.new(low.name + "_baked")
        mat.use_nodes = True
        if len(low.data.materials) > 0:
            low.data.materials[low.active_material_index] = mat
        else:
            low.data.materials.append(mat)
    if not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.get(BAKE_NODE_NAME)
    if tex is None or tex.bl_idname != 'ShaderNodeTexImage':
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.name = BAKE_NODE_NAME
        tex.label = BAKE_NODE_NAME
        tex.location = (-560.0, -260.0)
    tex.image = img
    for node in nt.nodes:
        node.select = False
    tex.select = True
    nt.nodes.active = tex
    return mat, tex


def _impasto_stack_node(mat):
    """Find Impasto's generated material group without depending on it."""
    if not mat.use_nodes:
        return None
    for node in mat.node_tree.nodes:
        tree = getattr(node, "node_tree", None)
        state = getattr(tree, "impasto", None) if tree is not None else None
        if state is not None and getattr(state, "is_stack", False):
            return node
    return None


def wire_normal_map(mat, tex, low=None):
    """Image Texture -> Normal Map (tangent) -> Principled BSDF Normal.
    Returns True when wired, False when the material has no Principled
    BSDF to receive it (reported as a warning, not an error)."""
    nt = mat.node_tree
    principled = next((n for n in nt.nodes
                       if n.bl_idname == 'ShaderNodeBsdfPrincipled'),
                      None)
    if principled is None or "Normal" not in principled.inputs:
        return False
    nm = nt.nodes.get(NORMAL_MAP_NODE_NAME)
    if nm is None or nm.bl_idname != 'ShaderNodeNormalMap':
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.name = NORMAL_MAP_NODE_NAME
        nm.label = NORMAL_MAP_NODE_NAME
        nm.location = (-300.0, -260.0)
    nm.space = 'TANGENT'
    nt.links.new(tex.outputs["Color"], nm.inputs["Color"])

    # Impasto already owns the final Principled Normal socket. A direct link
    # would bypass/damage that stack, so import the baked image as its bottom
    # Normal layer through the optional integration seam.
    if _impasto_stack_node(mat) is not None:
        try:
            from impasto import ops as impasto_ops
            uv_name = ""
            if low is not None and low.type == 'MESH' and low.data.uv_layers:
                uv = low.data.uv_layers.active
                uv_name = uv.name if uv is not None else ""
            impasto_ops.import_normal_baseline(
                mat, tex.image, uv_map=uv_name,
                fallback_node_name=NORMAL_MAP_NODE_NAME)
        except (ImportError, ValueError, RuntimeError) as exc:
            raise KilnError(
                "Impasto stack detected, but importing the baked normal "
                "failed: %s" % str(exc).strip())
        return True
    nt.links.new(nm.outputs["Normal"], principled.inputs["Normal"])
    return True


def _bake_kwargs(settings, extrusion, max_ray, cage_object=None):
    """Operator arguments for bpy.ops.object.bake, switched on the
    bake type."""
    mode = getattr(settings, "projection_mode", 'SURFACE')
    kwargs = dict(
        type=settings.bake_type,
        use_selected_to_active=True,
        margin=int(settings.margin),
        use_clear=True,
        target='IMAGE_TEXTURES',
    )
    if settings.bake_type == 'NORMAL':
        kwargs["normal_space"] = 'TANGENT'
    if mode == 'SURFACE':
        kwargs["use_cage"] = False
        kwargs["max_ray_distance"] = max_ray
    elif mode == 'AUTO_CAGE':
        kwargs["use_cage"] = True
        kwargs["cage_extrusion"] = extrusion
    elif mode == 'PAINTED_CAGE' and cage_object is not None:
        # The generated object is already displaced by ``extrusion``;
        # applying operator cage_extrusion again would double it.
        kwargs["use_cage"] = True
        kwargs["cage_object"] = cage_object.name
        kwargs["cage_extrusion"] = 0.0
        # Blender exposes Max Ray Distance only on the non-cage path. The
        # named cage supplies the ray launch geometry and direction.
    elif mode == 'PAINTED_CAGE':
        raise KilnError("Explicit / Painted Cage mode has no cage object")
    else:
        raise KilnError("Unknown projection mode %r" % mode)
    # TODO: AO, cavity/curvature, displacement — same machinery: add
    # the per-type operator settings here (e.g. AO pass_filter /
    # sample counts), plus the enum item and BAKE_TYPES entry.
    return kwargs


def resolved_distances(settings, high, low):
    """(extrusion, max_ray): the auto-heuristic from the pair's
    bounding-box diagonal, or the user's overrides."""
    if settings.use_auto_distances:
        return flowcore.auto_distances(readiness.pair_diagonal(high, low))
    return (float(settings.cage_extrusion),
            float(settings.max_ray_distance))


def run_bake(context, settings, report):
    """The whole gauntlet. ``report`` is the operator's self.report.
    Returns an info dict on success; raises KilnError otherwise."""
    high, low = _validate_pair(settings)

    # Stage 2 gate: hard failures block, warnings are reported.
    items = readiness.evaluate(low)
    fails = readiness.blocking(items)
    if fails:
        raise KilnError(
            "Low-poly not ready: " + "; ".join(
                "%s (%s)" % (i.label, i.detail) for i in fails))
    for item in readiness.warnings(items):
        report({'WARNING'}, "Checklist: %s - %s"
               % (item.label, item.detail))

    # Output path before any heavy work (fail fast, actionably).
    blend_dir = os.path.dirname(bpy.data.filepath)
    out_path, err = flowcore.resolve_output_path(
        settings.output_path, blend_dir, low.name, settings.bake_type)
    if err:
        raise KilnError(err)

    resolution_px = RESOLUTIONS[settings.resolution]
    extrusion, max_ray = resolved_distances(settings, high, low)

    img = ensure_bake_image(low.name, settings.bake_type, resolution_px)
    mat, tex = ensure_material_target(low, img)

    cage_object = None
    if getattr(settings, "projection_mode", 'SURFACE') == 'PAINTED_CAGE':
        try:
            cage_object, _inner = cage.build_guides(
                context, low, extrusion, max_ray,
                getattr(settings, "use_painted_cage", False))
        except cage.CageError as exc:
            raise KilnError("Cannot build explicit cage: %s" % exc)

    # --- remember state, bake, restore in finally ---------------------------
    scene = context.scene
    prev_engine = scene.render.engine
    # ``context.selected_objects`` excludes hidden objects. Preserve the real
    # view-layer selection so a hidden dense source can be temporarily exposed
    # for selected-to-active baking and restored exactly afterward.
    prev_selected = [ob.name for ob in context.view_layer.objects
                     if ob.select_get()]
    prev_active = context.view_layer.objects.active
    prev_active_name = prev_active.name if prev_active else None
    prev_visibility = {
        ob.name: (ob.hide_get(), ob.hide_viewport)
        for ob in (high, low)
    }

    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    try:
        try:
            scene.render.engine = 'CYCLES'
        except Exception:
            raise KilnError(
                "Cannot switch to Cycles (baking requires it) - is the "
                "Cycles render engine add-on enabled?")
        # Blender reports the misleading "No valid selected objects" when
        # the selected source is viewport-hidden. Temporarily expose the pair;
        # collection exclusion remains an actionable hard failure.
        for ob in (high, low):
            ob.hide_viewport = False
            ob.hide_set(False)
        if not high.visible_get() or not low.visible_get():
            raise KilnError(
                "High/low pair is hidden by a collection or view layer - "
                "enable that collection for baking")
        for ob in context.view_layer.objects:
            if ob.select_get():
                ob.select_set(False)
        high.select_set(True)
        low.select_set(True)
        context.view_layer.objects.active = low

        try:
            result = bpy.ops.object.bake(
                **_bake_kwargs(settings, extrusion, max_ray, cage_object))
        except RuntimeError as exc:
            raise KilnError(
                "Bake failed: %s" % str(exc).strip().splitlines()[-1])
        if 'FINISHED' not in result:
            raise KilnError("Bake did not finish (%r) - see the "
                                "status bar / console for the reason"
                                % (result,))
    finally:
        try:
            scene.render.engine = prev_engine
        except Exception:
            pass
        try:
            for ob in context.view_layer.objects:
                if ob.select_get():
                    ob.select_set(False)
            for name in prev_selected:
                ob = context.view_layer.objects.get(name)
                if ob is not None:
                    ob.select_set(True)
            if prev_active_name is not None:
                context.view_layer.objects.active = \
                    context.view_layer.objects.get(prev_active_name)
            for name, (hidden, hide_viewport) in prev_visibility.items():
                ob = context.view_layer.objects.get(name)
                if ob is not None:
                    ob.hide_set(hidden)
                    ob.hide_viewport = hide_viewport
        except Exception:
            pass

    # --- save to disk ---------------------------------------------------------
    out_dir = os.path.dirname(out_path)
    try:
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        img.filepath_raw = out_path
        img.file_format = 'PNG'
        img.save()
    except (OSError, RuntimeError) as exc:
        raise KilnError("Baked, but saving to %r failed: %s"
                            % (out_path, str(exc).strip()))

    # --- optional material wiring ----------------------------------------------
    wired = False
    if settings.wire_normal_map and settings.bake_type == 'NORMAL':
        wired = wire_normal_map(mat, tex, low)
        if not wired:
            report({'WARNING'},
                   "Baked and saved, but the material has no Principled "
                   "BSDF - normal map not wired")

    return {
        "image": img,
        "path": out_path,
        "resolution": resolution_px,
        "extrusion": extrusion,
        "max_ray_distance": max_ray,
        "projection_mode": getattr(settings, "projection_mode", 'SURFACE'),
        "wired": wired,
    }
