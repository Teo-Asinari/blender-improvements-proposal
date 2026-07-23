# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure planning and pixel reference for complete-stack resident preview.

The live painter owns the active Paint layer's GPU textures.  Accurate preview
also needs the layers below it (a baseline) and the effect of layers above it.
This module describes that partition without importing Blender or GPU modules,
and provides a scalar/RGBA reference compositor for shader tests.

Image samples passed to :func:`compose_channel_pixel` are already decoded into
the shader domain: Base Color is scene-linear as it would be after an Image
Texture node; scalar and tangent-normal images remain Non-Color.  Mask samples
must be scalar values matching Blender's Color-to-Value conversion.
"""

from dataclasses import dataclass

from . import model


AFFINE_BLEND_MODES = frozenset({
    "MIX", "ADD", "SUBTRACT", "MULTIPLY", "SCREEN",
})


@dataclass(frozen=True)
class PixelSample:
    value: object
    alpha: float = 1.0


@dataclass(frozen=True)
class ResidentPreviewPlan:
    active_uid: str
    lower_layer_uids: tuple
    upper_layer_uids: tuple
    nonlinear_upper: tuple
    image_dependencies: tuple
    mask_dependencies: tuple
    uv_maps: tuple
    has_implicit_uv: bool

    @property
    def affine_fast_path(self):
        return not self.nonlinear_upper

    @property
    def single_uv_fast_path(self):
        return (len(self.uv_maps) <= 1
                and not (self.has_implicit_uv and self.uv_maps))


@dataclass(frozen=True)
class BaselineScope:
    supported: bool
    reasons: tuple


def assess_lower_baseline_scope(plan):
    """Eligibility for the shippable same-UV, active-topmost baseline path.

    This deliberately rejects rather than silently ignores state that the
    reduced runtime cannot compose yet. Integration may resolve an implicit UV
    name before planning; unresolved mixed implicit/named UVs remain unsafe.
    """
    reasons = []
    if not plan.single_uv_fast_path:
        reasons.append("participating layers do not share one resolved UV map")
    if plan.upper_layer_uids:
        reasons.append("participating layers exist above the active layer")
    return BaselineScope(not reasons, tuple(reasons))


def _binding(layer, key):
    return next((b for b in layer.bindings if b.key == key), None)


def _active_layer(model_stack, active_uid):
    return next((layer for layer in model_stack.layers
                 if layer.uid == active_uid), None)


def _participates(model_stack, layer, binding):
    if binding is None or layer.layer_type == "GROUP":
        return False
    if not binding.enabled or not layer.visible:
        return False
    return all(group.visible for group in model._ancestors(model_stack, layer))


def _composition_layers(model_stack):
    """Bottom-to-top order (stored order is top-to-bottom)."""
    return tuple(reversed(model_stack.layers))


def plan_resident_preview(model_stack, active_uid, channel_keys=None):
    """Partition static lower/upper state around an active resident layer.

    Most upper blend operations are affine in the incoming value and can be
    collapsed into per-channel coefficient textures ``C`` and ``D`` such that
    final = ``C * active_result + D``.  OVERLAY is input-piecewise and is
    listed in ``nonlinear_upper`` for an additional live GPU pass.
    """
    active = _active_layer(model_stack, active_uid)
    if active is None:
        raise ValueError("active layer %r is not in the stack" % active_uid)
    keys = tuple(channel_keys or model_stack.channels)
    composition = _composition_layers(model_stack)
    active_i = composition.index(active)
    lower = composition[:active_i]
    upper = composition[active_i + 1:]

    def relevant(layer):
        return any(_participates(model_stack, layer, _binding(layer, key))
                   for key in keys)

    lower = tuple(layer for layer in lower if relevant(layer))
    upper = tuple(layer for layer in upper if relevant(layer))
    nonlinear = []
    for layer in upper:
        for key in keys:
            binding = _binding(layer, key)
            if (_participates(model_stack, layer, binding)
                    and model.effective_blend(layer, binding)
                    not in AFFINE_BLEND_MODES):
                nonlinear.append((layer.uid, key,
                                  model.effective_blend(layer, binding)))

    images = []
    masks = []
    uv_maps = [active.uv_map] if active.uv_map else []
    has_implicit_uv = not bool(active.uv_map)
    for layer in composition:
        if layer.layer_type == "GROUP":
            continue
        for key in keys:
            binding = _binding(layer, key)
            if not _participates(model_stack, layer, binding):
                continue
            image_name = model.binding_image(layer, binding)
            if (layer.uid != active_uid and layer.layer_type == "PAINT"
                    and image_name):
                images.append(image_name)
                if layer.uv_map:
                    uv_maps.append(layer.uv_map)
                else:
                    has_implicit_uv = True
            if binding.use_masks:
                for mask in layer.masks:
                    if mask.visible and mask.image_name:
                        masks.append(mask.image_name)
                        if mask.uv_map:
                            uv_maps.append(mask.uv_map)
                        else:
                            has_implicit_uv = True
    # Stable de-duplication keeps allocation plans deterministic.
    unique = lambda seq: tuple(dict.fromkeys(seq))
    return ResidentPreviewPlan(
        active_uid=active_uid,
        lower_layer_uids=tuple(layer.uid for layer in lower),
        upper_layer_uids=tuple(layer.uid for layer in upper),
        nonlinear_upper=tuple(nonlinear),
        image_dependencies=unique(images),
        mask_dependencies=unique(masks),
        uv_maps=unique(uv_maps),
        has_implicit_uv=has_implicit_uv,
    )


def _tuple(value):
    if isinstance(value, (tuple, list)):
        return tuple(float(x) for x in value)
    return (float(value),)


def _shape(values, scalar):
    return values[0] if scalar else tuple(values)


def blend_value(a, b, factor, blend_mode):
    """Reference for the root compiler's unclamped-result Mix nodes."""
    aa, bb = _tuple(a), _tuple(b)
    if len(aa) != len(bb):
        raise ValueError("blend operands have different shapes")
    f = max(0.0, min(1.0, float(factor)))
    mode = str(blend_mode).upper()
    out = []
    for av, bv in zip(aa, bb):
        if mode == "MIX":
            value = av * (1.0 - f) + bv * f
        elif mode == "ADD":
            value = av + bv * f
        elif mode == "SUBTRACT":
            value = av - bv * f
        elif mode == "MULTIPLY":
            value = av * ((1.0 - f) + bv * f)
        elif mode == "SCREEN":
            value = 1.0 - (1.0 - av) * (1.0 - bv * f)
        elif mode == "OVERLAY":
            overlay = (2.0 * av * bv if av < 0.5
                       else 1.0 - 2.0 * (1.0 - av) * (1.0 - bv))
            value = av * (1.0 - f) + overlay * f
        else:
            raise ValueError("unsupported blend mode %r" % blend_mode)
        out.append(value)
    return _shape(out, not isinstance(a, (tuple, list)))


