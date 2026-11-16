# Third-party content

The MIT license in this repository applies to the project's original code and documentation. It does not relicense third-party material quoted or retained as research evidence.

## vLLM evaluation sources

[`evals/cases/vllm-20261104-kv-correctness/sources.json`](evals/cases/vllm-20261104-kv-correctness/sources.json) contains selected public PR bodies, source metadata, and evidence text from [vllm-project/vllm](https://github.com/vllm-project/vllm). Source URLs, observation context, and hashes are retained in the case files. See the [case notes](evals/cases/vllm-20261104-kv-correctness/notes.md) for the scope and limitations.

The vLLM repository publishes its own [license](https://github.com/vllm-project/vllm/blob/main/LICENSE). Individual contributor text, linked artifacts, and quoted material may have separate rights. Retaining a public source URL or a hash does not grant additional redistribution rights. This notice records provenance and does not assert that every PR comment or external artifact is covered by the repository's source-code license.

The case's annotation and baseline research are authored evaluation artifacts; the retained source text remains attributable to its original authors. Repository and product names identify the sources and do not imply endorsement.

## Generated research snapshots

The tool may save source text from other repositories. Those snapshots are excluded from version control by default. Review source licenses, contributor rights, access restrictions, and personal information before publishing a report or redistributing evidence. Add notices for any new third-party material committed to this repository.

## Frontend fonts and dependencies

The workbench bundles DM Sans and Manrope variable fonts through Fontsource. Both fonts use the SIL Open Font License 1.1; the license texts are included in [`frontend/public/licenses/dm-sans.txt`](frontend/public/licenses/dm-sans.txt) and [`frontend/public/licenses/manrope.txt`](frontend/public/licenses/manrope.txt), and are copied into production builds. JavaScript dependencies and Lucide icons retain the licenses distributed in their respective npm packages. Dependency versions are recorded in `frontend/package-lock.json`.
