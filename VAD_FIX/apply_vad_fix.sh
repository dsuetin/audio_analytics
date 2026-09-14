#!/bin/bash
# Apply VAD fix to all Python environments in Triton container

FIXED_FILE="/tmp/online_handler_fixed.py"

# Check if fixed file exists
if [ ! -f "$FIXED_FILE" ]; then
    echo "Error: Fixed file not found at $FIXED_FILE"
    exit 1
fi

# Find all Python environments and apply fix
find /tmp -maxdepth 2 -type d -name 'python_env_*' | while read dir; do
    TARGET="$dir/0/lib/python3.10/site-packages/vad/online_handler.py"
    if [ -f "$TARGET" ]; then
        cp "$FIXED_FILE" "$TARGET"
        echo "Applied fix to: $dir"
    fi
done

echo "VAD fix applied to all Python environments"
