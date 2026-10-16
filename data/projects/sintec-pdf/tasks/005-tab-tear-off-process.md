+++
id = "005"
title = "Tab tear-off opens an independent window and process"
tier = "hard"
crates = ["pdfcraft-ui-egui"]
+++

Dragging a document tab out of the window today creates a viewport inside the same process
(crates/ui-egui/src/doc_windows.rs, tabs drag area in the tab widget code). Problems the user
reported: closing the torn-off window puts the document back as a tab in the main window, and
closing the main window closes the torn-off one too.

Wanted: tearing a tab off starts a separate Sintec.PDF process that owns the document, and the tab
is removed from the original window, so the two close independently.
- Hand-off: a saved document: spawn the current executable with its path (and page/zoom if easy).
  A document with unsaved changes or no path: write its current bytes (the engine's save/export of
  the working state) to a temp file under the app's data dir and open that in the new process
  marked as unsaved/untitled with the original name, so the user is still asked to save.
- Position the new window where the tab was dropped if the platform allows (eframe viewport
  position via command-line argument the app accepts).
- Keep single-instance logic (if any) from swallowing the spawn.
- Remove the in-process torn-off viewport path, or keep it only where spawning isn't possible (wasm).
Tests: unit-test the hand-off (temp file written, argument list built); keep existing tab tests green.
