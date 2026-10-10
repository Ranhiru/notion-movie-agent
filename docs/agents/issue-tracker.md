# Issue tracker: Local Markdown

Issues and specs live in `.scratch/<feature-slug>/`.

- Spec: `spec.md`.
- Implementation tickets: `issues/<NN>-<slug>.md`, numbered from 01.
- Triage state: a `Status:` line near the top, using triage-labels.md.
- Comments: append under `## Comments`.
- Publishing means creating the corresponding local Markdown file.
- Fetching a ticket means reading its referenced file.
- Preserve existing feature directories and tickets.

## Wayfinding

- Map: `.scratch/<effort>/map.md`; child tickets live in `issues/`.
- Record ticket type with `Type: research|prototype|grilling|task`.
- Record dependencies with `Blocked by: NN, NN`.
- Choose the first numbered open, unclaimed ticket whose blockers are resolved.
- Claim by saving `Status: claimed` before work.
- Resolve by appending `## Answer`, setting `Status: resolved`,
  and adding a summary and link to the map's Decisions-so-far.
