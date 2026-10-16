+++
id = "003"
title = "Bigger, reliable drag handle for text boxes in Edit PDF"
crates = ["pdfcraft-ui-egui"]
+++

In Edit PDF, moving a text box (paragraph) by dragging is unreliable and its handle is too small.
The code is in crates/ui-egui/src/edit_text_ui.rs: `grab_at`, `edge_at`, `width_handles`,
`draw_handles`, and the `BlockDrag` start in `page_input` (around "Dragging a box ... moves it").

Make the move handle clearly visible and at least 16×16 logical px (with a larger invisible hit
area, ~24 px), placed where it doesn't overlap the text (e.g. a grip above the top-left corner),
plus keep dragging the box body working. Starting a drag must be reliable: grab on press (egui's
drag threshold already handled via press_origin) and don't lose the drag when the pointer moves
fast or leaves the box. Show the move cursor over the handle.

Add a kittest test in crates/ui-egui/tests/ that drags a paragraph by its handle and checks it moved
(see existing editing tests for harness setup).