def blend_tangent_normals_rnm(base, detail, factor):
    """Blend encoded tangent normals with Reoriented Normal Mapping.

    ``factor`` attenuates the detail toward the neutral tangent normal before
    composition.  Any fourth component is preserved from the base because
    resident stack coverage is tracked separately from normal direction.
    """
    import math

    a, b = _tuple(base), _tuple(detail)
    if len(a) < 3 or len(b) < 3:
        raise ValueError("RNM operands require at least three components")
    f = max(0.0, min(1.0, float(factor)))

    def unit(v):
        length = math.sqrt(sum(x * x for x in v))
        return tuple(x / length for x in v) if length > 1e-12 else (
            0.0, 0.0, 1.0)

    n1 = unit(tuple(2.0 * x - 1.0 for x in a[:3]))
    raw_detail = tuple(2.0 * x - 1.0 for x in b[:3])
    n2 = unit((raw_detail[0] * f, raw_detail[1] * f,
               1.0 + (raw_detail[2] - 1.0) * f))
    t = (n1[0], n1[1], n1[2] + 1.0)
    u = (-n2[0], -n2[1], n2[2])
    dot_tu = sum(x * y for x, y in zip(t, u))
    tz = max(t[2], 1e-5)
    result = unit(tuple(t[i] * dot_tu / tz - u[i]
                        for i in range(3)))
    encoded = tuple(x * 0.5 + 0.5 for x in result)
    return encoded + a[3:] if len(a) > 3 else encoded


