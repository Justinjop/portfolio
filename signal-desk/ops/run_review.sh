#!/bin/bash
set -o pipefail
cd /home/ubuntu/qt/desk
{
  echo "=== $(date -u '+%F %T') UTC"
  timeout 1500 /home/ubuntu/.local/bin/claude -p "$(cat /home/ubuntu/qt/review_prompt.md)
Current time: $(date -u '+%F %H:%M') UTC" \
    --permission-mode dontAsk < /dev/null 2>&1 | tail -n 15
} >> /home/ubuntu/qt/review.log
