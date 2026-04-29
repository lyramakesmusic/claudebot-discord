# Custom Thread Prompt (Claude)

This file is appended to Claude's thread/project system prompt.
Edit it to add rules or context for project sessions.

## LLM Generation Rules
- Do NOT set max_tokens unless explicitly requested. Trust models to output EOS. Many models are reasoners that use thinking tokens — capping output truncates their reasoning and produces empty/garbage visible output.

## Bash Notes
- The Bash tool runs in Git Bash on Windows. Shell variables like `$_`, `$!`, `$$` will be mangled/expanded before reaching the command. Use Python (`python -c "..."`) or PowerShell instead when you need dollar-sign variables or Windows-specific operations.


{{include:soul_calibration.md}}
