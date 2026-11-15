#!/bin/sh
# Hands the TV key's passphrase to ssh (SSH_ASKPASS), so it is never on a command line.
printf '%s\n' "$LG_TV_PASSPHRASE"
