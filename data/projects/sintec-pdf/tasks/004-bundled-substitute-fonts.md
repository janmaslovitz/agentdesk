+++
id = "004"
title = "Ship metric-compatible substitute fonts with the app"
tier = "standard"
depends = ["001"]
+++

Bundle open fonts that stand in for common document fonts so edited text keeps its layout even on
machines without them: Carlito (= Calibri), Caladea (= Cambria), Tinos (= Times New Roman),
Cousine (= Courier New), PT Sans (general sans with good Cyrillic). Fonts are never committed
(CLAUDE.md, AGENTS.md §1.4). Follow the OCR-model pattern exactly:

1. ATTRIBUTION.toml: add `[[fetched]]` entries with kind = "substitute-font". Drafted entries are
   in C:/Users/kochurin.a/sintec-pdf/wip-0.2.7/substitute-fonts.toml (pinned google/fonts commit,
   sha256 computed). Tinos is missing: its directory ofl/tinos at that commit has no OFL.txt —
   find its licence file (LICENSE.txt / the repo's licence) and compute hashes the same way
   (see wip-0.2.7/fontmanifest.py). Verify each font has Cyrillic.
2. xtask: like `cargo xtask models` (xtask/src/assets.rs `models`), add `cargo xtask fonts [DIR]`
   fetching kind "substitute-font" into assets/substitute-fonts/ (git-ignored) with licences.
3. Packaging: packaging/windows/package.ps1 copies them to `fonts\` beside sintec-pdf.exe (as it
   does models\), packaging/windows/pdfcraft.wxs gets a component for them; macOS/Linux packaging
   scripts similarly if they ship models.
4. Runtime: crates/engine/src/text_fonts.rs gets `bundled_directories()` (exe_dir/fonts,
   ../Resources/fonts, ../share/pdfcraft/fonts, and the source tree's assets/substitute-fonts);
   discovery includes them; FontFile gets `bundled: bool` and the UI labels them "included with
   Sintec.PDF". Installed exact matches still win over bundled ones; bundled metric-compatible
   faces count as Match::Alias for their originals (ALIASES already pairs carlito/calibri,
   caladea/cambria, tinos/timesnewroman, cousine/couriernew; add ptsans to the arial group only as
   a class fallback, not alias).
5. `cargo xtask assets` must pass; run `cargo xtask assets --write` to regenerate ATTRIBUTION.md.
