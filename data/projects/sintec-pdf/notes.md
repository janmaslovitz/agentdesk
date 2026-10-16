# Project notes for agents (durable facts; appended by finished tasks)

- Sintec.PDF is a fork of PdfCraft (storytold/pdfcraft); interface languages are English and Russian only. Every new UI string needs a ru.tsv line; after editing ru.tsv run `python zz_rehash.py crates/ui-egui/src/i18n/ru.tsv`.
- Never commit fonts, PDFs from Downloads (they hold credentials) or corpus files. Fonts come from craft-fonts or `[[fetched]]` entries.
- Never panic in non-test code (CLAUDE.md): no unwrap/expect/indexing with input-derived positions; bound loops and allocations on PDF input.
- Font editing: FontFacts (crates/edit/src/text.rs) reads the embedded program's family/weight; PDFs often name fonts `CIDFont+F1`. Installed faces are embeddable when OS/2 fsType allows it (crates/fonts/src/embedding.rs validate_document_input).
- Gates: cargo fmt --all --check; cargo clippy (-D warnings); tests. UI tests use egui_kittest (crates/ui-egui/tests/).
