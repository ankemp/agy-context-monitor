# Task 05: Granular Turn Inspector, Telemetry & Export Tooling

## Checklist
- [ ] Implement a 6-tab modal/drawer inspector in the dashboard:
  - **Overview**: Step latency, TTFT, model, token usage, cache read/write, estimated cost, and status.
  - **Flow**: Chronological decision sequence diagram.
  - **System**: Active system prompt, workspace instructions, and skill definitions.
  - **Messages**: Formatted turn history with foldable bodies.
  - **Tools**: Active tool schemas (native Antigravity + registered MCP servers).
  - **Response & Raw**: Raw step JSON and planner output.
- [ ] Calculate and display step timing metrics: total duration, Time-to-First-Token (TTFT), and generation throughput (tokens/second).
- [ ] Track and display prompt caching metrics (cache read tokens, cache write tokens, cache hit rate percentage).
- [ ] Build one-click export options for individual steps:
  - Export as `cURL` command for replaying requests.
  - Export as standalone JSON payload.
  - Export as formatted Markdown transcript.

## Breakdown
CC Glass sets the benchmark for granular turn inspection by letting developers deeply analyze every facet of a specific agent turn without leaving the browser interface.

## Full Details
1. **Telemetry & Speed Calculations**:
   - For streaming steps with timestamps:
     - `Duration = finished_at - started_at`
     - `TTFT = first_token_at - started_at`
     - `Tokens/sec = output_tokens / (finished_at - first_token_at)`
2. **Cache Tracking**:
   - Track prompt caching economics across models that support context caching.
3. **Replay & cURL Generation**:
   - Reconstruct the full API request payload so developers can replay the exact step against external APIs via cURL or custom scripts.
