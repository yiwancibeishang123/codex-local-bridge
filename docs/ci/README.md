# Optional GitHub Actions configuration

The initial publisher is authenticated with GitHub `repo` scope but not `workflow`
scope. GitHub rejected pushing a file under `.github/workflows/`; the source was
therefore published with this configuration as a template. **No hosted CI run or
green CI status is claimed.** Local tests, including explicit real Codex opt-in,
passed separately; see [validation](../VALIDATION.md).

To enable hosted CI later, use a credential with workflow write permission, copy
`docs/ci/tests.yml` to `.github/workflows/tests.yml`, commit and push. Verify the
result in Actions. Review the immutable action references before updating them.

The template targets Ubuntu 22.04 and Python 3.10/3.12, installs bubblewrap, and
runs the credential-free test suite. The real Codex test is intentionally skipped.
No API keys, ChatGPT sessions or private Bridge config should be added as secrets.
A runner that cannot create user/mount namespaces should report a failure rather
than bypassing the sandbox or describing skipped isolation as a success.
