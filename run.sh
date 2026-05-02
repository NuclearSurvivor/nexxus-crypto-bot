#!/usr/bin/env bash
# NEXXUS launcher — uses venv python that has all packages
export DISPLAY="${DISPLAY:-:0}"
cd "/home/nuclear/Desktop/CRYPTO BOT"
exec "/home/nuclear/.pyenv/versions/NEXXUS-venv/bin/python3" "/home/nuclear/Desktop/CRYPTO BOT/main.py" >> "/home/nuclear/Desktop/CRYPTO BOT/crash.log" 2>&1
