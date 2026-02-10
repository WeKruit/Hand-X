#!/usr/bin/env bash
# Browser-Use Bootstrap Installer (DEV TESTING)
#
# NOTE: This script is for development testing only. Production version will
# install from PyPI. For now, use BROWSER_USE_BRANCH to install from GitHub.
#
# Usage:
#   # Install from GitHub branch (for testing)
#   curl -fsSL <raw-url> | BROWSER_USE_BRANCH=frictionless-install bash
#
#   # With profile
#   curl -fsSL <raw-url> | BROWSER_USE_BRANCH=frictionless-install bash -s -- --profile remote
#
# Once released, production usage will be:
#   curl -fsSL https://browser-use.com/install.sh | bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
	echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
	echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
	echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
	echo -e "${RED}✗${NC} $1"
}

# Detect platform
detect_platform() {
	local os=$(uname -s | tr '[:upper:]' '[:lower:]')
	local arch=$(uname -m)

	case "$os" in
		linux*)
			PLATFORM="linux"
			;;
		darwin*)
			PLATFORM="macos"
			;;
		msys*|mingw*|cygwin*)
			PLATFORM="windows"
			;;
		*)
			log_error "Unsupported OS: $os"
			exit 1
			;;
	esac

	log_info "Detected platform: $PLATFORM ($arch)"
}

# Check Python version
check_python() {
	log_info "Checking Python installation..."

	# Try python3 first, then python
	if command -v python3 &> /dev/null; then
		PYTHON_CMD="python3"
	elif command -v python &> /dev/null; then
		PYTHON_CMD="python"
	else
		log_warn "Python not found"
		return 1
	fi

	# Check version
	local version=$($PYTHON_CMD --version 2>&1 | awk '{print $2}')
	local major=$(echo $version | cut -d. -f1)
	local minor=$(echo $version | cut -d. -f2)

	if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
		log_success "Python $version found"
		return 0
	else
		log_warn "Python $version found, but 3.11+ required"
		return 1
	fi
}

# Install Python (platform-specific)
install_python() {
	log_info "Installing Python 3.11+..."

	case "$PLATFORM" in
		macos)
			if command -v brew &> /dev/null; then
				brew install python@3.11
			else
				log_error "Homebrew not found. Install from: https://brew.sh"
				exit 1
			fi
			;;
		linux)
			if command -v apt-get &> /dev/null; then
				sudo apt-get update
				sudo apt-get install -y python3.11 python3.11-venv python3-pip
			elif command -v yum &> /dev/null; then
				sudo yum install -y python311 python311-pip
			else
				log_error "Unsupported package manager. Install Python 3.11+ manually."
				exit 1
			fi
			;;
		windows)
			log_error "Please install Python 3.11+ from: https://www.python.org/downloads/"
			exit 1
			;;
	esac

	# Verify installation
	if check_python; then
		log_success "Python installed successfully"
	else
		log_error "Python installation failed"
		exit 1
	fi
}

# Install uv package manager
install_uv() {
	log_info "Installing uv package manager..."

	if command -v uv &> /dev/null; then
		log_success "uv already installed"
		return 0
	fi

	# Use official uv installer
	curl -LsSf https://astral.sh/uv/install.sh | sh

	# Add to PATH for current session
	export PATH="$HOME/.cargo/bin:$PATH"

	if command -v uv &> /dev/null; then
		log_success "uv installed successfully"
	else
		log_error "uv installation failed"
		exit 1
	fi
}

# Install browser-use
install_browser_use() {
	log_info "Installing browser-use..."

	# Create or use existing virtual environment
	if [ ! -d "$HOME/.browser-use-env" ]; then
		uv venv "$HOME/.browser-use-env" --python 3.11
	fi

	# Activate venv and install
	source "$HOME/.browser-use-env/bin/activate"

	# Install from GitHub branch (for testing) or PyPI (production)
	if [ -n "$BROWSER_USE_BRANCH" ]; then
		log_info "Installing from GitHub branch: $BROWSER_USE_BRANCH"
		uv pip install "git+https://github.com/browser-use/browser-use@$BROWSER_USE_BRANCH"
	else
		uv pip install browser-use
	fi

	log_success "browser-use installed"
}

# Add to PATH permanently
configure_path() {
	local shell_rc=""
	local bin_path="$HOME/.browser-use-env/bin"

	# Detect shell
	if [ -n "$BASH_VERSION" ]; then
		shell_rc="$HOME/.bashrc"
	elif [ -n "$ZSH_VERSION" ]; then
		shell_rc="$HOME/.zshrc"
	else
		shell_rc="$HOME/.profile"
	fi

	# Check if already in PATH
	if grep -q "browser-use-env/bin" "$shell_rc" 2>/dev/null; then
		log_info "PATH already configured"
		return 0
	fi

	# Add to shell config
	echo "" >> "$shell_rc"
	echo "# Browser-Use" >> "$shell_rc"
	echo "export PATH=\"$bin_path:\$PATH\"" >> "$shell_rc"

	log_success "Added to PATH in $shell_rc"
	log_warn "Restart your shell or run: source $shell_rc"
}

# Run setup wizard
run_setup() {
	log_info "Running setup wizard..."

	# Activate venv
	source "$HOME/.browser-use-env/bin/activate"

	# Parse profile from arguments
	local profile="local"
	while [[ $# -gt 0 ]]; do
		case $1 in
			--profile)
				profile="$2"
				shift 2
				;;
			*)
				shift
				;;
		esac
	done

	# Run setup
	if [ "$profile" = "remote" ] || [ "$profile" = "full" ]; then
		log_info "Setup requires API key for $profile profile"
		read -p "Enter Browser-Use API key (or leave empty to skip): " api_key

		if [ -n "$api_key" ]; then
			browser-use setup --profile "$profile" --api-key "$api_key"
		else
			log_warn "Skipping API key configuration"
			browser-use setup --profile local
		fi
	else
		browser-use setup --profile "$profile"
	fi
}

# Validate installation
validate() {
	log_info "Validating installation..."

	source "$HOME/.browser-use-env/bin/activate"

	if browser-use doctor; then
		log_success "Installation validated successfully!"
		return 0
	else
		log_warn "Some checks failed. Run 'browser-use doctor' for details."
		return 1
	fi
}

# Print next steps
print_next_steps() {
	echo ""
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""
	log_success "Browser-Use installed successfully!"
	echo ""
	echo "Next steps:"
	echo "  1. Restart your shell or run: source ~/.bashrc"
	echo "  2. Try: browser-use open https://example.com"
	echo "  3. For help: browser-use --help"
	echo ""
	echo "Documentation: https://docs.browser-use.com"
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""
}

# Main installation flow
main() {
	echo ""
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo "  Browser-Use Installer"
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""

	# Step 1: Detect platform
	detect_platform

	# Step 2: Check/install Python
	if ! check_python; then
		read -p "Python 3.11+ not found. Install now? [y/N] " -n 1 -r
		echo
		if [[ $REPLY =~ ^[Yy]$ ]]; then
			install_python
		else
			log_error "Python 3.11+ required. Exiting."
			exit 1
		fi
	fi

	# Step 3: Install uv
	install_uv

	# Step 4: Install browser-use
	install_browser_use

	# Step 5: Configure PATH
	configure_path

	# Step 6: Run setup wizard
	run_setup "$@"

	# Step 7: Validate
	validate

	# Step 8: Show next steps
	print_next_steps
}

# Run main function with all arguments
main "$@"
