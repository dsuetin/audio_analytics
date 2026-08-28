set -x
for d in 2026-01-01 2026-08-24 2026-08-25 2026-08-26 2026-08-27; do
  echo "=== PROCESSING $d ==="
  python3 offline_analysis/run.py \
    --input "reports/transcript_report_$d.xlsx" \
    --output "reports/final_transcript_report_$d.xlsx" \
    --debug "reports/final_${d}_debug.json" \
    --model qwen3.8:27b --workers 3 --retries 2 || echo "FAILED $d rc=$?"
done
echo "=== BATCH DONE ==="
