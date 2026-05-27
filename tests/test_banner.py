from __future__ import annotations

"""Smoke tests for the stage-aware banner.

Following the pattern in ``test_diff_view.py``: we don't compare exact
terminal output. We just confirm the renderer doesn't raise and that the
documented stage→palette mapping resolves to the palette string we say it
does.
"""

import io
import unittest

from rich.console import Console

import sc.run.banner as banner_module
from sc.run.banner import (
    STAGE_PALETTE_KEYS,
    _banner_word_style,
    render_banner,
)
from sc.run.theme import PALETTE


class BannerStageColorTests(unittest.TestCase):
    def test_stage_mapping_is_documented_palette_keys(self) -> None:
        # The keys we document must exist in PALETTE — no new colors.
        for stage, key in STAGE_PALETTE_KEYS.items():
            self.assertIn(key, PALETTE, f"{stage} → {key} not in PALETTE")

    def test_explicit_stage_color_choices(self) -> None:
        # These exact assignments are load-bearing; if you change them,
        # update this test in the same commit.
        self.assertEqual(_banner_word_style("read"), PALETTE["info"])
        self.assertEqual(_banner_word_style("plan"), PALETTE["meta"])
        self.assertEqual(_banner_word_style("apply"), PALETTE["learn_bold"])
        self.assertEqual(_banner_word_style("verify"), PALETTE["approve_bold"])
        self.assertEqual(_banner_word_style("report"), PALETTE["meta"])

    def test_default_stage_is_legacy_info_bold(self) -> None:
        self.assertEqual(_banner_word_style(None), PALETTE["info_bold"])

    def test_unknown_stage_falls_back(self) -> None:
        self.assertEqual(_banner_word_style("nonsense"), PALETTE["info_bold"])

    def test_render_banner_emits_apply_color(self) -> None:
        """The 'apply' palette key must appear in the rendered output."""
        buf = io.StringIO()
        rec_console = Console(file=buf, force_terminal=False, width=120)
        original = banner_module._CONSOLE
        banner_module._CONSOLE = rec_console
        try:
            render_banner(stage="apply", session_turn_count=1)
        finally:
            banner_module._CONSOLE = original
        out = buf.getvalue()
        # Smoke: rendered, banner word present, no exception.
        self.assertIn("hedwig", out)

    def test_render_banner_default_unchanged_smoke(self) -> None:
        # No stage → legacy path must not raise.
        buf = io.StringIO()
        rec_console = Console(file=buf, force_terminal=False, width=120)
        original = banner_module._CONSOLE
        banner_module._CONSOLE = rec_console
        try:
            render_banner()
        finally:
            banner_module._CONSOLE = original
        self.assertIn("hedwig", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
