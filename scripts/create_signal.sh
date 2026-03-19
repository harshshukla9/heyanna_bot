#!/bin/bash
# Create a test signal for autotrader testing
# Usage: ./create_signal.sh [--no] [--15m]

OUTPUT_FILE="${TELEGRAM_SIGNAL_OUTPUT:-logs/announcement_signals.jsonl}"

# Default values
SIGNAL="YES"
TIMEFRAME="5m"

# Parse arguments
for arg in "$@"; do
    case $arg in
        --no)
            SIGNAL="NO"
            ;;
        --15m)
            TIMEFRAME="15m"
            ;;
    esac
done

# Calculate timestamps
TIMESTAMP=$(date +%s)
if [ "$TIMEFRAME" = "15m" ]; then
    MARKET_END=$((TIMESTAMP + 900))
else
    MARKET_END=$((TIMESTAMP + 300))
fi

# Create signal JSON
cat >> "$OUTPUT_FILE" << EOF
{"source":"live:manual","chat_id":0,"chat_title":"manual","message_id":1,"message_date":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","signal_ts":${TIMESTAMP},"signal_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","time_utc_display":"$(date -u +%H:%M) UTC","market_end_ts":${MARKET_END},"sender_id":0,"message_key":"manual:test","parsed":true,"asset":"BTC","timeframe":"${TIMEFRAME}","series":"BTC Up or Down ${TIMEFRAME}","series_slug":"btc-up-or-down-${TIMEFRAME}","market":"Will Bitcoin go up in next ${TIMEFRAME} ?","signal":"${SIGNAL}","direction":"$( [ "$SIGNAL" = "YES" ] && echo "UP" || echo "DOWN" )"}
EOF

echo "Signal created: ${SIGNAL} ${TIMEFRAME} -> ${OUTPUT_FILE}"
