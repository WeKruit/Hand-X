#!/usr/bin/env bash
# Browser-Use Bootstrap Installer
#
# Usage:
#   # Interactive install (shows mode selection TUI)
#   curl -fsSL https://browser-use.com/install.sh | bash
#
#   # Non-interactive install with flags
#   curl -fsSL https://browser-use.com/install.sh | bash -s -- --full
#   curl -fsSL https://browser-use.com/install.sh | bash -s -- --remote-only
#   curl -fsSL https://browser-use.com/install.sh | bash -s -- --local-only
#
#   # With API key
#   curl -fsSL https://browser-use.com/install.sh | bash -s -- --remote-only --api-key bu_xxx
#
# For development testing:
#   curl -fsSL <raw-url> | BROWSER_USE_BRANCH=<branch-name> bash

set -e

# =============================================================================
# Configuration
# =============================================================================

# Mode flags (set by parse_args or TUI)
INSTALL_LOCAL=false
INSTALL_REMOTE=false
SKIP_INTERACTIVE=false
API_KEY=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# =============================================================================
# Logging functions
# =============================================================================

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

# =============================================================================
# Argument parsing
# =============================================================================

parse_args() {
	while [[ $# -gt 0 ]]; do
		case $1 in
			--full|--all)
				INSTALL_LOCAL=true
				INSTALL_REMOTE=true
				SKIP_INTERACTIVE=true
				shift
				;;
			--remote-only)
				INSTALL_REMOTE=true
				SKIP_INTERACTIVE=true
				shift
				;;
			--local-only)
				INSTALL_LOCAL=true
				SKIP_INTERACTIVE=true
				shift
				;;
			--api-key)
				if [ -z "$2" ] || [[ "$2" == --* ]]; then
					log_error "--api-key requires a value"
					exit 1
				fi
				API_KEY="$2"
				shift 2
				;;
			--help|-h)
				echo "Browser-Use Installer"
				echo ""
				echo "Usage: install.sh [OPTIONS]"
				echo ""
				echo "Options:"
				echo "  --full, --all     Install all modes (local + remote)"
				echo "  --remote-only     Install remote mode only (no Chromium)"
				echo "  --local-only      Install local modes only (no cloudflared)"
				echo "  --api-key KEY     Set Browser-Use API key"
				echo "  --help, -h        Show this help"
				echo ""
				echo "Without options, shows interactive mode selection."
				exit 0
				;;
			*)
				log_warn "Unknown argument: $1 (ignored)"
				shift
				;;
		esac
	done
}

# =============================================================================
# Platform detection
# =============================================================================

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

# =============================================================================
# Python management
# =============================================================================

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

# =============================================================================
# uv package manager
# =============================================================================

install_uv() {
	log_info "Installing uv package manager..."

	if command -v uv &> /dev/null; then
		log_success "uv already installed"
		return 0
	fi

	# Use official uv installer
	curl -LsSf https://astral.sh/uv/install.sh | sh

	# Add common uv install locations to PATH for current session
	export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

	if command -v uv &> /dev/null; then
		log_success "uv installed successfully"
	else
		log_error "uv installation failed. Try restarting your shell and run the installer again."
		exit 1
	fi
}

# =============================================================================
# Gum TUI installation
# =============================================================================

install_gum() {
	# Install gum for beautiful TUI - silent and fast
	if command -v gum &> /dev/null; then
		return 0
	fi

	local arch=$(uname -m)
	local gum_version="0.14.5"
	local gum_dir=""

	mkdir -p "$HOME/.local/bin"
	export PATH="$HOME/.local/bin:$PATH"

	case "$PLATFORM" in
		macos)
			if [ "$arch" = "arm64" ]; then
				gum_dir="gum_${gum_version}_Darwin_arm64"
				curl -sL "https://github.com/charmbracelet/gum/releases/download/v${gum_version}/gum_${gum_version}_Darwin_arm64.tar.gz" | tar -xz -C /tmp
			else
				gum_dir="gum_${gum_version}_Darwin_x86_64"
				curl -sL "https://github.com/charmbracelet/gum/releases/download/v${gum_version}/gum_${gum_version}_Darwin_x86_64.tar.gz" | tar -xz -C /tmp
			fi
			;;
		linux)
			if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
				gum_dir="gum_${gum_version}_Linux_arm64"
				curl -sL "https://github.com/charmbracelet/gum/releases/download/v${gum_version}/gum_${gum_version}_Linux_arm64.tar.gz" | tar -xz -C /tmp
			else
				gum_dir="gum_${gum_version}_Linux_x86_64"
				curl -sL "https://github.com/charmbracelet/gum/releases/download/v${gum_version}/gum_${gum_version}_Linux_x86_64.tar.gz" | tar -xz -C /tmp
			fi
			;;
		*)
			return 1
			;;
	esac

	# Move binary from extracted directory
	mv "/tmp/${gum_dir}/gum" "$HOME/.local/bin/" 2>/dev/null || return 1
	rm -rf "/tmp/${gum_dir}" 2>/dev/null

	command -v gum &> /dev/null
}

# =============================================================================
# Interactive mode selection TUI
# =============================================================================

