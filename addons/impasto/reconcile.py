# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto reconciler: GraphSpec -> minimal node-tree deltas.

The ONLY module that writes shader node trees (design §4.5). Every step
compares before writing, so a no-op pass performs zero datablock
mutations — the EEVEE-recompile guard. Per tree, in order:

1. hash gate (module-level cache, dropped on undo/load),
2. interface diff (add/remove only mismatches; never set subtype —
   5.1 landmine),
3. node diff by deterministic ``ps:`` name (remove extras, create
   missing, wrong bl_idname -> recreate; props and unlinked input
   default_values written only when actually different),
4. link diff (managed = links whose source node is ``ps:``-prefixed),
5. layout: only newly created nodes get positions; no rearrange pass.

Deltas are counted split into STRUCTURAL mutations vs VALUE writes:
value writes (socket default_values) update shader uniforms without an
EEVEE recompile, so the §4.6 invariant is assertable from the counts.
"""

import math
from dataclasses import dataclass, field

import bpy

from . import compat
from . import model

# spec-hash cache: cache_key -> tree_hash. Runtime-only; dropped on
# undo_post / load_post (engine installs the handlers).
_hash_cache = {}


def clear_caches():
    _hash_cache.clear()


def invalidate(cache_key):
    _hash_cache.pop(cache_key, None)


@dataclass
class Deltas:
    trees_created: int = 0
    nodes_created: int = 0
    nodes_removed: int = 0
    props_written: int = 0
    links_created: int = 0
    links_removed: int = 0
    iface_created: int = 0
    iface_removed: int = 0
    values_written: int = 0
    trees_skipped: int = 0
    errors: list = field(default_factory=list)

    def structural(self):
        return (self.trees_created + self.nodes_created
                + self.nodes_removed + self.props_written
                + self.links_created + self.links_removed
                + self.iface_created + self.iface_removed)

    def total(self):
        return self.structural() + self.values_written

    def __str__(self):
        return ("structural=%d (trees+%d nodes+%d/-%d props=%d "
                "links+%d/-%d iface+%d/-%d) values=%d skipped=%d "
                "errors=%d"
                % (self.structural(), self.trees_created,
                   self.nodes_created, self.nodes_removed,
                   self.props_written, self.links_created,
                   self.links_removed, self.iface_created,
                   self.iface_removed, self.values_written,
                   self.trees_skipped, len(self.errors)))


# ---------------------------------------------------------------------------
# value / prop comparison ("we wrote them, so drift means user or undo
# interference and gets repaired")
# ---------------------------------------------------------------------------

def _values_equal(cur, want):
    if isinstance(want, (int, float)) and isinstance(cur, (int, float)):
        return math.isclose(float(cur), float(want),
                            rel_tol=1e-6, abs_tol=1e-6)
    try:
        cur_seq = tuple(cur)
    except TypeError:
        return cur == want
    if isinstance(want, (tuple, list)):
        if len(cur_seq) != len(want):
            return False
        return all(_values_equal(c, w) for c, w in zip(cur_seq, want))
    return cur == want


def _prop_current(node, key):
    if key == "image":
        return node.image.name if node.image else ""
    if key == "node_tree":
        return node.node_tree.name if node.node_tree else ""
    return getattr(node, key)


def _prop_apply(node, key, value, deltas):
    if key == "image":
        img = bpy.data.images.get(value) if value else None
        if value and img is None:
            deltas.errors.append("missing image %r for %s"
                                 % (value, node.name))
        node.image = img
    elif key == "node_tree":
        ng = bpy.data.node_groups.get(value) if value else None
        if value and ng is None:
            deltas.errors.append("missing node group %r for %s"
                                 % (value, node.name))
        node.node_tree = ng
    elif key == "location":
        node.location = value
    else:
        setattr(node, key, value)


def _write_input_defaults(node, nspec, deltas):
    for key, want in nspec.inputs:
        sock = compat.find_socket(node.inputs, key)
        if sock is None:
            deltas.errors.append("missing input %r on %s"
                                 % (key, node.name))
            continue
        try:
            cur = sock.default_value
        except AttributeError:
            continue
        if not _values_equal(cur, want):
            sock.default_value = want
            deltas.values_written += 1


# ---------------------------------------------------------------------------
# per-tree reconciliation steps
# ---------------------------------------------------------------------------

def _diff_interface(tree_spec, tree, deltas):
    desired = [(s.name, s.in_out, s.socket_type)
               for s in tree_spec.interface]
    def sockets():
        return [it for it in tree.interface.items_tree
                if it.item_type == 'SOCKET']
    desired_set = set(desired)
    for it in list(sockets()):
        if (it.name, it.in_out, it.socket_type) not in desired_set:
            tree.interface.remove(it)
            deltas.iface_removed += 1
    current_set = {(it.name, it.in_out, it.socket_type)
                   for it in sockets()}
    for name, in_out, socket_type in desired:
        if (name, in_out, socket_type) not in current_set:
            # NOTE: never set socket subtype on creation (5.1 landmine).
            tree.interface.new_socket(name, in_out=in_out,
                                      socket_type=socket_type)
            deltas.iface_created += 1


def _diff_nodes(tree_spec, tree, deltas):
    desired = {n.name: n for n in tree_spec.nodes}
    for node in [n for n in tree.nodes
                 if n.name.startswith(model.NODE_PREFIX)
                 and n.name not in desired]:
        tree.nodes.remove(node)
        deltas.nodes_removed += 1
    for name, nspec in desired.items():
        node = tree.nodes.get(name)
        if node is not None and node.bl_idname != nspec.bl_idname:
            tree.nodes.remove(node)
            deltas.nodes_removed += 1
            node = None
        if node is None:
            node = tree.nodes.new(nspec.bl_idname)
            node.name = name
            if node.name != name:
                # a squatter holds our deterministic name: rename it
                # out of the way, then claim the name.
                squatter = tree.nodes.get(name)
                if squatter is not None and squatter != node:
                    squatter.name = name + ".displaced"
                node.name = name
            node.label = "Impasto (generated)"
            deltas.nodes_created += 1
            for key, value in nspec.props:
                _prop_apply(node, key, value, deltas)
        else:
            for key, value in nspec.props:
                if key == "location":
                    continue   # cosmetic; applied at creation only
                if not _values_equal(_prop_current(node, key), value):
                    _prop_apply(node, key, value, deltas)
                    deltas.props_written += 1
        _write_input_defaults(node, nspec, deltas)


def _link_matches(link, ls):
    return (link.from_node.name == ls.src[0]
            and link.to_node.name == ls.dst[0]
            and ls.src[1] in (link.from_socket.identifier,
                              link.from_socket.name)
            and ls.dst[1] in (link.to_socket.identifier,
                              link.to_socket.name))


def _diff_links(tree_spec, tree, deltas):
    desired = list(tree_spec.links)
    managed = [l for l in tree.links
               if l.from_node.name.startswith(model.NODE_PREFIX)]
    matched = set()
    for link in managed:
        hit = None
        for i, ls in enumerate(desired):
            if i not in matched and _link_matches(link, ls):
                hit = i
                break
        if hit is None:
            tree.links.remove(link)
            deltas.links_removed += 1
        else:
            matched.add(hit)
    for i, ls in enumerate(desired):
        if i in matched:
            continue
        src_node = tree.nodes.get(ls.src[0])
        dst_node = tree.nodes.get(ls.dst[0])
        if src_node is None or dst_node is None:
            deltas.errors.append("link endpoints missing: %r -> %r"
                                 % (ls.src, ls.dst))
            continue
        src = compat.find_socket(src_node.outputs, ls.src[1])
        dst = compat.find_socket(dst_node.inputs, ls.dst[1])
        if src is None or dst is None:
            deltas.errors.append("link sockets missing: %r -> %r"
                                 % (ls.src, ls.dst))
            continue
        tree.links.new(src, dst)
        deltas.links_created += 1


def _reconcile_tree(tree_spec, tree, deltas):
    if tree_spec.key != "material":
        _diff_interface(tree_spec, tree, deltas)
    _diff_nodes(tree_spec, tree, deltas)
    _diff_links(tree_spec, tree, deltas)


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def _cache_key(tree_spec, root_tree, material):
    if tree_spec.key == "material":
        return "material::" + material.name
    if tree_spec.key == "root":
        return root_tree.name
    return model.layer_tree_name(tree_spec.key)


def _resolve_tree(tree_spec, root_tree, material, deltas, create=True):
    if tree_spec.key == "root":
        return root_tree
    if tree_spec.key == "material":
        return material.node_tree if material else None
    name = model.layer_tree_name(tree_spec.key)
    tree = bpy.data.node_groups.get(name)
    if tree is None and create:
        tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
        deltas.trees_created += 1
    return tree


def reconcile_graph(spec, root_tree, material=None):
    """Apply a GraphSpec with minimal deltas. Idempotent: applying the
    same spec twice yields zero deltas on the second pass."""
    deltas = Deltas()
    for tree_spec in spec.trees:
        tree = _resolve_tree(tree_spec, root_tree, material, deltas)
        if tree is None:
            continue
        key = _cache_key(tree_spec, root_tree, material)
        h = model.tree_hash(tree_spec)
        if _hash_cache.get(key) == h:
            deltas.trees_skipped += 1
            continue
        try:
            _reconcile_tree(tree_spec, tree, deltas)
        except Exception as exc:
            # leave the hash cache invalidated so the next pass retries
            _hash_cache.pop(key, None)
            deltas.errors.append("tree %r: %s" % (tree_spec.key, exc))
            continue
        if deltas.errors:
            # missing images etc.: stay invalidated, retry next pass
            _hash_cache.pop(key, None)
        else:
            _hash_cache[key] = h
    return deltas


def apply_values(spec, root_tree, material=None):
    """UNIFORM-class flush: write unlinked-input default_values ONLY.
    Zero graph structure is touched — no nodes, links, interface, or
    node props. Skips nodes that do not exist (a pruned participant
    needs the debounced structural pass instead)."""
    deltas = Deltas()
    for tree_spec in spec.trees:
        tree = _resolve_tree(tree_spec, root_tree, material, deltas,
                             create=False)
        if tree is None:
            continue
        for nspec in tree_spec.nodes:
            node = tree.nodes.get(nspec.name)
            if node is None:
                continue
            _write_input_defaults(node, nspec, deltas)
    return deltas
