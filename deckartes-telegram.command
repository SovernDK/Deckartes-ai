#!/bin/zsh
# Runs the Telegram bot in a Terminal window; add this file to Login Items to start it at login.
# It has to be Terminal: a background service (launchd) is refused calendar access.
# caffeinate -is keeps the Mac awake: -s on power, -i also on battery. Closing the window stops the bot.

cd "${0:A:h}" || exit 1

while true; do
  /usr/bin/caffeinate -is venv/bin/python telegram_bot.py
  code=$?
  # 0: asked to stop. 2: a setting is missing, so restarting wouldn't help
  if [[ $code -eq 0 || $code -eq 2 ]]; then
    break
  fi
  echo "The bot stopped unexpectedly (status $code), restarting in 5 seconds"
  sleep 5
done