def affine_coefficients(b, factor, blend_mode):
    """Per-component ``(C, D)`` for an affine upper layer: out=C*A+D."""
    bb = _tuple(b)
    f = max(0.0, min(1.0, float(factor)))
    mode = str(blend_mode).upper()
    if mode not in AFFINE_BLEND_MODES:
        raise ValueError("%s is not affine in the incoming value" % mode)
    if mode == "MIX":
        c = (1.0 - f,) * len(bb)
        d = tuple(f * v for v in bb)
    elif mode == "ADD":
        c = (1.0,) * len(bb)
        d = tuple(f * v for v in bb)
    elif mode == "SUBTRACT":
        c = (1.0,) * len(bb)
        d = tuple(-f * v for v in bb)
    elif mode == "MULTIPLY":
        c = tuple(1.0 - f + f * v for v in bb)
        d = (0.0,) * len(bb)
    else:  # SCREEN
        c = tuple(1.0 - f * v for v in bb)
        d = tuple(f * v for v in bb)
    scalar = not isinstance(b, (tuple, list))
    return _shape(c, scalar), _shape(d, scalar)


def compose_affine_coefficients(steps):
    """Collapse bottom-to-top ``(B, factor, mode)`` upper-layer steps."""
    if not steps:
        return 1.0, 0.0
    first = _tuple(steps[0][0])
    c_total = (1.0,) * len(first)
    d_total = (0.0,) * len(first)
    for b, factor, mode in steps:
        c, d = affine_coefficients(b, factor, mode)
        cc, dd = _tuple(c), _tuple(d)
        c_total = tuple(x * y for x, y in zip(cc, c_total))
        d_total = tuple(x * y + z for x, y, z in
                        zip(cc, d_total, dd))
    scalar = not isinstance(steps[0][0], (tuple, list))
    return _shape(c_total, scalar), _shape(d_total, scalar)


def _sample(samples, image_name):
    try:
        sample = samples[image_name]
    except KeyError:
        raise KeyError("missing preview sample for image %r" % image_name)
    return sample if isinstance(sample, PixelSample) else PixelSample(sample)


def _source_value(layer, binding, channel, samples, resident_samples):
    if binding.mode == "COLOR":
        value = (float(binding.color[0]) if channel.kind == "SCALAR"
                 else tuple(float(x) for x in binding.color))
        return PixelSample(value, 1.0)
    if binding.mode == "VALUE":
        value = float(binding.value)
        if channel.kind != "SCALAR":
            value = (value, value, value, 1.0)
        return PixelSample(value, 1.0)
    if layer.uid in resident_samples and channel.key in resident_samples[layer.uid]:
        return resident_samples[layer.uid][channel.key]
    image_name = model.binding_image(layer, binding)
    if layer.layer_type == "PAINT" and image_name:
        return _sample(samples, image_name)
    return PixelSample(model.seed_native(channel), 1.0)


def _paint_alpha(layer, binding, samples, resident_samples):
    image_name = model.binding_image(layer, binding)
    if layer.layer_type != "PAINT" or not image_name:
        return 1.0
    resident = resident_samples.get(layer.uid, {}).get(binding.key)
    if resident is not None and binding.mode == "SHARED":
        return float(resident.alpha)
    return float(_sample(samples, image_name).alpha)


def _factor(model_stack, layer, binding, paint_alpha, samples):
    factor = model.const_factor(model_stack, layer, binding)
    if not binding.use_masks:
        return factor
    image_name = model.binding_image(layer, binding)
    if layer.layer_type == "PAINT" and image_name:
        factor *= paint_alpha
    for mask in layer.masks:
        if not (mask.visible and mask.image_name):
            continue
        value = float(_sample(samples, mask.image_name).value)
        op = float(mask.opacity)
        factor *= (1.0 - value * op) if mask.invert else (
            value * op + 1.0 - op)
    return factor


def compose_channel_pixel(model_stack, channel_key, samples,
                          resident_samples=None):
    """Compose one complete channel at one pixel, bottom to top.

    ``resident_samples`` maps ``layer_uid -> channel_key -> PixelSample`` and
    substitutes active GPU data for that layer's Blender image sample.
    """
    channel = model.CHANNEL_MAP[channel_key]
    value = model.seed_native(channel)
    resident_samples = resident_samples or {}
    for layer in _composition_layers(model_stack):
        binding = _binding(layer, channel_key)
        if not _participates(model_stack, layer, binding):
            continue
        source = _source_value(layer, binding, channel, samples,
                               resident_samples)
        paint_alpha = _paint_alpha(layer, binding, samples, resident_samples)
        factor = _factor(model_stack, layer, binding, paint_alpha, samples)
        blend = model.effective_blend(layer, binding)
        if channel_key == "normal":
            value = blend_tangent_normals_rnm(value, source.value, factor)
        else:
            value = blend_value(value, source.value, factor, blend)
    return value