show_mode_menu() {
	# Try to install gum for nice TUI
	if install_gum; then
		show_gum_menu
	else
		show_bash_menu
	fi
}

show_gum_menu() {
	echo ""

	# Styled header
	gum style --foreground 212 --bold "Select browser modes to install"
	gum style --foreground 240 "Use arrow keys to navigate, space to select, enter to confirm"
	echo ""

	# Checkbox selection with gum choose
	set +e
	SELECTED=$(gum choose --no-limit --height 10 \
		--cursor-prefix "[ ] " --selected-prefix "[✓] " --unselected-prefix "[ ] " \
		--header "" \
		--cursor.foreground 212 \
		--selected.foreground 212 \
		"Local browser   (chromium/real - requires Chromium)" \
		"Remote browser  (cloud - requires API key)" < /dev/tty)
	set -e

	# Parse selections
	if [[ "$SELECTED" == *"Local"* ]]; then INSTALL_LOCAL=true; fi
	if [[ "$SELECTED" == *"Remote"* ]]; then INSTALL_REMOTE=true; fi
}

show_bash_menu() {
	echo ""
	echo "Select browser modes to install (space-separated numbers):"
	echo ""
	echo "  1) Local browser  (chromium/real - requires Chromium download)"
	echo "  2) Remote browser (cloud - requires API key)"
	echo ""
	echo "Press Enter for default [1]"
	echo ""
	echo -n "> "

	# Read from /dev/tty to work even when script is piped
	# Keep set +e for the whole function to avoid issues with pattern matching
	set +e
	read -r choices < /dev/tty
	choices=${choices:-1}

	if [[ "$choices" == *"1"* ]]; then INSTALL_LOCAL=true; fi
	if [[ "$choices" == *"2"* ]]; then INSTALL_REMOTE=true; fi
	set -e
}

# =============================================================================
# Browser-Use installation
# =============================================================================

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
		BROWSER_USE_REPO="${BROWSER_USE_REPO:-browser-use/browser-use}"
		log_info "Installing from GitHub: $BROWSER_USE_REPO@$BROWSER_USE_BRANCH"
		# Clone and install locally to ensure all dependencies are resolved
		local tmp_dir=$(mktemp -d)
		git clone --depth 1 --branch "$BROWSER_USE_BRANCH" "https://github.com/$BROWSER_USE_REPO.git" "$tmp_dir"
		uv pip install "$tmp_dir"
		rm -rf "$tmp_dir"
	else
		uv pip install browser-use
	fi

	log_success "browser-use installed"
}

install_chromium() {
	log_info "Installing Chromium browser..."

	source "$HOME/.browser-use-env/bin/activate"

	# Build command - only use --with-deps on Linux (it fails on Windows/macOS)
	local cmd="uvx playwright install chromium"
	if [ "$PLATFORM" = "linux" ]; then
		cmd="$cmd --with-deps"
	fi
	cmd="$cmd --no-shell"

	eval $cmd

	log_success "Chromium installed"
}

install_cloudflared() {
	log_info "Installing cloudflared..."

	if command -v cloudflared &> /dev/null; then
		log_success "cloudflared already installed"
		return 0
	fi

	local arch=$(uname -m)

	case "$PLATFORM" in
		macos)
			if command -v brew &> /dev/null; then
				brew install cloudflared
			else
				# Direct download for macOS without Homebrew
				mkdir -p "$HOME/.local/bin"
				if [ "$arch" = "arm64" ]; then
					curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz -o /tmp/cloudflared.tgz
				else
					curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz -o /tmp/cloudflared.tgz
				fi
				tar -xzf /tmp/cloudflared.tgz -C "$HOME/.local/bin/"
				rm /tmp/cloudflared.tgz
			fi
			;;
		linux)
			mkdir -p "$HOME/.local/bin"
			if [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; then
				curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o "$HOME/.local/bin/cloudflared"
			else
				curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o "$HOME/.local/bin/cloudflared"
			fi
			chmod +x "$HOME/.local/bin/cloudflared"
			;;
		windows)
			log_warn "Please install cloudflared manually: winget install Cloudflare.cloudflared"
			return 0
			;;
	esac

	# Add ~/.local/bin to PATH for current session
	export PATH="$HOME/.local/bin:$PATH"

	if command -v cloudflared &> /dev/null; then
		log_success "cloudflared installed successfully"
	else
		log_warn "cloudflared installation failed. You can install it manually later."
	fi
}

# =============================================================================
# Install dependencies based on selected modes
# =============================================================================

install_dependencies() {
	# Install base package (always needed)
	install_browser_use

	# Install Chromium only if local mode selected
	if [ "$INSTALL_LOCAL" = true ]; then
		install_chromium
	else
		log_info "Skipping Chromium (remote-only mode)"
	fi

	# Install cloudflared only if remote mode selected
	if [ "$INSTALL_REMOTE" = true ]; then
		install_cloudflared
	else
		log_info "Skipping cloudflared (local-only mode)"
	fi
}

# =============================================================================
# Write install configuration
# =============================================================================

