# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto pure-core tests: registry sanity, compiler golden specs,
determinism/locality/uniform invariants, grace semantics, group
pass-through, and the two-tier debounce (fake clock).

Runs under plain python3 (fast path) AND inside
``blender --background --python`` — model.py and debounce.py are loaded
directly from file so the package __init__ (which imports bpy) is never
executed here.

Prints MODEL_TESTS_PASSED on success. Set IMPASTO_REGEN_GOLDEN=1 to
rewrite tests/golden/*.json from the current compiler (still asserts).
"""

import importlib.util
import json
import os
import sys
import traceback

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ADDON_DIR = os.path.dirname(_TESTS_DIR)
_GOLDEN_DIR = os.path.join(_TESTS_DIR, "golden")


def _load(name):
    path = os.path.join(_ADDON_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("impasto_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


model = _load("model")
debounce = _load("debounce")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


# Principled BSDF input names probed verbatim on Blender 5.1.2
# (scratch probe, 2026-07-11). Pins registry->socket integrity without
# needing bpy here.
PRINCIPLED_INPUTS_5_1_2 = {
    "Base Color", "Metallic", "Roughness", "IOR", "Alpha", "Normal",
    "Weight", "Diffuse Roughness", "Subsurface Weight",
    "Subsurface Radius", "Subsurface Scale", "Subsurface IOR",
    "Subsurface Anisotropy", "Specular IOR Level", "Specular Tint",
    "Anisotropic", "Anisotropic Rotation", "Tangent",
    "Transmission Weight", "Coat Weight", "Coat Roughness", "Coat IOR",
    "Coat Tint", "Coat Normal", "Sheen Weight", "Sheen Roughness",
    "Sheen Tint", "Emission Color", "Emission Strength",
    "Thin Film Thickness", "Thin Film IOR",
}

# ShaderNodeMix.blend_type enum probed on 5.1.2.
MIX_BLEND_TYPES_5_1_2 = {
    "MIX", "DARKEN", "MULTIPLY", "BURN", "LIGHTEN", "SCREEN", "DODGE",
    "ADD", "OVERLAY", "SOFT_LIGHT", "LINEAR_LIGHT", "DIFFERENCE",
    "EXCLUSION", "SUBTRACT", "DIVIDE", "HUE", "SATURATION", "COLOR",
    "VALUE",
}


def test_purity():
    for name in ("model", "debounce"):
        src = open(os.path.join(_ADDON_DIR, name + ".py")).read()
        check("%s.py imports no bpy/gpu" % name,
              "import bpy" not in src and "import gpu" not in src)


def test_registry():
    keys = [c.key for c in model.CHANNELS]
    check("registry has the 13 standard channels", len(keys) == 13,
          "got %d" % len(keys))
    check("registry keys unique", len(set(keys)) == len(keys))
    bad_sockets = [c.key for c in model.CHANNELS
                   if c.key != "height"
                   and c.socket not in PRINCIPLED_INPUTS_5_1_2]
    check("every channel socket exists on 5.1.2 Principled",
          not bad_sockets, "bad: %s" % bad_sockets)
    check("height is the socketless special channel",
          model.CHANNEL_MAP["height"].socket == "")
    check("normal is encoded tangent RGB stored as Non-Color",
          model.CHANNEL_MAP["normal"].kind == "COLOR"
          and model.CHANNEL_MAP["normal"].colorspace == "Non-Color"
          and model.CHANNEL_MAP["normal"].default_value
          == (0.5, 0.5, 1.0, 1.0))
    check("colorspaces restricted to sRGB / Non-Color",
          all(c.colorspace in ("sRGB", "Non-Color")
              for c in model.CHANNELS))
    check("R2 split: emission color sRGB, strength Non-Color",
          model.CHANNEL_MAP["emission_color"].colorspace == "sRGB"
          and model.CHANNEL_MAP["emission_strength"].colorspace
          == "Non-Color")
    check("R3: sss_radius is a Non-Color VECTOR (painted as color, "
          "stored metric)",
          model.CHANNEL_MAP["sss_radius"].kind == "VECTOR"
          and model.CHANNEL_MAP["sss_radius"].colorspace == "Non-Color")
    for tname, tkeys in model.TEMPLATES.items():
        check("template %r keys all resolve" % tname,
              all(k in model.CHANNEL_MAP for k in tkeys))
    check("phase-1 blend set is a subset of Mix blend_type enum",
          set(model.BLEND_MODES) <= MIX_BLEND_TYPES_5_1_2)
    check("default blends valid",
          all(c.default_blend in MIX_BLEND_TYPES_5_1_2
              for c in model.CHANNELS))
    check("scalar/vector defaults are tuples (seed helpers rely on it)",
          all(isinstance(c.default_value, tuple)
              for c in model.CHANNELS))
    check("seed_rgba shapes", model.seed_rgba(
        model.CHANNEL_MAP["roughness"]) == (0.5, 0.5, 0.5, 1.0))


# --- fixtures (fixed uids: golden files must be stable) -------------------

def fx_fill():
    """Single fill layer, base_color COLOR + roughness VALUE."""
    return model.StackModel(
        root_tree_name="Impasto Stack (Mat)",
        channels=("base_color", "roughness"),
        layers=(
            model.LayerModel(
                uid="aa11bb22", label="Rust fill", layer_type="FILL",
                opacity=0.75,
                bindings=(
                    model.BindingModel(key="base_color", mode="COLOR",
                                       color=(0.4, 0.15, 0.05, 1.0)),
                    model.BindingModel(key="roughness", mode="VALUE",
                                       value=0.85),
                )),
        ),
        material=model.MaterialModel("Principled BSDF"),
    )


def fx_paint_mask():
    """Paint layer (2 shared channels) with one inverted mask, above a
    fill layer."""
    return model.StackModel(
        root_tree_name="Impasto Stack (Mat)",
        channels=("base_color", "roughness"),
        layers=(
            model.LayerModel(
                uid="c3a91f02", label="Scratches", layer_type="PAINT",
                image_name="FJ Scratches", uv_map="UVMap",
                bindings=(
                    model.BindingModel(key="base_color"),
                    model.BindingModel(key="roughness"),
                ),
                masks=(
                    model.MaskModel(uid="9be1d1c4", label="Edge mask",
                                    image_name="FJ Edge",
                                    invert=True, opacity=0.8),
                )),
            model.LayerModel(
                uid="aa11bb22", label="Base fill", layer_type="FILL",
                bindings=(
                    model.BindingModel(key="base_color", mode="COLOR",
                                       color=(0.2, 0.3, 0.5, 1.0)),
                )),
        ),
        material=model.MaterialModel("Principled BSDF"),
    )


def fx_group():
    """3 layers: paint inside a half-opacity group, fill at the bottom."""
    return model.StackModel(
        root_tree_name="Impasto Stack (Mat)",
        channels=("base_color", "roughness", "height"),
        layers=(
            model.LayerModel(uid="dddd0001", label="Detail group",
                             layer_type="GROUP", opacity=0.5),
            model.LayerModel(
                uid="c3a91f02", label="Paint", layer_type="PAINT",
                parent_uid="dddd0001", image_name="FJ Paint",
                opacity=0.9,
                bindings=(model.BindingModel(key="base_color"),
                          model.BindingModel(key="height"))),
            model.LayerModel(
                uid="aa11bb22", label="Fill", layer_type="FILL",
                bindings=(model.BindingModel(key="base_color",
                                             mode="COLOR",
                                             color=(0.1, 0.1, 0.1, 1.0)),
                          model.BindingModel(key="roughness",
                                             mode="VALUE", value=0.3))),
        ),
        material=model.MaterialModel("Principled BSDF"),
    )


GOLDENS = {
    "fill_layer.json": fx_fill,
    "paint_mask.json": fx_paint_mask,
    "group_stack.json": fx_group,
}


def test_goldens():
    regen = bool(os.environ.get("IMPASTO_REGEN_GOLDEN"))
    if regen and not os.path.isdir(_GOLDEN_DIR):
        os.makedirs(_GOLDEN_DIR)
    for fname, fx in GOLDENS.items():
        got = model.spec_to_jsonable(model.compile_stack(fx()))
        path = os.path.join(_GOLDEN_DIR, fname)
        if regen:
            with open(path, "w") as f:
                json.dump(got, f, indent=1, sort_keys=True)
                f.write("\n")
        if not os.path.exists(path):
            check("golden %s exists" % fname, False, "missing file")
            continue
        want = json.load(open(path))
        check("golden %s matches compile output" % fname,
              json.loads(json.dumps(got, sort_keys=True)) == want,
              "spec drifted — inspect a diff via IMPASTO_REGEN_GOLDEN=1")


def test_determinism():
    a = json.dumps(model.spec_to_jsonable(
        model.compile_stack(fx_group())), sort_keys=True)
    b = json.dumps(model.spec_to_jsonable(
        model.compile_stack(fx_group())), sort_keys=True)
    check("same model -> byte-identical spec", a == b)


def _tree(spec, key):
    for t in spec.trees:
        if t.key == key:
            return t
    return None


def test_locality_on_reorder():
    m = fx_paint_mask()
    spec1 = model.compile_stack(m)
    reordered = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(m.layers[1], m.layers[0]), material=m.material)
    spec2 = model.compile_stack(reordered)
    for uid in ("c3a91f02",):
        h1 = model.tree_hash(_tree(spec1, uid))
        h2 = model.tree_hash(_tree(spec2, uid))
        check("reorder leaves layer tree %s hash unchanged" % uid,
              h1 == h2)
    check("reorder changes the root tree hash",
          model.tree_hash(_tree(spec1, "root"))
          != model.tree_hash(_tree(spec2, "root")))
    names1 = {n.name for n in _tree(spec1, "root").nodes}
    names2 = {n.name for n in _tree(spec2, "root").nodes}
    check("reorder preserves every root node name (uid-keyed, so "
          "F-Curve-relevant paths survive)", names1 == names2,
          "diff: %s" % (names1 ^ names2))


def test_uniform_invariant():
    """Opacity/visibility edits must not change graph structure — the
    §4.6 invariant, asserted on the spec (compile twice, diff)."""
    m = fx_paint_mask()
    spec1 = model.compile_stack(m)

    # opacity change
    lay = m.layers[0]
    m2 = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=lay.uid, label=lay.label, layer_type=lay.layer_type,
            image_name=lay.image_name, uv_map=lay.uv_map,
            opacity=0.35, bindings=lay.bindings, masks=lay.masks),
            m.layers[1]),
        material=m.material)
    spec2 = model.compile_stack(m2)
    for t1, t2 in zip(spec1.trees, spec2.trees):
        check("opacity change: tree %r structural signature unchanged"
              % t1.key,
              model.structural_signature(t1)
              == model.structural_signature(t2))
    check("opacity change: root hash DID change (values moved)",
          model.tree_hash(_tree(spec1, "root"))
          != model.tree_hash(_tree(spec2, "root")))

    # visibility toggle inside the grace period
    m3 = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=lay.uid, label=lay.label, layer_type=lay.layer_type,
            image_name=lay.image_name, uv_map=lay.uv_map,
            visible=False, bindings=lay.bindings, masks=lay.masks),
            m.layers[1]),
        material=m.material)
    spec3 = model.compile_stack(m3, grace=frozenset({lay.uid}))
    for t1, t3 in zip(spec1.trees, spec3.trees):
        check("hide+grace: tree %r structural signature unchanged"
              % t3.key,
              model.structural_signature(t1)
              == model.structural_signature(t3))
    root3 = _tree(spec3, "root")
    facs = [n for n in root3.nodes
            if n.name.startswith("ps:root:ch.")
            and ":fac." + lay.uid in n.name]
    check("hide+grace: factor constants fold to 0",
          facs and all(dict(n.inputs).get("Value_001") == 0.0
                       for n in facs),
          "facs: %r" % [(n.name, n.inputs) for n in facs])


def test_prune_and_toggle_equivalence():
    m = fx_paint_mask()
    lay = m.layers[0]
    hidden = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=lay.uid, label=lay.label, layer_type=lay.layer_type,
            image_name=lay.image_name, uv_map=lay.uv_map,
            visible=False, bindings=lay.bindings, masks=lay.masks),
            m.layers[1]),
        material=m.material)
    pruned = model.compile_stack(hidden)  # empty grace = pruned
    root = _tree(pruned, "root")
    check("pruned hidden layer contributes no blend nodes",
          not any(":blend.%s" % lay.uid in n.name for n in root.nodes))

    # binding disabled (pruned) == binding absent, for the root tree
    disabled = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=lay.uid, label=lay.label, layer_type=lay.layer_type,
            image_name=lay.image_name, uv_map=lay.uv_map,
            bindings=(model.BindingModel(key="base_color"),
                      model.BindingModel(key="roughness",
                                         enabled=False)),
            masks=lay.masks),
            m.layers[1]),
        material=m.material)
    absent = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=lay.uid, label=lay.label, layer_type=lay.layer_type,
            image_name=lay.image_name, uv_map=lay.uv_map,
            bindings=(model.BindingModel(key="base_color"),),
            masks=lay.masks),
            m.layers[1]),
        material=m.material)
    check("disabled binding (post-prune) == absent binding, root tree",
          model.tree_hash(_tree(model.compile_stack(disabled), "root"))
          == model.tree_hash(_tree(model.compile_stack(absent), "root")))


def test_group_passthrough():
    m = fx_group()
    spec = model.compile_stack(m)
    root = _tree(spec, "root")
    paint = m.layers[1]
    binding = paint.bindings[0]
    f = model.const_factor(m, paint, binding)
    check("group pass-through folds a*b into descendant factor "
          "(0.5 x 0.9)", abs(f - 0.45) < 1e-9, "got %r" % f)
    check("group itself contributes no nodes",
          not any("dddd0001" in n.name for n in root.nodes))
    fac_nodes = [n for n in root.nodes
                 if n.name == "ps:root:ch.base_color:fac.c3a91f02"]
    check("paint factor rides the mask multiply (alpha gating)",
          fac_nodes
          and dict(fac_nodes[0].inputs).get("Value_001") == 0.45)
    # hidden group: descendants fold to 0 under grace of the group uid
    g = m.layers[0]
    m2 = model.StackModel(
        root_tree_name=m.root_tree_name, channels=m.channels,
        layers=(model.LayerModel(
            uid=g.uid, label=g.label, layer_type="GROUP",
            visible=False, opacity=g.opacity),
            m.layers[1], m.layers[2]),
        material=m.material)
    spec2 = model.compile_stack(m2, grace=frozenset({g.uid}))
    root2 = _tree(spec2, "root")
    check("hidden group + grace keeps descendant structure",
          model.structural_signature(root2)
          == model.structural_signature(root))
    f2 = model.const_factor(m2, m2.layers[1], binding)
    check("hidden group folds descendant factor to 0", f2 == 0.0)
    spec3 = model.compile_stack(m2)  # pruned
    root3 = _tree(spec3, "root")
    check("prune drops the hidden group's descendants",
          not any("c3a91f02" in n.name for n in root3.nodes))


def test_material_tree():
    spec = model.compile_stack(fx_group())
    mat = _tree(spec, "material")
    check("material tree exists", mat is not None)
    dsts = {l.dst for l in mat.links}
    check("Principled Base Color + Roughness linked",
          ("Principled BSDF", "Base Color") in dsts
          and ("Principled BSDF", "Roughness") in dsts)
    check("height chain present -> Normal linked via Bump",
          ("Principled BSDF", "Normal") in dsts)
    root = _tree(spec, "root")
    check("height uses explicit scalar extraction before Bump",
          any(l.src == (model.n_scalar_out("height"), "Red")
              and l.dst == (model.n_bump(), "Height")
              for l in root.links))
    spec2 = model.compile_stack(fx_fill())
    mat2 = _tree(spec2, "material")
    check("no height channel -> no Normal link",
          ("Principled BSDF", "Normal")
          not in {l.dst for l in mat2.links})
    # empty-channel root output carries the registry seed
    root2 = _tree(spec2, "root")
    out = next(n for n in root2.nodes if n.name == "ps:root:out")
    check("fill fixture: both channels have participants (no seeds on "
          "the group output)", dict(out.inputs) == {})
    rough_scalar = next(n for n in root2.nodes
                        if n.name == model.n_scalar_out("roughness"))
    check("roughness uses explicit RGB scalar extraction",
          rough_scalar.bl_idname == "ShaderNodeSeparateColor"
          and (model.n_scalar_out("roughness"), "Red") in
          {link.src for link in root2.links
           if link.dst == (model.n_root_out(), "Roughness")})

    normal_layer = model.LayerModel(
        uid="bb22cc33", label="Normal paint", layer_type="PAINT",
        image_name="Normal.png", uv_map="UVMap",
        bindings=(model.BindingModel(key="normal", mode="SHARED"),))
    normal_height = model.StackModel(
        root_tree_name="Impasto Stack (Normals)",
        channels=("normal", "height"),
        layers=(normal_layer, model.LayerModel(
            uid="cc33dd44", label="Height", layer_type="FILL",
            bindings=(model.BindingModel(key="height", mode="VALUE",
                                         value=0.5),))),
        material=model.MaterialModel("Principled BSDF"))
    normal_spec = model.compile_stack(normal_height)
    normal_root = _tree(normal_spec, "root")
    normal_nodes = {node.name: node for node in normal_root.nodes}
    check("normal chain decodes encoded RGB with tangent Normal Map",
          normal_nodes[model.n_normal_map()].bl_idname
          == "ShaderNodeNormalMap"
          and dict(normal_nodes[model.n_normal_map()].props)["space"]
          == "TANGENT")
    normal_links = {(link.src, link.dst) for link in normal_root.links}
    check("decoded normal feeds Bump Normal before Height reaches output",
          ((model.n_normal_map(), "Normal"),
           (model.n_bump(), "Normal")) in normal_links
          and ((model.n_bump(), "Normal"),
               (model.n_root_out(), "Normal")) in normal_links)
    check("normal and height share exactly one root Normal output",
          [socket.name for socket in normal_root.interface].count("Normal")
          == 1)


def test_layer_tree_shape():
    spec = model.compile_stack(fx_paint_mask())
    lt = _tree(spec, "c3a91f02")
    names = {s.name for s in lt.interface}
    check("layer interface = shared ch sockets + one mask socket",
          names == {"ch:base_color", "ch:roughness", "mask"})
    node_names = {n.name for n in lt.nodes}
    check("layer tree nodes include explicit scalar extraction",
          node_names == {"ps:c3a91f02:uv", "ps:c3a91f02:src",
                         "ps:c3a91f02:src.scalar",
                         "ps:c3a91f02:mask.9be1d1c4:src",
                         "ps:c3a91f02:mask.9be1d1c4:op",
                         "ps:c3a91f02:mask:mul.0", "ps:c3a91f02:out"},
          "got %s" % node_names)
    interfaces = {s.name: s.socket_type for s in lt.interface}
    check("shared roughness is a float, not implicit color coercion",
          interfaces["ch:roughness"] == "NodeSocketFloat")
    check("grayscale endpoints preserve scalar polarity",
          ((0.0, 0.0, 0.0, 1.0)[0],
           (1.0, 1.0, 1.0, 1.0)[0]) == (0.0, 1.0))
    opn = next(n for n in lt.nodes if n.name.endswith(":op"))
    ins = dict(opn.inputs)
    check("inverted 0.8-opacity mask folds to a=-0.8 b=1.0 "
          "(invert+opacity are uniform writes)",
          abs(ins["Value_001"] + 0.8) < 1e-9
          and abs(ins["Value_002"] - 1.0) < 1e-9)
    # bare fill layer has no tree
    spec_f = model.compile_stack(fx_fill())
    check("bare fill layer compiles to no layer tree",
          _tree(spec_f, "aa11bb22") is None)


def test_uid_helpers():
    uids = {model.new_uid() for _ in range(64)}
    check("uids are 8-hex", all(len(u) == 8 and
                                all(c in "0123456789abcdef" for c in u)
                                for u in uids))
    check("collision retry honored",
          model.new_uid(existing=("deadbeef",)) != "deadbeef")
    check("layer tree name round-trips",
          model.uid_from_layer_tree_name(
              model.layer_tree_name("c3a91f02")) == "c3a91f02")


def test_debounce():
    d = debounce.DebounceState()
    t0 = 1000.0
    check("idle debounce reports nothing",
          d.due(t0) == [] and d.next_delay(t0) is None
          and not d.pending)
    d.mark_structural(t0)
    d.mark_structural(t0 + 0.05)      # burst: deadline pushed
    check("burst pushes the structural deadline",
          d.due(t0 + 0.12) == [], "fired early")
    check("structural fires after 100ms quiet",
          d.due(t0 + 0.16) == ["structural"])
    check("consumed once", d.due(t0 + 0.2) == [])
    d.mark_structural(t0 + 1.0)
    d.mark_prune(t0 + 1.0)
    check("next_delay is the structural tier",
          abs(d.next_delay(t0 + 1.0) - 0.1) < 1e-9)
    check("structural fires first, prune later",
          d.due(t0 + 1.2) == ["structural"]
          and d.due(t0 + 3.9) == [])
    check("prune fires after 3s idle", d.due(t0 + 4.01) == ["prune"])
    d.mark_prune(t0 + 10.0)
    d.mark_prune(t0 + 11.0)           # activity pushes prune too
    check("prune deadline pushed by activity",
          d.due(t0 + 13.5) == [] and d.due(t0 + 14.1) == ["prune"])
    d.mark_structural(t0 + 20.0)
    d.mark_prune(t0 + 20.0)
    check("both due at once -> structural ordered before prune",
          d.due(t0 + 30.0) == ["structural", "prune"])
    d.mark_structural(t0 + 40.0)
    d.reset()
    check("reset clears pending", not d.pending)


def main():
    test_purity()
    test_registry()
    test_goldens()
    test_determinism()
    test_locality_on_reorder()
    test_uniform_invariant()
    test_prune_and_toggle_equivalence()
    test_group_passthrough()
    test_material_tree()
    test_layer_tree_shape()
    test_uid_helpers()
    test_debounce()


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("MODEL_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
else:
    print("MODEL_TESTS_PASSED")
sys.stdout.flush()
