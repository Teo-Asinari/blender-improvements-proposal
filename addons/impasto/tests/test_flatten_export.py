import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ADDONS = os.path.dirname(os.path.dirname(HERE))
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import flatten_export, model


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS:", label)


def rgba_image(name, color):
    image = bpy.data.images.new(name, 2, 2, alpha=True)
    image.pixels.foreach_set(list(color) * 4)
    return image


source = rgba_image("Flatten Source", (0.0, 1.0, 0.0, 0.5))
bottom = model.LayerModel(
    uid="bottom", label="Bottom", layer_type="FILL",
    bindings=(model.BindingModel("base_color", mode="COLOR",
                                 color=(1.0, 0.0, 0.0, 1.0)),))
top = model.LayerModel(
    uid="top", label="Top", layer_type="PAINT",
    bindings=(model.BindingModel("base_color", image_name=source.name),))
stack = model.StackModel("Test", ("base_color",), (top, bottom))
before = tuple(source.pixels[:])
result = flatten_export.composite_channel(stack, "base_color", 2, 2)
stored_alpha = float(flatten_export._pixels(source)[0, 0, 3])
check("paint alpha gates the source over the fill",
      all(abs(float(result[0, 0, i]) - v) < 1e-5
          for i, v in enumerate((1.0 - stored_alpha, stored_alpha,
                                 0.0, 1.0))))
check("source image is unchanged", tuple(source.pixels[:]) == before)

scalar = rgba_image("Flatten Scalar", (0.25, 0.25, 0.25, 1.0))
layer = model.LayerModel(
    uid="paint", layer_type="PAINT",
    bindings=(model.BindingModel("roughness", image_name=scalar.name),))
stack = model.StackModel("Test", ("roughness",), (layer,))
result = flatten_export.composite_channel(stack, "roughness", 3, 5)
check("explicit output dimensions are honored", result.shape == (5, 3, 4))
check("material export is opaque", bool((result[..., 3] == 1.0).all()))
print("flatten export tests passed")
