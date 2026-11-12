#!/usr/bin/env bash
# Подготовка директории PF-модели для Triton из git-репозитория.
#
# Использование:
#   bash scripts/prepare_model_dir.sh <model-repo-dir>  # копирование + проверка
#   bash scripts/prepare_model_dir.sh --verify <dir>    # только проверка сумм
#
# <model-repo-dir> — каталог model repository (например /mnt/models
# или хостовая директория, которая будет смонтирована в Triton).
# В <model-repo-dir> появится/обновится подкаталог diarization_online_pf.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED="
config.pbtxt:290e311eedf40cade68fb4e6ceda5d31
1/model.py:32a39ee940c8077ec50cbf98048869f2
1/model_impl.py:83ac0723865bf2238ab698fc8736a70e
1/utils.py:691da583da33d8ee46a7172ce55f0a87
"

verify() {
  local dir="$1" fail=0 line f sum actual
  while read -r line; do
    [ -z "${line:-}" ] && continue
    f="${line%%:*}"
    sum="${line##*:}"
    if [ ! -f "$dir/$f" ]; then
      echo "MISSING $f"
      fail=1
      continue
    fi
    actual="$(md5sum "$dir/$f" | awk '{print $1}')"
    if [ "$actual" != "$sum" ]; then
      echo "MISMATCH $f: expected $sum, actual $actual"
      fail=1
    else
      echo "OK $f"
    fi
  done <<< "$EXPECTED"
  return "$fail"
}

if [ "${1:-}" = "--verify" ]; then
  verify "${2:?каталог модели для проверки}"
  exit $?
fi

TGT="${1:?model-repo каталог, например /mnt/models}"
mkdir -p "$TGT/diarization_online_pf/1"
cp -f "$SRC/config.pbtxt" "$TGT/diarization_online_pf/config.pbtxt"
cp -f "$SRC/1/model.py" "$SRC/1/model_impl.py" "$SRC/1/utils.py" \
      "$TGT/diarization_online_pf/1/"
verify "$TGT/diarization_online_pf"
echo "READY: $TGT/diarization_online_pf"
