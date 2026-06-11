#!/bin/bash

# Music Toolkit - Application Launcher
# Runs the application in a terminal window so you can see any errors

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Project directory
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

# Check if virtual environment exists
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${RED}Error: Virtual environment not found!${NC}"
    echo ""
    echo "Please run the setup script first:"
    echo -e "  ${BLUE}./setup_env.sh${NC}"
    echo ""
    exit 1
fi

# Activate virtual environment (ensures PATH/VIRTUAL_ENV/conda libs are set)
activate_venv() {
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
        return 0
    fi
    echo -e "${RED}Error: Failed to activate virtual environment.${NC}"
    return 1
}

# Function to run in current terminal
run_in_current_terminal() {
    echo "========================================="
    echo "Music Toolkit"
    echo "========================================="
    echo ""
    echo -e "${BLUE}Starting application...${NC}"
    echo -e "${YELLOW}Press Ctrl+C to exit${NC}"
    echo ""

    cd "$PROJECT_DIR"

    if ! activate_venv; then
        exit 1
    fi

    # Use venv Python from PATH (activation ensures correct env/paths)
    python Music-Toolkit.py 2>&1
    EXIT_CODE=$?

    # Always show exit status and wait
    echo ""
    if [ $EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}Application exited normally${NC}"
    else
        echo -e "${RED}Application exited with error code: $EXIT_CODE${NC}"
    fi
    echo -e "${YELLOW}Press Enter to close...${NC}"
    read
}

main() {
    # Wrapper script for terminal emulators - ensures errors are shown
    WRAPPER_CMD="cd '$PROJECT_DIR' && source '$PROJECT_DIR/run.sh' && activate_venv && echo '=========================================' && echo 'Music Toolkit' && echo '=========================================' && echo '' && echo 'Starting application...' && echo '' && python Music-Toolkit.py 2>&1; EXIT_CODE=\$?; echo ''; if [ \$EXIT_CODE -eq 0 ]; then echo -e '${GREEN}Application exited normally${NC}'; else echo -e '${RED}Application exited with error code:' \$EXIT_CODE'${NC}'; fi; echo -e '${YELLOW}Press Enter to close...${NC}'; read"

    # If running from a terminal, just run it
    if [ -t 0 ]; then
        run_in_current_terminal
    else
        # Try to open in a new terminal window
        # Detect available terminal emulator
        if command -v konsole &> /dev/null; then
            # KDE Konsole
            konsole -e bash -c "$WRAPPER_CMD"
        elif command -v gnome-terminal &> /dev/null; then
            # GNOME Terminal
            gnome-terminal -- bash -c "$WRAPPER_CMD"
        elif command -v xfce4-terminal &> /dev/null; then
            # XFCE Terminal
            xfce4-terminal -e "bash -c \"$WRAPPER_CMD\""
        elif command -v alacritty &> /dev/null; then
            # Alacritty
            alacritty -e bash -c "$WRAPPER_CMD"
        elif command -v kitty &> /dev/null; then
            # Kitty
            kitty bash -c "$WRAPPER_CMD"
        elif command -v xterm &> /dev/null; then
            # xterm (fallback)
            xterm -e bash -c "$WRAPPER_CMD"
        else
            # No terminal found, run in current shell
            echo -e "${YELLOW}No suitable terminal emulator found. Running in current shell...${NC}"
            run_in_current_terminal
        fi
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
