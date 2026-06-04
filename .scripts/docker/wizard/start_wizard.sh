#!/bin/bash

# ===================================================
#   EasyAIoT Deployment Wizard Startup (Linux/macOS)
# ===================================================

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==================================================="
echo "  EasyAIoT Deployment Wizard Startup (Linux/macOS)"
echo "==================================================="

# Check Python
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "[ERROR] Python was not found. Please install Python 3.10+."
    exit 1
fi

echo "Using Python command: $PYTHON_CMD"
echo "Starting configuration wizard backend..."

$PYTHON_CMD app.py &
WIZARD_PID=$!

echo "Waiting for server to start and opening web page..."
sleep 2

# Open browser
if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:8899/
elif command -v open &>/dev/null; then
    open http://localhost:8899/
else
    echo "Please open http://localhost:8899/ manually in your browser."
fi

# Trap SIGINT/SIGTERM and clean up
trap "kill $WIZARD_PID; echo -e '\nWizard server stopped.'; exit 0" SIGINT SIGTERM

echo "Wizard server is running with PID: $WIZARD_PID."
echo "Press Ctrl+C to stop the wizard."

wait $WIZARD_PID
