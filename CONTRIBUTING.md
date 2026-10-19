# Contributing

This app handles business receipt images and explicit scanner jobs. Keep changes focused and preserve the boundaries below.

## Local checks

Use Python 3.13 and a current Node.js LTS release. From `lexware_upload_bridge/`:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pip check
python -m pytest -q tests
node --test tests/test_browser_profile.js
```

Tests use generated images, temporary storage, mocks and temporary loopback servers. A loopback test skipped because the environment forbids binding is not a completed check. Never put real scanner addresses, receipts, account identifiers or credentials into fixtures or CI.

## Required behavior

- Startup, status and archive-history import make no scanner or provider calls.
- Each scan is an explicit request with its own request ID. Lost responses must be reconciled against that ID, never the newest job.
- Original and upload copy must be durable and hash-verified before the remote write.
- Persist upload intent before sending. Preserve valid acknowledgements and block duplicate or uncertain uploads across restarts.
- The direct provider transport sends only a receipt file to the fixed HTTPS endpoint, with no redirects, environment proxies, automatic retries or backend fallback.
- Archive import requires both operation gates off and an empty destination journal. Validate every record, acknowledgement and file before committing any imported job. Incomplete or unknown states must fail without partial import or network access.
- Preserve authentication, CSRF checks, file-path restrictions, safe error messages and secret redaction.

Add tests for changed behavior and failure paths. Do not add tests that require a real Lexware account or a physical scanner. Report exactly what was checked; a successful unit test or image build does not establish a successful real receipt upload.

## Versions and releases

Change the app version only in `lexware_upload_bridge/config.yaml` and add the matching changelog entry. The runtime and release scripts read this value; CI passes it to the Docker build. A release tag must identify that same version. Keep the repository URL and app slug stable so existing Store installations remain updateable.

Release checks must pass before publishing the AMD64 and ARM64 images to GHCR. Test dependency changes together and build both architectures. Update pinned image references and dependency locks deliberately; do not silently replace an existing version tag with different source.

Changes to journal formats, archive validation or configuration need an explicit migration and rollback description. Preserve existing jobs and receipt files. Do not make a release migration send or resend receipts.

## Reports and pull requests

Describe the problem, resulting behavior and relevant checks. Include only synthetic or redacted examples. Use the private reporting route in [SECURITY.md](SECURITY.md) for security issues; do not publish credentials or receipt contents in an issue.
