#!/usr/bin/env bash
# Records one minute of your voice from every listed microphone at once, in one
# take, and shows the text to read, part by part, with a countdown.
#
#   tools/record-sample.sh [output-dir]
#
# Output: <dir>/raw-<name>.wav, 48 kHz mono, one file per microphone.
# Ctrl+C stops the recording early.
set -u
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

out="${1:-$HOME/studiomic-sample/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$out"

P=alsa_input.pci-0000_00_1f.3-platform-sof_sdw
sources="
laptop-array=$P.HiFi__Mic__source
laptop-headset=$P.HiFi__Headset__source
pro10=$P.pro-input-10
dock=alsa_input.usb-Lenovo_ThinkPad_USB-C_Dock_Audio_000000000000-00.mono-fallback
pebble=alsa_input.usb-Creative_Technology_Ltd_Creative_Pebble_X_MINION-MX-DEV-SN0-01.mono-fallback
voice_clean=voice_clean
"

total=60
pids=""
stop() { for p in $pids; do kill -INT "$p" 2>/dev/null; done; }
trap 'stop; echo; echo "Stopped early."; exit 1' INT

part() { # <seconds> <title> <instruction> <text>
  local secs="$1" title="$2" how="$3" text="$4" i
  clear
  printf '\n  %s   (%s s)\n  %s\n\n' "$title" "$secs" "$how"
  printf '%s\n' "$text" | fold -s -w 70 | sed 's/^/      /'
  for ((i = secs; i > 0; i--)); do
    printf '\r  %2d s left in this part, %2d s in total   ' "$i" "$((total - elapsed))"
    sleep 1
    elapsed=$((elapsed + 1))
  done
  printf '\n'
}

clear
cat <<EOF

  One-minute voice sample.
  Sit where you normally work. Speak at your normal call volume.
  The recording runs for $total s. Output: $out

EOF
for n in 3 2 1; do printf '\r  Start in %s ...' "$n"; sleep 1; done
printf '\r  Recording.            \n'

for line in $sources; do
  n="${line%%=*}"
  node="${line#*=}"
  timeout -s INT $((total + 3)) pw-record --target "$node" \
    -P "{ node.name=rec-$n }" --rate 48000 --channels 1 \
    "$out/raw-$n.wav" >/dev/null 2>&1 &
  pids="$pids $!"
done

elapsed=0
part 5 "1. ROOM TONE" "Say nothing. Do not type. Sit still." "(silence)"
part 20 "2. NORMAL SPEECH" "Read at your normal pace." \
  "When the sunlight strikes raindrops in the air, they act as a prism and form a rainbow. Thank you for calling. My name is Alexander, and I will be your contact this week. Please send the invoice by Friday, and we will confirm the delivery date on Monday. Could you repeat the last number? Seven, three, nine, two, five, six, one."
part 8 "3. SOFT, THEN LOUD" "First line quietly, as if someone sleeps next door. Then loudly, across a room." \
  "Quietly:  Sixty-six thousand six hundred and sixty-six, that is the total.

Loudly:   We ship the first batch on Thursday!"
part 15 "4. SPEECH WHILE YOU TYPE" "Type on the keyboard at a normal speed while you read. Keep typing 3 s after the last word." \
  "The new build passes all tests except two, and both of them fail only on the slow runner. I think the cause is a race in the cache, but I have not proved it yet."
part 7 "5. SWEDISH" "Normal distance, normal pace." \
  "Hej, det är Alexander. Vi ses på måndag klockan åtta, och jag tar med kaffe och kanelbullar till alla som kommer."
part 5 "6. TYPING ALONE" "Say nothing. Type on the keyboard, any text." "(typing only)"

stop
wait
clear
printf '\n  Done. %s s recorded.\n\n' "$elapsed"
ls -l "$out" | sed 's/^/  /'
printf '\n  Tell Claude: %s\n\n' "$out"
