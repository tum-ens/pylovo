# Project Instructions

These facts are always true for this project. Apply them to every session without exception. If any task conflicts with one of these, flag it before proceeding.

## Technical Requirements

Tech stack for this project. Always use these. Never suggest alternatives unless asked:

- OS: Ubuntu
- Language: Python
- Package manager: `uv`
- Database: PostgreSQL with PostGIS

Additional technical rules:

- Find database connection details in the `.env` file.
- Always use `uv run` and the `pyproject.toml` environment to test changes.
- Use `<package> --help` to get further information on dependencies if required.

## Behavioral Requirements

- Never open responses with filler phrases like "Great question!", "Of course!", "Certainly!", or similar warmups. Start every response with the actual answer. No preamble, no acknowledgment of the question.
- If you are uncertain about any fact, statistic, date, or piece of technical information, say so explicitly before including it. Never fill gaps in your knowledge with plausible-sounding information. When in doubt, say so.

## Core Requirements

1. Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements.
2. Simplest solution first. Always implement the simplest thing that could work. Do not add abstractions or flexibility that weren't explicitly requested.
3. Don't touch unrelated code. If a file or function is not directly part of the current task, do not modify it, even if you think it could be improved. If you notice something worth fixing elsewhere, mention it in a note at the end. Do not touch it. Ever.
4. Flag uncertainty explicitly. If you are not confident about an approach or technical detail, say so before proceeding. Confidence without certainty causes more damage than admitting a gap.

Additional core rules:

- You are working in a greenfield project environment, so you don't need to add fallbacks for backward compatibility if not explicitly specified.
- For any task involving architecture decisions, debugging complex issues, or non-trivial features, work through the problem step by step before writing any code. Show your reasoning. Identify where you're uncertain. Then implement.
- Before any significant task, show me 2-3 ways you could approach this work and point out pros and cons. Wait for me to choose before proceeding.

## Memory Requirements

- Maintain a file called `MEMORY.md` in this project. After any significant decision, add an entry:
  - What was decided
  - Why
  - What was rejected and why
- Read `MEMORY.md` at the start of every session. Never contradict a logged decision without flagging it first.
- When the user says "session end", "wrapping up", or "let's stop here", write a session summary to `MEMORY.md`. Include:
  - Worked on
  - Completed
  - In progress
  - Decisions made
  - Next session priorities
- Maintain a file called `ERRORS.md`. When an approach takes more than 2 attempts to work, log it:
  - What didn't work
  - What worked instead
  - Note for next time
- Check `ERRORS.md` before suggesting approaches to similar tasks.
