#!/usr/bin/env bash
# Export the Mermaid diagrams in docs/architecture.md to SVG files in docs/diagrams/.
# Uses @mermaid-js/mermaid-cli via npx with a local Chrome/Chromium (no browser download).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "$(command -v chromium || true)" "$(command -v chromium-browser || true)" "$(command -v google-chrome || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then export PUPPETEER_EXECUTABLE_PATH="$candidate"; break; fi
  done
fi
if [[ -z "${PUPPETEER_EXECUTABLE_PATH:-}" ]]; then
  echo "No Chrome/Chromium found; set PUPPETEER_EXECUTABLE_PATH" >&2
  exit 1
fi
export PUPPETEER_SKIP_DOWNLOAD=true

mkdir -p docs/diagrams
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# Split each ```mermaid block of the architecture doc into its own file.
awk -v dir="$tmp" '/^```mermaid/{n++; f=sprintf("%s/%02d.mmd", dir, n); inblock=1; next} /^```/{inblock=0; next} inblock{print > f}' docs/architecture.md
names=(system agent-graph chat-sequence data-model caching evaluation security)
i=0
for src in "$tmp"/*.mmd; do
  name="${names[$i]:-diagram-$i}"
  npx -y @mermaid-js/mermaid-cli@11.4.2 -i "$src" -o "docs/diagrams/${name}.svg" -b transparent
  i=$((i + 1))
done
echo "Rendered $i diagrams to docs/diagrams/"
