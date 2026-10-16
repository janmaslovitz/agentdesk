+++
id = "001"
title = "Finish the font analysis WIP and make the branch green"
tier = "standard"
crates = ["pdfcraft-edit", "pdfcraft-fonts", "pdfcraft-engine", "pdfcraft-ui-egui"]
+++

The last commit on feature/0.2.6 ("WIP 0.2.7: font recognition…") compiles but isn't green:
`PdfCraftApp::page_fonts` and `suggest_fonts` in crates/ui-egui/src/font_substitute.rs are unused,
so `cargo clippy -D warnings` fails.

Do:
1. Call `suggest_fonts(id, page)` when Edit PDF's text editing is active and a page is shown
   (find where the edit-text overlay runs: crates/ui-egui/src/edit_text_ui.rs `page_input`, or
   where `QuickTool` for editing text is handled). It opens the Font substitution dialog once per
   font that has no similar installed face (Match::Other). Don't call it every frame without
   need: page_fonts is cached per document version.
2. Tests:
   - pdfcraft-edit: `font_facts` reads the embedded program's family/weight (a synthetic
     CIDFontType2 whose FontFile2 has a name table saying e.g. family "Rect Font", weight 700, while
     /BaseFont is "CIDFont+F1"); `first_unencodable` returns the first char the font can't encode.
     The fixture font generator in crates/engine/tests/support/font_fixture.rs builds fonts in memory.
   - pdfcraft-engine text_fonts: `Wanted::from_facts` prefers program family/weight over flags.
   - A styled edit: asking bold for a paragraph whose real family has an installed/registered bold
     face uses that face (resource) instead of standard Helvetica-Bold.
3. Add Russian strings for new UI text to crates/ui-egui/src/i18n/ru.tsv (the `sintec_guard` test
   checks coverage), e.g. "No similar font is installed. Choose the font edited text in it is set in."
   After editing ru.tsv run `python zz_rehash.py crates/ui-egui/src/i18n/ru.tsv` (refreshes its
   ATTRIBUTION.toml hash) and `cargo xtask assets`.

Done when clippy is clean and the tests of the four crates pass.
