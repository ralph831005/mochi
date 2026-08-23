#!/usr/bin/env bash
# 🍡 Mochi — One-liner install script
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/ralph831005/mochi/main/install.sh | bash
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, git)
#   2. Clones the repo
#   3. Creates a virtual environment
#   4. Installs dependencies
#   5. Runs the interactive setup wizard
#
set -euo pipefail

REPO_URL="https://github.com/ralph831005/mochi.git"
INSTALL_DIR="${MOCHI_DIR:-$HOME/.mochi}"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}       Mochi — Agent Installer${NC}"
echo -e "${CYAN}   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# --- Clone ---
if [ -d "$INSTALL_DIR" ]; then
    echo ""
    echo -e "${YELLOW}  ⚠️  Directory already exists: $INSTALL_DIR${NC}"
    read -p "  Overwrite? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "  ❌ Aborted."
        exit 1
    fi
    rm -rf "$INSTALL_DIR"
fi

echo ""
echo -e "  📥 Cloning into ${INSTALL_DIR}..."
git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>&1 | sed 's/^/     /'
echo ""

cd "$INSTALL_DIR"

# --- Python setup ---
if command -v pyenv &>/dev/null; then
    echo -e "  ✅ pyenv detected"

    VENV_NAME="${MOCHI_VENV:-mochi-agents}"

    # Find highest installed Python >= 3.10
    PYTHON_VERSION=$(pyenv versions --bare | grep -E '^3\.(1[0-9]|[2-9][0-9])' | grep -v '/' | sort -V | tail -1)

    if [ -z "$PYTHON_VERSION" ]; then
        echo -e "${RED}  ❌ No Python >= 3.10 found in pyenv.${NC}"
        echo "     Install one with: pyenv install 3.12"
        exit 1
    fi
    echo -e "  ✅ Using Python $PYTHON_VERSION"

    # Create pyenv virtualenv if missing
    if ! pyenv versions --bare | grep -q "^${VENV_NAME}$"; then
        echo -e "  📦 Creating pyenv virtualenv: $VENV_NAME ($PYTHON_VERSION)"
        pyenv virtualenv "$PYTHON_VERSION" "$VENV_NAME"
    else
        echo -e "  ✅ pyenv virtualenv '$VENV_NAME' already exists"
    fi

    # .python-version already points to mochi-agents
    eval "$(pyenv init -)"
    eval "$(pyenv virtualenv-init -)"
    pyenv activate "$VENV_NAME"
else
    # No pyenv — use system Python with venv
    PYTHON=""
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            major=$("$cmd" -c 'import sys; print(sys.version_info.major)' 2>/dev/null)
            minor=$("$cmd" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON="$cmd"
                break
            fi
        fi
    done

    if [ -z "$PYTHON" ]; then
        echo -e "${RED}  ❌ Python 3.10+ is required. Install it or use pyenv.${NC}"
        exit 1
    fi

    echo -e "  ✅ Python: $($PYTHON --version)"
    echo -e "  📦 Creating virtual environment..."
    $PYTHON -m venv .venv
    source .venv/bin/activate
fi

echo -e "  📦 Installing dependencies..."
pip install --upgrade pip -q
pip install -e ".[dev]" -q 2>&1 | tail -1

# --- Create global shim ---
SHIM_DIR="$HOME/.local/bin"
SHIM_PATH="$SHIM_DIR/mochi-agents"
mkdir -p "$SHIM_DIR"

if command -v pyenv &>/dev/null; then
    # pyenv shim — activate the virtualenv then run
    cat > "$SHIM_PATH" << SHIM
#!/usr/bin/env bash
export PYENV_VIRTUALENV_DISABLE_PROMPT=1
eval "\$(pyenv init -)"
eval "\$(pyenv virtualenv-init -)"
cd "$INSTALL_DIR" && exec python -m mochi_agents.cli "\$@"
SHIM
else
    # venv shim — source the venv then run
    cat > "$SHIM_PATH" << SHIM
#!/usr/bin/env bash
source "$INSTALL_DIR/.venv/bin/activate"
cd "$INSTALL_DIR" && exec mochi-agents "\$@"
SHIM
fi
chmod +x "$SHIM_PATH"
echo -e "  ✅ Installed shim: $SHIM_PATH"

# Check if ~/.local/bin is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$SHIM_DIR"; then
    echo ""
    echo -e "${YELLOW}  ⚠️  $SHIM_DIR is not on your PATH.${NC}"
    echo "     Add this to your ~/.bashrc or ~/.zshrc:"
    echo ""
    echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

echo ""
echo -e "${GREEN}  ✅ Mochi installed successfully!${NC}"
echo ""
echo -e "${CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  You can now run from anywhere:"
echo ""
echo "    mochi-agents setup              # configure API keys"
echo "    mochi-agents agent install nutritionist  # optional agents"
echo "    mochi-agents                    # start!"
echo ""

# --- Offer to run setup ---
read -p "  Run setup wizard now? [Y/n] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo ""
    "$SHIM_PATH" setup
fi
