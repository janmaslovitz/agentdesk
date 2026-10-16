+++
id = "002"
title = "Photoshop-like floating text panel on the right"
tier = "hard"
depends = ["001"]
crates = ["pdfcraft-ui-egui"]
+++

The user wants the text formatting controls ("Format text": font, size, bold/italic, colour,
alignment, spacing, the font status line, Replace font…, installed fonts list) out of the left
tools panel and in a floating panel on the right side of the document area, immediately left of
the right icon rail (crates/ui-egui/src/chrome.rs `right_rail`). It opens only while text is being
edited (a paragraph open in the line editor: `views[i].line_editor.is_some()`, or a selected added
text box / the Add text tool) and closes when editing stops.

Today the controls live in crates/ui-egui/src/panels.rs `format_section` (called from the tools
panel around line 206). Move them into a new module (e.g. text_panel.rs) drawn as an egui Area or
Window anchored to the right edge, styled like the app's other panels (theme::Tokens, widgets).
It must not cover the right rail, must not steal focus from the text being typed, and must keep
working with the docking workspace and torn-off document windows (doc_windows.rs).

Keep behaviour identical otherwise (every control applies as it does now). Add a kittest UI test
(crates/ui-egui/tests/) that opening a paragraph shows the panel and Esc closes it. Add Russian
strings to ru.tsv for any new text (then `python zz_rehash.py crates/ui-egui/src/i18n/ru.tsv`).
For visual checking, `cargo run -p pdfcraft-ui-egui --example shot` takes a headless screenshot.
