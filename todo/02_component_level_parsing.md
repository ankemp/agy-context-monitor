# Task 02: Component-Level Message Parsing

## Checklist
- [ ] Update Python backend (`analytics_query.py` or a new parser module) to intercept `<think>` tags in `PLANNER_RESPONSE` steps.
- [ ] Parse JSON arguments and results from `GENERIC` tool blocks.
- [ ] Transform the raw markdown string into a structured JSON array of blocks (e.g., `text`, `think`, `tool_call`, `tool_result`).
- [ ] Update the frontend UI to consume this JSON array and render custom components (collapsible reasoning cards, stylized terminal panels).
- [ ] Test backward compatibility with older conversation transcripts.

## Breakdown
Instead of passing a massive raw Markdown string to the browser, the Python backend will act as a middleware parser. It will slice the agent's text into logical components so the UI can render them cleanly, similar to CanvasMind's specialized widgets.

## Full Details
1. **`<think>` Block Extraction**:
   - Reasoning models output massive `<think>...</think>` tags. The backend should use regex or a parser to extract these blocks, allowing the frontend to render them as collapsed `<details>` or custom accordion UI components.
2. **Tool Execution Cards**:
   - `GENERIC` steps often contain raw JSON arrays of tool arguments and stdout/stderr text.
   - The parser should identify tool names (e.g., `run_command`, `view_file`) and format their arguments and results into a structured format.
   - The UI will render these as native-looking floating panels or stylized terminal windows.
3. **Context References**:
   - Intercept internal file URI references (`file:///Users/...`) and convert them into clickable HTML badges or buttons in the dashboard.
