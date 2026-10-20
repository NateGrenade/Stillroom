#!/bin/zsh
cd -- "${0:A:h}" || exit 1
for music_python in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x "$music_python" ]]; then
    "$music_python" -m app.launch "$@"
    music_result=$?
    if [[ $music_result -ne 0 ]]; then
      read -r "music_pause?Press Return to close. "
    fi
    exit $music_result
  fi
done
print "Python 3 is required to open Stillroom."
read -r "music_pause?Press Return to close. "
exit 1
