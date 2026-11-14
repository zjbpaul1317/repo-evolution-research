# Real workflow acceptance, 2026-11-14

This is an operational regression check for collection and snapshot integrity. It is not an independent assessment of research accuracy.

## Environment and scope

- Repository: public `vllm-project/router`, GitHub CLI `2.73.0`, authenticated account `zjbpaul1317`.
- Tool checkout before the progress/runner changes: commit `4374647`; macOS arm64, Python `3.13.3`, `jsonschema 4.26.0`.
- Window: 14 days, `2026-10-30T07:38:17Z` through `2026-11-13T07:38:17Z`, configured as `Asia/Shanghai`.
- Run: `20261113T073817Z-eca0b702`, stored under ignored `.repo-research/acceptance-20261113/`.
- Limits: 300 requests, 150 MiB total response, 8 MiB per response, 20 minutes, four deep entities and two deep PRs.
- GitHub calls were read-only. The run and its authored selection file are local, ignored data.

## Results

Initial collection returned complete coverage with 54 requests, 5,542,665 response bytes, and 237 activity candidates. It read 13 documents (11 fetched and two reused from the repository profile), one PR detail, and two issue details; three deep entities were consumed. Discussion collection was not applicable for this repository profile. The `docs` channel was complete with one non-text path filtered.

`prepare` produced 1,158 evidence blocks from 125 text sources and 55 relations. The first `audit` passed, rechecking all 1,158 quotes, 55 relations, and 187 text blobs.

A second full collection with `--force` also completed. Cumulative usage reached 96 requests and 9,704,210 response bytes. All 13 document snapshots were reused, existing deep reads remained present, and deep-entity usage stayed at three. Activity search returned 222 candidates on the second pass versus 237 on the first; GitHub search results can change while a window is being collected.

After preparing again, a selection for PR #396 fetched one entity over eight pages and eight requests without a partial result. Deep usage moved from three to the configured maximum of four; PR detail usage moved from one to two. Final cumulative run usage was 104 requests, 10,902,865 response bytes, and four deep entities.

The final `prepare` completed with 1,535 evidence blocks from 253 text sources and 109 relations. Final `audit` passed: 1,535 quotes, 109 relations, and 419 text blobs checked, with no problems or recovery actions.

## Limits

The run exercises public REST and GraphQL reads, repeat collection, document reuse, selection deep-read, preparation, and audit. It does not validate authored research claims, private-repository access, rate-limit recovery, forced termination and resume in a live collection, Linux behavior, or target-project tests. Progress reporting and bounded child-process output were added after the collection was performed; the revised runner received a separate single-request live smoke test.
