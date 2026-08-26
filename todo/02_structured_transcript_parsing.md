# Task 02: Structured Content Block & Transcript Parsing

## Checklist
- [ ] Implement a transcript block extractor in `scripts/ingest.py` / `src/context_monitor/` that extracts structured blocks (`system`, `messages`, `tools`, `response`).
- [ ] Intercept and parse `<think>...</think>` reasoning tags and planning blocks into discrete `thinking` blocks.
- [ ] Parse tool call invocations, extracting tool names, tool call IDs, and normalized JSON arguments.
- [ ] Parse tool execution results and link them to their originating tool call ID.
- [ ] Extract full tool schemas offered by Antigravity and registered MCP servers.
- [ ] Generate SHA-1 content hashes for every extracted block to support turn-to-turn diffing.
- [ ] Ensure backward compatibility with all historical session transcripts in SQLite.

## Breakdown
Following the architecture of CC Glass (`src/formats/` and `src/diff.js`), we must convert raw transcript steps into an array of structured, typed content blocks instead of passing unparsed string blobs to the frontend.

## Full Details
1. **Block Normalization (`src/formats/` pattern)**:
   - Each transcript step will be parsed into canonical block types:
     - `system`: System instructions, workspace context, and active skill rules.
     - `tools`: Available tool definitions and MCP schemas.
     - `user`: User input requests and attached file context.
     - `thinking`: Model reasoning and planning traces.
     - `tool_use`: Tool invocation with `name`, `call_id`, and `arguments`.
     - `tool_result`: Tool output with `call_id`, `result`, and `success` status.
     - `assistant`: Text response emitted to the user.
2. **Deterministic Block Hashing**:
   - Compute `sha1(kind + ":" + text)` for every block to enable instant turn-to-turn comparison.
3. **Tool Schema Ingestion**:
   - Capture tool descriptions and JSON parameters from native tools and MCP server declarations.
