# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through
[GitHub security advisories](https://github.com/nelsonrogers/youtube-studio-mcp/security/advisories/new)
rather than public issues. You'll get an acknowledgement within a few days; fixes are released as
soon as practical and credited unless you prefer otherwise.

Only the latest release is supported.

## How the server protects your account

- **Local only.** The server talks to MCP clients over stdio and to Google's APIs over HTTPS. It
  does not open network ports, except a short-lived `127.0.0.1` listener during sign-in.
- **Your own OAuth client.** No shared credentials; tokens never leave your machine.
- **Token storage.** Tokens and the client secret are written atomically with `0600` permissions
  inside a `0700` directory (`~/.config/youtube-studio-mcp`, or `%APPDATA%\youtube-studio-mcp` on
  Windows, where the user-profile ACLs apply). `youtube-studio-mcp logout` revokes and deletes them.
- **Sign-in flow.** Authorization code flow with PKCE, a random `state` that is verified, and a
  loopback redirect bound to `127.0.0.1` on a random port.
- **Least privilege.** Read-only scopes by default. Write scope (`youtube.force-ssl`) and write
  tools are separate opt-ins, and write tools default to dry runs / confirmation.
- **File output.** Tools only write chart/data files (`.png .svg .pdf .csv .json`) and refuse to
  write inside the credentials directory. Report downloads are restricted to
  `youtubereporting.googleapis.com`.

## Prompt injection

Comment text, video titles and descriptions are untrusted content written by other people. They
are returned to your agent as data. Keep write tools disabled unless you need them, and review
what your agent proposes before approving any write action.
