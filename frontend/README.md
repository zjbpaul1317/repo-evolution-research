# Evolution research workbench

A responsive, local-first frontend for Repository Evolution Research. The interface uses Chinese and presents the existing CLI's research concepts without running collection or model inference in the browser.

## Run locally

Use Node.js **22.12+** (or 20.19+). From the repository root:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. The server binds only to the loopback interface. If port 5173 is occupied, stop the old development server or run `npm run dev -- --port 5174`.

```bash
npm test         # Snapshot import, report and command-generation checks
npm run build   # Strict TypeScript checking and production bundle
npm run preview # Preview the production bundle at http://127.0.0.1:4173
```

## Explore

- **Overview:** research window, metrics derived from the loaded snapshot, an interactive source-activity chart, animated research illustration, workflow and topic previews.
- **Topics:** text search, research gaps, claims, confidence, linked capabilities and evidence.
- **Evidence ledger:** source and verification filters, text search, chronological sorting, pagination, session bookmarks and source-detail drawers.
- **Capabilities:** separate proposal, implementation, delivery and availability states, plus scope, requirements, revision and evidence.
- **Report:** Markdown preview, source view, section navigation and `.md` download. Imported `report.md` is preserved verbatim; a generated browser reading draft is explicitly labeled and does not replace CLI `validate` / `render`.
- **New research:** configure a repository, quick / standard / deep mode, time window and focus terms. Copy the real CLI workflow, including the explicit Agent writing step.
- **Global search:** open with `⌘K` / `Ctrl+K`; find topics, capabilities and evidence. Dialogs support Escape, focus containment and focus restoration.

The initial `acme/nebula` workspace is **fictional demonstration data**: 6 topics, 12 capabilities and 48 evidence records. It is not a research result for a real repository. No GitHub requests or shell commands run when using the demo or generating commands.

## Import real research

Choose **导入快照**, then select one run directory, or select multiple files together:

```text
<run>/
  manifest.json              # Optional; repository and time window
  analysis/research.json     # Required; schema 1.2 or 1.3
  normalized/evidence.jsonl  # Optional; source excerpts and locators
  report.md                  # Optional; original rendered report
```

The importer accepts the repository's `research.sample.json`, `manifest.sample.json`, `evidence.sample.json` and `baseline-research.json` fixtures as well. It rejects duplicate snapshots, mismatched manifest/research run IDs, malformed evidence and duplicate evidence IDs. Missing evidence is disclosed, never substituted with demo content.

Import validates the shapes required for display, **not the complete CLI contract, content hashes, source authenticity, lifecycle rules or semantic correctness**. Run `audit` / `validate` through the CLI for those checks. Verification badges retain the input's labels; they do not mean the browser independently verified a source.

Only recognized research artifacts are read. Maximum file size is 12 MB and maximum combined size is 32 MB. Files stay in browser memory; nothing is uploaded. Imported data and bookmarks reset on refresh. Import the complete snapshot together, because a subsequent import replaces the previous dataset.

## Implementation

React 19, TypeScript, Vite, Lucide icons, Zod, React Markdown and remark-gfm. DM Sans and Manrope are bundled locally; Chinese text uses system fonts. No remote image or font service is required.

The UI adapts to desktop, tablet and phone layouts. SVG and CSS provide orbit motion, flowing connections, chart entrance and hover effects. `prefers-reduced-motion` disables nonessential animation. Markdown raw HTML is not executed, external image loading is suppressed, and outbound links are restricted to HTTP(S); report-local footnotes remain navigable.

Source layout:

```text
src/
  App.tsx                  # Workspace shell, navigation and session state
  data.ts                  # View types, demo data and reading-draft generation
  import.ts                # Snapshot parsing and CLI command generation
  components/Overview.tsx  # Research dashboard
  components/ResearchPages.tsx
  components/Dialogs.tsx   # Import, new research, search and details
  components/Visuals.tsx   # Animated illustration and activity chart
  components/UI.tsx       # Shared accessible controls
  styles.css               # Design tokens, layout, motion and breakpoints
```

The frontend is independent of the Python CLI and does not change its storage or research pipeline. To connect live collection later, add an explicit local service with job state, cancellation and error reporting; the present interface intentionally generates commands and reads completed artifacts.