write_install_config() {
	# Determine installed modes and default
	local modes=""
	local default_mode=""

	if [ "$INSTALL_LOCAL" = true ] && [ "$INSTALL_REMOTE" = true ]; then
		modes='["chromium", "real", "remote"]'
		default_mode="chromium"
	elif [ "$INSTALL_REMOTE" = true ]; then
		modes='["remote"]'
		default_mode="remote"
	else
		modes='["chromium", "real"]'
		default_mode="chromium"
	fi

	# Write config file
	mkdir -p "$HOME/.browser-use"
	cat > "$HOME/.browser-use/install-config.json" << EOF
{
  "installed_modes": $modes,
  "default_mode": "$default_mode"
}
EOF

	local mode_names=$(echo $modes | tr -d '[]"' | tr ',' ' ')
	log_success "Configured: $mode_names"
}

# =============================================================================
# PATH configuration
# =============================================================================

configure_path() {
	local shell_rc=""
	local bin_path="$HOME/.browser-use-env/bin"
	local local_bin="$HOME/.local/bin"

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

	# Add to shell config (includes ~/.local/bin for cloudflared)
	echo "" >> "$shell_rc"
	echo "# Browser-Use" >> "$shell_rc"
	echo "export PATH=\"$bin_path:$local_bin:\$PATH\"" >> "$shell_rc"

	log_success "Added to PATH in $shell_rc"
}

# =============================================================================
# Setup wizard
# =============================================================================

run_setup() {
	log_info "Running setup wizard..."

	# Activate venv
	source "$HOME/.browser-use-env/bin/activate"

	# Determine profile based on mode selections
	local profile="local"
	if [ "$INSTALL_REMOTE" = true ] && [ "$INSTALL_LOCAL" = true ]; then
		profile="full"
	elif [ "$INSTALL_REMOTE" = true ]; then
		profile="remote"
	fi

	# Run setup with API key if provided
	if [ -n "$API_KEY" ]; then
		browser-use setup --mode "$profile" --api-key "$API_KEY" --yes
	else
		browser-use setup --mode "$profile" --yes
	fi
}

# =============================================================================
# Validation
# =============================================================================

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

# =============================================================================
# Print completion message
# =============================================================================

print_next_steps() {
	# Detect shell for source command
	local shell_rc=".bashrc"
	if [ -n "$ZSH_VERSION" ]; then
		shell_rc=".zshrc"
	fi

	echo ""
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""
	log_success "Browser-Use installed successfully!"
	echo ""
	echo "Installed modes:"
	[ "$INSTALL_LOCAL" = true ]  && echo "  ✓ Local (chromium, real)"
	[ "$INSTALL_REMOTE" = true ] && echo "  ✓ Remote (cloud)"
	echo ""

	# Show API key instructions if remote selected but no key provided
	if [ "$INSTALL_REMOTE" = true ] && [ -z "$API_KEY" ]; then
		echo "⚠ API key required for remote mode:"
		echo "  export BROWSER_USE_API_KEY=<your-api-key>"
		echo ""
		echo "  Get your API key at: https://browser-use.com"
		echo ""
	fi

	echo "Next steps:"
	echo "  1. Restart your shell or run: source ~/$shell_rc"

	if [ "$INSTALL_REMOTE" = true ] && [ -z "$API_KEY" ]; then
		echo "  2. Set your API key (see above)"
		echo "  3. Try: browser-use open https://example.com"
	else
		echo "  2. Try: browser-use open https://example.com"
	fi

	echo ""
	echo "Documentation: https://docs.browser-use.com"
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""
}

# =============================================================================
# Main installation flow
# =============================================================================

main() {
	echo ""
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo "  Browser-Use Installer"
	echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	echo ""

	# Parse command-line flags
	parse_args "$@"

	# Show install mode if flags provided
	if [ "$SKIP_INTERACTIVE" = true ]; then
		if [ "$INSTALL_LOCAL" = true ] && [ "$INSTALL_REMOTE" = true ]; then
			log_info "Install mode: full (local + remote)"
		elif [ "$INSTALL_REMOTE" = true ]; then
			log_info "Install mode: remote-only"
		else
			log_info "Install mode: local-only"
		fi
		echo ""
	fi

	# Step 1: Detect platform
	detect_platform

	# Step 2: Check/install Python
	if ! check_python; then
		read -p "Python 3.11+ not found. Install now? [y/N] " -n 1 -r < /dev/tty
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

	# Step 4: Show mode selection TUI (unless skipped via flags)
	if [ "$SKIP_INTERACTIVE" = false ]; then
		show_mode_menu
	fi

	# Default to local-only if nothing selected
	if [ "$INSTALL_LOCAL" = false ] && [ "$INSTALL_REMOTE" = false ]; then
		log_warn "No modes selected, defaulting to local"
		INSTALL_LOCAL=true
	fi

	echo ""

	# Step 5: Install dependencies
	install_dependencies

	# Step 6: Write install config
	write_install_config

	# Step 7: Configure PATH
	configure_path

	# Step 8: Run setup wizard
	run_setup

	# Step 9: Validate
	validate

	# Step 10: Show next steps
	print_next_steps
}

# Run main function with all arguments
main "$@"
