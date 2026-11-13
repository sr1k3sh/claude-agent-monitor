# Privacy policy

Agent Monitor runs entirely on your computer. It has no server, account, analytics or telemetry, and it makes no outbound network requests.

## What it reads

Claude Code's session transcripts under `~/.claude/projects/`, only for the repositories in your watch list. Transcripts can contain anything you or Claude wrote in a session, including personal data such as names or email addresses. The dashboard shows excerpts of them (prompts, tool calls, token counts) to you, in your browser, from a server bound to `127.0.0.1`.

## What it stores

Only two kinds of files, under `~/.claude/agent-monitor/`:

- `scopes.json`: the paths of the repositories you chose to watch.
- `pipelines/*.json`: task pipelines (a title, stage names, a session id) that you or Claude create.

It does not copy transcript content anywhere.

## What it shares

Nothing. No data is sent to the author, to Anthropic or to any third party.

## Retention and deletion

Nothing is retained outside your machine. To remove everything the plugin stored, delete `~/.claude/agent-monitor/` and uninstall the plugin (`claude plugin uninstall agent-monitor@sr1k3sh-plugins`).

## Contact

Questions: open an issue at https://github.com/sr1k3sh/claude-agent-monitor/issues.
