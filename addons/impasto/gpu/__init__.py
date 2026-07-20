# SPDX-License-Identifier: GPL-2.0-or-later
"""Focused implementation helpers for Impasto's GPU painting engine.

``impasto.gpu_engine`` remains the compatibility-facing module.  New code
belongs in small modules here so the engine can shed independent concerns
without breaking existing imports or saved Blender workflows.
"""

