+++
id = "006"
title = "Identify anonymised fonts by their glyph widths"
tier = "standard"
depends = ["001"]
crates = ["pdfcraft-engine", "pdfcraft-fonts", "pdfcraft-edit"]
+++

Some PDFs name fonts "CIDFont+F1" and embed programs without usable name tables (CFF/Type 1, or
stripped TrueType), so `FontFacts.program_family` is None and matching falls back to class only.
Add a width fingerprint: at discovery (crates/engine/src/text_fonts.rs `discover`) record each
installed face's advance widths (per 1000 em) for a fixed probe set of characters (Latin and
Cyrillic letters, digits, common punctuation). For a PDF font, take the widths of those characters
it has (via ToUnicode + /W or /Widths, normalised to 1000 em; pdfcraft_fonts::pdf::Metrics has the
pieces) and score installed faces by mean absolute difference over the shared characters (needs at
least ~8 shared). A close fingerprint (e.g. < 15 units mean) identifies the family: use it in
`rank` as a strong signal (between Exact and Alias), and show it in the analysis.
Tests: synthetic fonts with known widths are identified; too few shared characters gives no claim.
