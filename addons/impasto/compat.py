# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto Blender-API compatibility choke point (design §12.4).

Every probe-don't-version-check helper lives here so API churn across
Blender releases is absorbed in one file. Target: Blender 5.1.2.
"""

import bpy


def resolve_colorspace(image, wanted):
    """Return the best colorspace enum identifier for ``wanted``
    ('sRGB' / 'Non-Color'), resolved against this build's OCIO config by
    RNA enum probe with prefix-match fallback — never a hardcoded write
    of an unverified literal."""
    try:
        items = [i.identifier for i in image.colorspace_settings.bl_rna
                 .properties["name"].enum_items]
    except Exception:
        return wanted
    if wanted in items:
        return wanted
    for ident in items:
        if ident.lower().startswith(wanted.lower()):
            return ident
    return wanted


def set_image_colorspace(image, wanted):
    name = resolve_colorspace(image, wanted)
    if image.colorspace_settings.name != name:
        image.colorspace_settings.name = name
    return name


def find_principled(node_tree):
    """The Principled BSDF this material renders with: prefer the one
    feeding the active Material Output's Surface, else the first."""
    if node_tree is None:
        return None
    out = None
    for n in node_tree.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial":
            if getattr(n, "is_active_output", False) or out is None:
                out = n
    if out is not None:
        surf = find_socket(out.inputs, "Surface")
        if surf is not None and surf.is_linked:
            src = surf.links[0].from_node
            if src.bl_idname == "ShaderNodeBsdfPrincipled":
                return src
    for n in node_tree.nodes:
        if n.bl_idname == "ShaderNodeBsdfPrincipled":
            return n
    return None


def find_socket(sockets, key):
    """Socket lookup by identifier, then by name, by ITERATION.

    Never use ``sockets[key]`` / ``sockets.get(key)``: on 5.1.2,
    string lookup on node socket collections skips disabled sockets
    (probed: ``Subsurface IOR`` is disabled by default on Principled
    and raises KeyError despite being present)."""
    for s in sockets:
        if s.identifier == key:
            return s
    for s in sockets:
        if s.name == key:
            return s
    return None
