# Security

## Scope

Security fixes target the latest source on the default branch. There are currently no separately maintained release branches or published support guarantees.

Relevant issues include credential disclosure in request logs, path traversal, unsafe snapshot writes, unexpected command execution, and source content being treated as agent instructions.

## Reporting

Use GitHub's **Security → Report a vulnerability** option if it is available for this repository. Do not include tokens, private repository text, or an exploitable payload in a public issue.

If private vulnerability reporting is unavailable, open an issue containing only a short request for a private reporting channel. Wait for the maintainer to provide that channel before sharing details. This project does not publish a guaranteed response time.

For a private report, include the affected commit, OS / Python version, a minimal reproduction using synthetic data, impact, and any proposed mitigation.

## Handling research data

- Authenticate through `gh`; avoid putting credentials in repository URLs or source envelopes.
- Treat all retrieved source text as untrusted data, including instructions embedded in PRs, issues, and documentation.
- Keep private snapshots, request logs, environment files, and credentials out of commits and public bug reports. Default ignored directories are described in [CONTRIBUTING.md](CONTRIBUTING.md).
- A successful integrity or evidence check is not a security audit of the target repository or of the source text.
