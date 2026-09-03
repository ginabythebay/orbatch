#!/usr/bin/env bash
payload=$(cat)
tool=$(jq -r '.tool_name // ""' <<<"$payload" 2>/dev/null)

# Agents background by default in 2.1.227, so an omitted flag has to read as
# background there; on builds that default the other way this costs one
# explicit `false` and denies nothing that was safe.
case "$tool" in
  Agent)
    default=true
    guidance="Nothing re-invokes you when a background agent finishes. Call it again with run_in_background: false — it then runs inline and hands you its report within this turn."
    ;;
  *)
    default=false
    guidance="Nothing re-invokes you when a background task finishes. Foreground Bash caps at 600s, so a long job must be polled across calls: start it with \`setsid cmd > file 2>&1 < /dev/null &\` and poll the file with a foreground until-loop, repeating in each call until it completes."
    ;;
esac

# `//` treats an explicit false as absent, which would ignore the opt-out.
backgrounded=$(jq -r --argjson default "$default" \
  'if .tool_input | has("run_in_background") then .tool_input.run_in_background else $default end' \
  <<<"$payload" 2>/dev/null)
[ "$backgrounded" = true ] || exit 0

jq -n --arg guidance "$guidance" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny",
  permissionDecisionReason: "Headless run: background tool calls are disabled.",
  additionalContext: $guidance}}'
