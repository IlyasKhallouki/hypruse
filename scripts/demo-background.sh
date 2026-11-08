#!/usr/bin/env bash
# Records the README demo of background mode: the agent fills a form on its
# own workspace while you keep typing in yours, then the form is shown.
#
# 1. Open a terminal with a large font (kitty -o font_size=15) and place it
#    beside the window you will type in (an editor, a chat).
# 2. Run this script in that terminal: scripts/demo-background.sh
# 3. During the countdown, click into your own window and keep typing until
#    the script switches to workspace 99 near the end. That switch and the
#    switch back are the only things it does on your screen.
#
# It writes demo-out/raw.mp4 (the full recording), demo-out/demo.mp4 (for the
# release notes and social posts) and demo-out/demo.gif (for the README).
# Review them before copying demo.gif over assets/demo.gif.
#
# Settings: OUT=dir (default demo-out), REC=0 to run the steps without
# recording or the reveal, HYPRUSE="uv run --quiet hypruse" from a checkout.
set -euo pipefail

OUT=${OUT:-demo-out}
REC=${REC:-1}
read -r -a H <<<"${HYPRUSE:-hypruse}"
FORM=(yad --form --title "Trip to Lisbon" --text "Booking"
      --field "Note" --field "Window seat:CHK" --field "Email me the receipt:CHK")

say() { printf '\n\033[1;36magent $\033[0m %s\n' "$*"; }
# show a command the way you would type it: quote the arguments with spaces
shown() {
  local a out=hypruse
  for a in "$@"; do
    if [[ $a == *" "* ]]; then out+=" \"$a\""; else out+=" $a"; fi
  done
  printf '%s' "$out"
}
# run one verb, show its result lines without the long notes, wrapped
step() {
  say "$(shown "$@")"
  sleep 0.8
  "${H[@]}" "$@" 2>&1 | grep -v '^note ' | fold -s -w 96 || true
  sleep 1.6
}

mkdir -p "$OUT"
ws_human=$(hyprctl -j activeworkspace | jq .id)
monitor=$(hyprctl -j monitors | jq -r '.[] | select(.focused) | .name')

printf 'hypruse background mode demo. Click into your own window now and keep typing.\n'
for i in 5 4 3 2 1; do printf '\rstarting in %s ' "$i"; sleep 1; done
printf '\n'

recpid=
if [ "$REC" = 1 ]; then
  clear
  printf '\033[1mThe agent books a trip in a form on workspace 99, out of sight.\033[0m\n'
  wf-recorder -y -o "$monitor" -f "$OUT/raw.mp4" >/dev/null 2>&1 &
  recpid=$!
  sleep 1.5
fi

say "hypruse launch -- yad --form --title \"Trip to Lisbon\" ..."
launched=$("${H[@]}" launch -- "${FORM[@]}" 2>&1)
printf '%s\n' "$launched" | head -1 | fold -s -w 96
addr=$(printf '%s' "$launched" | grep -o '0x[0-9a-f]\+' | head -1)
[ -n "$addr" ] || { echo "launch did not report a window"; exit 1; }
sleep 1.6

step ui --window "$addr"
step keyboard type "Aisle is fine if the window seats are gone" --window "$addr"
step click_ui "Window seat" --window "$addr"
step click_ui "Email me the receipt" --window "$addr"
step ui --window "$addr"

if [ "$REC" = 1 ]; then
  say "here is workspace 99, where all of that happened"
  sleep 1
  "${H[@]}" hypr workspace 99 >/dev/null
  sleep 3
  "${H[@]}" hypr workspace "$ws_human" >/dev/null
  sleep 1.5
  kill -INT "$recpid"
  wait "$recpid" 2>/dev/null || true
fi
"${H[@]}" hypr close_window "$addr" >/dev/null 2>&1 || true

if [ "$REC" = 1 ]; then
  # end on a close-up of the form (yad opens in the middle of the screen),
  # so the filled field and ticked boxes are readable at README width
  last=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/raw.mp4")
  ffmpeg -loglevel error -y -ss "$(awk -v d="$last" 'BEGIN{print d-3}')" -i "$OUT/raw.mp4" -frames:v 1 \
    -vf "crop=iw/4:ih/4:iw*3/8:ih*0.385,scale=1920:1080:flags=lanczos" "$OUT/zoom.png"
  ffmpeg -loglevel error -y -i "$OUT/raw.mp4" -loop 1 -t 2.5 -framerate 30 -i "$OUT/zoom.png" \
    -filter_complex "[0:v]fps=30,scale=1920:1080,setsar=1,format=yuv420p[a];[1:v]fps=30,scale=1920:1080,setsar=1,format=yuv420p[b];[a][b]concat=n=2:v=1[v]" \
    -map "[v]" -c:v libx264 -crf 20 -pix_fmt yuv420p "$OUT/full.mp4"
  ffmpeg -loglevel error -y -i "$OUT/full.mp4" -vf "scale=1280:-2" -c:v libx264 \
    -pix_fmt yuv420p -crf 23 -movflags +faststart "$OUT/demo.mp4"
  ffmpeg -loglevel error -y -i "$OUT/full.mp4" -vf \
    "fps=10,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer" \
    "$OUT/demo.gif"
  printf '\nwrote %s/demo.mp4 and %s/demo.gif (%s)\n' "$OUT" "$OUT" "$(du -h "$OUT/demo.gif" | cut -f1)"
fi
