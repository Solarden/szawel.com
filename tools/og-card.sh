#!/bin/sh
# Render a case study's link-preview card: tools/og-card.sh <slug> <kicker> <title> <number>
# writes work/og/<slug>.png at 1200x630, the size LinkedIn and Slack expect.
set -eu

slug=$1
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
here=$(cd "$(dirname "$0")" && pwd)
query=$(python3 -c 'import sys, urllib.parse as u; print(u.urlencode(dict(zip(("kicker", "title", "number"), sys.argv[1:]))))' "$2" "$3" "$4")

mkdir -p "$here/../work/og"
"$chrome" --headless=new --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --force-device-scale-factor=1 --window-size=1200,630 --virtual-time-budget=3000 \
    --screenshot="$here/../work/og/$slug.png" "file://$here/og-card.html?$query" 2>/dev/null
